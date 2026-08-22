"""Reading a drive into manifest entries, and diffing one capture against another.

Capture is strictly read-only. It walks each volume, hashes every file into the blob store,
and records directories explicitly -- empty ones are load-bearing on AmigaOS (`T/`,
`WBStartup`, `Prefs/Presets`) and are the classic thing a file-list-based tool loses.

Diffing is where this design either earns trust or loses it. A naive comparison is full of
noise, because between two captures the Amiga boots and runs: preferences get saved, `T:`
fills up, `Disk.info` is rewritten when an icon moves, and every directory that gained an
entry is restamped. So the comparison key is content hash plus protection plus comment, and
**timestamps are recorded but do not by themselves make an entry a difference**
(docs/KIP-FFS-LAYERS.md section 5). Every difference carries a reason, which is what turns a
surprising diff into a diagnosable one.

**Links are a known gap, and it is amitools', not ours.** Measured against amitools 0.8.x:
its `fs` package contains no link node type at all and defines only `ST_FILE`, `ST_ROOT` and
`ST_USERDIR`, so a link's target is not reachable through it. Capture therefore records what
it can see and warns, rather than inventing a target that composition would act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

from ..errors import UnsupportedError
from ..image import Container, ImageKind
from ..volume import Volume
from . import manifest as M
from .blobs import BlobStore
from .hostdir import DirectoryVolume

# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

#: Opening position only. The layers design is explicit that the right list has to be tuned
#: against real machines, and that the first few diffs will reveal noise nobody predicted.
#: `snap review --drop` is what makes editing it a five-minute job.
#:
#: Deliberately **not** excluded, because they are plausibly the entire point of a "my
#: configuration" layer: `ENV:`/`S/env-archive`, `Devs/system-configuration`,
#: `S/Startup-Sequence`, `S/User-Startup`, `WBStartup/`.
DEFAULT_EXCLUSIONS: tuple[str, ...] = (
    # Temporary by definition.
    "**/T/**",
    # Deleted files, and only the Trashcan's own icon -- never `*.info` generally, since
    # icons are load-bearing on AmigaOS.
    "**/Trashcan/**",
    "**/Trashcan.info",
    # macOS pollution, travelling in the other direction.
    "**/.DS_Store",
    "**/._*",
)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex.

    `*` stops at a path separator and `**` crosses them, which is the distinction
    `fnmatch` does not make and the reason for hand-rolling this. A pattern ending in
    `/**` also matches the directory itself, so `Workbench:T/**` excludes `Workbench:T`
    rather than leaving an excluded-but-present empty directory behind.
    """
    out: list[str] = []
    i = 0
    text = pattern
    trailing_any = text.endswith("/**")
    if trailing_any:
        text = text[: -len("/**")]
    while i < len(text):
        ch = text[i]
        if ch == "*":
            if text.startswith("**/", i):
                # Zero or more whole leading components, so '**/T' matches both
                # 'Workbench:T' and 'Work:a/b/T'. The separator class includes ':' because
                # the volume prefix is a component boundary too, and without it a pattern
                # like '**/T/**' would never match a path at the top of a volume.
                out.append("(?:.*[:/])?")
                i += 3
                continue
            if text.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(ch))
        i += 1
    body = "".join(out)
    if trailing_any:
        # The directory itself, or anything beneath it.
        return re.compile(f"^{body}(/.*)?$", re.DOTALL)
    return re.compile(f"^{body}$", re.DOTALL)


@dataclass(frozen=True)
class Exclusions:
    """A set of path globs, matched case-insensitively against volume-qualified paths.

    Case-insensitive because FFS is: a pattern for `Trashcan` must also catch `TRASHCAN`,
    which is the same directory as far as the Amiga is concerned.
    """

    patterns: tuple[str, ...] = DEFAULT_EXCLUSIONS

    @classmethod
    def build(
        cls,
        patterns: Sequence[str] | None = None,
        *,
        extra: Sequence[str] = (),
        defaults: bool = True,
    ) -> Exclusions:
        base = tuple(patterns) if patterns is not None else (DEFAULT_EXCLUSIONS if defaults else ())
        return cls(patterns=tuple(base) + tuple(extra))

    @property
    def _compiled(self) -> list[re.Pattern[str]]:
        return [_glob_to_regex(p.casefold()) for p in self.patterns]

    def matches(self, path: str) -> str | None:
        """The pattern that excludes `path`, or None. Returns the pattern so callers can say
        *why* something was skipped."""
        folded = path.casefold()
        for pattern, rx in zip(self.patterns, self._compiled):
            if rx.match(folded):
                return pattern
        return None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


