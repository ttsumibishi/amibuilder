"""Block-level primitives: raw reads, checksums, block identification, DosType decoding.

This exists alongside amitools rather than on top of it, for three reasons:

* `hexdump` and `check` need to look at bytes that amitools has already interpreted, or
  has refused to interpret.
* amitools' validator has no dircache support, so a DOS4/DOS5 volume produces one false
  error per dircache block (notes section 5.5). Filtering that needs independent
  knowledge of what a dircache block looks like.
* DosType has to be decoded even when the filesystem is one amibuilder will not touch,
  so that PFS3/SFS can be *named* in a refusal instead of reported as garbage.

`test/helpers/blocks.py` holds a separate implementation of the same checksum and offset
maths, used as the reference oracle when testing amitools itself. The two agree by
construction today -- there is only one correct answer -- but `test_blocks.py` pins them
together so a later refactor here cannot silently drift.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

BLOCK_SIZE = 512

# -- Primary block types (longword 0) ---------------------------------------
T_SHORT = 2
T_DATA = 8
T_LIST = 16
T_DIR_CACHE = 33
T_COMMENT = 64

# -- Secondary block types (longword -1) ------------------------------------
ST_ROOT = 1
ST_USERDIR = 2
ST_SOFTLINK = 3
ST_LINKDIR = 4
ST_FILE = 0xFFFFFFFD  # -3
ST_LINKFILE = 0xFFFFFFFC  # -4

# -- Checksum slots, as longword indexes ------------------------------------
# Four different conventions exist; using the wrong one silently corrupts a volume.
CHK_HEADER = 5  # root / dir / file header / file list / comment / dircache
CHK_BITMAP = 0
CHK_BOOT = 1  # and a different algorithm entirely, see boot_checksum()
CHK_RDB = 2  # RDSK / PART / FSHD / LSEG / BADB

PRIMARY_NAMES = {
    T_SHORT: "SHORT",
    T_DATA: "DATA",
    T_LIST: "LIST",
    T_DIR_CACHE: "DIRCACHE",
    T_COMMENT: "COMMENT",
}

SECONDARY_NAMES = {
    ST_ROOT: "ROOT",
    ST_USERDIR: "USERDIR",
    ST_SOFTLINK: "SOFTLINK",
    ST_LINKDIR: "LINKDIR",
    ST_FILE: "FILE",
    ST_LINKFILE: "LINKFILE",
}

#: RDB-family block identifiers, as they appear at longword 0.
RDB_MAGICS = {
    b"RDSK": "RigidDiskBlock",
    b"PART": "PartitionBlock",
    b"FSHD": "FileSystemHeaderBlock",
    b"LSEG": "LoadSegBlock",
    b"BADB": "BadBlockBlock",
}


# ---------------------------------------------------------------------------
# Raw access
# ---------------------------------------------------------------------------


def read_block(f: BinaryIO, block_num: int, block_size: int = BLOCK_SIZE) -> bytes:
    """Read one block from an open, seekable binary stream.

    Takes a stream rather than a path so this works unchanged against a plain file, a
    raw device, or a SliceFile view of an MBR partition.
    """
    f.seek(block_num * block_size)
    data = f.read(block_size)
    if len(data) != block_size:
        raise EOFError(
            f"short read at block {block_num}: wanted {block_size} bytes, got {len(data)}"
        )
    return data


def longs(block: bytes) -> list[int]:
    """Unpack a block into big-endian unsigned longwords."""
    return list(struct.unpack(f">{len(block) // 4}I", block))


def get_long(block: bytes, index: int) -> int:
    """Read a longword by index; negative indexes count from the end."""
    if index < 0:
        index = len(block) // 4 + index
    return struct.unpack_from(">I", block, index * 4)[0]


def read_bstr(block: bytes, len_offset: int, max_len: int) -> str:
    """Read an Amiga BSTR: one length byte followed by that many characters.

    A length byte larger than the field is a corruption signal, so it is clamped rather
    than trusted -- reading past the field would pull in unrelated structure.
    """
    n = min(block[len_offset], max_len)
    return block[len_offset + 1 : len_offset + 1 + n].decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def header_checksum(block: bytes, slot: int = CHK_HEADER) -> int:
    """Sum every longword except the checksum slot, then negate."""
    total = 0
    for i, v in enumerate(longs(block)):
        if i != slot:
            total += v
    return (-total) & 0xFFFFFFFF


def boot_checksum(boot_blocks: bytes) -> int:
    """Boot block checksum: one's-complement addition, then bitwise NOT.

    Spans all boot blocks together (normally two) and skips longword 1, which holds the
    checksum itself. Deliberately unlike header_checksum.
    """
    total = 0
    for i, v in enumerate(longs(boot_blocks)):
        if i == 1:
            continue
        total += v
        if total > 0xFFFFFFFF:  # the carry wraps back in
            total = (total + 1) & 0xFFFFFFFF
    return (~total) & 0xFFFFFFFF


def checksum_ok(block: bytes, slot: int = CHK_HEADER) -> bool:
    """True if the stored checksum matches a freshly computed one."""
    return get_long(block, slot) == header_checksum(block, slot)


def apply_checksum(block: bytearray, slot: int = CHK_HEADER) -> None:
    """Recompute and store a block's checksum in place."""
    struct.pack_into(">I", block, slot * 4, 0)
    struct.pack_into(">I", block, slot * 4, header_checksum(bytes(block), slot))


