"""Layer manifests: one JSON object per path, sorted by path.

JSON Lines rather than a single document, for three reasons that all showed up in the
design (docs/KIP-FFS-LAYERS.md section 3): it streams, so a base layer with thousands of
files never loads at once; `diff` and `git diff` work on it directly, so a layer's contents
are reviewable with ordinary tools; and appending during capture is trivial.

The byte form is canonical -- keys sorted, no incidental whitespace, ASCII-escaped -- so a
manifest hashes reproducibly. That matters because a layer ID *is* the hash of its
manifest plus its metadata, so two captures of identical content must produce identical
bytes on any machine.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import IO, Any

from .. import timestamps
from ..errors import ImageError, UsageError

# ---------------------------------------------------------------------------
# Entry kinds
# ---------------------------------------------------------------------------

#: A regular file. Carries a blob hash.
FILE = "f"
#: A directory. Recorded explicitly, because empty directories are load-bearing on
#: AmigaOS -- `T/`, `WBStartup`, `Prefs/Presets` -- and are the classic thing a
#: file-list-based tool silently loses.
DIR = "d"
#: A deletion, relative to the parent layer. Applied by default on composition.
WHITEOUT = "w"
#: Hard link, soft link. Reported and preserved, never followed.
HARDLINK = "h"
SOFTLINK = "s"

KINDS = frozenset({FILE, DIR, WHITEOUT, HARDLINK, SOFTLINK})

#: Kinds that carry content.
CONTENT_KINDS = frozenset({FILE})
#: Kinds that carry a link target.
LINK_KINDS = frozenset({HARDLINK, SOFTLINK})
#: Kinds that carry metadata worth comparing (protection, comment, timestamp).
METADATA_KINDS = frozenset({FILE, DIR, HARDLINK, SOFTLINK})

#: AmigaDOS protection with nothing set: readable, writable, executable, deletable.
DEFAULT_PROTECT = "----rwed"

#: An absent timestamp. A real state, not a sentinel: a freshly created directory can
#: carry no datestamp at all, which `amibuilder.timestamps.format` renders as dashes.
NO_TS = (0, 0, 0)

# JSON keys are deliberately short. A base layer manifest runs to thousands of lines and
# the long-form spelling would roughly double the file for no benefit.
K_PATH = "p"
K_KIND = "t"
K_BLOB = "b"
K_SIZE = "sz"
K_PROTECT = "pr"
K_TS = "ts"
K_COMMENT = "c"
K_LINK = "lt"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def split_path(path: str) -> tuple[str, str]:
    """Split a volume-qualified path into `(volume, relative)`.

    `Workbench:S/Startup-Sequence` -> `("Workbench", "S/Startup-Sequence")`.
    `Workbench:` -> `("Workbench", "")`, which is the volume root and is legal.
    """
    volume, sep, rel = path.partition(":")
    if not sep:
        raise UsageError(
            f"path must be volume-qualified, got {path!r} -- expected 'Volume:path', "
            "as in 'Workbench:S/Startup-Sequence'"
        )
    if not volume:
        raise UsageError(f"path is missing a volume name: {path!r}")
    if ":" in rel:
        raise UsageError(f"path contains more than one ':': {path!r}")
    return volume, rel.strip("/")


def join_path(volume: str, rel: str) -> str:
    """Join a volume name and a volume-relative path into the manifest form."""
    return f"{volume}:{rel.strip('/')}"


def fold(path: str) -> str:
    """Case-folded form of a path, used for matching.

    FFS is case-insensitive but case-preserving, so `S/Startup-Sequence` and
    `s/startup-sequence` cannot both exist in one volume. Matching on the folded form is
    what stops a diff from reporting a case change as a delete plus an add -- a pair that
    would compose into a deletion of the very file being added.

    Note this uses Python's casefold rather than FFS's own hash-time upcasing, which for
    non-international volumes only folds A-Z. The two agree on ASCII, which covers
    essentially all real Amiga filenames; accented names on a plain `DOS\\3` volume are
    the known divergence.
    """
    return path.casefold()


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    """One recorded path.

    Frozen because entries flow through capture, diff, review and composition, and an
    accidental mutation partway would be near-impossible to trace. Use
    `dataclasses.replace` to derive a changed copy.
    """

    #: Volume-qualified: `Workbench:S/Startup-Sequence`.
    path: str
    kind: str
    #: Content hash, files only.
    blob: str | None = None
    size: int = 0
    #: Canonical 8-character protection string.
    protect: str = DEFAULT_PROTECT
    #: Raw on-disk `(days, mins, ticks)`. Never a Unix timestamp -- see module docstring
    #: of `amibuilder.timestamps` for the hour-adrift measurement that motivates this.
    ts: tuple[int, int, int] = NO_TS
    #: AmigaDOS file comment, up to 79 characters.
    comment: str = ""
    #: Volume-qualified target, links only.
    link_target: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ImageError(f"unknown manifest entry kind {self.kind!r} for {self.path!r}")
        if self.kind in CONTENT_KINDS and self.blob is None:
            raise ImageError(f"file entry without a blob hash: {self.path!r}")
        if self.kind not in CONTENT_KINDS and self.blob is not None:
            raise ImageError(f"{self.kind!r} entry must not carry a blob: {self.path!r}")
        if self.kind in LINK_KINDS and not self.link_target:
            raise ImageError(f"{self.kind!r} entry without a link target: {self.path!r}")
        if self.kind not in LINK_KINDS and self.link_target is not None:
            raise ImageError(f"{self.kind!r} entry must not carry a link target: {self.path!r}")
        # Validates the shape and raises a good message early, rather than at compose time
        # when a partial drive has already been written.
        split_path(self.path)

    # -- derived ------------------------------------------------------------

    @property
    def folded(self) -> str:
        """Match key. See `fold`."""
        return fold(self.path)

    @property
    def volume(self) -> str:
        return split_path(self.path)[0]

    @property
    def relative(self) -> str:
        return split_path(self.path)[1]

    @property
    def sort_key(self) -> tuple[str, str]:
        """Folded first so case variants sort together; exact path breaks ties."""
        return (self.folded, self.path)

    @property
    def is_whiteout(self) -> bool:
        return self.kind == WHITEOUT

    @property
    def mod_secs(self) -> int:
        """Seconds since the Amiga epoch, as naive wall clock. For display only."""
        return timestamps.from_triple(*self.ts)[0]

    @property
    def mod_ticks(self) -> int:
        return timestamps.from_triple(*self.ts)[1]

    def compare_key(self, *, timestamps_significant: bool = False) -> tuple[Any, ...]:
        """What makes two entries for the same path 'the same'.

        Content hash, protection bits and comment -- **not** timestamps. Booting an Amiga
        and using Workbench restamps directories and preference files that have nothing to
        do with whatever was just installed, so a timestamp-sensitive key turns a
        forty-file layer into a four-hundred-entry one and destroys trust in the diff
        (layers doc section 5). Timestamps are still *recorded*, so a round trip is exact;
        they merely do not by themselves make an entry a difference.

        `--timestamps-significant` opts into the strict behaviour.
        """
        key: tuple[Any, ...] = (self.kind, self.blob, self.protect, self.comment, self.link_target)
        if timestamps_significant:
            key += (tuple(self.ts),)
        return key

    # -- serialisation ------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Minimal dict form: defaults are omitted to keep manifest lines short.

        Omission is unambiguous because `from_json` restores the same defaults, and
        `ManifestEntry` is the only writer.
        """
        d: dict[str, Any] = {K_PATH: self.path, K_KIND: self.kind}
        if self.blob is not None:
            d[K_BLOB] = self.blob
        if self.size:
            d[K_SIZE] = self.size
        if self.kind in METADATA_KINDS and self.protect != DEFAULT_PROTECT:
            d[K_PROTECT] = self.protect
        if tuple(self.ts) != NO_TS:
            d[K_TS] = list(self.ts)
        if self.comment:
            d[K_COMMENT] = self.comment
        if self.link_target is not None:
            d[K_LINK] = self.link_target
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ManifestEntry:
        try:
            path = d[K_PATH]
            kind = d[K_KIND]
        except KeyError as e:
            raise ImageError(f"manifest entry missing required field {e.args[0]!r}: {d!r}") from e
        ts = d.get(K_TS, NO_TS)
        if not isinstance(ts, (list, tuple)) or len(ts) != 3:
            raise ImageError(f"manifest timestamp must be [days, mins, ticks], got {ts!r}")
        return cls(
            path=path,
            kind=kind,
            blob=d.get(K_BLOB),
            size=int(d.get(K_SIZE, 0)),
            protect=d.get(K_PROTECT, DEFAULT_PROTECT),
            ts=(int(ts[0]), int(ts[1]), int(ts[2])),
            comment=d.get(K_COMMENT, ""),
            link_target=d.get(K_LINK),
        )

    def to_line(self) -> str:
        """Canonical single-line JSON. No trailing newline."""
        return json.dumps(
            self.to_json(),
            sort_keys=True,
            separators=(",", ":"),
            # ASCII-escaped so the bytes cannot vary with anyone's locale or editor. A
            # manifest's bytes are hashed, so stability outranks readability of the rare
            # accented filename.
            ensure_ascii=True,
        )

    @classmethod
    def from_line(cls, line: str) -> ManifestEntry:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as e:
            raise ImageError(f"manifest line is not valid JSON: {line[:120]!r}") from e
        if not isinstance(parsed, dict):
            raise ImageError(f"manifest line must be a JSON object, got {type(parsed).__name__}")
        return cls.from_json(parsed)

    # -- construction from a mounted volume ---------------------------------

    @classmethod
    def from_volume_entry(
        cls,
        volume_name: str,
        entry: Any,
        *,
        blob: str | None = None,
        link_target: str | None = None,
    ) -> ManifestEntry:
        """Build from an `amibuilder.volume.Entry`.

        `blob` is required for files and must be supplied by the caller, because hashing
        needs the volume the entry came from and this type deliberately knows nothing
        about images.
        """
        if entry.link_kind == "hard":
            kind = HARDLINK
        elif entry.link_kind == "soft":
            kind = SOFTLINK
        elif entry.is_dir:
            kind = DIR
        else:
            kind = FILE
        return cls(
            path=join_path(volume_name, entry.path),
            kind=kind,
            blob=blob if kind in CONTENT_KINDS else None,
            size=0 if kind == DIR else int(entry.size),
            protect=entry.protect_str or DEFAULT_PROTECT,
            ts=timestamps.to_triple(entry.mod_secs, entry.mod_ticks),
            comment=entry.comment or "",
            link_target=link_target if kind in LINK_KINDS else None,
        )


