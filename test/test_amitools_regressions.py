"""Pins for known amitools bugs and surprising behaviours.

Every test here documents something found by investigation and asserts the *current*
behaviour. When a version bump changes one, the test fails and the change gets noticed
deliberately rather than silently altering how the tool behaves.

Marked `regression` so they can be run alone:  pytest -m regression
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from helpers import images

sys.path.insert(0, str(Path(images.XDFTOOL).parent.parent))

pytestmark = pytest.mark.regression


def test_amitools_version_and_licence():
    """amitools is GPL-2.0-or-later, which constrains distribution of this project."""
    from importlib.metadata import metadata, version

    assert version("amitools") >= "0.8.1"
    lic = metadata("amitools").get("License-Expression")
    assert lic == "GPL-2.0-or-later", (
        f"licence changed to {lic!r} -- revisit docs/KIP-FFS-NOTES.md section 5.3"
    )


def test_amitools_has_no_runtime_dependencies():
    """Zero runtime deps is a large part of why amitools is a safe dependency."""
    from importlib.metadata import metadata

    reqs = metadata("amitools").get_all("Requires-Dist") or []
    mandatory = [r for r in reqs if "extra ==" not in r]
    assert mandatory == [], f"amitools gained runtime dependencies: {mandatory}"


def test_comment_command_is_broken(workdir):
    """BUG: `xdftool comment` crashes on a long-filename length check.

    TypeError: object of type 'FileName' has no len()

    Comments are metadata worth preserving, so this needs patching or working around
    via the API. If this test starts failing, the upstream bug was fixed.
    """
    path = images.make_plain_hdf(str(workdir / "c.hdf"), size="10Mi", volume="C")
    images.write_files(path, {"thing": b"data"})

    res = images.xdftool(path, "open", "+", "comment", "thing", "a comment", check=False)
    assert "has no len()" in res.output or "TypeError" in res.output, (
        "the comment crash appears to be fixed upstream -- remove the workaround"
    )


def test_rdbtool_free_crashes_when_no_space_remains(workdir):
    """BUG: `rdbtool free` raises TypeError instead of reporting no free space.

    get_free_cyl_ranges() returns None rather than an empty list.
    """
    path = str(workdir / "full.hdf")
    images.rdbtool(path, "create", "size=16Mi", "+", "init",
                   "+", "add", "dostype=ffs+intl")

    res = images.rdbtool(path, "free", check=False)
    assert "TypeError" in res.output or "NoneType" in res.output, (
        "rdbtool free no longer crashes on a full disk -- upstream fix?"
    )


def test_protect_replaces_flags_rather_than_modifying(workdir):
    """SURPRISE: `protect <file> +s` sets the flags absolutely, despite the '+' sigil.

    `+s` yields `-s------`, silently dropping read/write/execute/delete. A wrapper must
    always pass the complete desired flag set, never a delta.
    """
    path = images.make_plain_hdf(str(workdir / "p.hdf"), size="10Mi", volume="P")
    images.write_files(path, {"a": b"x", "b": b"x"})

    images.xdftool(path, "open", "+", "protect", "a", "+s")
    images.xdftool(path, "open", "+", "protect", "b", "rwed+s")

    out = images.xdftool(path, "open", "+", "list").output
    line_a = next(ln for ln in out.splitlines() if ln.strip().startswith("a "))
    line_b = next(ln for ln in out.splitlines() if ln.strip().startswith("b "))

    assert "-s------" in line_a, (
        f"'+s' should replace the whole flag set; got {line_a.strip()!r}"
    )
    assert "-s--rwed" in line_b, (
        f"'rwed+s' should retain rwed; got {line_b.strip()!r}"
    )


def test_validator_reports_false_positives_on_dircache_volumes(workdir):
    """BUG: xdfscan has no dircache support, so DOS4/DOS5 volumes report bogus errors.

    Exactly one "expected free" block per dircache block. Verified originally by
    counting: 99 dircache blocks produced 99 such blocks across 92 findings.

    The project targets DOS3 (no dircache), so this is a documented limitation rather
    than a blocker -- but a wrapper must not treat xdfscan as a safety gate for DOS4/5.
    """
    tree, count, _ = images.workbench_like_tree(
        str(workdir / "t"), dirs=8, files_per_dir=20
    )
    assert count > 100, "need enough entries to force several dircache blocks"

    # format and pack must be one invocation: a separate `pack` recreates the volume
    # and loses the dircache DosType.
    path = str(workdir / "dc.hdf")
    images.xdftool(path, "create", "size=40Mi",
                   "+", "format", "DC", "ffs+dircache",
                   "+", "pack", tree)
    assert "dircache" in images.xdftool(path, "open", "+", "list").output

    dircache_blocks = _count_dircache_blocks(path)
    assert dircache_blocks > 0, "ffs+dircache should have produced dircache blocks"

    errors = _validator_expected_free_blocks(path)
    assert errors == dircache_blocks, (
        f"expected one false positive per dircache block: {errors} findings vs "
        f"{dircache_blocks} dircache blocks. If these now differ, xdfscan may have "
        "gained dircache support."
    )


def test_validator_is_clean_on_dos3_with_the_same_content(workdir):
    """The control for the test above: no dircache means no false positives."""
    tree, _, _ = images.workbench_like_tree(
        str(workdir / "t2"), dirs=8, files_per_dir=20
    )
    path = str(workdir / "intl.hdf")
    images.xdftool(path, "create", "size=40Mi",
                   "+", "format", "OK", "ffs+intl",
                   "+", "pack", tree)

    assert _count_dircache_blocks(path) == 0
    assert images.scan_is_ok(path)


def test_validator_needs_scan_files_or_it_invents_errors(workdir):
    """The validator must be driven through its full 5-step sequence.

    Omitting scan_files() leaves data blocks unclassified, so BitmapScan concludes they
    should be free and reports an error per data-block region. Any wrapper that drives
    the Validator API directly must reproduce the sequence xdfscan uses, or it will
    produce confident nonsense about healthy volumes.
    """
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.validate.Log import Log
    from amitools.fs.validate.Validator import Validator

    path = images.make_plain_hdf(str(workdir / "v.hdf"), size="10Mi", volume="V")
    images.write_files(path, {"data.bin": os.urandom(50_000)})

    def errors(include_scan_files: bool) -> int:
        blkdev = BlkDevFactory().open(path, read_only=True)
        v = Validator(blkdev, min_level=Log.WARN)
        v.scan_boot()
        v.scan_root()
        v.scan_dir_tree()
        if include_scan_files:
            v.scan_files()
        v.scan_bitmap()
        return v.log.get_num_level(Log.ERROR)

    assert errors(include_scan_files=True) == 0, "correct sequence must be clean"
    assert errors(include_scan_files=False) > 0, (
        "omitting scan_files should produce false positives -- if it no longer does, "
        "the ordering hazard is gone and this note can be relaxed"
    )


def test_thirty_character_filename_limit_fails_safe(workdir):
    """Names over 30 characters cannot exist on classic FFS; the write must be refused."""
    path = images.make_plain_hdf(str(workdir / "n.hdf"), size="10Mi", volume="N")

    long_name = "A" * 31
    src = workdir / "src.bin"
    src.write_bytes(b"x")

    res = images.xdftool(path, "open", "+", "write", str(src), long_name, check=False)
    assert "Invalid File Name" in res.output, f"unexpected outcome: {res.output!r}"

    listing = images.xdftool(path, "open", "+", "list").output
    assert long_name not in listing, "nothing should have been written"


def test_exactly_thirty_characters_is_accepted(workdir):
    """The boundary itself: 30 is legal, 31 is not."""
    path = images.make_plain_hdf(str(workdir / "n30.hdf"), size="10Mi", volume="N")
    src = workdir / "src.bin"
    src.write_bytes(b"x")

    name = "B" * 30
    images.xdftool(path, "open", "+", "write", str(src), name)
    assert name in images.xdftool(path, "open", "+", "list").output


def test_pfs3_partition_is_visible_in_rdb_but_not_readable(workdir):
    """Non-FFS filesystems must be reported at RDB level and refused at file level."""
    path = str(workdir / "pfs.hdf")
    images.rdbtool(path, "create", "size=32Mi", "+", "init",
                   "+", "add", "dostype=0x50465303")

    listing = images.rdbtool(path, "list").output
    assert "PFS3" in listing or "50465303" in listing.replace("0x", "")

    res = images.xdftool(path, "open", "part=0", "+", "list", check=False)
    assert "Invalid Boot Block" in res.output or "FSError" in res.output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_dircache_blocks(path: str) -> int:
    """Count blocks with primary type 33 and a valid header checksum."""
    from helpers import blocks as B

    return len(B.find_blocks_by_type(path, B.T_DIR_CACHE, verify_checksum=True))


def _validator_expected_free_blocks(path: str) -> int:
    """Total blocks the validator claims should be free but the bitmap marks used."""
    import re

    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.validate.Log import Log
    from amitools.fs.validate.Validator import Validator

    blkdev = BlkDevFactory().open(path, read_only=True)
    v = Validator(blkdev, min_level=Log.WARN)
    v.scan_boot()
    v.scan_root()
    v.scan_dir_tree()
    v.scan_files()
    v.scan_bitmap()

    pat = re.compile(r"got=([0-9a-f]{8}) expect=([0-9a-f]{8})")
    total = 0
    for entry in v.log.entries:
        m = pat.search(entry.msg)
        if m:
            got, exp = int(m.group(1), 16), int(m.group(2), 16)
            total += (exp & ~got & 0xFFFFFFFF).bit_count()
    return total


# ---------------------------------------------------------------------------
# Python API quirks found while building amibuilder's volume layer.
# Each of these is absorbed inside amibuilder/volume.py; if one is fixed
# upstream, the corresponding workaround can be removed.
# ---------------------------------------------------------------------------


def _open_volume(path: str):
    """Open a plain image's volume through the amitools API."""
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory

    blkdev = BlkDevFactory().open(path, read_only=True)
    vol = ADFSVolume(blkdev)
    vol.open()
    return vol, blkdev


