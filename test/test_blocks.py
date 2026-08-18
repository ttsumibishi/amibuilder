"""The package's block layer, pinned against the independent test-helper implementation.

`test/helpers/blocks.py` implements Amiga checksum and offset maths separately, and is the
oracle the structural tests use to assert against bytes rather than against amitools'
constants. `amibuilder/blocks.py` is a second implementation, shaped for streams and for
display.

They agree by construction today -- there is one correct answer -- so this file is not
independent verification. Its job is regression protection: a later refactor of the
package cannot silently drift from the oracle without turning these red.
"""

from __future__ import annotations

import io
import struct

import pytest
from amibuilder import blocks as P
from helpers import blocks as H

# ---------------------------------------------------------------------------
# Agreement with the oracle
# ---------------------------------------------------------------------------


def _sample_block(seed: int = 7) -> bytes:
    import random

    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(512))


def test_constants_agree():
    assert (P.T_SHORT, P.T_DATA, P.T_LIST, P.T_DIR_CACHE, P.T_COMMENT) == (
        H.T_SHORT, H.T_DATA, H.T_LIST, H.T_DIR_CACHE, H.T_COMMENT
    )
    assert (P.ST_ROOT, P.ST_USERDIR, P.ST_FILE) == (H.ST_ROOT, H.ST_USERDIR, H.ST_FILE)
    assert P.CHK_HEADER == H.CHK_LONGWORD_HEADER
    assert P.CHK_BITMAP == H.CHK_LONGWORD_BITMAP
    assert P.CHK_BOOT == H.CHK_LONGWORD_BOOT
    assert P.CHK_RDB == H.CHK_LONGWORD_RDB


@pytest.mark.parametrize("slot", [P.CHK_HEADER, P.CHK_BITMAP, P.CHK_RDB])
def test_header_checksum_agrees(slot):
    block = _sample_block()
    assert P.header_checksum(block, slot) == H.header_checksum(block, slot)


def test_boot_checksum_agrees():
    two = _sample_block(1) + _sample_block(2)
    assert P.boot_checksum(two) == H.boot_checksum(two)


@pytest.mark.parametrize("name", ["S", "Startup-Sequence", "a", "", "WBStartup",
                                  "Español", "ÜBER", "x" * 30])
@pytest.mark.parametrize("intl", [False, True])
def test_name_hash_agrees(name, intl):
    assert P.name_hash(name, 72, intl) == H.ffs_hash(name, 72, intl)


def test_hash_size_agrees():
    for bs in (512, 1024, 2048):
        assert P.hash_size(bs) == H.expected_hash_size(bs)


def test_root_block_number_agrees():
    for n in (1760, 3520, 20478, 65536):
        assert P.root_block_number(n) == H.expected_root_block(n)


def test_data_block_slot_agrees():
    for i in range(0, 70):
        assert P.data_block_slot(i) == H.data_block_longword(i)


def test_get_long_agrees_including_negative_indexes():
    block = _sample_block()
    for i in (0, 1, 5, 127, -1, -2, -24):
        assert P.get_long(block, i) == H.get_long(block, i)


# ---------------------------------------------------------------------------
# Checksum behaviour
# ---------------------------------------------------------------------------


def test_apply_checksum_makes_the_block_valid():
    block = bytearray(_sample_block())
    assert not P.checksum_ok(block)  # random data almost never validates
    P.apply_checksum(block)
    assert P.checksum_ok(bytes(block))


def test_apply_checksum_is_idempotent():
    block = bytearray(_sample_block())
    P.apply_checksum(block)
    first = bytes(block)
    P.apply_checksum(block)
    assert bytes(block) == first


def test_checksum_detects_a_single_flipped_bit():
    block = bytearray(_sample_block())
    P.apply_checksum(block)
    assert P.checksum_ok(bytes(block))
    block[100] ^= 0x01
    assert not P.checksum_ok(bytes(block))


