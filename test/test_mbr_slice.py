"""The PiStorm / Emu68 path: an RDB image inside an MBR partition of type 0x76.

Emu68 cannot mount an HDF at all. It exposes the microSD card as a block device and
presents MBR primary partitions of type 0x76 as separate Amiga drive units, each holding
its own RDB. Reaching them means addressing a byte range rather than a whole file.

amitools' BlkDevFactory.open() accepts an `fobj`, so a slice view is sufficient with no
amitools changes. See docs/KIP-FFS-NOTES.md section 9.3.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from amibuilder.errors import ImageError
from helpers import images
from helpers.mbr import (
    PTYPE_AMIGA_VIRTUAL,
    SECTOR,
    SliceFile,
    amiga_partitions,
    build_mbr,
    parse_mbr,
)

sys.path.insert(0, str(Path(images.XDFTOOL).parent.parent))

FAT_SECTORS = 2048 * 8  # 8 MiB of "boot partition" ahead of the Amiga slice


@pytest.fixture
def card_image(workdir):
    """A container shaped like a PiStorm microSD card.

    Entry 0 is a FAT32 boot partition (Emu68 lives there); entry 1 is the type 0x76
    Amiga virtual drive holding a real RDB image.
    """
    inner = images.make_rdb_hdf(
        str(workdir / "inner.hdf"),
        size="32Mi",
        partitions=[images.Partition(dos_type="ffs+intl", bootable=True, volume="Inner")],
    )
    images.write_files(inner, {"S/Startup-Sequence": b"Echo booting\n"}, part=0)
    inner_size = os.path.getsize(inner)

    card = str(workdir / "card.img")
    part_off = FAT_SECTORS * SECTOR
    with open(card, "wb") as f:
        f.truncate(part_off + inner_size + (1 << 20))
    with open(card, "r+b") as f:
        f.seek(part_off)
        with open(inner, "rb") as g:
            while chunk := g.read(1 << 20):
                f.write(chunk)
        f.seek(0)
        f.write(
            build_mbr(
                [
                    (0x0C, 2048, FAT_SECTORS - 2048),
                    (PTYPE_AMIGA_VIRTUAL, FAT_SECTORS, inner_size // SECTOR),
                ]
            )
        )
    return card, inner_size


# ---------------------------------------------------------------------------
# MBR parsing
# ---------------------------------------------------------------------------


def test_mbr_signature_is_required(workdir):
    bad = str(workdir / "nosig.img")
    with open(bad, "wb") as f:
        f.write(bytes(SECTOR))
    with pytest.raises(ImageError, match="signature"):
        parse_mbr(bad)


def test_mbr_entries_are_parsed_with_stable_indexes(card_image):
    card, inner_size = card_image
    parts = parse_mbr(card)

    assert len(parts) == 2
    assert parts[0].index == 0 and parts[0].ptype == 0x0C
    assert parts[1].index == 1 and parts[1].ptype == PTYPE_AMIGA_VIRTUAL
    assert parts[1].byte_offset == FAT_SECTORS * SECTOR
    assert parts[1].byte_length == inner_size


def test_only_type_0x76_counts_as_an_amiga_drive(card_image):
    card, _ = card_image
    amiga = amiga_partitions(card)
    assert len(amiga) == 1
    assert amiga[0].index == 1
    assert amiga[0].is_amiga


def test_fat_boot_partition_is_not_treated_as_amiga(card_image):
    """Emu68's boot partition holds the emulator itself. Writing to it is destructive."""
    card, _ = card_image
    fat = [p for p in parse_mbr(card) if p.ptype == 0x0C]
    assert fat and not fat[0].is_amiga


# ---------------------------------------------------------------------------
# Slice view
# ---------------------------------------------------------------------------