def test_filename_str_and_repr_are_broken(populated_hdf):
    """BUG: FileName.__str__ and __repr__ return an FSString, not a str.

    Python requires both to return `str`, so the interpreter raises TypeError. Any code
    doing `str(node.get_file_name())` or printing a node crashes. amibuilder uses
    `get_unicode_name()`; see `_name_of` in amibuilder/volume.py.
    """
    vol, blkdev = _open_volume(populated_hdf)
    try:
        node = vol.get_root_dir().get_entries()[0]
        name = node.get_file_name()

        with pytest.raises(TypeError, match="__str__ returned non-string"):
            str(name)
        with pytest.raises(TypeError, match="__repr__ returned non-string"):
            repr(name)

        # The working accessors:
        assert isinstance(name.get_unicode_name(), str)
        assert isinstance(name.get_ami_str_name(), bytes)
        assert isinstance(str(name.get_name()), str)
    finally:
        blkdev.close()


def test_fsstring_str_works_even_though_filename_str_does_not(populated_hdf):
    """FSString.__str__ is fine; only the FileName wrapper is broken.

    Worth pinning separately so a fix to one is not mistaken for a fix to both.
    """
    from amitools.fs.FSString import FSString

    assert str(FSString("DH0")) == "DH0"
    vol, blkdev = _open_volume(populated_hdf)
    try:
        assert str(vol.get_volume_name()) == "Pop"
    finally:
        blkdev.close()


