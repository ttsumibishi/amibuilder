"""Containers: what an image *is*, and how to reach the volumes inside it.

Six shapes have to be handled behind one door (docs/KIP-FFS-PLAN.md Phase 1):

    plain HDF      one volume, no partition table
    RDB image      a whole disk: RigidDiskBlock plus N partitions
    ADF            a floppy image, optionally gzipped
    raw device     /dev/rdiskN -- the SD card itself
    MBR slice      a byte range inside a device or file, holding its own RDB
    directory      the host side of a sync

Detection is by content first and extension second, matching amitools' own precedence:
an RDB whole-disk image is routinely named `.hdf`, so trusting the extension would open
partition 0's filesystem as if it were the whole disk and report nonsense geometry.

One amitools behaviour worth stating plainly, because it shaped this module:
`BlkDevFactory.open()` on an RDB image does *not* hand back the disk -- it silently
resolves partition 0 (or `options["part"]`) and returns that partition's block device.
Enumerating partitions therefore has to drive `RawBlockDevice` and `RDisk` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, BinaryIO

from .addressing import Address
from .blocks import DosType, decode_dos_type
from .errors import AddressError, ImageError, UnsupportedError
from .mbr import MBR_SIGNATURE, MbrPartition, SliceFile, parse_mbr_bytes
from .volume import Volume, open_adfs_volume


class ImageKind(str, Enum):
    RDB = "rdb"
    PLAIN_HDF = "hdf"
    ADF = "adf"
    MBR = "mbr"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Geometry:
    block_size: int
    num_blocks: int
    cyls: int = 0
    heads: int = 0
    sectors: int = 0

    @property
    def total_bytes(self) -> int:
        return self.num_blocks * self.block_size

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_size": self.block_size,
            "num_blocks": self.num_blocks,
            "cylinders": self.cyls,
            "heads": self.heads,
            "sectors": self.sectors,
            "total_bytes": self.total_bytes,
        }


#: Every field of the RDB DosEnvec, listed explicitly rather than harvested from the object.
#:
#: Two reasons it is a fixed list. A base layer records this verbatim so `compose` can
#: rebuild a drive that behaves identically -- the layers design calls this out for
#: `de_Mask`/`de_MaxTransfer`, where HDToolBox's suggested value silently breaks PFS3 on
#: PiStorm (docs/KIP-FFS-LAYERS.md section 4), so guessing a default is the failure mode to
#: avoid. And because the record feeds a layer's identity hash, an amitools release that
#: added a field would otherwise change every layer ID it touched.
DOS_ENV_FIELDS = (
    "size",
    "block_size",
    "sec_org",
    "surfaces",
    "sec_per_blk",
    "blk_per_trk",
    "reserved",
    "pre_alloc",
    "interleave",
    "low_cyl",
    "high_cyl",
    "num_buffer",
    "buf_mem_type",
    "max_transfer",
    "mask",
    "boot_pri",
    "dos_type",
    "baud",
    "control",
    "boot_blocks",
)


@dataclass(frozen=True)
class PartitionInfo:
    """One RDB partition, as reported without mounting its filesystem."""

    index: int
    device_name: str  #: the AmigaDOS device, e.g. DH0
    dos_type: DosType
    low_cyl: int
    high_cyl: int
    num_blocks: int
    num_bytes: int
    block_size: int
    bootable: bool
    automount: bool
    boot_pri: int
    reserved: int
    mask: int
    max_transfer: int
    num_buffer: int
    #: None when the filesystem could not be mounted; the reason is in `volume_error`.
    volume_name: str | None = None
    volume_error: str | None = None
    #: The complete DosEnvec exactly as read, keyed by `DOS_ENV_FIELDS`. The curated fields
    #: above are the ones worth showing a person; this is what reproducing the drive needs.
    dos_env: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "device": self.device_name,
            "volume": self.volume_name,
            "volume_error": self.volume_error,
            "dos_type": self.dos_type.label,
            "dos_type_raw": f"0x{self.dos_type.raw:08x}",
            "filesystem": self.dos_type.filesystem,
            "features": self.dos_type.features,
            "supported": self.dos_type.supported,
            "low_cyl": self.low_cyl,
            "high_cyl": self.high_cyl,
            "num_blocks": self.num_blocks,
            "num_bytes": self.num_bytes,
            "block_size": self.block_size,
            "bootable": self.bootable,
            "automount": self.automount,
            "boot_pri": self.boot_pri,
            "reserved": self.reserved,
            "mask": f"0x{self.mask:08x}",
            "max_transfer": f"0x{self.max_transfer:08x}",
            "num_buffer": self.num_buffer,
            "dos_env": dict(self.dos_env),
        }


def _device_size(path: str) -> int:
    """Size of a raw device, which os.path.getsize() reports as 0.

    Note the import path: `amitools.util.BlkDevTools`, **not** `amitools.fs.blkdev`. The
    latter is where it feels like it should live and where amitools' own blkdev modules
    import it *from* elsewhere, and it does not exist there -- so getting this wrong turns
    every raw-device operation into "cannot determine device size: cannot import name
    'BlkDevTools'", at construction, before any device is touched. Pinned by
    test_image.py::test_device_size_uses_an_import_path_that_exists.
    """
    from amitools.util import BlkDevTools

    return BlkDevTools.getblkdevsize(path)


def _adf_sizes() -> tuple[set[int], set[int]]:
    from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

    return set(ADFBlockDevice.DD_IMG_SIZES), set(ADFBlockDevice.HD_IMG_SIZES)


class Container:
    """An opened image container. Use as a context manager."""

    def __init__(self, address: Address, writable: bool = False):
        self.address = address
        self.writable = writable
        self._closers: list[Any] = []
        self._mbr: list[MbrPartition] | None = None
        self._partitions: list[PartitionInfo] | None = None

        if address.is_directory:
            self.kind = ImageKind.DIRECTORY
            self.size_bytes = 0
            self.geometry = Geometry(0, 0)
            self._slice_offset = 0
            self._slice_length = 0
            return

        self._whole_bytes = self._measure_whole()
        self._slice_offset, self._slice_length = self._resolve_slice()
        self.size_bytes = self._slice_length
        self.kind = self._detect_kind()
        #: DosType found at offset 0, or None for an RDB/MBR container where offset 0
        #: holds a partition table instead of a boot block.
        self.boot_dos_type = self._detect_boot_dos_type()
        self.geometry = self._detect_geometry()

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> Container:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for c in reversed(self._closers):
            try:
                c()
            except Exception:  # noqa: BLE001
                pass
        self._closers = []

    # -- slicing -------------------------------------------------------------
    def _measure_whole(self) -> int:
        """Total size of the underlying file or device. Measured once, at construction."""
        path = self.address.path
        if self.address.is_device:
            try:
                return _device_size(path)
            except Exception as e:
                raise ImageError(f"{path}: cannot determine device size: {e}") from e
        size = os.path.getsize(path)
        if size == 0:
            raise ImageError(f"{path}: file is empty")
        return size

    @property
    def _whole_size(self) -> int:
        return self._whole_bytes

    @property
    def is_sliced(self) -> bool:
        """True when this container covers only part of the underlying file or device."""
        return self._slice_offset != 0 or self._slice_length != self._whole_bytes

    @property
    def slice_offset(self) -> int:
        """Byte offset of this container within the underlying file or device."""
        return self._slice_offset

    @property
    def slice_length(self) -> int:
        return self._slice_length

    def _resolve_slice(self) -> tuple[int, int]:
        """Work out the byte range this container covers.

        Without an MBR selector that is the whole file or device. With one it is the
        named primary partition -- and the type is checked, because addressing the FAT
        boot partition of a PiStorm card as if it held an RDB is exactly the mistake
        that would destroy Emu68.
        """
        whole = self._whole_bytes
        addr = self.address
        if not addr.has_mbr_selector:
            return 0, whole

        with open(addr.path, "rb") as f:
            first = f.read(512)
        parts = parse_mbr_bytes(first, addr.path)
        match = [p for p in parts if p.index == addr.mbr_slot]
        if not match:
            populated = ", ".join(f"{p.index}={p.type_name}" for p in parts) or "none"
            raise AddressError(
                f"{addr.spec}: MBR slot {addr.mbr_slot} is empty. Populated slots: {populated}"
            )
        p = match[0]
        if addr.mbr_type is not None and p.ptype != addr.mbr_type:
            raise AddressError(
                f"{addr.spec}: MBR slot {addr.mbr_slot} is type 0x{p.ptype:02x} "
                f"({p.type_name}), not 0x{addr.mbr_type:02x}. Refusing to treat it as an "
                f"Amiga partition -- on a PiStorm card the other slots hold Emu68 itself."
            )
        if p.byte_offset + p.byte_length > whole:
            raise ImageError(
                f"{addr.spec}: MBR slot {addr.mbr_slot} extends to "
                f"{p.byte_offset + p.byte_length} bytes but the container is only {whole}"
            )
        return p.byte_offset, p.byte_length

    def stream(self) -> BinaryIO:
        """A fresh read-only stream over this container's byte range.

        The caller owns it. Used by `hexdump` and by block-level checks.
        """
        if self.address.is_directory:
            raise ImageError(f"{self.address.path}: is a directory, not an image")
        if self.is_sliced:
            return SliceFile(self.address.path, self._slice_offset, self._slice_length)  # type: ignore[return-value]
        return open(self.address.path, "rb")

    def _fobj_for_amitools(self) -> Any:
        """An fobj to hand amitools, or None to let it open the path itself.

        Only slices need an fobj. Passing None for a whole file lets amitools use its own
        raw-device size handling, which knows how to size /dev/rdiskN.
        """
        if not self.is_sliced:
            return None
        sl = SliceFile(self.address.path, self._slice_offset, self._slice_length,
                       writable=self.writable)
        self._closers.append(sl.close)
        return sl

    # -- detection -----------------------------------------------------------
    def _read_head(self, n: int = 512) -> bytes:
        with self.stream() as f:
            return f.read(n)

    def _detect_kind(self) -> ImageKind:
        head = self._read_head()
        if head[:4] == b"RDSK":
            return ImageKind.RDB

        # An MBR with no RDB at offset 0 means the Amiga data lives in a slice.
        if len(head) >= 512 and head[510:512] == MBR_SIGNATURE:
            try:
                if parse_mbr_bytes(head, self.address.path):
                    return ImageKind.MBR
            except ImageError:
                pass

        dd, hd = _adf_sizes()
        if self.size_bytes in dd or self.size_bytes in hd:
            return ImageKind.ADF
        if self.address.looks_like_adf:
            # Trust the extension enough to give a size-specific error rather than
            # silently treating a truncated ADF as an HDF with odd geometry.
            raise ImageError(
                f"{self.address.path}: named like an ADF but its size ({self.size_bytes} "
                f"bytes) is not a valid floppy image size"
            )
        return ImageKind.PLAIN_HDF

    def _detect_boot_dos_type(self) -> DosType | None:
        """Decode the boot block DosType of a flat container.

        This is what separates "unformatted", "a filesystem I will not touch" and "not an
        Amiga disk at all" -- three cases that must produce three different messages
        rather than one vague mount failure. Slicing the FAT boot partition of a PiStorm
        card lands here, and has to say so.
        """
        if self.kind not in (ImageKind.PLAIN_HDF, ImageKind.ADF):
            return None
        import struct

        head = self._read_head(4)
        if len(head) < 4:
            return None
        return decode_dos_type(struct.unpack_from(">I", head, 0)[0])

    @property
    def is_formatted(self) -> bool:
        """True if a flat container carries a filesystem amibuilder can name."""
        dt = self.boot_dos_type
        return dt is not None and dt.filesystem != "none"

    def _detect_geometry(self) -> Geometry:
        bs = 512
        if self.kind in (ImageKind.MBR, ImageKind.UNKNOWN):
            return Geometry(bs, self.size_bytes // bs)
        if self.kind is ImageKind.RDB:
            return self._rdb_geometry()
        if self.kind is ImageKind.ADF:
            return Geometry(bs, self.size_bytes // bs, cyls=80, heads=2,
                            sectors=self.size_bytes // bs // 160)
        # Plain HDF: amitools has to infer a CHS geometry, and can fail.
        from amitools.fs.blkdev.DiskGeometry import DiskGeometry

        geo = DiskGeometry(block_bytes=bs)
        if not geo.detect(self.size_bytes, None):
            raise ImageError(
                f"{self.address.path}: cannot infer a disk geometry from "
                f"{self.size_bytes} bytes. A plain HDF has no partition table, so its "
                f"size must map onto whole cylinders."
            )
        return Geometry(bs, geo.get_num_blocks(), geo.cyls, geo.heads, geo.secs)

    def _rdb_geometry(self) -> Geometry:
        with self._rdisk() as (rdisk, _):
            cyls, heads, secs = rdisk.get_cyls_heads_secs()
            return Geometry(rdisk.block_bytes, rdisk.get_total_blocks(), cyls, heads, secs)

    # -- RDB ------------------------------------------------------------------
    class _RDiskCtx:
        """Opens RawBlockDevice + RDisk together and guarantees both are closed."""

        def __init__(self, container: Container):
            self.c = container
            self.raw: Any = None
            self.rdisk: Any = None

        def __enter__(self) -> tuple[Any, Any]:
            from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
            from amitools.fs.rdb.RDisk import RDisk

            path, c = self.c.address.path, self.c
            fobj = c._fobj_for_amitools()

            # The RDB records its own block size, which may not be 512. Peek, then
            # re-open at the right size if it differs -- reading a 1024-byte-block RDB
            # through a 512-byte device yields garbage partition entries.
            self.raw = RawBlockDevice(path, read_only=not c.writable, fobj=fobj,
                                      block_bytes=512)
            self.raw.open()
            self.rdisk = RDisk(self.raw)
            bs = self.rdisk.peek_block_size()
            if bs != 512:
                self.raw.close()
                if fobj is not None:
                    fobj = c._fobj_for_amitools()
                self.raw = RawBlockDevice(path, read_only=not c.writable, fobj=fobj,
                                          block_bytes=bs)
                self.raw.open()
                self.rdisk = RDisk(self.raw)
            if not self.rdisk.open():
                raise ImageError(f"{path}: RigidDiskBlock is present but unreadable")
            return self.rdisk, self.raw

        def __exit__(self, *exc: object) -> None:
            for obj in (self.rdisk, self.raw):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:  # noqa: BLE001
                    pass

    def _rdisk(self) -> Container._RDiskCtx:
        if self.kind is not ImageKind.RDB:
            raise ImageError(f"{self.address.path}: not an RDB image")
        return Container._RDiskCtx(self)

    def mbr_partitions(self) -> list[MbrPartition]:
        """MBR primary partitions, or [] if there is no MBR."""
        if self._mbr is None:
            try:
                self._mbr = parse_mbr_bytes(self._read_head(), self.address.path)
            except ImageError:
                self._mbr = []
        return self._mbr

    def partitions(self, probe_volumes: bool = True) -> list[PartitionInfo]:
        """RDB partition table.

        `probe_volumes` mounts each partition to recover its volume name, which is what
        the user actually thinks in ("Workbench:", not "DH0"). A partition that will not
        mount records the reason instead of failing the whole listing -- one PFS3
        partition must not make the other three unlistable.
        """
        if self.kind is not ImageKind.RDB:
            return []
        if self._partitions is not None:
            return self._partitions

        out: list[PartitionInfo] = []
        with self._rdisk() as (rdisk, _):
            for i in range(rdisk.get_num_partitions()):
                p = rdisk.get_partition(i)
                pb = p.part_blk
                de = pb.dos_env
                flags = p.get_flags()
                out.append(
                    PartitionInfo(
                        index=p.get_index(),
                        device_name=str(p.get_drive_name()),
                        dos_type=decode_dos_type(de.dos_type),
                        low_cyl=de.low_cyl,
                        high_cyl=de.high_cyl,
                        num_blocks=p.get_num_blocks(),
                        num_bytes=p.get_num_bytes(),
                        # dos_env.block_size counts LONGWORDS, not bytes.
                        block_size=de.block_size * 4,
                        bootable=bool(flags & pb.FLAG_BOOTABLE),
                        automount=not (flags & pb.FLAG_NO_AUTOMOUNT),
                        boot_pri=de.boot_pri,
                        reserved=de.reserved,
                        mask=de.mask,
                        max_transfer=de.max_transfer,
                        num_buffer=de.num_buffer,
                        dos_env={
                            name: int(getattr(de, name)) for name in DOS_ENV_FIELDS
                        },
                    )
                )

        if probe_volumes:
            out = [self._with_volume_name(p) for p in out]
        self._partitions = out
        return out

    def _with_volume_name(self, p: PartitionInfo) -> PartitionInfo:
        from dataclasses import replace

        if not p.dos_type.supported:
            return replace(p, volume_error=p.dos_type.note or "unsupported filesystem")
        try:
            with self.open_volume(p.index) as vol:
                return replace(p, volume_name=vol.info().name)
        except (ImageError, UnsupportedError) as e:
            return replace(p, volume_error=str(e).split(": ", 1)[-1])

    def resolve_partition(self, selector: int | str | None) -> int:
        """Turn a selector into a partition index.

        Accepts an index, an AmigaDOS device name (DH0) or a volume name (Workbench).
        amitools resolves the first two; volume names need each partition mounting,
        which is done only on a miss so the common cases stay cheap.
        """
        parts = self.partitions(probe_volumes=False)
        if not parts:
            raise ImageError(f"{self.address.path}: no partitions")

        if selector is None:
            return parts[0].index
        if isinstance(selector, int):
            if not any(p.index == selector for p in parts):
                raise AddressError(
                    f"{self.address.spec}: no partition {selector}; this disk has "
                    f"{len(parts)} ({', '.join(str(p.index) for p in parts)})"
                )
            return selector

        low = selector.lower()
        for p in parts:
            if p.device_name.lower() == low:
                return p.index
        # Fall back to volume names, mounting as needed.
        for p in parts:
            try:
                with self.open_volume(p.index) as vol:
                    if vol.info().name.lower() == low:
                        return p.index
            except (ImageError, UnsupportedError):
                continue

        names = ", ".join(
            f"{p.index}={p.device_name}" for p in parts
        )
        raise AddressError(
            f"{self.address.spec}: no partition named {selector!r}. Available: {names}"
        )

    # -- volumes -------------------------------------------------------------
    def open_volume(self, selector: int | str | None = None) -> Volume:
        """Mount a filesystem inside this container."""
        if self.kind is ImageKind.DIRECTORY:
            raise ImageError(f"{self.address.path}: is a host directory, not an image")
        if self.kind is ImageKind.MBR:
            slots = ", ".join(
                f"0x{p.ptype:02x}:{p.index} ({p.type_name})" for p in self.mbr_partitions()
            )
            raise AddressError(
                f"{self.address.path}: this is an MBR-partitioned device, not a single "
                f"Amiga disk. Select a partition, e.g. "
                f"'{self.address.path}:0x76:1'. Slots present: {slots}"
            )

        if self.kind is ImageKind.RDB:
            return self._open_rdb_volume(selector)

        if selector is not None:
            raise AddressError(
                f"{self.address.spec}: {self.kind.value} images hold a single volume and "
                f"have no partition table, so ':{selector}' does not apply"
            )

        dt = self.boot_dos_type
        if dt is not None and not dt.supported:
            where = self.address.describe()
            if dt.filesystem == "none":
                raise UnsupportedError(
                    f"{where}: unformatted -- no filesystem to read. "
                    f"Use 'amibuilder format' to create one."
                )
            if self.is_sliced:
                raise UnsupportedError(
                    f"{where}: this slice does not hold an Amiga filesystem "
                    f"({dt.describe()}). On a PiStorm card only type 0x76 slots do; the "
                    f"others hold Emu68 itself."
                )
            raise UnsupportedError(f"{where}: {dt.describe()} -- {dt.note}")

        return self._open_flat_volume()

    def open_addressed_volume(self) -> Volume:
        """Mount the volume the address names, using its partition selector if any.

        Commands should call this rather than `open_volume()`, so that the ':N' the user
        typed cannot be silently dropped.
        """
        return self.open_volume(self.address.partition)

    def _open_flat_volume(self) -> Volume:
        from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory

        fobj = self._fobj_for_amitools()
        try:
            blkdev = BlkDevFactory().open(self.address.path, read_only=not self.writable,
                                          fobj=fobj)
        except Exception as e:
            raise ImageError(f"{self.address.path}: cannot open as a block device: {e}") from e
        return open_adfs_volume(blkdev, self.address.describe(), [blkdev.close],
                                writable=self.writable)

    def _open_rdb_volume(self, selector: int | str | None) -> Volume:
        from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
        from amitools.fs.rdb.RDisk import RDisk

        # Resolve against a throwaway RDisk so the long-lived one below is opened once.
        index = self.resolve_partition(selector)

        fobj = self._fobj_for_amitools()
        raw = RawBlockDevice(self.address.path, read_only=not self.writable, fobj=fobj)
        raw.open()
        rdisk = RDisk(raw)
        bs = rdisk.peek_block_size()
        if bs != 512:
            raw.close()
            raw = RawBlockDevice(self.address.path, read_only=not self.writable,
                                 fobj=self._fobj_for_amitools(), block_bytes=bs)
            raw.open()
            rdisk = RDisk(raw)
        if not rdisk.open():
            raw.close()
            raise ImageError(f"{self.address.path}: RigidDiskBlock unreadable")

        part = rdisk.get_partition(index)
        if part is None:
            rdisk.close()
            raw.close()
            raise AddressError(f"{self.address.spec}: no partition {index}")

        blkdev = part.create_blkdev(False)
        blkdev.open()
        label = f"{self.address.path}:{index}"
        return open_adfs_volume(
            blkdev, label, [blkdev.close, rdisk.close, raw.close],
            writable=self.writable,
        )

    # -- reporting -----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.address.path,
            "spec": self.address.spec,
            "kind": self.kind.value,
            "is_device": self.address.is_device,
            "size_bytes": self.size_bytes,
            "geometry": self.geometry.as_dict(),
        }
        if self.address.has_mbr_selector:
            d["mbr_slice"] = {
                "slot": self.address.mbr_slot,
                "type": f"0x{self.address.mbr_type:02x}" if self.address.mbr_type else None,
                "offset": self._slice_offset,
                "length": self._slice_length,
            }
        return d


def open_container(address: Address, writable: bool = False) -> Container:
    return Container(address, writable=writable)