# ---------------------------------------------------------------------------
# Geometry and hashing
# ---------------------------------------------------------------------------


def hash_size(block_size: int = BLOCK_SIZE) -> int:
    """Hash table entry count: block_longs - 56. 72 at 512 bytes, 200 at 1024."""
    return (block_size // 4) - 56


def root_block_number(num_blocks: int) -> int:
    """Root block index within a volume: num_blocks // 2 (880 for a DD floppy)."""
    return num_blocks // 2


def name_hash(name: str, size: int = 72, intl: bool = False) -> int:
    """The AmigaDOS FFS/OFS filename hash.

    International mode also maps bytes 224-254 down by 32, excluding 247 (division
    sign). Using the wrong variant makes files invisible to AmigaDOS while still
    occupying space, so the volume's DosType decides this, never a default.
    """
    raw = name.encode("latin-1", errors="replace").upper()
    if intl:
        raw = bytes((c - 32) if (224 <= c <= 254 and c != 247) else c for c in raw)
    h = len(raw)
    for c in raw:
        h = (h * 13 + c) & 0x7FF
    return h % size


def data_block_slot(index: int, block_size: int = BLOCK_SIZE) -> int:
    """Longword index of data-block pointer `index` in a file header.

    The table is stored in reverse: entry 0 sits at the high end (byte 308 at a 512-byte
    block size) and grows downward toward byte 24.
    """
    return (block_size // 4) - 51 - index


# ---------------------------------------------------------------------------
# DosType
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DosType:
    """A decoded DosType, including ones amibuilder deliberately will not touch."""

    raw: int
    label: str  #: e.g. "DOS\\3"
    filesystem: str  #: "OFS" | "FFS" | "PFS3" | "SFS" | "unknown"
    intl: bool = False
    dircache: bool = False
    longnames: bool = False
    #: False for anything amibuilder cannot safely read at file level.
    supported: bool = False
    #: What to tell the user when unsupported.
    note: str = ""

    @property
    def features(self) -> list[str]:
        out = []
        if self.intl:
            out.append("intl")
        if self.dircache:
            out.append("dircache")
        if self.longnames:
            out.append("longnames")
        return out

    def describe(self) -> str:
        f = ("+" + "+".join(self.features)) if self.features else ""
        return f"{self.label} ({self.filesystem}{f})"


_DOS_FLAGS = {
    0: ("OFS", False, False, False),
    1: ("FFS", False, False, False),
    2: ("OFS", True, False, False),
    3: ("FFS", True, False, False),
    4: ("OFS", False, True, False),
    5: ("FFS", False, True, False),
    6: ("OFS", True, False, True),
    7: ("FFS", True, False, True),
}

_UNSUPPORTED = {
    b"PFS": "PFS3 is a third-party filesystem with no file-level support here",
    b"PDS": "PFS3 (PDS variant) is a third-party filesystem with no file-level support here",
    b"SFS": "SFS (Smart Filesystem) has no file-level support here",
    b"muFS": "MultiUserFS is not supported",
}


def decode_dos_type(raw: int) -> DosType:
    """Decode a 32-bit DosType into something reportable.

    Never raises: an unrecognised DosType is a legitimate finding to report, and
    `supported=False` is what stops a command from touching it.
    """
    packed = struct.pack(">I", raw)
    tag, flag = packed[:3], packed[3]
    printable = "".join(chr(c) if 32 <= c < 127 else f"\\{c}" for c in packed)

    if tag == b"DOS":
        if flag in _DOS_FLAGS:
            fs, intl, dc, ln = _DOS_FLAGS[flag]
            return DosType(raw, f"DOS\\{flag}", fs, intl, dc, ln, supported=True)
        return DosType(
            raw,
            printable,
            "unknown",
            supported=False,
            note=f"DOS filesystem with unrecognised variant byte {flag}",
        )

    for prefix, note in _UNSUPPORTED.items():
        if tag == prefix[:3] and (len(prefix) == 3 or packed[:4] == prefix):
            name = prefix.decode() + (str(flag) if flag else "")
            return DosType(raw, printable, name, supported=False, note=note)

    if raw == 0:
        return DosType(raw, "unformatted", "none", supported=False,
                       note="no DosType -- partition is unformatted")

    if tag == b"KIC":  # KICK -- a kickstart-replacement disk, not a filesystem
        return DosType(raw, printable, "KICK", supported=False,
                       note="kickstart disk, not a filesystem")

    return DosType(raw, printable, "unknown", supported=False,
                   note="unrecognised DosType")


def parse_dos_type(spec: str) -> int:
    """Parse a DosType written as 'ffs+intl', 'DOS3', '0x444f5303' or 'DOS\\3'.

    Accepts the amitools spellings so a value copied out of `info` output can be pasted
    straight back into a command.
    """
    s = spec.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)

    up = s.upper().replace("\\", "")
    if up.startswith("DOS") and up[3:].isdigit():
        flag = int(up[3:])
        if flag > 7:
            raise ValueError(f"DOS variant {flag} is out of range (0-7)")
        return 0x444F5300 | flag

    parts = [p.strip().lower() for p in s.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty DosType: {spec!r}")
    base = parts[0]
    if base not in ("ofs", "ffs"):
        raise ValueError(f"unknown DosType base {base!r} (expected ofs or ffs)")

    flag = 1 if base == "ffs" else 0
    feats = set(parts[1:])
    unknown = feats - {"intl", "dircache", "dc", "longnames", "ln"}
    if unknown:
        raise ValueError(f"unknown DosType feature(s): {', '.join(sorted(unknown))}")
    if {"dircache", "dc"} & feats and {"longnames", "ln"} & feats:
        raise ValueError("dircache and longnames are mutually exclusive")

    if {"longnames", "ln"} & feats:
        flag += 6  # DOS\6 / DOS\7 imply intl
    elif {"dircache", "dc"} & feats:
        flag += 4
    elif "intl" in feats:
        flag += 2
    return 0x444F5300 | flag


# ---------------------------------------------------------------------------
# Block identification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockId:
    """What a block appears to be, from its bytes alone."""

    block_num: int
    kind: str  #: "root" | "userdir" | "file" | "filelist" | "data" | "bitmap" | ...
    primary: int | None = None
    secondary: int | None = None
    checksum_ok: bool | None = None
    name: str | None = None
    detail: str = ""


def identify_block(block: bytes, block_num: int = 0) -> BlockId:
    """Best-effort identification of a single block.

    FFS data blocks are raw file bytes, so any block can coincidentally start with the
    value 2. The checksum is what separates a real header from a lookalike, which is why
    `checksum_ok` is reported rather than used to force a "data" verdict -- a corrupt
    header and a data block need to look different to the user.
    """
    magic = block[:4]
    if magic in RDB_MAGICS:
        return BlockId(
            block_num,
            RDB_MAGICS[magic].lower().replace("block", ""),
            checksum_ok=checksum_ok(block, CHK_RDB),
            detail=RDB_MAGICS[magic],
        )
    if magic[:3] == b"DOS" or magic[:3] in (b"PFS", b"SFS", b"PDS"):
        return BlockId(block_num, "boot", detail=decode_dos_type(get_long(block, 0)).describe())

    primary = get_long(block, 0)
    secondary = get_long(block, -1)
    ok = checksum_ok(block, CHK_HEADER)

    if primary == T_SHORT and secondary in SECONDARY_NAMES:
        kind = SECONDARY_NAMES[secondary].lower()
        name = None
        if (secondary in (ST_USERDIR, ST_FILE, ST_LINKDIR, ST_LINKFILE, ST_SOFTLINK)
                or secondary == ST_ROOT):
            name = read_bstr(block, len(block) - 80, 30)
        return BlockId(block_num, kind, primary, secondary, ok, name)

    if primary == T_LIST:
        return BlockId(block_num, "filelist", primary, secondary, ok)
    if primary == T_DIR_CACHE:
        return BlockId(block_num, "dircache", primary, secondary, ok)
    if primary == T_COMMENT:
        return BlockId(block_num, "comment", primary, secondary, ok)
    if primary == T_DATA:
        # OFS data blocks are typed; FFS ones are not, so this only fires for OFS.
        return BlockId(block_num, "data-ofs", primary, secondary, ok)

    if not any(block):
        return BlockId(block_num, "empty", detail="all zero")

    return BlockId(block_num, "raw", primary, secondary, detail="data or unrecognised")
