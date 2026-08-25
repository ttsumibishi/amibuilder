"""Structural characterisation of the Amiga FFS on-disk format.

These assert against *raw bytes in the image*, using checksum and offset maths
implemented independently in helpers/blocks.py. That matters: a test that compared
amitools to its own constants would still pass if amitools changed its layout.

Every offset here was verified against the amitools 0.8.1 implementation and is
documented in docs/KIP-FFS-NOTES.md section 3. Several correct a widely-circulated
but wrong offset table, so these tests are the guard against that error creeping back.
"""

from __future__ import annotations

import os

import pytest
from helpers import blocks as B
from helpers import images

# ---------------------------------------------------------------------------
# Checksums: four different conventions
# ---------------------------------------------------------------------------


def test_header_checksum_lives_at_byte_20_not_byte_4(populated_hdf):
    """Root/dir/file-header blocks put the checksum at longword 5 = byte offset 20.

    Byte 4 holds header_key. Placing a checksum there would corrupt the block.
    """
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))

    assert B.CHK_LONGWORD_HEADER == 5
    assert B.checksum_ok(root, B.CHK_LONGWORD_HEADER), "root block checksum invalid at slot 5"
    assert B.get_long(root, 5) != 0, "slot 5 holds a real checksum value"


def test_sum_to_zero_checksum_validates_at_every_slot(populated_hdf):
    """A sum-to-zero checksum cannot reveal *which* longword holds it.

    Because the stored checksum forces the total of all longwords to 0, the value in
    any slot k equals the negated sum of the others. So "validate the checksum" passes
    for every k, and a parser cannot locate the checksum slot empirically -- it has to
    know the block type first.

    This matters for block scanners: identifying a block by "the checksum validates"
    is meaningless on its own. Only the type fields at byte 0 and 508 carry that
    information. Pinned here because it is a natural thing to get wrong.
    """
    blk, hdr = _find_named_file_header(populated_hdf, "TESTFILE")
    assert B.get_long(hdr, 1) == blk != 0, "own_key is non-zero, unlike root header_key"

    for slot in (0, 1, 2, 5, 20, 127):
        assert B.checksum_ok(hdr, slot), f"slot {slot} unexpectedly failed"

    # The total really is zero, which is the property that causes the above.
    assert sum(B.longs(hdr)) % (1 << 32) == 0

    # Corrupt one byte and every slot should now fail -- so the check does detect damage.
    bad = bytearray(hdr)
    bad[300] ^= 0xFF
    assert not B.checksum_ok(bytes(bad), B.CHK_LONGWORD_HEADER)


def test_root_block_header_key_is_zero_at_byte_4(populated_hdf):
    """Byte 4 of the root block is header_key, which is 0 -- not a checksum."""
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))
    assert B.get_long(root, 1) == 0


def test_format_alone_leaves_boot_checksum_zero(populated_hdf):
    """Formatting writes the DosType and root pointer but no boot checksum.

    Worth pinning down because it looks like corruption if you expect one. On a hard
    disk partition the boot block is not what boots the machine -- the RDB is -- so
    Format has no reason to checksum it. Installing boot code is a separate step.
    """
    boot = B.read_block(populated_hdf, 0)
    assert B.get_long(boot, B.CHK_LONGWORD_BOOT) == 0


def test_boot_checksum_matches_amitools_after_install(workdir):
    """Differential check: our one's-complement algorithm vs amitools' own output.

    `boot install` writes real boot code and a genuine checksum, so this compares two
    independent implementations rather than amitools against itself.
    """
    path = images.make_plain_hdf(str(workdir / "boot.hdf"), size="10Mi", volume="Boot")
    images.xdftool(path, "open", "+", "boot", "install")

    boot = B.read_block(path, 0) + B.read_block(path, 1)
    stored = B.get_long(boot, B.CHK_LONGWORD_BOOT)

    assert stored != 0, "boot install should write a real checksum"
    assert stored == B.boot_checksum(boot), "independent boot checksum disagrees"

    # And the header-style algorithm must give a different answer, or the two
    # conventions would be indistinguishable and this test would prove nothing.
    assert B.header_checksum(boot[: B.BLOCK_SIZE], B.CHK_LONGWORD_BOOT) != stored