def test_boot_checksum_differs_from_header_checksum():
    """They are different algorithms; conflating them corrupts boot blocks silently."""
    data = _sample_block(3) + _sample_block(4)
    assert P.boot_checksum(data) != P.header_checksum(data[:512], P.CHK_BOOT)


# ---------------------------------------------------------------------------
# read_block works on any stream
# ---------------------------------------------------------------------------


def test_read_block_from_a_bytesio():
    payload = b"".join(bytes([i]) * 512 for i in range(4))
    f = io.BytesIO(payload)
    assert P.read_block(f, 0) == bytes([0]) * 512
    assert P.read_block(f, 3) == bytes([3]) * 512


def test_read_block_past_the_end_raises_eof():
    f = io.BytesIO(bytes(512))
    with pytest.raises(EOFError, match="short read"):
        P.read_block(f, 5)


def test_read_block_honours_a_larger_block_size():
    f = io.BytesIO(bytes(2048))
    assert len(P.read_block(f, 1, 1024)) == 1024


# ---------------------------------------------------------------------------
# BSTR reading
# ---------------------------------------------------------------------------


def test_read_bstr_matches_the_oracle():
    block = bytearray(512)
    block[432] = 3
    block[433:436] = b"DH0"
    assert P.read_bstr(bytes(block), 432, 30) == H.read_bstr(bytes(block), 432, 30)


def test_read_bstr_clamps_an_overlong_length():
    """A corrupt length byte must not pull in neighbouring structure."""
    block = bytearray(512)
    block[432] = 200  # far larger than the 30-byte field
    block[433:463] = b"A" * 30
    assert P.read_bstr(bytes(block), 432, 30) == "A" * 30


def test_read_bstr_replaces_undecodable_bytes():
    block = bytearray(512)
    block[432] = 2
    block[433:435] = b"\xff\xfe"
    assert len(P.read_bstr(bytes(block), 432, 30)) == 2


# ---------------------------------------------------------------------------
# DosType
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,label,fs,intl,dc,ln",
    [
        (0x444F5300, "DOS\\0", "OFS", False, False, False),
        (0x444F5301, "DOS\\1", "FFS", False, False, False),
        (0x444F5302, "DOS\\2", "OFS", True, False, False),
        (0x444F5303, "DOS\\3", "FFS", True, False, False),
        (0x444F5304, "DOS\\4", "OFS", False, True, False),
        (0x444F5305, "DOS\\5", "FFS", False, True, False),
        (0x444F5306, "DOS\\6", "OFS", True, False, True),
        (0x444F5307, "DOS\\7", "FFS", True, False, True),
    ],
)
def test_dos_types_decode(raw, label, fs, intl, dc, ln):
    dt = P.decode_dos_type(raw)
    assert (dt.label, dt.filesystem) == (label, fs)
    assert (dt.intl, dt.dircache, dt.longnames) == (intl, dc, ln)
    assert dt.supported


def test_unformatted_decodes_as_none():
    dt = P.decode_dos_type(0)
    assert dt.filesystem == "none"
    assert not dt.supported
    assert "unformatted" in dt.note


@pytest.mark.parametrize("tag,expect", [(b"PFS\x03", "PFS3"), (b"SFS\x00", "SFS"),
                                        (b"PDS\x03", "PDS3")])
def test_foreign_filesystems_are_named_not_guessed(tag, expect):
    """A PFS3 partition must be *named* in a refusal, never treated as FFS."""
    dt = P.decode_dos_type(struct.unpack(">I", tag)[0])
    assert not dt.supported
    assert dt.filesystem.startswith(expect[:3])
    assert dt.note


def test_unknown_dos_variant_is_unsupported():
    dt = P.decode_dos_type(0x444F5309)  # DOS\9 does not exist
    assert not dt.supported
    assert "variant" in dt.note


def test_kickstart_disk_is_named():
    dt = P.decode_dos_type(struct.unpack(">I", b"KICK")[0])
    assert dt.filesystem == "KICK"
    assert not dt.supported