@dataclass
class CaptureStats:
    """What a capture did. The numbers the Phase 2 experiment needs."""

    files: int = 0
    dirs: int = 0
    skipped: int = 0
    #: Bytes read off the volume.
    content_bytes: int = 0
    blobs_written: int = 0
    blobs_deduplicated: int = 0
    #: Bytes newly occupied in the store, after compression.
    stored_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "dirs": self.dirs,
            "skipped": self.skipped,
            "content_bytes": self.content_bytes,
            "blobs_written": self.blobs_written,
            "blobs_deduplicated": self.blobs_deduplicated,
            "stored_bytes": self.stored_bytes,
        }


@dataclass
class CaptureResult:
    entries: list[M.ManifestEntry] = field(default_factory=list)
    #: Things the user should know but which do not invalidate the capture: an unmountable
    #: partition, a link whose target amitools cannot report.
    warnings: list[str] = field(default_factory=list)
    stats: CaptureStats = field(default_factory=CaptureStats)
    #: Volume names actually captured, in order.
    volumes: list[str] = field(default_factory=list)

    def extend(self, other: CaptureResult) -> None:
        self.entries.extend(other.entries)
        self.warnings.extend(other.warnings)
        self.volumes.extend(other.volumes)
        s, o = self.stats, other.stats
        self.stats = CaptureStats(
            files=s.files + o.files,
            dirs=s.dirs + o.dirs,
            skipped=s.skipped + o.skipped,
            content_bytes=s.content_bytes + o.content_bytes,
            blobs_written=s.blobs_written + o.blobs_written,
            blobs_deduplicated=s.blobs_deduplicated + o.blobs_deduplicated,
            stored_bytes=s.stored_bytes + o.stored_bytes,
        )


#: Called with (volume_qualified_path, size_bytes) as each file is read.
ProgressFn = Callable[[str, int], None]


def capture_volume(
    volume: Volume,
    blobs: BlobStore,
    *,
    exclusions: Exclusions | None = None,
    volume_name: str | None = None,
    on_file: ProgressFn | None = None,
) -> CaptureResult:
    """Walk one mounted volume into manifest entries, storing blobs as it goes.

    The volume root is not recorded as an entry: composition creates it by formatting, so a
    record of it would describe something the target already has.
    """
    excl = exclusions if exclusions is not None else Exclusions()
    name = volume_name or volume.name
    result = CaptureResult(volumes=[name])

    for _dirpath, dirs, files in volume.walk():
        for entry in dirs:
            qualified = M.join_path(name, entry.path)
            if excl.matches(qualified):
                result.stats.skipped += 1
                continue
            result.entries.append(M.ManifestEntry.from_volume_entry(name, entry))
            result.stats.dirs += 1

        for entry in files:
            qualified = M.join_path(name, entry.path)
            if excl.matches(qualified):
                result.stats.skipped += 1
                continue

            if entry.is_link:
                # See the module docstring: amitools cannot report a link's target, so
                # recording one would mean inventing it.
                result.warnings.append(
                    f"{qualified}: {entry.link_kind} link skipped -- amitools cannot report "
                    "link targets, so it cannot be captured or reproduced"
                )
                result.stats.skipped += 1
                continue

            data = volume.read_file(entry.path)
            put = blobs.put_bytes(data)
            result.entries.append(
                M.ManifestEntry.from_volume_entry(name, entry, blob=put.hash)
            )
            result.stats.files += 1
            result.stats.content_bytes += len(data)
            if put.written:
                result.stats.blobs_written += 1
                result.stats.stored_bytes += put.stored_size
            else:
                result.stats.blobs_deduplicated += 1
            if on_file is not None:
                on_file(qualified, len(data))

    return result


