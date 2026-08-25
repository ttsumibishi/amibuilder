"""Container detection, partition enumeration and volume access.

This is the layer that decides *what an image is*. Getting it wrong means reporting a
whole-disk RDB image's geometry as if it were partition 0's filesystem, or -- much worse
on a card -- treating a FAT slot as an Amiga partition.
"""

from __future__ import annotations

import pytest

from amibuilder.addressing import parse
from amibuilder.errors import AddressError, ImageError, UnsupportedError
from amibuilder.image import ImageKind, open_container

# ---------------------------------------------------------------------------
# Kind detection
# ---------------------------------------------------------------------------


def test_rdb_is_detected_by_content_not_extension(rdb_hdf):
    """A whole-disk RDB image is routinely named .hdf.

    Trusting the extension would open partition 0 as if it were the whole disk.
    """
    assert rdb_hdf.endswith(".hdf")
    with open_container(parse(rdb_hdf)) as c:
        assert c.kind is ImageKind.RDB


def test_plain_hdf_is_detected(plain_hdf):
    with open_container(parse(plain_hdf)) as c:
        assert c.kind is ImageKind.PLAIN_HDF
        assert c.boot_dos_type is not None
        assert c.boot_dos_type.supported
        assert c.is_formatted


def test_adf_is_detected_by_size(adf):
    with open_container(parse(adf)) as c:
        assert c.kind is ImageKind.ADF
        assert c.size_bytes == 901_120


def test_mbr_container_is_detected(amiga_card):
    card, _ = amiga_card
    with open_container(parse(card)) as c:
        assert c.kind is ImageKind.MBR
        slots = {p.index: p for p in c.mbr_partitions()}
        assert slots[0].ptype == 0x0C and not slots[0].is_amiga
        assert slots[1].ptype == 0x76 and slots[1].is_amiga


def test_unformatted_hdf_is_reported_not_guessed(unformatted_hdf):
    with open_container(parse(unformatted_hdf)) as c:
        assert c.kind is ImageKind.PLAIN_HDF
        assert not c.is_formatted
        with pytest.raises(UnsupportedError, match="unformatted"):
            c.open_addressed_volume()


def test_an_adf_named_file_of_the_wrong_size_is_refused(workdir):
    """Better a size-specific error than treating a truncated ADF as an odd HDF."""
    bad = workdir / "truncated.adf"
    bad.write_bytes(b"\x00" * 4096)
    with pytest.raises(ImageError, match="not a valid floppy image size"):
        open_container(parse(str(bad)))


