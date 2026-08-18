"""Raw block reading and Amiga checksum maths, implemented independently of amitools.

These helpers exist so the structural tests assert against *bytes on disk* rather than
against amitools' own constants. If amitools ever changed its layout, a test that only
compared amitools to itself would still pass; these would not.

Reference: docs/KIP-FFS-NOTES.md section 3.
"""

from __future__ import annotations

import struct

BLOCK_SIZE = 512

# Primary block types (longword 0)
T_SHORT = 2
T_DATA = 8
T_LIST = 16
T_DIR_CACHE = 33
T_COMMENT = 64

# Secondary block types (longword -1, i.e. byte offset 508 for a 512-byte block)
ST_ROOT = 1
ST_USERDIR = 2
ST_FILE = 0xFFFFFFFD  # -3 as unsigned

# Checksum slot, expressed as a longword index. Four different conventions exist.
CHK_LONGWORD_HEADER = 5  # root / dir / file header / file list / comment / dircache
CHK_LONGWORD_BITMAP = 0
CHK_LONGWORD_BOOT = 1  # and a different algorithm, see boot_checksum()
CHK_LONGWORD_RDB = 2  # RDSK / PART / FSHD / LSEG / BADB


def read_block(path: str, block_num: int, block_size: int = BLOCK_SIZE) -> bytes:
    """Read one raw block from an image file."""
    with open(path, "rb") as f:
        f.seek(block_num * block_size)
        data = f.read(block_size)
    if len(data) != block_size:
        raise EOFError(f"short read at block {block_num} of {path}")
    return data


def longs(block: bytes) -> list[int]:
    """Unpack a block into big-endian unsigned longwords."""
    n = len(block) // 4
    return list(struct.unpack(f">{n}I", block))


def get_long(block: bytes, index: int) -> int:
    """Read longword by index; negative indexes count from the end, as amitools does."""
    n = len(block) // 4
    if index < 0:
        index = n + index
    return struct.unpack_from(">I", block, index * 4)[0]


def header_checksum(block: bytes, chk_longword: int = CHK_LONGWORD_HEADER) -> int:
    """Sum every longword except the checksum slot, then negate.

    Used by root, user dir, file header, file list, comment and dircache blocks
    (slot 5), by bitmap blocks (slot 0) and by the RDB family (slot 2).
    """
    total = 0
    for i, v in enumerate(longs(block)):
        if i != chk_longword:
            total += v
    return (-total) & 0xFFFFFFFF


def boot_checksum(blocks: bytes) -> int:
    """Boot block checksum: one's-complement addition, then bitwise NOT.

    Deliberately different from header_checksum. Spans *all* boot blocks
    (normally two), and skips longword 1 which holds the checksum itself.
    """
    total = 0
    for i, v in enumerate(longs(blocks)):
        if i == 1:
            continue
        total += v
        if total > 0xFFFFFFFF:  # carry wraps back in
            total = (total + 1) & 0xFFFFFFFF
    return (~total) & 0xFFFFFFFF


def checksum_ok(block: bytes, chk_longword: int = CHK_LONGWORD_HEADER) -> bool:
    """True if the block's stored checksum matches a freshly computed one."""
    stored = get_long(block, chk_longword)
    return stored == header_checksum(block, chk_longword)


def ffs_hash(name: str, hash_size: int = 72, intl: bool = False) -> int:
    """The AmigaDOS FFS/OFS filename hash.

    In international mode the uppercase step also maps bytes 224-254 down by 32,
    excluding 247 (division sign). Using the wrong variant makes files invisible
    to AmigaDOS while still occupying space.
    """
    raw = name.encode("latin-1").upper()
    if intl:
        raw = bytes((c - 32) if (224 <= c <= 254 and c != 247) else c for c in raw)
    h = len(raw)
    for c in raw:
        h = (h * 13 + c) & 0x7FF
    return h % hash_size