def test_path_lookup_requires_an_fsstring_not_a_str(populated_hdf):
    """API constraint: passing a plain str raises ValueError rather than being coerced."""
    from amitools.fs.FSString import FSString

    vol, blkdev = _open_volume(populated_hdf)
    try:
        with pytest.raises(ValueError, match="must be a FSString"):
            vol.get_path_name("S/Startup-Sequence")
        assert vol.get_path_name(FSString("S/Startup-Sequence")) is not None
    finally:
        blkdev.close()


def test_get_blocks_omits_data_blocks_on_ffs(workdir):
    """BUG: ADFSFile.get_blocks(with_data=True) returns no data blocks on FFS volumes.

    `read()` only appends to `self.data_blks` in its OFS branch; the FFS branch reads raw
    blocks straight into the output buffer. So `get_blocks(with_data=True)` yields just
    the header and extension blocks, understating a 200 KB file by 391 blocks.

    `data_blk_nums` is populated correctly, which is what amibuilder's
    `Volume.file_blocks()` uses instead.
    """
    from amitools.fs.FSString import FSString

    path = images.make_plain_hdf(str(workdir / "blk.hdf"), size="20Mi", volume="B")
    images.write_files(path, {"Big": bytes(4096)})

    vol, blkdev = _open_volume(path)
    try:
        node = vol.get_path_name(FSString("Big"))
        assert node.get_size() == 4096

        reported = node.get_blocks(with_data=True)
        assert len(reported) == 1, "the bug: only the header block comes back"

        # The correct set, composed the way amibuilder does it.
        assert node.num_data_blks == 8
        assert len(node.data_blk_nums) == 8
        assert node.total_blks == 9
    finally:
        blkdev.close()


def test_find_partition_by_string_ignores_volume_names(rdb_two_part):
    """LIMITATION: partition lookup matches device names and indexes, not volume names.

    A user thinks in "Workbench:", not "DH0", so amibuilder resolves volume names itself
    by mounting each partition on a miss (Container.resolve_partition).
    """
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
    from amitools.fs.rdb.RDisk import RDisk

    raw = RawBlockDevice(rdb_two_part, read_only=True)
    raw.open()
    rdisk = RDisk(raw)
    rdisk.open()
    try:
        assert rdisk.find_partition_by_string("DH0") is not None
        assert rdisk.find_partition_by_string("0") is not None
        # These are the *volume* names of the two partitions.
        assert rdisk.find_partition_by_string("Workbench") is None
        assert rdisk.find_partition_by_string("Extra") is None
    finally:
        rdisk.close()
        raw.close()


