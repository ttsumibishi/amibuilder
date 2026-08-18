"""MBR parsing and byte-range slicing, for the PiStorm / Emu68 workflow.

Emu68 exposes MBR primary partitions of type 0x76 as separate Amiga drive units, each
holding its own RDB. Reaching them means addressing a byte range inside a device or
container rather than a whole file.

amitools' BlkDevFactory.open() accepts an `fobj`, so SliceFile below is enough to open
an embedded RDB with no amitools changes. Verified in docs/KIP-FFS-NOTES.md section 9.3.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from .errors import ImageError

SECTOR = 512
MBR_SIGNATURE = b"\x55\xaa"
MBR_TABLE_OFFSET = 446
MBR_ENTRY_SIZE = 16
MBR_ENTRY_COUNT = 4

#: Emu68 / WinUAE / Amithlon "virtual Amiga drive" partition type.
PTYPE_AMIGA_VIRTUAL = 0x76

#: Partition types worth naming in output. Everything else prints as bare hex.
PTYPE_NAMES = {
    0x00: "empty",
    0x01: "FAT12",
    0x05: "extended",
    0x06: "FAT16",
    0x07: "NTFS/exFAT",
    0x0B: "FAT32",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x0F: "extended (LBA)",
    0x82: "Linux swap",
    0x83: "Linux",
    0xEE: "GPT protective",
    0xEF: "EFI system",
    PTYPE_AMIGA_VIRTUAL: "Amiga (RDB)",
}


def ptype_name(ptype: int) -> str:
    return PTYPE_NAMES.get(ptype, f"0x{ptype:02x}")


@dataclass(frozen=True)
class MbrPartition:
    index: int
    ptype: int
    start_lba: int
    sector_count: int

    @property
    def byte_offset(self) -> int:
        return self.start_lba * SECTOR

    @property
    def byte_length(self) -> int:
        return self.sector_count * SECTOR

    @property
    def is_amiga(self) -> bool:
        return self.ptype == PTYPE_AMIGA_VIRTUAL

    @property
    def type_name(self) -> str:
        return ptype_name(self.ptype)


def parse_mbr_bytes(mbr: bytes, source: str = "<bytes>") -> list[MbrPartition]:
    """Parse a 512-byte MBR. Raises ImageError if the signature is absent.

    Returns only populated entries (type != 0), preserving their table index so that
    "0x76:1" style addressing refers to a stable slot rather than a position in a
    filtered list.
    """
    if len(mbr) < SECTOR:
        raise ImageError(f"{source}: too short to contain an MBR")
    if mbr[510:512] != MBR_SIGNATURE:
        raise ImageError(f"{source}: no MBR signature (expected 55AA)")

    parts: list[MbrPartition] = []
    for i in range(MBR_ENTRY_COUNT):
        base = MBR_TABLE_OFFSET + i * MBR_ENTRY_SIZE
        ptype = mbr[base + 4]
        if ptype == 0:
            continue
        start = struct.unpack_from("<I", mbr, base + 8)[0]
        count = struct.unpack_from("<I", mbr, base + 12)[0]
        parts.append(MbrPartition(i, ptype, start, count))
    return parts


def parse_mbr(path: str) -> list[MbrPartition]:
    """Read and parse the MBR partition table of a file or device."""
    with open(path, "rb") as f:
        mbr = f.read(SECTOR)
    return parse_mbr_bytes(mbr, path)


def has_mbr(path: str) -> bool:
    """True if the first sector carries an MBR signature. Never raises."""
    try:
        with open(path, "rb") as f:
            return f.read(SECTOR)[510:512] == MBR_SIGNATURE
    except OSError:
        return False


def amiga_partitions(path: str) -> list[MbrPartition]:
    """Just the type 0x76 entries -- the ones Emu68 presents as Amiga drives."""
    return [p for p in parse_mbr(path) if p.is_amiga]


class SliceFile(io.RawIOBase):
    """A read/write file-like view onto a byte range of an underlying file or device.

    Writes are clamped to the slice, so a bug in offset arithmetic cannot scribble
    past the partition boundary -- which on a PiStorm card would mean destroying the
    Emu68 boot partition.
    """

    def __init__(self, path: str, offset: int, length: int, writable: bool = False):
        if offset < 0 or length <= 0:
            raise ValueError(f"invalid slice offset={offset} length={length}")
        self._f = open(path, "r+b" if writable else "rb")
        self._off = offset
        self._len = length
        self._pos = 0
        self._writable = writable

    # -- io.RawIOBase contract ------------------------------------------------
    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return self._writable

    def seekable(self) -> bool:
        return True

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new = pos
        elif whence == io.SEEK_CUR:
            new = self._pos + pos
        elif whence == io.SEEK_END:
            new = self._len + pos
        else:
            raise ValueError(f"bad whence {whence}")
        self._pos = max(0, new)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._len - self._pos
        size = max(0, min(size, self._len - self._pos))
        if size == 0:
            return b""
        self._f.seek(self._off + self._pos)
        data = self._f.read(size)
        self._pos += len(data)
        return data

    def readinto(self, buf) -> int:  # type: ignore[override]
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)

    def write(self, buf) -> int:  # type: ignore[override]
        if not self._writable:
            raise io.UnsupportedOperation("slice opened read-only")
        avail = self._len - self._pos
        if len(buf) > avail:
            raise ValueError(
                f"write of {len(buf)} bytes at slice offset {self._pos} would "
                f"overrun the {self._len}-byte slice"
            )
        self._f.seek(self._off + self._pos)
        n = self._f.write(bytes(buf))
        self._pos += n
        return n

    def flush(self) -> None:
        # Guard on the underlying handle, not self.closed: RawIOBase.close() calls
        # flush() while self.closed is still False, after we have closed self._f.
        if not self._f.closed:
            self._f.flush()

    def close(self) -> None:
        try:
            if not self._f.closed:
                self._f.flush()
                self._f.close()
        finally:
            super().close()
