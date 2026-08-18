"""RDB (Rigid Disk Block) creation, geometry and partition isolation.

An RDB whole-disk image is the universal interchange format for this project's targets:
ZuluSCSI reads it directly, both emulators accept it, and it is byte-compatible with the
contents of a PiStorm/Emu68 MBR 0x76 partition. See docs/KIP-FFS-NOTES.md section 9.
"""

from __future__ import annotations

import hashlib
import os
import struct

import pytest
from helpers import blocks as B
from helpers import images

RDSK = 0x5244534B  # 'RDSK'
PART = 0x50415254  # 'PART'


def _find_rdb_block(path: str, magic: int, limit: int = 16) -> int:
    """The RDB must appear within the first 16 blocks; convention puts it at 0."""
    for n in range(limit):
        if B.get_long(B.read_block(path, n), 0) == magic:
            return n
    raise AssertionError(f"no block with magic {magic:#x} in the first {limit} blocks")


def test_rdb_magic_is_at_block_zero(rdb_hdf):
    assert _find_rdb_block(rdb_hdf, RDSK) == 0


def test_rdb_checksum_is_at_longword_2(rdb_hdf):
    """The RDB family (RDSK/PART/FSHD/LSEG/BADB) checksums at byte 8, not 20."""
    rdb = B.read_block(rdb_hdf, 0)
    assert B.CHK_LONGWORD_RDB == 2

    summed_longs = B.get_long(rdb, 1)
    assert 0 < summed_longs <= 128, f"implausible rdb_SummedLongs {summed_longs}"

    # Only the first rdb_SummedLongs longwords participate in the sum.
    total = sum(B.longs(rdb)[:summed_longs]) & 0xFFFFFFFF
    assert total == 0, "RDB checksum property is sum-to-zero over rdb_SummedLongs"


def test_partition_block_is_reachable_and_checksums(rdb_hdf):
    """rdb_PartitionList at byte 28 heads a linked list of PART blocks."""
    rdb = B.read_block(rdb_hdf, 0)
    part_blk = B.get_long(rdb, 7)  # byte 28
    assert part_blk not in (0, 0xFFFFFFFF), "expected at least one partition"

    part = B.read_block(rdb_hdf, part_blk)
    assert B.get_long(part, 0) == PART
    summed = B.get_long(part, 1)
    assert sum(B.longs(part)[:summed]) & 0xFFFFFFFF == 0


def test_partition_drive_name_is_a_bstr_at_byte_36(rdb_hdf):
    """pb_DriveName is a BSTR (length byte + chars) at offset 36."""
    rdb = B.read_block(rdb_hdf, 0)
    part = B.read_block(rdb_hdf, B.get_long(rdb, 7))
    assert B.read_bstr(part, 36, 31) == "DH0"


def test_dos_env_block_size_is_in_longwords(rdb_hdf):
    """de_SizeBlock is expressed in longwords: 128 means 512 bytes, not 512."""
    rdb = B.read_block(rdb_hdf, 0)
    part = B.read_block(rdb_hdf, B.get_long(rdb, 7))

    # DosEnvec starts at longword 32 (byte 128). de_SizeBlock is entry 1.
    size_block = B.get_long(part, 33)
    assert size_block == 128, "128 longwords = 512 bytes"
    assert size_block * 4 == B.BLOCK_SIZE


def test_partition_geometry_and_dostype(rdb_hdf):
    """low/high cylinder and DosType are where the layout capture will read them."""
    rdb = B.read_block(rdb_hdf, 0)
    part = B.read_block(rdb_hdf, B.get_long(rdb, 7))

    low_cyl = B.get_long(part, 32 + 9)
    high_cyl = B.get_long(part, 32 + 10)
    dos_type = B.get_long(part, 32 + 16)

    assert low_cyl >= 1, "cylinder 0 is reserved for the RDB itself"
    assert high_cyl > low_cyl
    assert dos_type == 0x444F5303, "fixture is ffs+intl = DOS\\3"


def test_rdb_reserves_cylinder_zero(rdb_hdf):
    """The RDB lives in cylinder 0, so no partition may start there.

    This is the invariant a partition-bounds guard must enforce before any write.
    """
    rdb = B.read_block(rdb_hdf, 0)
    part = B.read_block(rdb_hdf, B.get_long(rdb, 7))
    assert B.get_long(part, 32 + 9) >= 1


def test_info_reports_expected_partition_count(rdb_two_part):
    out = images.rdbtool(rdb_two_part, "list").output
    assert "DH0" in out and "DH1" in out


