"""The narrow interface onto an Amiga filesystem.

Everything amitools-specific about *file access* lives here. Nothing above this module
imports from `amitools`, which is what keeps the GPL dependency replaceable and what
would make a from-scratch or ported FFS implementation a drop-in swap later
(docs/KIP-FFS-NOTES.md section 5.3).

Three amitools quirks are absorbed here rather than leaked upward:

* `FileName.__str__` and `__repr__` both return an `FSString` instead of a `str`, so
  `str(node.get_file_name())` raises TypeError. The correct accessor is
  `get_unicode_name()`. Pinned by test_amitools_regressions.py.
* Path arguments must be `FSString`, never `str` -- passing a `str` raises ValueError.
* `ADFSVolume.get_info()` returns preformatted display lines, not numbers, so block
  accounting is taken from the bitmap instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import timestamps
from .blocks import DosType, decode_dos_type
from .errors import ImageError, NotFoundError, UnsupportedError, UsageError

# ---------------------------------------------------------------------------
# Filesystem limits
# ---------------------------------------------------------------------------
# These are properties of the filesystem, so they live with the code that talks to it.
# `layers.compose` imports them from here rather than keeping a second copy that could
# drift.

#: Classic FFS filename limit, per path component (notes G1).
NAME_LIMIT = 30
#: Long-filename FFS (DOS\6 / DOS\7) raises it.
NAME_LIMIT_LONG = 110
#: The comment field is 80 bytes, so 79 are usable (notes G2).
COMMENT_LIMIT = 79
#: Illegal in an AmigaDOS filename. ':' separates a volume, '/' a path component.
ILLEGAL_NAME_CHARS = (":", "/")

# ---------------------------------------------------------------------------
# amitools adapters
# ---------------------------------------------------------------------------


def _fs(text: str) -> Any:
    """Wrap a str as the FSString that amitools path APIs demand."""
    from amitools.fs.FSString import FSString

    return FSString(text)


def _name_of(node: Any) -> str:
    """Extract a node's name as a real str.

    Must not use str()/repr() on the FileName: both are broken upstream.
    """
    return node.get_file_name().get_unicode_name()


def _text(value: Any) -> str:
    """Coerce an amitools FSString (or None) to a str."""
    if value is None:
        return ""
    getter = getattr(value, "get_unicode", None)
    return getter() if getter else str(value)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One directory entry.

    `path` is volume-relative and '/'-separated with no leading slash, so it can be
    joined onto a volume name to form the `Workbench:S/Startup-Sequence` form the layer
    manifests use.
    """

    name: str
    path: str
    is_dir: bool
    size: int = 0
    protect: int = 0
    protect_str: str = "----rwed"
    comment: str = ""
    #: Seconds since 1978-01-01 as *naive local wall clock*, derived only from the
    #: on-disk days/mins/ticks triple. Deliberately not a Unix timestamp: see
    #: amibuilder.timestamps for why converting through one is lossy.
    mod_secs: int = 0
    #: Remainder ticks (0-49) of the on-disk timestamp, at 50 ticks per second.
    mod_ticks: int = 0
    block: int = 0

    #: Set for hard/soft links, which amibuilder reports but does not follow.
    link_kind: str | None = None

    @property
    def is_link(self) -> bool:
        return self.link_kind is not None

    def as_dict(self) -> dict[str, Any]:
        from . import timestamps

        d = {
            "name": self.name,
            "path": self.path,
            "type": "dir" if self.is_dir else "file",
            "size": self.size,
            "protect": self.protect_str,
            "protect_bits": self.protect,
            "comment": self.comment,
            # Naive by design -- an AmigaDOS timestamp carries no timezone.
            "modified": timestamps.iso(self.mod_secs),
            "modified_amiga_secs": self.mod_secs,
            "modified_ticks": self.mod_ticks,
            "block": self.block,
        }
        if self.link_kind:
            d["link"] = self.link_kind
        return d