def whiteout(path: str) -> ManifestEntry:
    """A deletion marker for `path`."""
    return ManifestEntry(path=path, kind=WHITEOUT)


# ---------------------------------------------------------------------------
# Whole-manifest I/O
# ---------------------------------------------------------------------------


def sort_entries(entries: Iterable[ManifestEntry]) -> list[ManifestEntry]:
    """Canonical order: by folded path, then exact path."""
    return sorted(entries, key=lambda e: e.sort_key)


def canonical_bytes(entries: Iterable[ManifestEntry]) -> bytes:
    """The exact on-disk bytes for a manifest.

    One function so that writing a manifest and hashing one cannot diverge -- if they
    did, a layer's recorded ID would not match its own contents.
    """
    out = bytearray()
    for entry in sort_entries(entries):
        out += entry.to_line().encode("ascii")
        out += b"\n"
    return bytes(out)


def write(entries: Iterable[ManifestEntry], stream: IO[bytes]) -> int:
    """Write a full manifest. Returns the entry count."""
    ordered = sort_entries(entries)
    stream.write(canonical_bytes(ordered))
    return len(ordered)


def read(stream: IO[bytes] | IO[str]) -> Iterator[ManifestEntry]:
    """Stream a manifest. Blank lines are skipped; anything else must parse."""
    for lineno, raw in enumerate(stream, start=1):
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            continue
        try:
            yield ManifestEntry.from_line(line)
        except ImageError as e:
            raise ImageError(f"manifest line {lineno}: {e}") from e