def test_partitions_are_independently_formatted(rdb_two_part):
    """Each partition carries its own boot block, root block and bitmap."""
    for part, volume in ((0, "Workbench"), (1, "Extra")):
        out = images.xdftool(rdb_two_part, "open", f"part={part}", "+", "info").output
        assert "total:" in out
        listing = images.xdftool(rdb_two_part, "open", f"part={part}", "+", "list").output
        assert volume in listing


def test_writing_one_partition_leaves_the_other_byte_identical(rdb_two_part):
    """Partition isolation -- the guard for preferring partitions over separate drives.

    A partition-offset bug would show up here as collateral damage to the neighbour.
    """
    rdb = B.read_block(rdb_two_part, 0)
    p0 = B.read_block(rdb_two_part, B.get_long(rdb, 7))
    p0_next = B.get_long(p0, 4)  # pb_Next at byte 16
    p1 = B.read_block(rdb_two_part, p0_next)

    heads = B.get_long(p1, 32 + 3)
    secs = B.get_long(p1, 32 + 5)
    lo = B.get_long(p1, 32 + 9)
    hi = B.get_long(p1, 32 + 10)
    start = heads * secs * lo
    length = heads * secs * (hi - lo + 1)

    def part1_digest() -> str:
        h = hashlib.sha256()
        with open(rdb_two_part, "rb") as f:
            f.seek(start * B.BLOCK_SIZE)
            remaining = length * B.BLOCK_SIZE
            while remaining:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()

    before = part1_digest()
    images.write_files(rdb_two_part, {"S/Startup-Sequence": b"Echo hi\n"}, part=0)
    after = part1_digest()

    assert before == after, "writing partition 0 modified bytes inside partition 1"
    assert images.scan_is_ok(rdb_two_part)


def test_rdbtool_size_suffix_trap():
    """`size=500Mi` means CYLINDERS; only a trailing b/B means bytes.

    Documented as G18. Pinned because it silently produces an absurd request rather
    than an error about units.
    """
    from helpers.images import Partition, make_rdb_hdf

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        good = make_rdb_hdf(
            os.path.join(td, "good.hdf"), size="32Mi",
            partitions=[Partition(size="8MiB", dos_type="ffs+intl", volume="Ok")],
        )
        assert "DH0" in images.rdbtool(good, "list").output

        # The same value without the B suffix is read as cylinders and must fail.
        bad = os.path.join(td, "bad.hdf")
        images.rdbtool(bad, "create", "size=32Mi", "+", "init",
                       "+", "add", "size=8Mi", "dostype=ffs+intl", check=False)
        out = images.rdbtool(bad, "list", check=False).output
        assert "DH0" not in out, "size=8Mi should not have produced a partition"


def test_plain_hdf_has_no_rdb(plain_hdf):
    """A plain HDF starts with a DosType, not RDSK. Only emulators can mount it."""
    first = B.read_block(plain_hdf, 0)
    assert B.get_long(first, 0) != RDSK
    assert first[0:3] == b"DOS"


@pytest.mark.slow
def test_four_gib_rdb_is_created_sparsely(workdir):
    """A 4 GiB image should cost kilobytes on APFS, not gigabytes."""
    path = str(workdir / "4g.hdf")
    images.rdbtool(path, "create", "size=4Gi", "+", "init")

    assert os.path.getsize(path) == 4 * 1024**3
    assert images.du_bytes(path) < 8 * 1024 * 1024, (
        "expected a sparse file; got "
        f"{images.du_bytes(path)} bytes actually allocated"
    )


@pytest.mark.slow
def test_large_partition_uses_bitmap_extension_blocks(workdir):
    """A 3.5 GiB FFS partition needs bitmap extension blocks; verify metadata cost."""
    path = str(workdir / "big.hdf")
    images.rdbtool(path, "create", "size=4Gi", "+", "init",
                   "+", "add", "dostype=ffs+intl")
    images.xdftool(path, "open", "part=0", "+", "format", "Big", "ffs+intl")

    out = images.xdftool(path, "open", "part=0", "+", "info").output
    used = int([ln for ln in out.splitlines() if ln.startswith("used:")][0].split()[1])

    # ~8.3M blocks / 4064 per bitmap block, plus ext blocks, root and boot blocks.
    assert 1500 < used < 2500, f"unexpected metadata block count {used}"
    assert images.scan_is_ok(path)