def test_slice_reads_only_within_its_range(card_image):
    card, inner_size = card_image
    part = amiga_partitions(card)[0]

    with SliceFile(card, part.byte_offset, part.byte_length) as sl:
        assert sl.seek(0, io.SEEK_END) == inner_size
        sl.seek(0)
        head = sl.read(4)
        assert head == b"RDSK", "the slice should start at the embedded RDB"

        sl.seek(inner_size - 16)
        assert len(sl.read(999)) == 16, "reads must clamp at the slice end"


def test_slice_refuses_writes_past_its_end(card_image):
    card, inner_size = card_image
    part = amiga_partitions(card)[0]

    with SliceFile(card, part.byte_offset, part.byte_length, writable=True) as sl:
        sl.seek(inner_size - 4)
        with pytest.raises(ValueError, match="overrun"):
            sl.write(b"12345678")


def test_slice_is_read_only_by_default(card_image):
    card, _ = card_image
    part = amiga_partitions(card)[0]
    with SliceFile(card, part.byte_offset, part.byte_length) as sl:
        with pytest.raises(io.UnsupportedOperation):
            sl.write(b"x")


# ---------------------------------------------------------------------------
# amitools through the slice
# ---------------------------------------------------------------------------


def test_amitools_opens_the_embedded_rdb_through_a_slice(card_image):
    """The load-bearing verification for the whole PiStorm workflow."""
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.FSString import FSString

    card, _ = card_image
    part = amiga_partitions(card)[0]

    sl = SliceFile(card, part.byte_offset, part.byte_length)
    blkdev = BlkDevFactory().open("card.hdf", fobj=sl, read_only=True, options={"part": 0})
    vol = ADFSVolume(blkdev)
    vol.open()
    try:
        assert vol.name.get_unicode() == "Inner"
        assert vol.boot.dos_type == 0x444F5303
        data = bytes(vol.read_file(FSString("S/Startup-Sequence")))
        assert data == b"Echo booting\n"
    finally:
        vol.close()


def test_writes_through_the_slice_stay_inside_the_partition(card_image):
    """A partition-offset bug here would destroy the Emu68 boot partition."""
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.FSString import FSString

    card, _ = card_image
    part = amiga_partitions(card)[0]

    with open(card, "rb") as f:
        fat_before = f.read(part.byte_offset)

    sl = SliceFile(card, part.byte_offset, part.byte_length, writable=True)
    blkdev = BlkDevFactory().open("card.hdf", fobj=sl, read_only=False,
                                  options={"part": 0})
    vol = ADFSVolume(blkdev)
    vol.open()
    try:
        vol.write_file(b"new content", FSString("added.bin"))
    finally:
        vol.close()

    with open(card, "rb") as f:
        fat_after = f.read(part.byte_offset)

    assert fat_before == fat_after, (
        "writing inside the 0x76 partition modified bytes in the FAT boot partition"
    )

    # And the write really landed.
    sl = SliceFile(card, part.byte_offset, part.byte_length)
    blkdev = BlkDevFactory().open("card.hdf", fobj=sl, read_only=True,
                                  options={"part": 0})
    vol = ADFSVolume(blkdev)
    vol.open()
    try:
        assert bytes(vol.read_file(FSString("added.bin"))) == b"new content"
    finally:
        vol.close()


def test_extracted_slice_is_a_standalone_rdb_image(card_image, workdir):
    """A 0x76 partition's contents are dd-equivalent to an RDB HDF.

    That equivalence is what lets one layer stack target ZuluSCSI, an emulator, or a
    PiStorm card interchangeably.
    """
    card, inner_size = card_image
    part = amiga_partitions(card)[0]

    extracted = str(workdir / "extracted.hdf")
    with SliceFile(card, part.byte_offset, part.byte_length) as sl, \
            open(extracted, "wb") as out:
        while chunk := sl.read(1 << 20):
            out.write(chunk)

    assert os.path.getsize(extracted) == inner_size
    listing = images.xdftool(extracted, "open", "part=0", "+", "list").output
    assert "Inner" in listing and "Startup-Sequence" in listing
    assert images.scan_is_ok(extracted)