def expected_hash_size(block_size: int = BLOCK_SIZE) -> int:
    """Hash table entry count: block_longs - 56. 72 at 512 bytes, 200 at 1024."""
    return (block_size // 4) - 56


def expected_root_block(num_blocks: int) -> int:
    """Root block index within a volume: num_blocks // 2.

    Gives 880 for a DD floppy (1760 blocks), which is the known-correct value.
    """
    return num_blocks // 2


def data_block_longword(index: int, block_size: int = BLOCK_SIZE) -> int:
    """Longword index of data block pointer `index` in a file header.

    The table is stored in reverse: entry 0 sits at the high end (byte 308 for a
    512-byte block) and the table grows downward to byte 24.
    """
    return (block_size // 4) - 51 - index


def find_blocks_by_type(
    path: str,
    primary: int,
    secondary: int | None = None,
    block_size: int = BLOCK_SIZE,
    verify_checksum: bool = True,
    limit: int | None = None,
) -> list[int]:
    """Scan an image for blocks of a given type, optionally verifying the checksum.

    Checksum verification matters: file data in FFS is raw bytes, so a data block can
    easily begin with the value 2 and masquerade as a header block.
    """
    found: list[int] = []
    with open(path, "rb") as f:
        n = 0
        while True:
            block = f.read(block_size)
            if len(block) < block_size:
                break
            if get_long(block, 0) == primary:
                if secondary is None or get_long(block, -1) == secondary:
                    if not verify_checksum or checksum_ok(block):
                        found.append(n)
                        if limit is not None and len(found) >= limit:
                            break
            n += 1
    return found


def read_bstr(block: bytes, len_offset: int, max_len: int) -> str:
    """Read an Amiga BSTR: one length byte followed by characters."""
    n = block[len_offset]
    if n > max_len:
        raise ValueError(f"BSTR length {n} exceeds max {max_len}")
    return block[len_offset + 1 : len_offset + 1 + n].decode("latin-1")


def bitmap_bit_location(block_num: int, reserved: int = 2) -> tuple[int, int]:
    """Where block `block_num` lives in the allocation bitmap, globally.

    Returns (longword_index, bit_index) counting across the whole bitmap. Bit 0 (LSB)
    of longword 0 maps to block `reserved` -- LSB-first bit order inside a big-endian
    longword. A set bit means FREE, which is inverted relative to most filesystems.
    """
    off = block_num - reserved
    if off < 0:
        raise ValueError(f"block {block_num} is below reserved={reserved}")
    return off // 32, off % 32


def bitmap_entry(
    block_num: int, reserved: int = 2, block_size: int = BLOCK_SIZE
) -> tuple[int, int, int]:
    """Locate a block's bit within a specific bitmap block.

    Returns (bitmap_page_index, longword_index_within_that_block, bit_index).

    `bitmap_page_index` indexes the concatenated pointer list: the root block's 25
    `bm_pages` entries first, then any entries held in bitmap extension blocks. The
    returned longword index already skips longword 0, which holds the bitmap block's
    own checksum.

    A single bitmap block maps (block_longs - 1) * 32 = 4064 blocks at 512 bytes, so
    anything past ~2 MiB into a volume lands beyond the first page. Forgetting that is
    an easy way to write a test that silently checks nothing.
    """
    lw, bit = bitmap_bit_location(block_num, reserved)
    longs_per_block = (block_size // 4) - 1
    return lw // longs_per_block, (lw % longs_per_block) + 1, bit


def read_bitmap_pages(path: str, root_block: int, block_size: int = BLOCK_SIZE) -> list[int]:
    """Collect every bitmap block pointer, following the extension chain.

    Root holds 25 pointers at byte 316; the extension pointer at byte 416 heads a
    chain of blocks each holding (block_longs - 1) further pointers.
    """
    root = read_block(path, root_block, block_size)
    n_longs = block_size // 4
    pages = [get_long(root, n_longs - 49 + i) for i in range(25)]

    ext = get_long(root, n_longs - 24)
    while ext not in (0, 0xFFFFFFFF):
        blk = read_block(path, ext, block_size)
        pages += [get_long(blk, i) for i in range(n_longs - 1)]
        ext = get_long(blk, n_longs - 1)

    return [p for p in pages if p not in (0, 0xFFFFFFFF)]


def is_block_free(
    path: str, block_num: int, root_block: int, reserved: int = 2,
    block_size: int = BLOCK_SIZE,
) -> bool:
    """True if the allocation bitmap marks `block_num` as free (bit set)."""
    page_idx, lw, bit = bitmap_entry(block_num, reserved, block_size)
    pages = read_bitmap_pages(path, root_block, block_size)
    if page_idx >= len(pages):
        raise AssertionError(
            f"block {block_num} maps to bitmap page {page_idx} but only "
            f"{len(pages)} pages exist"
        )
    bm = read_block(path, pages[page_idx], block_size)
    return bool(get_long(bm, lw) & (1 << bit))