def test_boot_checksum_invariant_is_ones_complement_sum(workdir):
    """With the checksum in place, the one's-complement sum of all longwords is ~0."""
    path = images.make_plain_hdf(str(workdir / "inv.hdf"), size="10Mi", volume="Inv")
    images.xdftool(path, "open", "+", "boot", "install")
    boot = B.read_block(path, 0) + B.read_block(path, 1)

    total = 0
    for v in B.longs(boot):
        total += v
        if total > 0xFFFFFFFF:
            total = (total + 1) & 0xFFFFFFFF
    assert total == 0xFFFFFFFF


def test_boot_block_carries_dos_type(populated_hdf):
    """Offset 0 of the boot block is the DosType tag: 'DOS' plus a mode byte."""
    boot = B.read_block(populated_hdf, 0)
    assert boot[0:3] == b"DOS"
    assert boot[3] == 3, "fixture is formatted ffs+intl, so DOS\\3"


def test_bitmap_block_checksum_is_at_byte_0(populated_hdf):
    """Bitmap blocks put their checksum at longword 0, unlike header blocks."""
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))

    first_bm = B.get_long(root, -49)  # bm_pages[0], byte offset 316
    assert first_bm != 0, "formatted volume must have at least one bitmap block"

    bm = B.read_block(populated_hdf, first_bm)
    assert B.CHK_LONGWORD_BITMAP == 0
    assert B.checksum_ok(bm, B.CHK_LONGWORD_BITMAP)


# ---------------------------------------------------------------------------
# Root block layout and position
# ---------------------------------------------------------------------------


def test_root_block_is_at_num_blocks_over_two(populated_hdf):
    """Root block index is num_blocks // 2, not (num_blocks - 1) // 2."""
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    expected = num_blocks // 2

    root = B.read_block(populated_hdf, expected)
    assert B.get_long(root, 0) == B.T_SHORT
    assert B.get_long(root, -1) == B.ST_ROOT

    # The off-by-one variant must not also be a root block.
    other = B.read_block(populated_hdf, expected - 1)
    assert not (
        B.get_long(other, 0) == B.T_SHORT and B.get_long(other, -1) == B.ST_ROOT
    ), "both candidate offsets look like a root block; test cannot discriminate"


def test_dd_floppy_root_block_is_880(adf):
    """The canonical check: a DD ADF has 1760 blocks and its root block is 880."""
    num_blocks = os.path.getsize(adf) // B.BLOCK_SIZE
    assert num_blocks == 1760
    assert B.expected_root_block(num_blocks) == 880

    root = B.read_block(adf, 880)
    assert B.get_long(root, 0) == B.T_SHORT
    assert B.get_long(root, -1) == B.ST_ROOT
    assert B.checksum_ok(root)


def test_root_block_field_offsets(populated_hdf):
    """ht_size at 12, hash table at 24, bitmap flag at 312, bm_pages at 316."""
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))

    assert B.get_long(root, 3) == B.expected_hash_size(B.BLOCK_SIZE) == 72
    assert B.get_long(root, -50) == 0xFFFFFFFF, "bitmap valid flag at byte 312"
    assert B.get_long(root, -49) != 0, "bm_pages[0] at byte 316"
    assert B.get_long(root, -2) == 0, "extension at byte 504 is 0 on a plain volume"


def test_hash_table_occupies_bytes_24_to_311(populated_hdf):
    """72 longwords starting at byte 24, so the table ends at byte 311.

    The bitmap valid flag immediately follows at 312, which is what fixes the size.
    """
    assert B.expected_hash_size() * 4 == 288
    assert 24 + 288 == 312

    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))
    populated = [i for i in range(72) if B.get_long(root, 6 + i) != 0]
    assert populated, "root hash table should reference the fixture's entries"


def test_volume_name_bstr_at_offset_432(populated_hdf):
    """Volume name is a BSTR: length byte at 432, characters from 433."""
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))
    assert B.read_bstr(root, 432, 30) == "Pop"


# ---------------------------------------------------------------------------
# File header layout
# ---------------------------------------------------------------------------


def _find_named_file_header(image: str, name: str) -> tuple[int, bytes]:
    for blk in B.find_blocks_by_type(image, B.T_SHORT, B.ST_FILE):
        data = B.read_block(image, blk)
        if B.read_bstr(data, 432, 30) == name:
            return blk, data
    raise AssertionError(f"no file header block named {name!r} found in {image}")


