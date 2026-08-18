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
from .errors import ImageError, NotFoundError, UnsupportedError

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


def _norm(path: str) -> str:
    """Normalise a volume-relative path.

    Accepts and strips a leading '/' or ':' so that both `S/Startup-Sequence` and
    `/S/Startup-Sequence` work, and collapses redundant separators.
    """
    p = path.strip()
    if ":" in p:  # tolerate a volume-qualified path by dropping the volume part
        p = p.split(":", 1)[1]
    p = p.strip("/")
    while "//" in p:
        p = p.replace("//", "/")
    return p


class Volume:
    """A mounted Amiga filesystem, read-only for Phase 1.

    Constructed by `amibuilder.image`, not directly. Usable as a context manager; the
    underlying block device is closed on exit.
    """

    def __init__(self, adfs_volume: Any, blkdev: Any, label: str, closers: list[Any]):
        self._vol = adfs_volume
        self._blkdev = blkdev
        self._closers = closers
        #: Human-readable source, e.g. "card.hdf:0". Used in error messages.
        self.label = label
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


def open_adfs_volume(blkdev: Any, label: str, closers: list[Any]) -> Volume:
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
    closers = closers + [vol.close]
    return Volume(vol, blkdev, label, closers)
