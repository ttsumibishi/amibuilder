"""MBR test helpers.

The parser, SliceFile and constants now live in `amibuilder.mbr` -- this module
re-exports them so existing tests keep working, and adds `build_mbr`, which is a
fixture-only concern with no place in the shipped package.
"""

from __future__ import annotations

import struct

from amibuilder.mbr import (
    MBR_ENTRY_COUNT,
    MBR_ENTRY_SIZE,
    MBR_SIGNATURE,
    MBR_TABLE_OFFSET,
    PTYPE_AMIGA_VIRTUAL,
    SECTOR,
    SliceFile,
    amiga_partitions,
    parse_mbr,
)

# Re-exported for the tests that reach these as `helpers.mbr.<name>`; __all__ declares the
# surface so they are not seen as unused imports.
__all__ = [
    "MBR_ENTRY_COUNT",
    "MBR_ENTRY_SIZE",
    "MBR_SIGNATURE",
    "MBR_TABLE_OFFSET",
    "PTYPE_AMIGA_VIRTUAL",
    "SECTOR",
    "SliceFile",
    "amiga_partitions",
    "build_mbr",
    "parse_mbr",
]


def build_mbr(entries: list[tuple[int, int, int]]) -> bytes:
    """Build a minimal MBR from (ptype, start_lba, sector_count) tuples.

    Test-fixture use only: this writes no bootstrap code and no CHS fields, which is
    enough for partition discovery but is not a bootable MBR.
    """
    if len(entries) > MBR_ENTRY_COUNT:
        raise ValueError("an MBR holds at most 4 primary partitions")
    mbr = bytearray(SECTOR)
    mbr[510:512] = MBR_SIGNATURE
    for i, (ptype, start, count) in enumerate(entries):
        base = MBR_TABLE_OFFSET + i * MBR_ENTRY_SIZE
        mbr[base + 4] = ptype
        struct.pack_into("<I", mbr, base + 8, start)
        struct.pack_into("<I", mbr, base + 12, count)
    return bytes(mbr)