def test_file_header_field_offsets(populated_hdf):
    """protect at 320, byte size at 324, name at 432/433, links at 496/500/504."""
    blk, hdr = _find_named_file_header(populated_hdf, "TESTFILE")

    assert B.get_long(hdr, 0) == B.T_SHORT
    assert B.get_long(hdr, -1) == B.ST_FILE
    assert B.get_long(hdr, 1) == blk, "own_key at byte 4 must equal the block number"
    assert B.checksum_ok(hdr)

    assert B.get_long(hdr, -47) == 100, "byte size at offset 324"
    assert B.read_bstr(hdr, 432, 30) == "TESTFILE"
    assert B.get_long(hdr, -3) != 0, "parent pointer at offset 500"
    assert B.get_long(hdr, -2) == 0, "extension at 504 is 0 for a small file"

    # Offsets the wrong table claimed. 316 is a reserved longword and must be 0;
    # if size were there, this would be 100.
    assert B.get_long(hdr, 79) == 0, "offset 316 is reserved, not the file size"


def test_data_block_table_runs_downward_from_byte_308(populated_hdf):
    """Entry i sits at longword (block_longs - 51 - i): first at 308, table ends at 24."""
    assert B.data_block_longword(0) * 4 == 308
    assert B.data_block_longword(71) * 4 == 24

    _, hdr = _find_named_file_header(populated_hdf, "TESTFILE")
    block_count = B.get_long(hdr, 2)
    assert block_count == 1, "100 bytes fits in one FFS data block"

    first_ptr = B.get_long(hdr, B.data_block_longword(0))
    assert first_ptr != 0
    assert first_ptr == B.get_long(hdr, 4), "first_data at byte 16 matches table entry 0"


def test_data_block_pointers_are_not_at_byte_500(populated_hdf):
    """Byte 500 is the parent pointer. Writing data pointers there destroys metadata."""
    _, hdr = _find_named_file_header(populated_hdf, "TESTFILE")
    parent = B.get_long(hdr, -3)

    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    assert parent == B.expected_root_block(num_blocks), (
        "byte 500 holds the parent directory, which for a root-level file is the root block"
    )


def test_multi_block_file_fills_table_in_reverse(populated_hdf):
    """A 2048-byte file needs 4 data blocks, recorded from byte 308 downward."""
    _, hdr = _find_named_file_header(populated_hdf, "thing.library")
    assert B.get_long(hdr, -47) == 2048
    assert B.get_long(hdr, 2) == 4

    ptrs = [B.get_long(hdr, B.data_block_longword(i)) for i in range(4)]
    assert all(p != 0 for p in ptrs), f"expected 4 data pointers, got {ptrs}"
    # Entry 4 is beyond block_count and must be empty.
    assert B.get_long(hdr, B.data_block_longword(4)) == 0


def test_ffs_data_blocks_have_no_header(populated_hdf):
    """In FFS all 512 bytes of a data block are payload, unlike OFS."""
    _, hdr = _find_named_file_header(populated_hdf, "TESTFILE")
    data = B.read_block(populated_hdf, B.get_long(hdr, 4))
    assert data[:100] == b"A" * 100, "payload starts at byte 0, no 24-byte OFS header"


# ---------------------------------------------------------------------------
# Hash function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["TESTFILE", "S", "Libs"])
def test_file_is_reachable_via_computed_hash_slot(populated_hdf, name):
    """The independently implemented hash must land on the slot AmigaDOS used.

    This validates h = len(name); h = (h*13 + c) & 0x7FF; h %= hash_size against real
    on-disk placement rather than against amitools' own implementation.
    """
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))

    slot = B.ffs_hash(name, hash_size=72, intl=True)
    blk = B.get_long(root, 6 + slot)
    assert blk != 0, f"hash slot {slot} for {name!r} is empty"

    # Walk the collision chain at byte 496 until the name matches.
    seen = 0
    while blk != 0 and seen < 100:
        data = B.read_block(populated_hdf, blk)
        if B.read_bstr(data, 432, 30) == name:
            return
        blk = B.get_long(data, -4)
        seen += 1
    raise AssertionError(f"{name!r} not found in the chain from hash slot {slot}")