def capture_container(
    container: Container,
    blobs: BlobStore,
    *,
    exclusions: Exclusions | None = None,
    on_file: ProgressFn | None = None,
    directory_volume_name: str | None = None,
) -> CaptureResult:
    """Capture every mountable volume in a container.

    A partition that will not mount -- PFS3, SFS, anything the file-level path cannot read --
    is recorded as a warning rather than failing the capture. One unreadable partition must
    not make a four-partition drive uncapturable, and the warning is what tells the user that
    a byte-level snapshot is the tool for that volume.

    A host **directory** is captured as a single volume via `DirectoryVolume`, which reads the
    same `.uaem`-sidecar layout `targets.write_directory` writes -- so a directory produced by
    `compose --format dir` snapshots straight back into a layer. `directory_volume_name` names
    that volume; without it the directory's own basename is used. It applies only to a directory
    source, and `open_volume` refuses a directory, which is why this branch comes first.
    """
    result = CaptureResult()

    if container.kind is ImageKind.DIRECTORY:
        vol = DirectoryVolume(container.address.path, name=directory_volume_name)
        sub = capture_volume(vol, blobs, exclusions=exclusions, on_file=on_file)
        # DirectoryVolume records metadata-read problems (a corrupt sidecar, an unreadable
        # subdirectory) as it walks; fold them in so nothing is lost silently.
        sub.warnings.extend(vol.warnings)
        result.extend(sub)
        if not result.entries and not result.warnings:
            result.warnings.append(
                f"nothing captured: {container.address.path} holds no files"
            )
        return result

    if container.kind is not ImageKind.RDB:
        with container.open_volume() as vol:
            result.extend(
                capture_volume(vol, blobs, exclusions=exclusions, on_file=on_file)
            )
        return result

    for part in container.partitions(probe_volumes=True):
        if part.volume_name is None:
            result.warnings.append(
                f"{part.device_name}: not captured -- {part.volume_error or 'will not mount'}"
                f" (dos type {part.dos_type.label})"
            )
            continue
        try:
            with container.open_volume(part.index) as vol:
                result.extend(
                    capture_volume(
                        vol,
                        blobs,
                        exclusions=exclusions,
                        volume_name=part.volume_name,
                        on_file=on_file,
                    )
                )
        except UnsupportedError as exc:
            result.warnings.append(f"{part.device_name}: not captured -- {exc}")

    if not result.entries and not result.warnings:
        result.warnings.append("nothing captured: the image has no mountable volumes")
    return result


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

REASON_NEW = "new"
REASON_CONTENT = "content"
REASON_PROTECTION = "protection"
REASON_COMMENT = "comment"
REASON_KIND = "kind"
REASON_LINK = "link-target"
REASON_TIMESTAMP = "timestamp"
REASON_CASE = "case"
REASON_DELETED = "deleted"


@dataclass(frozen=True)
class DiffEntry:
    """One difference, and why it is one."""

    entry: M.ManifestEntry
    #: One or more reasons joined by '+', e.g. `content+protection`.
    reason: str
    #: The parent's version, absent for a new path.
    previous: M.ManifestEntry | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(self.reason.split("+"))

    def as_dict(self) -> dict[str, Any]:
        d = {"path": self.entry.path, "kind": self.entry.kind, "reason": self.reason}
        if self.entry.size:
            d["size"] = self.entry.size
        return d