@dataclass(frozen=True)
class VolumeInfo:
    """Volume geometry and space accounting.

    Note the relationship between the block counts, which is easy to get wrong: the
    allocation bitmap covers only the non-reserved region, so

        used_blocks + free_blocks + reserved == total_blocks

    `total_blocks` is the volume's full extent, matching the RDB partition table and
    amitools' own reporting. Deriving it from the bitmap instead would understate every
    volume by the reserved block count and disagree with `partitions` output.
    """

    name: str
    dos_type: DosType
    block_size: int
    total_blocks: int
    used_blocks: int
    free_blocks: int
    root_block: int
    reserved: int
    boot_blocks: int
    create_secs: int = 0
    disk_secs: int = 0
    mod_secs: int = 0

    @property
    def bitmap_blocks(self) -> int:
        """Blocks the allocation bitmap accounts for -- everything but the reserved area."""
        return self.used_blocks + self.free_blocks

    @property
    def total_bytes(self) -> int:
        return self.total_blocks * self.block_size

    @property
    def used_bytes(self) -> int:
        return self.used_blocks * self.block_size

    @property
    def free_bytes(self) -> int:
        return self.free_blocks * self.block_size

    @property
    def percent_used(self) -> float:
        return (self.used_blocks / self.total_blocks * 100.0) if self.total_blocks else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume": self.name,
            "dos_type": self.dos_type.label,
            "dos_type_raw": f"0x{self.dos_type.raw:08x}",
            "filesystem": self.dos_type.filesystem,
            "features": self.dos_type.features,
            "block_size": self.block_size,
            "total_blocks": self.total_blocks,
            "used_blocks": self.used_blocks,
            "free_blocks": self.free_blocks,
            "bitmap_blocks": self.bitmap_blocks,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "percent_used": round(self.percent_used, 2),
            "root_block": self.root_block,
            "reserved": self.reserved,
        }


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def normalise(path: str) -> str:
    """Normalise a volume-relative path.

    Accepts and strips a leading '/' or ':' so that both `S/Startup-Sequence` and
    `/S/Startup-Sequence` work, and collapses redundant separators. Public because the
    write commands need to normalise a destination the same way lookups do -- two
    spellings of one path must not be able to disagree about whether it already exists.
    """
    p = path.strip()
    if ":" in p:  # tolerate a volume-qualified path by dropping the volume part
        p = p.split(":", 1)[1]
    p = p.strip("/")
    while "//" in p:
        p = p.replace("//", "/")
    return p


#: Retained so existing internal call sites keep reading naturally.
_norm = normalise