def load(path: str) -> list[ManifestEntry]:
    with open(path, "rb") as fh:
        return list(read(fh))


def save(path: str, entries: Iterable[ManifestEntry]) -> int:
    with open(path, "wb") as fh:
        return write(entries, fh)


def index(entries: Iterable[ManifestEntry]) -> dict[str, ManifestEntry]:
    """Map folded path -> entry, for matching between two manifests.

    A duplicate folded path is a real fault rather than something to resolve quietly: FFS
    cannot hold two such names in one directory, so a manifest containing both was built
    wrongly and would compose unpredictably.
    """
    out: dict[str, ManifestEntry] = {}
    for entry in entries:
        key = entry.folded
        if key in out:
            raise ImageError(
                f"manifest contains two entries for the same path: "
                f"{out[key].path!r} and {entry.path!r} -- FFS cannot represent both"
            )
        out[key] = entry
    return out


def total_size(entries: Iterable[ManifestEntry]) -> int:
    """Sum of file sizes, ignoring directories and whiteouts."""
    return sum(e.size for e in entries if e.kind in CONTENT_KINDS)


def counts(entries: Iterable[ManifestEntry]) -> dict[str, int]:
    """Entry count per kind, for `snap ls` and `snap show` summaries."""
    out = dict.fromkeys(sorted(KINDS), 0)
    for entry in entries:
        out[entry.kind] += 1
    return out


__all__ = [
    "CONTENT_KINDS",
    "DEFAULT_PROTECT",
    "DIR",
    "FILE",
    "HARDLINK",
    "KINDS",
    "LINK_KINDS",
    "NO_TS",
    "SOFTLINK",
    "WHITEOUT",
    "ManifestEntry",
    "canonical_bytes",
    "counts",
    "fold",
    "index",
    "join_path",
    "load",
    "read",
    "replace",
    "save",
    "sort_entries",
    "split_path",
    "total_size",
    "whiteout",
    "write",
]
