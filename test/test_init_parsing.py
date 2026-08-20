"""Parsing for `amibuilder init`: sizes and partition specs.

Pure functions, so these are fast and cover the refusals properly. The size limits come straight
from the original brief -- 1-1000M or 1-16G -- and exist so a typo cannot ask for a 1000 GB file.
"""

from __future__ import annotations

import pytest

from amibuilder.commands import init as I
from amibuilder.errors import UsageError

MI = 1024 ** 2
GI = 1024 ** 3


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec,expected", [
    ("1M", MI),
    ("100M", 100 * MI),
    ("1000M", 1000 * MI),
    ("1G", GI),
    ("4G", 4 * GI),
    ("16G", 16 * GI),
])
def test_valid_sizes(spec, expected):
    assert I.parse_init_size(spec) == expected


@pytest.mark.parametrize("spec", ["4g", "4G", "4Gb", "4GB", "4GiB", "4gib", " 4G "])
def test_spelling_and_whitespace_are_tolerated(spec):
    """A size pasted out of documentation or a previous command should just work."""
    assert I.parse_init_size(spec) == 4 * GI


def test_megabytes_are_binary():
    """Decimal megabytes would be 4.8% out, which is worse than refusing the input."""
    assert I.parse_init_size("100M") == 104857600


def test_gigabytes_are_binary():
    assert I.parse_init_size("1G") == 1073741824


def test_a_fraction_of_a_gigabyte_is_allowed():
    """Without fractions every size between 1000M and 2G would be unreachable.

    1000M is the M ceiling and is *less* than 1G, so an integers-only reading of the brief leaves
    a hole. `1.5G` closes it while staying inside the stated 1-16 range.
    """
    assert I.parse_init_size("1.5G") == GI + GI // 2


@pytest.mark.parametrize("spec", ["0M", "0G", "1001M", "17G", "100G", "1000G"])
def test_sizes_outside_the_permitted_range_are_refused(spec):
    with pytest.raises(UsageError):
        I.parse_init_size(spec)


def test_the_absurd_size_the_limit_exists_for():
    """The case the brief called out by name."""
    with pytest.raises(UsageError, match="1000 GB"):
        I.parse_init_size("1000G")


def test_the_refusal_names_both_ranges():
    """A size just over the M ceiling should point at G rather than leaving a dead end."""
    with pytest.raises(UsageError) as caught:
        I.parse_init_size("1024M")
    message = str(caught.value)
    assert "1000M" in message.replace(" ", "") or "1-1000M" in message.replace(" ", "")
    assert "16" in message


@pytest.mark.parametrize("spec", ["", "   ", "M", "G", "4", "4T", "4K", "big", "-4G", "4 G B"])
def test_unparseable_sizes_are_refused(spec):
    with pytest.raises(UsageError):
        I.parse_init_size(spec)


def test_kilobytes_are_refused_rather_than_silently_accepted():
    """`render.parse_size` accepts K and T; init deliberately does not."""
    with pytest.raises(UsageError, match="M for megabytes"):
        I.parse_init_size("512K")


# ---------------------------------------------------------------------------
# Partition specs
# ---------------------------------------------------------------------------


def test_a_simple_partition():
    request = I.parse_partition_spec("Workbench=1G")
    assert request.volume == "Workbench"
    assert request.size_bytes == GI
    assert not request.bootable
    assert not request.takes_rest


def test_bootable_flag():
    assert I.parse_partition_spec("Workbench=1G,bootable").bootable
    assert I.parse_partition_spec("Workbench=1G,boot").bootable


def test_bootable_can_be_switched_off_explicitly():
    assert not I.parse_partition_spec("Work=2G,bootable=no").bootable


def test_rest_takes_the_remaining_space():
    request = I.parse_partition_spec("Persist=rest")
    assert request.takes_rest
    assert request.size_bytes is None


def test_a_bare_name_means_the_rest():
    """Shorthand, because the last partition in a layout usually wants what is left."""
    assert I.parse_partition_spec("Persist").takes_rest


def test_dostype_defaults_to_ffs_intl():
    request = I.parse_partition_spec("Work=2G")
    assert request.dos_type_name == "ffs+intl"
    assert request.dos_type == 0x444F5303