def test_empty_file_is_refused(workdir):
    empty = workdir / "empty.hdf"
    empty.write_bytes(b"")
    with pytest.raises(ImageError, match="empty"):
        open_container(parse(str(empty)))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_rdb_geometry_describes_the_whole_disk(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        g = c.geometry
        assert g.block_size == 512
        assert g.num_blocks * g.block_size == 33_554_432
        assert g.cyls * g.heads * g.sectors == g.num_blocks


def test_adf_geometry_is_a_dd_floppy(adf):
    with open_container(parse(adf)) as c:
        assert (c.geometry.cyls, c.geometry.heads) == (80, 2)
        assert c.geometry.num_blocks == 1760


# ---------------------------------------------------------------------------
# Partition enumeration
# ---------------------------------------------------------------------------


def test_partitions_are_enumerated_with_volume_names(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        parts = c.partitions()
        assert [p.index for p in parts] == [0, 1]
        assert [p.device_name for p in parts] == ["DH0", "DH1"]
        assert [p.volume_name for p in parts] == ["Workbench", "Work"]
        assert parts[0].bootable and not parts[1].bootable
        assert all(p.dos_type.label == "DOS\\3" for p in parts)


def test_partition_block_size_is_reported_in_bytes(rdb_populated):
    """dos_env.block_size counts longwords upstream; reporting 128 would be wrong."""
    with open_container(parse(rdb_populated)) as c:
        assert all(p.block_size == 512 for p in c.partitions(probe_volumes=False))


def test_partitions_do_not_overlap_and_cover_the_disk(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        parts = c.partitions(probe_volumes=False)
        assert parts[0].high_cyl < parts[1].low_cyl
        assert parts[1].low_cyl == parts[0].high_cyl + 1


def test_no_probe_skips_volume_names(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        assert all(p.volume_name is None for p in c.partitions(probe_volumes=False))


def test_flat_images_report_no_partitions(plain_hdf, adf):
    for path in (plain_hdf, adf):
        with open_container(parse(path)) as c:
            assert c.partitions() == []


# ---------------------------------------------------------------------------
# Partition selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selector,expected", [
    (None, "Workbench"),
    (0, "Workbench"),
    (1, "Work"),
    ("DH0", "Workbench"),
    ("DH1", "Work"),
    ("Workbench", "Workbench"),
    ("Work", "Work"),
    ("workbench", "Workbench"),  # Amiga volume names are case-insensitive
    ("dh1", "Work"),
])
def test_partition_selectors(rdb_populated, selector, expected):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(selector) as vol:
            assert vol.info().name == expected


def test_volume_name_selection_works_where_amitools_cannot(rdb_populated):
    """amitools' find_partition_by_string matches device names and indexes only.

    Volume names are what a user actually thinks in, so amibuilder resolves them by
    mounting each partition on a miss.
    """
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
    from amitools.fs.rdb.RDisk import RDisk

    raw = RawBlockDevice(rdb_populated, read_only=True)
    raw.open()
    rdisk = RDisk(raw)
    rdisk.open()
    try:
        assert rdisk.find_partition_by_string("Workbench") is None
    finally:
        rdisk.close()
        raw.close()

    with open_container(parse(rdb_populated)) as c:
        assert c.resolve_partition("Workbench") == 0


def test_unknown_partition_index_lists_what_exists(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with pytest.raises(AddressError, match=r"no partition 9.*has 2"):
            c.open_volume(9)


def test_unknown_partition_name_lists_the_devices(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with pytest.raises(AddressError, match="DH0"):
            c.open_volume("Nope")


def test_selector_on_a_flat_image_is_refused(adf):
    with open_container(parse(f"{adf}:0")) as c:
        with pytest.raises(AddressError, match="single volume"):
            c.open_addressed_volume()


def test_address_partition_is_used_by_open_addressed_volume(rdb_populated):
    """A ':N' the user typed must not be silently dropped."""
    with open_container(parse(f"{rdb_populated}:1")) as c:
        with c.open_addressed_volume() as vol:
            assert vol.info().name == "Work"


# ---------------------------------------------------------------------------
# MBR slicing -- the PiStorm path
# ---------------------------------------------------------------------------


def test_slicing_an_mbr_slot_reaches_the_embedded_rdb(amiga_card):
    card, slot = amiga_card
    with open_container(parse(f"{card}:0x76:{slot}")) as c:
        assert c.kind is ImageKind.RDB
        assert c.is_sliced
        assert c.slice_offset == 2048 * 512
        assert [p.volume_name for p in c.partitions()] == ["Workbench", "Work"]


def test_reading_a_file_through_an_mbr_slice(amiga_card):
    card, slot = amiga_card
    with open_container(parse(f"{card}:0x76:{slot}:0")) as c:
        with c.open_addressed_volume() as vol:
            data = vol.read_file("S/Startup-Sequence")
            assert b"SetPatch" in data


def test_addressing_a_non_amiga_slot_as_amiga_is_refused(amiga_card):
    """The guard that stops amibuilder scribbling on Emu68's own boot partition."""
    card, _ = amiga_card
    with pytest.raises(AddressError, match=r"is type 0x0c.*Refusing"):
        open_container(parse(f"{card}:0x76:0"))


def test_addressing_an_empty_mbr_slot_reports_what_is_populated(amiga_card):
    card, _ = amiga_card
    with pytest.raises(AddressError, match="is empty"):
        open_container(parse(f"{card}:0x76:3"))


def test_mbr_container_refuses_to_mount_directly(amiga_card):
    card, _ = amiga_card
    with open_container(parse(card)) as c:
        with pytest.raises(AddressError, match=r"0x76:1"):
            c.open_addressed_volume()


def test_a_slice_containing_no_amiga_filesystem_says_so(amiga_card):
    """Explicitly asking for the FAT slot must produce an explanation, not a guess."""
    card, _ = amiga_card
    with open_container(parse(f"{card}:0x0c:0")) as c:
        with pytest.raises(UnsupportedError):
            c.open_addressed_volume()


# ---------------------------------------------------------------------------
# Volume operations
# ---------------------------------------------------------------------------


def test_volume_info(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            info = vol.info()
            assert info.name == "Workbench"
            assert info.dos_type.filesystem == "FFS" and info.dos_type.intl
            assert info.block_size == 512
            assert 0 < info.percent_used < 100
            assert info.root_block == info.total_blocks // 2


def test_block_accounting_reserves_are_outside_the_bitmap(rdb_populated):
    """used + free + reserved == total. The bitmap does not cover the reserved area.

    Reporting used+free as the total would understate every volume by two blocks and
    disagree with the RDB partition table.
    """
    with open_container(parse(rdb_populated)) as c:
        part = c.partitions(probe_volumes=False)[0]
        with c.open_volume(0) as vol:
            info = vol.info()
            assert info.total_blocks == part.num_blocks
            assert info.used_blocks + info.free_blocks + info.reserved == info.total_blocks
            assert info.bitmap_blocks == info.total_blocks - info.reserved


def test_listdir_sorts_directories_first_then_by_name(rdb_populated):
    """FFS stores entries in hash order, which is arbitrary.

    Sorting makes two listings of the same volume comparable.
    """
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            names = [e.name for e in vol.listdir("")]
            assert names == ["C", "Devs", "Prefs", "S", "Tools"]
            dirs = [e.is_dir for e in vol.listdir("")]
            assert dirs == sorted(dirs, reverse=True)


def test_listdir_is_stable_across_reopens(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            first = [e.name for e in vol.listdir("C")]
        with c.open_volume(0) as vol:
            assert [e.name for e in vol.listdir("C")] == first


@pytest.mark.parametrize("path", ["S", "/S", "S/", "Workbench:S"])
def test_path_forms_are_equivalent(rdb_populated, path):
    """A volume-qualified path is accepted so manifest paths can be passed through."""
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            assert {e.name for e in vol.listdir(path)} == {"Startup-Sequence",
                                                           "Shell-Startup"}


def test_read_file_returns_immutable_bytes(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            data = vol.read_file("S/Shell-Startup")
            assert isinstance(data, bytes)
            assert data == b'Prompt "%N.%S> "\n'


def test_stat_reports_size_and_protection(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            e = vol.stat("Tools/Calculator")
            assert e.size == 4096
            assert not e.is_dir
            assert e.protect_str.endswith("rwed")
            assert e.block > 0


def test_stat_on_a_directory(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            e = vol.stat("Devs")
            assert e.is_dir and e.size == 0


def test_missing_path_raises_not_found(rdb_populated):
    from amibuilder.errors import NotFoundError

    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            with pytest.raises(NotFoundError):
                vol.stat("No/Such/Thing")
            assert not vol.exists("No/Such/Thing")


def test_read_file_on_a_directory_is_refused(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            with pytest.raises(ImageError, match="is a directory"):
                vol.read_file("S")


def test_listdir_on_a_file_is_refused(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            with pytest.raises(ImageError, match="not a directory"):
                vol.listdir("S/Startup-Sequence")


def test_walk_visits_every_directory_once(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            visited = [d for d, _, _ in vol.walk("")]
            assert len(visited) == len(set(visited))
            assert set(visited) == {
                "", "C", "Devs", "Devs/DOSDrivers", "Prefs", "Prefs/Env-Archive",
                "Prefs/Env-Archive/Sys", "S", "Tools",
            }


def test_walk_finds_every_file(rdb_populated):
    from conftest import WORKBENCH_FILES

    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            found = {f.path for _, _, files in vol.walk("") for f in files}
            assert found == set(WORKBENCH_FILES)


def test_walk_from_a_subdirectory(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            dirs = [d for d, _, _ in vol.walk("Prefs")]
            assert dirs == ["Prefs", "Prefs/Env-Archive", "Prefs/Env-Archive/Sys"]


def test_file_blocks_includes_the_data_blocks(rdb_populated):
    """amitools' get_blocks(with_data=True) omits data blocks on FFS volumes.

    A 4096-byte file needs 8 data blocks plus a header, so anything near 1 means the
    data blocks were dropped -- which would make byte-exact work in later phases wrong.
    """
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            blocks = vol.file_blocks("Tools/Calculator")
            assert len(blocks) == 9
            assert len(blocks) == len(set(blocks)), "no block counted twice"


def test_file_blocks_scales_with_file_size(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            small = vol.file_blocks("S/Shell-Startup")
            big = vol.file_blocks("C/List")
            assert len(small) == 2  # header + one data block
            assert len(big) == 1 + (vol.stat("C/List").size + 511) // 512


def test_file_blocks_on_a_directory_is_refused(rdb_populated):
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            with pytest.raises(ImageError, match="is a directory"):
                vol.file_blocks("S")


def test_partition_isolation(rdb_populated):
    """Files written to partition 0 must not be visible in partition 1."""
    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(1) as vol:
            assert vol.listdir("") == []
            assert not vol.exists("S")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_as_dict_includes_the_slice_for_a_card(amiga_card):
    card, slot = amiga_card
    with open_container(parse(f"{card}:0x76:{slot}")) as c:
        d = c.as_dict()
        assert d["kind"] == "rdb"
        assert d["mbr_slice"]["slot"] == slot
        assert d["mbr_slice"]["offset"] == 2048 * 512


def test_stream_is_clamped_to_the_slice(amiga_card):
    """A read past the slice must stop, not run into the next partition."""
    card, slot = amiga_card
    with open_container(parse(f"{card}:0x76:{slot}")) as c:
        with c.stream() as f:
            assert f.read(4) == b"RDSK"
            f.seek(c.size_bytes - 8)
            assert len(f.read(999)) == 8


# ---------------------------------------------------------------------------
# Raw-device size detection
#
# No test here opens a real device. That is the point: the bug this pins was an import
# path, so it can be caught without going anywhere near /dev.
# ---------------------------------------------------------------------------


def test_device_size_uses_an_import_path_that_exists(tmp_path):
    """`_device_size` imported from a module that does not exist, so raw devices never worked.

    It reached for `BlkDevTools` in `amitools.fs.blkdev` -- where it feels like it belongs,
    and where amitools' own blkdev modules import it *from elsewhere* -- rather than from
    `amitools.util`. Every raw-device operation therefore died at Container construction with
    `cannot determine device size: cannot import name 'BlkDevTools'`, before touching
    anything. Nothing caught it because every card fixture is a file, and `is_device` is
    false for those.

    Asserted by calling it on a regular file, so no device is involved: the import must
    resolve and the code must run far enough to fail on the *ioctl* instead.
    """
    from amibuilder.image import _device_size

    plain = tmp_path / "not-a-device.bin"
    plain.write_bytes(b"\0" * 4096)

    with pytest.raises(Exception) as exc:
        _device_size(str(plain))

    # The distinction that matters: an OS-level refusal means the import resolved and the
    # real code ran. An ImportError means it did not.
    assert not isinstance(exc.value, ImportError), (
        f"_device_size could not even import its dependency: {exc.value}"
    )
    assert "BlkDevTools" not in str(exc.value), (
        f"still failing on the import rather than the device: {exc.value}"
    )


def test_a_device_address_is_refused_before_any_size_lookup():
    """The guard rail fires first, so a typo'd device path never reaches an ioctl."""
    from amibuilder import device
    from amibuilder.errors import DeviceRefused

    with pytest.raises(DeviceRefused):
        device.check_access("/dev/rdisk99", device_flag=False, writable=False)