def test_blkdevfactory_auto_resolves_a_partition_for_rdb_images(rdb_two_part):
    """BEHAVIOUR: BlkDevFactory.open() on an RDB returns partition 0, not the disk.

    This is why amibuilder drives RawBlockDevice + RDisk directly to enumerate
    partitions: asking the factory for the image hands back a filesystem view, whose
    block count is the partition's rather than the disk's.
    """
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.blkdev.PartBlockDevice import PartBlockDevice
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice

    blkdev = BlkDevFactory().open(rdb_two_part, read_only=True)
    try:
        assert isinstance(blkdev, PartBlockDevice)
        part_blocks = blkdev.num_blocks
    finally:
        blkdev.close()

    raw = RawBlockDevice(rdb_two_part, read_only=True)
    raw.open()
    try:
        assert raw.num_blocks > part_blocks, (
            "the factory returned a partition view, so its block count is smaller "
            "than the whole disk's"
        )
    finally:
        raw.close()

    # And options={"part": N} is how a specific partition is selected.
    blkdev = BlkDevFactory().open(rdb_two_part, read_only=True, options={"part": 1})
    try:
        assert isinstance(blkdev, PartBlockDevice)
    finally:
        blkdev.close()


def test_timestamp_epoch_constant_is_timezone_dependent():
    """BUG: amiga_epoch is built with time.mktime, so it varies by local timezone.

    `amiga_epoch = time.mktime(time.strptime("01.01.1978 00:00:00", ...))` interprets the
    date as local time, giving 252489600 in UTC-8 against 252460800 for true UTC. Values
    written on one machine therefore render shifted on another, and a value written in
    summer is off by the DST difference from the January offset used in the constant.

    amibuilder renders from the on-disk (days, mins, ticks) triple instead. See
    amibuilder/timestamps.py and test_timestamps.py.
    """
    import datetime as dt

    import amitools.fs.TimeStamp as TS

    true_utc = int((dt.datetime(1978, 1, 1) - dt.datetime(1970, 1, 1)).total_seconds())
    assert true_utc == 252460800

    # The constant is whatever mktime made of it here; assert only that it is derived
    # that way, so the test is meaningful in any timezone.
    import time

    assert TS.amiga_epoch == time.mktime(
        time.strptime("01.01.1978 00:00:00", TS.ts_format)
    )

    # time.timezone is seconds *west* of UTC, so local midnight on 1978-01-01 is that
    # many seconds later in UTC than true epoch midnight.
    offset = TS.amiga_epoch - true_utc
    assert offset == time.timezone, (
        "the epoch constant carries the host's January UTC offset"
    )


def test_dos_env_block_size_is_in_longwords(rdb_two_part):
    """TRAP: PartitionBlock.dos_env.block_size counts longwords, not bytes.

    A 512-byte block reports 128. Reporting it verbatim as a byte count would be wrong by
    a factor of four, so amibuilder multiplies by 4.
    """
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
    from amitools.fs.rdb.RDisk import RDisk

    raw = RawBlockDevice(rdb_two_part, read_only=True)
    raw.open()
    rdisk = RDisk(raw)
    rdisk.open()
    try:
        de = rdisk.get_partition(0).part_blk.dos_env
        assert de.block_size == 128
        assert de.block_size * 4 == raw.block_bytes == 512
    finally:
        rdisk.close()
        raw.close()


@pytest.mark.regression
def test_amitools_has_no_link_support():
    """amitools cannot represent AmigaDOS links, which bounds what layer capture can record.

    Measured against 0.8.x: `Block` defines only ST_ROOT, ST_USERDIR and ST_FILE -- not
    ST_LINKFILE (4), ST_LINKDIR (3) or ST_SOFTLINK (-4) -- and the `fs` package contains no
    link node class at all. A link's target is therefore unreachable through amitools, so
    `amibuilder.layers.capture` records a warning and skips it rather than inventing a target
    that composition would later act on.

    Two consequences worth stating, because both are easy to trip over:

    * `amibuilder.volume.Entry.link_kind` is set by testing whether the amitools node class
      name contains "Link". Since no such class exists, that branch is currently unreachable.
      It is left in place because it costs nothing and would start working the day amitools
      grows link classes -- which is exactly what this test watches for.
    * `manifest` already carries the 'h' and 's' entry kinds and a `link_target` field, so
      the format is ready. Only the reading side is missing.

    If this fails, amitools has gained link support and capture should be revisited.
    """
    from amitools.fs.block.Block import Block

    assert Block.ST_ROOT == 1
    assert Block.ST_USERDIR == 2
    for absent in ("ST_LINKFILE", "ST_LINKDIR", "ST_SOFTLINK"):
        assert not hasattr(Block, absent), (
            f"amitools now defines {absent}; link capture may be implementable -- "
            "see amibuilder/layers/capture.py"
        )