def test_dostype_can_be_chosen():
    assert I.parse_partition_spec("Old=100M,dostype=ffs").dos_type == 0x444F5301
    assert I.parse_partition_spec("Older=100M,dostype=ofs").dos_type == 0x444F5300


def test_dostype_accepts_the_spellings_info_prints():
    """So a value copied out of `info` output can be pasted straight back in."""
    assert I.parse_partition_spec("X=100M,dostype=DOS3").dos_type == 0x444F5303
    assert I.parse_partition_spec("X=100M,dostype=0x444f5303").dos_type == 0x444F5303


def test_flags_can_be_combined():
    request = I.parse_partition_spec("Workbench=1G,bootable,dostype=ffs")
    assert request.bootable
    assert request.dos_type == 0x444F5301


@pytest.mark.parametrize("spec", ["", "  ", "=1G", ",bootable"])
def test_a_partition_with_no_name_is_refused(spec):
    with pytest.raises(UsageError):
        I.parse_partition_spec(spec)


@pytest.mark.parametrize("name", ["Work:", "Wo/rk"])
def test_illegal_characters_in_a_volume_name_are_refused(name):
    with pytest.raises(UsageError, match="cannot contain"):
        I.parse_partition_spec(f"{name}=1G")


def test_an_over_long_volume_name_is_refused():
    """AmigaDOS truncates rather than complaining, which is worse than refusing here."""
    with pytest.raises(UsageError, match="30"):
        I.parse_partition_spec(f"{'V' * 31}=1G")


def test_a_name_at_the_limit_is_accepted():
    assert I.parse_partition_spec(f"{'V' * 30}=1G").volume == "V" * 30


def test_an_unknown_option_is_refused_rather_than_ignored():
    """Silently dropping `--partition Work=2G,readonly` would be the worst outcome."""
    with pytest.raises(UsageError, match="unknown option"):
        I.parse_partition_spec("Work=2G,readonly")


def test_an_unknown_dostype_is_refused():
    with pytest.raises(UsageError):
        I.parse_partition_spec("Work=2G,dostype=pfs3")


def test_a_partition_size_outside_the_range_is_refused():
    with pytest.raises(UsageError):
        I.parse_partition_spec("Huge=64G")


# ---------------------------------------------------------------------------
# Whole layouts
# ---------------------------------------------------------------------------


def test_daves_layout_parses():
    """The layout this was built for: 1G boot, 2G work, 1G persistent."""
    requests = I.parse_partition_specs([
        "Workbench=1G,bootable", "Work=2G", "Persist=rest",
    ])
    assert [r.volume for r in requests] == ["Workbench", "Work", "Persist"]
    assert requests[0].bootable
    assert requests[2].takes_rest


def test_no_partitions_is_fine():
    """`--size` alone builds a blank unpartitioned image, which is the original brief."""
    assert I.parse_partition_specs([]) == []
    assert I.parse_partition_specs(None) == []


def test_duplicate_volume_names_are_refused():
    """AmigaDOS would mount only one, and the other would be invisible rather than reported."""
    with pytest.raises(UsageError, match="both called"):
        I.parse_partition_specs(["Work=1G", "Work=1G"])


def test_duplicate_names_are_caught_regardless_of_case():
    """FFS is case-insensitive, so 'work' and 'Work' are the same volume."""
    with pytest.raises(UsageError, match="both called"):
        I.parse_partition_specs(["Work=1G", "work=1G"])


def test_two_partitions_cannot_both_take_the_rest():
    with pytest.raises(UsageError, match="remaining space"):
        I.parse_partition_specs(["A=1G", "B=rest", "C=rest"])


def test_the_rest_partition_has_to_come_last():
    """Otherwise the partitions after it have no space to be laid out in."""
    with pytest.raises(UsageError, match="last"):
        I.parse_partition_specs(["A=rest", "B=1G"])


def test_two_bootable_partitions_are_refused():
    """Boot priority decides between several, and init does not set it."""
    with pytest.raises(UsageError, match="bootable"):
        I.parse_partition_specs(["A=1G,bootable", "B=1G,bootable"])


def test_the_duplicate_refusal_quotes_what_was_typed():
    """So the message is actionable when several --partition flags look alike."""
    with pytest.raises(UsageError) as caught:
        I.parse_partition_specs(["Work=1G", "Work=2G,bootable"])
    assert "Work=2G,bootable" in str(caught.value)