class Volume:
    """A mounted Amiga filesystem.

    Constructed by `amibuilder.image`, not directly. Usable as a context manager; the
    underlying block device is closed on exit.

    Reading needs no ceremony. Writing does, and all of it lives here so that no caller
    can get it wrong:

    * every create passes `update_ts=False`, because amitools' own timestamp update goes
      through the hour-adrift epoch described in `amibuilder.timestamps`
    * parent-directory and volume timestamps are then stamped from `timestamps.now()`,
      so the disk does record the modification -- with correct bytes. Note that this
      *masks* the previous point rather than depending on it: `_stamp` overwrites whatever
      amitools wrote, so `update_ts=False` here is belt-and-braces. It is load-bearing in
      `layers.targets`, which re-creates a recorded tree and must not restamp anything
    * names are checked against the volume's real limits before anything is allocated
    * overwriting is explicit, because amitools raises rather than replacing (notes G22)
    """

    def __init__(self, adfs_volume: Any, blkdev: Any, label: str, closers: list[Any],
                 writable: bool = False):
        self._vol = adfs_volume
        self._blkdev = blkdev
        self._closers = closers
        #: Human-readable source, e.g. "card.hdf:0". Used in error messages.
        self.label = label
        #: Whether the caller asked for write access. Taken from the container rather than
        #: sniffed off the block device, because amitools keeps `read_only` in a different
        #: place on each device class -- `ImageFile` for HDF and raw, the device itself for
        #: ADF, and nowhere at all on the partition wrapper.
        self._writable = bool(writable)
        self._info: VolumeInfo | None = None

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> Volume:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for c in reversed(self._closers):
            try:
                c()
            except Exception:  # noqa: BLE001 - closing must never mask a real error
                pass
        self._closers = []

    @property
    def blkdev(self) -> Any:
        """The underlying amitools block device.

        Exposed because amitools' validator takes a device rather than a filesystem, so
        `check` genuinely needs it. Nothing else above this module should touch it.
        """
        return self._blkdev

    # -- metadata ------------------------------------------------------------
    def info(self) -> VolumeInfo:
        if self._info is not None:
            return self._info

        vol, bd = self._vol, self._blkdev
        bitmap = vol.bitmap
        used = bitmap.get_num_used()
        free = bitmap.get_num_free()
        root = vol.root

        mi = vol.meta_info

        def secs(getter: str) -> int:
            """Amiga-epoch seconds from a root timestamp, via the on-disk triple."""
            ts = getattr(mi, getter, None)
            ts = ts() if callable(ts) else ts
            if ts is None or not hasattr(ts, "days"):
                return 0
            return timestamps.from_triple(ts.days, ts.mins, ts.ticks)[0]

        self._info = VolumeInfo(
            name=_text(vol.get_volume_name()),
            dos_type=decode_dos_type(vol.boot.dos_type),
            block_size=bd.block_bytes,
            # The device's own count, not used+free: the bitmap excludes reserved blocks.
            total_blocks=bd.num_blocks,
            used_blocks=used,
            free_blocks=free,
            root_block=root.blk_num,
            reserved=getattr(bd, "reserved", 2),
            boot_blocks=getattr(bd, "bootblocks", 2),
            create_secs=secs("get_create_ts"),
            disk_secs=secs("get_disk_ts"),
            mod_secs=secs("get_mod_ts"),
        )
        return self._info

    @property
    def name(self) -> str:
        return self.info().name

    # -- lookup --------------------------------------------------------------
    def _node(self, path: str) -> Any:
        p = _norm(path)
        if p == "":
            return self._vol.get_root_dir()
        try:
            node = self._vol.get_path_name(_fs(p))
        except Exception as e:  # amitools raises a variety of types here
            raise ImageError(f"{self.label}: cannot resolve {path!r}: {e}") from e
        if node is None:
            raise NotFoundError(f"{self.label}: no such path: {path}")
        return node

    def exists(self, path: str) -> bool:
        try:
            self._node(path)
            return True
        except (NotFoundError, ImageError):
            return False

    def is_dir(self, path: str) -> bool:
        return bool(self._node(path).is_dir())

    def stat(self, path: str) -> Entry:
        return self._entry(self._node(path), _norm(path))

    # -- listing -------------------------------------------------------------
    def _entry(self, node: Any, path: str) -> Entry:
        mi = node.get_meta_info()
        is_dir = bool(node.is_dir())
        comment = mi.get_comment_unicode_str() if hasattr(mi, "get_comment_unicode_str") \
            else _text(mi.get_comment())
        ts = mi.get_mod_ts()
        name = _name_of(node) if path else self.info().name

        # amitools models links as distinct node classes; report rather than follow.
        cls = type(node).__name__
        link_kind = None
        if "Link" in cls:
            link_kind = "soft" if "Soft" in cls else "hard"

        # Read the on-disk triple rather than get_secs(): identical arithmetic, but taking
        # days/mins/ticks makes it explicit that no Unix-epoch conversion is involved.
        mod_secs, mod_ticks = (0, 0)
        if ts is not None:
            mod_secs, mod_ticks = timestamps.from_triple(ts.days, ts.mins, ts.ticks)

        return Entry(
            name=name,
            path=path,
            is_dir=is_dir,
            size=0 if is_dir else int(node.get_size()),
            protect=mi.get_protect(),
            protect_str=mi.get_protect_str(),
            comment=comment,
            mod_secs=mod_secs,
            mod_ticks=mod_ticks,
            block=getattr(getattr(node, "block", None), "blk_num", 0) or 0,
            link_kind=link_kind,
        )

    def listdir(self, path: str = "") -> list[Entry]:
        """Entries directly inside `path`, sorted directories-first then by name.

        Sorting is applied here rather than left to hash order so that output is stable
        across runs -- FFS stores entries in hash-bucket order, which is effectively
        arbitrary and would make diffs between two listings meaningless.
        """
        node = self._node(path)
        if not node.is_dir():
            raise ImageError(f"{self.label}: not a directory: {path}")
        base = _norm(path)
        out = [
            self._entry(child, f"{base}/{_name_of(child)}" if base else _name_of(child))
            for child in node.get_entries()
        ]
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    def walk(self, path: str = "") -> Iterator[tuple[str, list[Entry], list[Entry]]]:
        """Depth-first walk yielding (dir_path, subdirs, files).

        Iterative rather than recursive: an Amiga volume can hold a deep tree, and a
        corrupt volume with a cyclic parent pointer would blow the Python stack.
        Directories already visited are tracked by header block so a cycle terminates.
        """
        start = _norm(path)
        stack = [start]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            entries = self.listdir(current)
            dirs = [e for e in entries if e.is_dir and not e.is_link]
            files = [e for e in entries if not e.is_dir or e.is_link]
            yield current, dirs, files
            for d in reversed(dirs):
                if d.block and d.block in seen:
                    continue
                if d.block:
                    seen.add(d.block)
                stack.append(d.path)

    # -- reading -------------------------------------------------------------
    def read_file(self, path: str) -> bytes:
        node = self._node(path)
        if node.is_dir():
            raise ImageError(f"{self.label}: is a directory: {path}")
        try:
            # amitools returns a bytearray; normalise so callers can rely on immutability.
            return bytes(node.get_file_data())
        except Exception as e:
            raise ImageError(f"{self.label}: cannot read {path}: {e}") from e

    # -- writing -------------------------------------------------------------
    @property
    def writable(self) -> bool:
        """Whether this volume was opened for writing."""
        return self._writable

    def _require_writable(self) -> None:
        if not self._writable:
            raise ImageError(f"{self.label}: opened read-only, so it cannot be modified")

    @property
    def name_limit(self) -> int:
        """Longest filename this volume accepts, per path component.

        Read from the mounted volume rather than the partition's recorded DosType, so it
        reflects what the filesystem will actually do with a name.
        """
        return NAME_LIMIT_LONG if getattr(self._vol, "is_longname", False) else NAME_LIMIT

    def check_name(self, name: str) -> None:
        """Refuse a single path component the filesystem cannot hold.

        Length is checked in *bytes* as encoded on disk, not characters: the on-disk field
        is a byte count, so a name of legal character length can still overflow once
        Latin-1 encoded. Refusing here means a rejected name costs nothing, rather than
        aborting a copy halfway and leaving a half-populated volume (notes G1).
        """
        if not name:
            raise UsageError("an empty path component is not a valid name")
        for bad in ILLEGAL_NAME_CHARS:
            if bad in name:
                raise UsageError(f"{name!r}: an AmigaDOS name cannot contain {bad!r}")
        encoded = len(name.encode("latin-1", errors="replace"))
        if encoded > self.name_limit:
            raise UsageError(
                f"{name!r} is {encoded} bytes; this volume "
                f"({self.info().dos_type.describe()}) allows {self.name_limit}"
            )

    def check_comment(self, comment: str) -> None:
        if len(comment.encode("latin-1", errors="replace")) > COMMENT_LIMIT:
            raise UsageError(
                f"comment is longer than the {COMMENT_LIMIT} bytes AmigaDOS allows: "
                f"{comment!r}"
            )

    def blocks_for(self, size: int) -> int:
        """Blocks a new file of `size` bytes will consume, header included.

        Mirrors `ADFSFile.blocks_get_create_num` exactly rather than approximating, so a
        preflight can be trusted: header, data blocks, and the extension blocks needed
        once the header's own pointer table is full. OFS reserves 24 bytes per data block
        for its header; FFS uses the whole block.
        """
        size = max(0, int(size))
        bs = self._blkdev.block_bytes
        per_block = bs if getattr(self._vol, "is_ffs", True) else bs - 24
        data_blocks = -(-size // per_block) if per_block > 0 else 0
        # The header block carries `block_longs - 56` data pointers; each extension block
        # carries the same number again.
        per_table = (bs // 4) - 56
        ext_blocks = 0
        if per_table > 0 and data_blocks > per_table:
            ext_blocks = -(-(data_blocks - per_table) // per_table)
        return 1 + data_blocks + ext_blocks

    def _invalidate(self) -> None:
        """Drop cached geometry after a write, so free-space figures stay honest."""
        self._info = None

    @staticmethod
    def parse_protect(spec: str) -> int:
        """Turn a protection-bit spec into an AmigaDOS mask.

        Two spellings are accepted, because the canonical one is awkward on a command line:

        * the full 8-character form, `----rwed` or `h--pr-d-`, exactly as `ls -l` prints it
        * a short form naming only what is permitted, `rwed` or `rw`, optionally with
          `+`/`-` runs as in `+e-w`

        The short form exists because argparse reads `--protect ----rwed` as another option
        and refuses it; `--protect rwed` needs no escaping and says the same thing. The
        full form still works when written `--protect=----rwed`.

        Note that the bits are stored inverted: a *set* bit means the operation is
        forbidden, so a mask of 0 is the familiar all-permitted `----rwed`.
        """
        from amitools.fs.ProtectFlags import ProtectFlags

        text = spec.strip()
        if not text:
            raise UsageError("--protect needs some bits, e.g. 'rwed' or '----rwed'")
        flags = ProtectFlags()
        try:
            if len(text) == ProtectFlags.flag_num and "+" not in text:
                flags.parse_full(text)
            else:
                flags.parse(text)
        except Exception as e:  # amitools raises ValueError here and FSError there
            raise UsageError(
                f"{spec!r} is not a protection-bit spec. Give the eight-character form as "
                f"--protect=----rwed, or name only the permitted bits as --protect rwed "
                f"(letters from {ProtectFlags.flag_txt})"
            ) from e
        return flags.get_mask()

    def _meta(self, protect: str | None, comment: str | None,
              secs: int | None, ticks: int) -> Any:
        """Build an amitools MetaInfo with exactly the metadata asked for.

        `TimeStamp(days, mins, ticks)` assigns the triple directly; only `from_secs`,
        `parse` and the formatters consult amitools' broken epoch, and none are used
        here. So the bytes reaching the disk are the bytes computed by
        `amibuilder.timestamps`.
        """
        from amitools.fs.MetaInfo import MetaInfo
        from amitools.fs.TimeStamp import TimeStamp

        if secs is None:
            secs, ticks = timestamps.now()
        # Amiga protection bits are inverted, so a zero mask is the familiar `----rwed`:
        # read, write, execute and delete all permitted. That is what AmigaDOS gives a
        # newly created file, so it is the right default here too.
        mask = 0 if protect is None else self.parse_protect(protect)
        days, mins, raw_ticks = timestamps.to_triple(secs, ticks)
        return MetaInfo(
            protect=mask,
            mod_ts=TimeStamp(days=days, mins=mins, ticks=raw_ticks),
            comment=_fs(comment) if comment else None,
        )

    def _stamp(self, node: Any) -> None:
        """Record that a directory, and the volume, changed just now.

        Done here rather than by leaving amitools' `update_ts=True` alone: that path
        calls `time.mktime(time.localtime())` through the January-offset epoch and writes
        a timestamp an hour out for half the year. A real Amiga does update these, so
        omitting them entirely would make the image claim it was never touched -- the
        choice is between wrong bytes and correct bytes, not between writing and not.
        """
        from amitools.fs.MetaInfo import MetaInfo
        from amitools.fs.TimeStamp import TimeStamp

        secs, ticks = timestamps.now()
        days, mins, raw_ticks = timestamps.to_triple(secs, ticks)
        stamp = TimeStamp(days=days, mins=mins, ticks=raw_ticks)
        try:
            node.change_meta_info(MetaInfo(mod_ts=stamp))
            # The volume's "last changed" date is a separate root-block field from the
            # root directory's mod time, and AmigaDOS updates it on any write.
            root = getattr(self._vol, "root", None)
            if root is not None and getattr(root, "valid", False):
                root.disk_ts = stamp
                root.write()
        except Exception as e:  # noqa: BLE001
            # The data is already on disk at this point; a failed timestamp update must
            # not be reported as a failed copy, but it must not be silent either.
            raise ImageError(
                f"{self.label}: wrote the entry but could not update the directory "
                f"timestamp: {e}"
            ) from e
        self._invalidate()

    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False,
              protect: str | None = None, comment: str | None = None,
              secs: int | None = None, ticks: int = 0) -> Entry:
        """Create a directory, returning its entry.

        `create_dir` is not recursive (notes G19) -- creating `S/Prefs` without `S`
        raises `Invalid Parent Directory` -- so ancestors are walked explicitly.
        """
        self._require_writable()
        rel = _norm(path)
        if not rel:
            raise UsageError("mkdir needs a path; the volume root already exists")

        parts = rel.split("/")
        for part in parts:
            self.check_name(part)
        if comment:
            self.check_comment(comment)

        parent_rel = "/".join(parts[:-1])
        name = parts[-1]

        existing = self._find(rel)
        if existing is not None:
            if not exist_ok:
                kind = "directory" if existing.is_dir() else "file"
                raise ImageError(f"{self.label}: {rel} already exists as a {kind}")
            if not existing.is_dir():
                raise ImageError(f"{self.label}: {rel} exists and is a file, not a directory")
            return self._entry(existing, rel)

        # Built before the try, so a bad protection spec stays a usage error rather than
        # being reported as a filesystem failure.
        meta = self._meta(protect, comment, secs, ticks)
        parent = self._dir_node(parent_rel, create=parents)
        try:
            node = parent.create_dir(_fs(name), meta, False)
        except Exception as e:
            raise ImageError(f"{self.label}: cannot create directory {rel}: {e}") from e
        self._stamp(parent)
        return self._entry(node, rel)

    def write_file(self, path: str, data: bytes, *, protect: str | None = None,
                   comment: str | None = None, secs: int | None = None, ticks: int = 0,
                   replace: bool = False, parents: bool = False) -> Entry:
        """Create or replace a file, returning its entry."""
        self._require_writable()
        rel = _norm(path)
        if not rel:
            raise UsageError("write_file needs a path inside the volume")

        parts = rel.split("/")
        for part in parts:
            self.check_name(part)
        if comment:
            self.check_comment(comment)

        parent_rel = "/".join(parts[:-1])
        name = parts[-1]

        # Built first, so a malformed protection spec is reported as the typo it is rather
        # than after a capacity refusal or, worse, halfway through a write.
        meta = self._meta(protect, comment, secs, ticks)

        # Check capacity before touching anything. amitools would raise NO_FREE_BLOCKS
        # partway through otherwise, and this way the message can name the shortfall.
        needed = self.blocks_for(len(data))
        free = self.info().free_blocks
        existing = self._find(rel)
        if existing is not None and not existing.is_dir():
            free += self.blocks_for(int(existing.get_size()))
        if needed > free:
            raise ImageError(
                f"{self.label}: {rel} needs {needed} block(s) but only {free} are free "
                f"({self.info().block_size * free} bytes)"
            )

        if existing is not None:
            if existing.is_dir():
                raise ImageError(f"{self.label}: {rel} exists and is a directory")
            if not replace:
                raise ImageError(f"{self.label}: {rel} already exists")
            # amitools refuses to overwrite (notes G22), so replacing means deleting
            # first. update_ts=False for the same reason as everywhere else here.
            try:
                existing.delete(wipe=False, all=False, update_ts=False)
            except Exception as e:
                raise ImageError(f"{self.label}: cannot replace {rel}: {e}") from e
            self._invalidate()

        parent = self._dir_node(parent_rel, create=parents)
        try:
            node = parent.create_file(_fs(name), data, meta, False)
        except Exception as e:
            raise ImageError(f"{self.label}: cannot write {rel}: {e}") from e
        self._stamp(parent)
        return self._entry(node, rel)

    def set_times(self, path: str, secs: int, ticks: int = 0) -> Entry:
        """Set an existing entry's modification timestamp.

        Needed because writing into a directory stamps that directory -- which is correct
        AmigaDOS behaviour, and exactly wrong when the caller is trying to reproduce a
        recorded tree. A `cp --preserve-times` of a directory therefore writes the
        contents first and re-applies the directory's own timestamp afterwards.
        """
        self._require_writable()
        rel = _norm(path)
        node = self._node(rel)

        from amitools.fs.MetaInfo import MetaInfo
        from amitools.fs.TimeStamp import TimeStamp

        days, mins, raw_ticks = timestamps.to_triple(secs, ticks)
        try:
            node.change_meta_info(MetaInfo(mod_ts=TimeStamp(days=days, mins=mins,
                                                            ticks=raw_ticks)))
        except Exception as e:
            raise ImageError(f"{self.label}: cannot set the timestamp on {rel}: {e}") from e
        self._invalidate()
        return self._entry(node, rel)

    def remove(self, path: str, *, recursive: bool = False) -> Entry:
        """Delete a file, or -- with `recursive` -- a directory and everything under it.

        Returns the entry as it was *before* deletion, so a caller can report what went.

        Mirrors AmigaDOS `Delete`: the entry is unlinked and its blocks freed, but the data
        is not wiped (`wipe=False`). That is deliberately the same non-reclaiming behaviour
        the dead-space finding is about -- a removed file's bytes stay on the disk until
        something else allocates over them -- so a delete here behaves exactly as it would
        on the real machine.

        A directory is refused unless `recursive`, so a plain `remove` can never take a
        subtree by accident. The volume root is refused outright. A missing path raises
        `NotFoundError`, distinct from the `ImageError` a directory-without-recursive
        raises, so the three outcomes stay tellable apart.

        `update_ts=False` for the same reason as every other write here: amitools' own
        timestamp update runs through the hour-adrift epoch (see `amibuilder.timestamps`).
        The parent's modification time is then stamped correctly by `_stamp`, which a real
        Amiga does on a delete.
        """
        self._require_writable()
        rel = _norm(path)
        if not rel:
            raise UsageError(f"{self.label}: cannot remove the volume root")

        node = self._node(rel)  # raises NotFoundError for a missing path
        # Captured before the node is unlinked, so the return value still has its size/kind.
        entry = self._entry(node, rel)

        if node.is_dir() and not recursive:
            raise ImageError(
                f"{self.label}: {rel} is a directory; pass recursive to remove it and "
                f"everything under it"
            )

        # amitools' own volume-level delete calls node.delete() on a get_path_name result,
        # so .parent is guaranteed populated here.
        parent = node.parent
        try:
            node.delete(wipe=False, all=recursive, update_ts=False)
        except Exception as e:
            raise ImageError(f"{self.label}: cannot remove {rel}: {e}") from e

        if parent is not None:
            self._stamp(parent)
        else:
            self._invalidate()
        return entry

    def flush(self) -> None:
        """Force everything written so far out to the underlying file or device.

        A CLI command opens and closes the volume per invocation, so its writes are always
        durable by the time it returns. A long-lived holder of an open volume -- the
        interactive shell -- is different: an unclean exit partway through a session would
        otherwise leave file and directory blocks written but the on-disk allocation bitmap
        stale, which is latent corruption (notes G29). Calling this after every mutating
        command shrinks that window to nothing short of a power cut.

        Two steps, and both are needed:

        * `ADFSBitmap.write()` is the only thing that serialises the in-memory bitmap (and
          the root block's bitmap pointers) into block writes. It is dirty-gated upstream,
          so calling it when nothing changed is a cheap no-op.
        * amitools' block writes -- data, headers *and* that bitmap -- go through a
          Python-buffered file object, so they do not reach the OS until the block device's
          buffer is flushed. `ADFSVolume.close()` gets this for free by closing the file;
          mid-session there is no close, so the flush is explicit.

        Both are reached defensively: a freshly created volume has a bitmap, and every
        amitools block device class exposes `flush`, but guarding means a future device
        without one degrades to "bitmap serialised, OS flush skipped" rather than raising.
        """
        bitmap = getattr(self._vol, "bitmap", None)
        if bitmap is not None:
            bitmap.write()
        flush = getattr(self._blkdev, "flush", None)
        if callable(flush):
            flush()
        self._invalidate()

    def _find(self, relative: str) -> Any | None:
        """The node at a volume-relative path, or None. Never raises for absence."""
        try:
            return self._node(relative)
        except (NotFoundError, ImageError):
            return None

    def _dir_node(self, relative: str, *, create: bool) -> Any:
        """Resolve a directory path to its node, optionally creating the chain."""
        if not relative:
            return self._vol.get_root_dir()
        node = self._find(relative)
        if node is not None:
            if not node.is_dir():
                raise ImageError(f"{self.label}: {relative} is a file, not a directory")
            return node
        if not create:
            raise NotFoundError(
                f"{self.label}: no such directory: {relative}. Create it first, or pass "
                f"--parents"
            )
        parent_rel, _, name = relative.rpartition("/")
        parent = self._dir_node(parent_rel, create=True)
        try:
            node = parent.create_dir(_fs(name), self._meta(None, None, None, 0), False)
        except Exception as e:
            raise ImageError(f"{self.label}: cannot create directory {relative}: {e}") from e
        self._stamp(parent)
        return node

    def file_blocks(self, path: str) -> list[int]:
        """Header, extension and data block numbers for a file, in order.

        Composed by hand rather than via `node.get_blocks(with_data=True)`, which omits
        every data block on an FFS volume: amitools only appends to `data_blks` in the
        OFS branch of `ADFSFile.read()`, so on FFS it returns just the header and
        extension blocks. `data_blk_nums` is populated correctly, so it is used instead.
        Pinned by test_amitools_regressions.py.
        """
        node = self._node(path)
        if node.is_dir():
            raise ImageError(f"{self.label}: is a directory: {path}")
        nums = [node.block.blk_num]
        nums += [b.blk_num for b in getattr(node, "ext_blks", [])]
        nums += list(getattr(node, "data_blk_nums", []))
        return nums


def open_adfs_volume(blkdev: Any, label: str, closers: list[Any],
                     writable: bool = False) -> Volume:
    """Wrap an open amitools block device as a Volume, or refuse with a clear reason.

    An unformatted or foreign-filesystem partition reaches here and must produce a
    named refusal, never a traceback: the whole point is to say "this is PFS3" rather
    than to guess at the layout.
    """
    from amitools.fs.ADFSVolume import ADFSVolume

    # Peek the DosType from the boot block before trusting amitools to mount it, so a
    # PFS3/SFS partition is named rather than reported as a generic mount failure.
    try:
        blkdev.read_block(0)
    except Exception:  # noqa: BLE001 - fall through to the mount attempt
        pass
    else:
        import struct

        raw = struct.unpack_from(">I", bytes(blkdev.read_block(0)), 0)[0]
        dt = decode_dos_type(raw)
        if not dt.supported and dt.filesystem not in ("unknown", "none"):
            raise UnsupportedError(f"{label}: {dt.filesystem} -- {dt.note}")

    vol = ADFSVolume(blkdev)
    try:
        vol.open()
    except Exception as e:
        raise ImageError(
            f"{label}: not a mountable AmigaDOS volume ({e}). If this partition uses "
            f"PFS3 or SFS, amibuilder cannot read it at file level."
        ) from e
    # vol.close flushes the allocation bitmap, so it must run before the device closes.
    # Volume.close walks the list in reverse, which puts it first.
    closers = closers + [vol.close]
    return Volume(vol, blkdev, label, closers, writable=writable)