def test_describe_includes_features():
    assert P.decode_dos_type(0x444F5303).describe() == "DOS\\3 (FFS+intl)"
    assert P.decode_dos_type(0x444F5305).describe() == "DOS\\5 (FFS+dircache)"


# ---------------------------------------------------------------------------
# DosType parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,raw",
    [
        ("ffs", 0x444F5301),
        ("ofs", 0x444F5300),
        ("ffs+intl", 0x444F5303),
        ("ofs+intl", 0x444F5302),
        ("ffs+dircache", 0x444F5305),
        ("ffs+dc", 0x444F5305),
        ("ffs+longnames", 0x444F5307),
        ("DOS3", 0x444F5303),
        ("DOS\\3", 0x444F5303),
        ("0x444f5303", 0x444F5303),
    ],
)
def test_parse_dos_type(spec, raw):
    assert P.parse_dos_type(spec) == raw


def test_parse_dos_type_round_trips_decode():
    for spec in ("ffs", "ffs+intl", "ofs", "ffs+dircache"):
        assert P.decode_dos_type(P.parse_dos_type(spec)).supported


@pytest.mark.parametrize("bad", ["", "xfs", "ffs+bogus", "DOS9",
                                 "ffs+dircache+longnames"])
def test_parse_dos_type_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        P.parse_dos_type(bad)


# ---------------------------------------------------------------------------
# Block identification
# ---------------------------------------------------------------------------


def _typed_block(primary: int, secondary: int, name: str = "") -> bytes:
    block = bytearray(512)
    struct.pack_into(">I", block, 0, primary)
    struct.pack_into(">I", block, 508, secondary)
    if name:
        block[432] = len(name)
        block[433 : 433 + len(name)] = name.encode("latin-1")
    P.apply_checksum(block)
    return bytes(block)


def test_identify_root_dir_and_file():
    assert P.identify_block(_typed_block(P.T_SHORT, P.ST_ROOT, "Workbench")).kind == "root"
    d = P.identify_block(_typed_block(P.T_SHORT, P.ST_USERDIR, "S"))
    assert d.kind == "userdir" and d.name == "S" and d.checksum_ok
    f = P.identify_block(_typed_block(P.T_SHORT, P.ST_FILE, "Startup-Sequence"))
    assert f.kind == "file" and f.name == "Startup-Sequence"


def test_identify_dircache_and_filelist():
    assert P.identify_block(_typed_block(P.T_DIR_CACHE, 0)).kind == "dircache"
    assert P.identify_block(_typed_block(P.T_LIST, P.ST_FILE)).kind == "filelist"


def test_identify_rdb_family():
    for magic, kind in ((b"RDSK", "rigiddisk"), (b"PART", "partition"),
                        (b"FSHD", "filesystemheader"), (b"LSEG", "loadseg")):
        block = bytearray(512)
        block[:4] = magic
        P.apply_checksum(block, P.CHK_RDB)
        ident = P.identify_block(bytes(block))
        assert ident.kind == kind
        assert ident.checksum_ok


def test_identify_boot_block():
    block = bytearray(512)
    struct.pack_into(">I", block, 0, 0x444F5303)
    ident = P.identify_block(bytes(block))
    assert ident.kind == "boot"
    assert "FFS" in ident.detail


def test_identify_empty_and_raw():
    assert P.identify_block(bytes(512)).kind == "empty"
    raw = P.identify_block(_sample_block(99))
    assert raw.kind in ("raw", "data-ofs", "filelist", "dircache", "comment")


def test_a_data_block_masquerading_as_a_header_is_caught_by_checksum():
    """FFS data blocks are raw bytes, so one can begin with the value 2.

    identify_block reports the bad checksum rather than silently accepting it as a
    directory -- that distinction is what keeps `check` honest.
    """
    block = bytearray(_sample_block(11))
    struct.pack_into(">I", block, 0, P.T_SHORT)
    struct.pack_into(">I", block, 508, P.ST_USERDIR)
    ident = P.identify_block(bytes(block))
    assert ident.kind == "userdir"
    assert ident.checksum_ok is False