@dataclass
class DiffResult:
    changes: list[DiffEntry] = field(default_factory=list)
    #: Paths present in both and considered identical.
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def entries(self) -> list[M.ManifestEntry]:
        """The manifest a diff layer would carry."""
        return M.sort_entries(change.entry for change in self.changes)

    def by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for change in self.changes:
            for reason in change.reasons:
                out[reason] = out.get(reason, 0) + 1
        return dict(sorted(out.items()))

    def counts(self) -> dict[str, int]:
        return M.counts(self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.changes


def _reasons_for(
    current: M.ManifestEntry, previous: M.ManifestEntry, *, timestamps_significant: bool
) -> list[str]:
    """Which aspects of an entry changed. Empty means unchanged."""
    reasons: list[str] = []
    if current.kind != previous.kind:
        reasons.append(REASON_KIND)
    if current.blob != previous.blob:
        reasons.append(REASON_CONTENT)
    if current.protect != previous.protect:
        reasons.append(REASON_PROTECTION)
    if current.comment != previous.comment:
        reasons.append(REASON_COMMENT)
    if current.link_target != previous.link_target:
        reasons.append(REASON_LINK)
    if current.path != previous.path:
        # Same file as far as FFS is concerned, but the recorded spelling changed. Worth
        # surfacing rather than hiding, and cheaper than a delete plus an add.
        reasons.append(REASON_CASE)
    if timestamps_significant and tuple(current.ts) != tuple(previous.ts):
        reasons.append(REASON_TIMESTAMP)
    return reasons


def diff(
    parent: Iterable[M.ManifestEntry],
    current: Iterable[M.ManifestEntry],
    *,
    timestamps_significant: bool = False,
    deletions: bool = True,
) -> DiffResult:
    """Compare two captures and produce the entries a diff layer would carry.

    `deletions` records paths the parent had and the capture does not, as whiteouts. On by
    default: AmigaOS patch installers really do remove obsolete libraries, and a composition
    that silently skipped a removal would leave two conflicting versions of a library
    installed -- miserable to diagnose on real hardware. `--no-deletions` exists for treating
    a layer as purely additive.
    """
    before = M.index(parent)
    after = M.index(current)
    result = DiffResult()

    for key, entry in after.items():
        prior = before.get(key)
        if prior is None:
            result.changes.append(DiffEntry(entry=entry, reason=REASON_NEW))
            continue
        reasons = _reasons_for(
            entry, prior, timestamps_significant=timestamps_significant
        )
        if reasons:
            result.changes.append(
                DiffEntry(entry=entry, reason="+".join(reasons), previous=prior)
            )
        else:
            result.unchanged += 1

    if deletions:
        for key, entry in before.items():
            if key not in after:
                result.changes.append(
                    DiffEntry(
                        entry=M.whiteout(entry.path),
                        reason=REASON_DELETED,
                        previous=entry,
                    )
                )

    result.changes.sort(key=lambda c: c.entry.sort_key)
    return result


def apply_drops(
    changes: Iterable[DiffEntry], patterns: Sequence[str]
) -> tuple[list[DiffEntry], dict[str, int]]:
    """Remove changes matching any pattern. Used by `snap review --drop`.

    Returns the survivors and a count per pattern, so the user is told what each pattern
    actually did rather than having to infer it from a total.
    """
    excl = Exclusions(patterns=tuple(patterns))
    kept: list[DiffEntry] = []
    dropped: dict[str, int] = {p: 0 for p in patterns}
    for change in changes:
        hit = excl.matches(change.entry.path)
        if hit is None:
            kept.append(change)
        else:
            dropped[hit] = dropped.get(hit, 0) + 1
    return kept, dropped


def apply_keeps(
    changes: Iterable[DiffEntry], patterns: Sequence[str]
) -> list[DiffEntry]:
    """Keep only changes matching a pattern. The inverse of `apply_drops`."""
    excl = Exclusions(patterns=tuple(patterns))
    return [c for c in changes if excl.matches(c.entry.path) is not None]


__all__ = [
    "DEFAULT_EXCLUSIONS",
    "CaptureResult",
    "CaptureStats",
    "DiffEntry",
    "DiffResult",
    "Exclusions",
    "REASON_CASE",
    "REASON_COMMENT",
    "REASON_CONTENT",
    "REASON_DELETED",
    "REASON_KIND",
    "REASON_LINK",
    "REASON_NEW",
    "REASON_PROTECTION",
    "REASON_TIMESTAMP",
    "apply_drops",
    "apply_keeps",
    "capture_container",
    "capture_volume",
    "diff",
    "replace",
]