def test_hash_size_scales_with_block_size():
    """hash_size is block_longs - 56, so it is not the constant 72."""
    assert B.expected_hash_size(512) == 72
    assert B.expected_hash_size(1024) == 200
    assert B.expected_hash_size(2048) == 456


def test_international_hashing_differs_for_high_bytes():
    """DOS3 folds bytes 224-254 to uppercase; DOS1 does not. 247 is excluded."""
    name = "caf\xe9"  # 0xe9 = e-acute
    assert B.ffs_hash(name, intl=False) != B.ffs_hash(name, intl=True)

    # Pure ASCII names must hash identically under both modes.
    assert B.ffs_hash("Startup-Sequence", intl=False) == B.ffs_hash(
        "Startup-Sequence", intl=True
    )

    # 247 (division sign) is deliberately not folded.
    div = "\xf7"
    assert B.ffs_hash(div, intl=False) == B.ffs_hash(div, intl=True)


# ---------------------------------------------------------------------------
# Allocation bitmap
# ---------------------------------------------------------------------------


def test_bitmap_set_bit_means_free(populated_hdf):
    """Amiga inverts the usual convention: a set bit is a FREE block."""
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root_blk = B.expected_root_block(num_blocks)
    assert not B.is_block_free(populated_hdf, root_blk, root_blk), (
        "root block must be marked allocated (bit clear)"
    )
    assert B.is_block_free(populated_hdf, num_blocks - 2, root_blk), (
        "a block near the end of a nearly-empty volume must be free (bit set)"
    )


def test_bitmap_spans_multiple_pages_on_a_modest_volume(populated_hdf):
    """One bitmap block covers only 4064 blocks, so a 20 MiB volume needs several.

    Pinned because a helper that consults only bm_pages[0] appears to work while
    silently checking nothing for most of the volume -- which this suite did at first.
    """
    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root_blk = B.expected_root_block(num_blocks)

    pages = B.read_bitmap_pages(populated_hdf, root_blk)
    assert len(pages) > 1, f"expected multiple bitmap pages for {num_blocks} blocks"

    page_idx, _, _ = B.bitmap_entry(root_blk)
    assert page_idx > 0, "the root block itself lies beyond the first bitmap page"


def test_bitmap_bit_location_is_lsb_first_from_reserved():
    """Bit 0 of longword 0 maps to block `reserved`, and bits run LSB-first."""
    assert B.bitmap_bit_location(2) == (0, 0)
    assert B.bitmap_bit_location(3) == (0, 1)
    assert B.bitmap_bit_location(33) == (0, 31)
    assert B.bitmap_bit_location(34) == (1, 0)

    with pytest.raises(ValueError):
        B.bitmap_bit_location(1)


def test_one_bitmap_block_maps_4064_blocks():
    """(block_longs - 1) * 32 = 4064 blocks per bitmap block at 512 bytes."""
    assert ((B.BLOCK_SIZE // 4) - 1) * 32 == 4064


def test_root_holds_25_bitmap_pointers_then_extension_needed(populated_hdf):
    """25 pointers cover ~49.6 MiB; beyond that extension blocks are mandatory."""
    assert 25 * 4064 * B.BLOCK_SIZE == 52_019_200  # ~49.6 MiB

    num_blocks = os.path.getsize(populated_hdf) // B.BLOCK_SIZE
    root = B.read_block(populated_hdf, B.expected_root_block(num_blocks))
    # A 20 MiB volume needs fewer than 25 bitmap blocks, so no extension.
    assert B.get_long(root, -24) == 0, "bitmap ext pointer at byte 416 should be unused"


@pytest.mark.slow
def test_large_partition_requires_bitmap_extension_blocks(workdir):
    """A volume over ~49.6 MiB must chain bitmap extension blocks from byte 416."""
    path = images.make_plain_hdf(str(workdir / "big.hdf"), size="200Mi", volume="Big")
    num_blocks = os.path.getsize(path) // B.BLOCK_SIZE
    root = B.read_block(path, B.expected_root_block(num_blocks))

    ext = B.get_long(root, -24)
    assert ext != 0, "200 MiB needs more than 25 bitmap blocks, so an ext block"

    ext_blk = B.read_block(path, ext)
    assert B.get_long(ext_blk, 0) != 0, "ext block should carry bitmap pointers"
    assert images.scan_is_ok(path)
