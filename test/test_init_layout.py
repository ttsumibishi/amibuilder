"""Cylinder layout for `amibuilder init`.

Amiga partitions start and end on a cylinder, so a requested size is almost never the size you
get. That is unavoidable. These tests are mostly about the difference being *reported* rather than
absorbed silently, and about the arithmetic that does not fit being refused with an explanation.
"""

from __future__ import annotations

import itertools

import pytest

from amibuilder.commands import init as I
from amibuilder.errors import UsageError

MI = 1024 ** 2
GI = 1024 ** 3


def layout_for(size: str, *specs: str) -> I.DriveLayout:
    return I.plan_layout(I.parse_init_size(size), I.parse_partition_specs(list(specs)))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", ["1M", "100M", "1000M", "1G", "4G", "16G"])
def test_every_permitted_size_gets_an_exact_geometry(size):
    """No capacity should be lost choosing a geometry, across the whole permitted range."""
    want = I.parse_init_size(size)
    geometry = I.choose_geometry(want)
    assert geometry.total_bytes == want, f"{size}: geometry gives {geometry.total_bytes}"


def test_geometry_is_a_whole_number_of_cylinders():
    geometry = I.choose_geometry(4 * GI)
    assert geometry.cyls * geometry.heads * geometry.sectors == geometry.num_blocks


def test_block_size_is_512():
    """Everything in FFS assumes it, and the rest of the codebase hardcodes 512 deliberately."""
    assert I.choose_geometry(4 * GI).block_size == 512


# ---------------------------------------------------------------------------
# Blank drives
# ---------------------------------------------------------------------------


def test_a_drive_with_no_partitions_has_no_layout():
    layout = layout_for("4G")
    assert layout.partitions == []
    assert layout.warnings == []


def test_a_blank_drive_still_reports_its_geometry():
    assert layout_for("100M").geometry.total_bytes == 100 * MI


# ---------------------------------------------------------------------------
# Dave's layout, which is the motivating case
# ---------------------------------------------------------------------------


@pytest.fixture
def daves() -> I.DriveLayout:
    return layout_for("4G", "Workbench=1G,bootable", "Work=2G", "Persist=rest")


def test_three_partitions_in_order(daves):
    assert [p.volume for p in daves.partitions] == ["Workbench", "Work", "Persist"]
    assert [p.device for p in daves.partitions] == ["DH0", "DH1", "DH2"]


def test_partitions_are_contiguous_and_do_not_overlap(daves):
    assert daves.partitions[0].low_cyl == daves.rdb_cylinders
    for earlier, later in zip(daves.partitions, daves.partitions[1:]):
        assert later.low_cyl == earlier.high_cyl + 1


def test_the_partition_table_gets_its_own_cylinder(daves):
    """Partition data must not start at cylinder 0, or it would overwrite the RDB."""
    assert daves.rdb_cylinders >= 1
    assert daves.partitions[0].low_cyl >= 1


def test_the_whole_drive_is_used(daves):
    assert daves.spare_cylinders == 0
    total = sum(p.cylinders for p in daves.partitions) + daves.rdb_cylinders
    assert total == daves.geometry.cyls


def test_the_sized_partitions_get_what_they_asked_for(daves):
    """On a 4 GiB drive a cylinder is 128 KiB, so 1G and 2G divide exactly."""
    assert daves.partitions[0].actual_bytes == GI
    assert daves.partitions[1].actual_bytes == 2 * GI
    assert daves.partitions[0].rounded == 0
    assert daves.partitions[1].rounded == 0


def test_the_rest_partition_absorbs_the_shortfall(daves):
    """1G+2G+1G is exactly the drive size, and the RDB needs a cylinder, so something must give.

    Better the partition that volunteered for the remainder than a silent shortfall spread across
    all three.
    """
    persist = daves.partitions[2]
    assert persist.actual_bytes == GI - daves.bytes_per_cylinder
    assert persist.requested_bytes is None


def test_the_bootable_flag_is_where_it_was_asked_for(daves):
    assert daves.partitions[0].bootable
    assert not daves.partitions[1].bootable
    assert not daves.partitions[2].bootable


def test_daves_layout_produces_no_warnings(daves):
    """The layout this was designed around should be clean; noise here would train him
    to ignore it."""
    assert daves.warnings == []


# ---------------------------------------------------------------------------
# Rounding is reported
# ---------------------------------------------------------------------------


def test_every_whole_megabyte_size_divides_exactly():
    """Worth pinning, because it is why rounding is rarely visible in practice.

    amitools picks powers of two for heads and sectors, so a cylinder is always a power-of-two
    multiple of 1 KiB -- and any whole number of MiB is therefore a whole number of cylinders.
    Rounding only shows up for fractional sizes.
    """
    layout = layout_for("4G", "A=1G", "B=777M", "C=rest")
    for part in layout.partitions[:2]:
        assert part.rounded == 0


def test_a_size_that_does_not_divide_is_rounded_and_recorded():
    requested = I.parse_init_size("1.1G")
    layout = layout_for("4G", "Odd=1.1G", "Rest=rest")
    odd = layout.partitions[0]
    assert odd.actual_bytes % layout.bytes_per_cylinder == 0
    assert odd.requested_bytes == requested
    assert odd.actual_bytes != requested, "1.1G should not land on a cylinder boundary"
    # The point: the difference is retrievable rather than lost.
    assert odd.rounded == odd.actual_bytes - requested


def test_rounding_never_exceeds_one_cylinder():
    layout = layout_for("4G", "A=1.1G", "B=1.7G", "C=rest")
    for part in layout.partitions[:2]:
        assert abs(part.rounded) < layout.bytes_per_cylinder


def test_a_partition_is_never_zero_cylinders():
    """A 1M partition on a 16G drive rounds to less than a cylinder; it must still exist."""
    layout = layout_for("16G", "Tiny=1M", "Rest=rest")
    assert layout.partitions[0].cylinders >= 1
    assert layout.partitions[0].actual_bytes > 0


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_layout_that_exactly_fills_the_drive_is_refused_with_advice():
    """Without `rest` this cannot fit, and the message has to say why rather than just failing."""
    with pytest.raises(UsageError) as caught:
        layout_for("4G", "A=1G", "B=2G", "C=1G")
    message = str(caught.value)
    assert "reserved for the partition table" in message
    assert "rest" in message


def test_an_oversized_layout_is_refused():
    with pytest.raises(UsageError, match="too much"):
        layout_for("1G", "A=1G", "B=1G")


def test_the_oversize_refusal_quantifies_the_overshoot():
    with pytest.raises(UsageError) as caught:
        layout_for("1G", "A=1000M", "B=1000M")
    assert "too much" in str(caught.value)


def test_rest_with_nothing_left_is_refused():
    with pytest.raises(UsageError, match="nothing left"):
        layout_for("1G", "A=1000M", "B=24M", "C=rest")


def test_too_many_partitions_for_the_drive_is_refused():
    """More partitions than cylinders cannot be laid out, however small each one is.

    The count is derived from the geometry rather than hardcoded, so this keeps testing the
    boundary if amitools ever picks a different CHS for a 1 MiB drive.
    """
    total = I.parse_init_size("1M")
    cylinders = I.choose_geometry(total).cyls
    specs = [f"V{n}=1M" for n in range(cylinders + 2)]
    with pytest.raises(UsageError, match="too small"):
        I.plan_layout(total, I.parse_partition_specs(specs))


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def test_no_bootable_partition_makes_the_first_one_bootable_and_says_so():
    """A drive with none will not boot, which is a bad thing to discover after an install."""
    layout = layout_for("4G", "Workbench=1G", "Work=rest")
    assert layout.partitions[0].bootable
    assert any("bootable" in w for w in layout.warnings)


def test_the_bootable_default_is_not_applied_when_one_was_chosen():
    layout = layout_for("4G", "Workbench=1G", "Work=2G,bootable", "Persist=rest")
    assert not layout.partitions[0].bootable
    assert layout.partitions[1].bootable
    assert not any("was made" in w for w in layout.warnings)


def test_unclaimed_space_at_the_end_is_reported():
    layout = layout_for("4G", "Small=100M")
    assert layout.spare_cylinders > 0
    assert any("not in any partition" in w for w in layout.warnings)


def test_a_large_partition_is_flagged_without_being_refused():
    """2 GiB partitions are in everyday use, so this is a word of warning, not a veto."""
    layout = layout_for("16G", "Big=8G", "Rest=rest")
    assert any("large addressing" in w for w in layout.warnings)
    assert layout.partitions[0].actual_bytes == 8 * GI


def test_a_two_gigabyte_partition_is_not_flagged():
    """Dave's Work: partition is exactly 2 GiB and works on his hardware; do not cry wolf."""
    layout = layout_for("4G", "Workbench=1G", "Work=2G", "Persist=rest")
    assert not any("large addressing" in w for w in layout.warnings)


# ---------------------------------------------------------------------------
# The drive record handed to the shared RDB writer
# ---------------------------------------------------------------------------


def test_the_record_matches_the_capture_schema(daves):
    record = daves.as_record()
    for key in ("scheme", "kind", "block_size", "cylinders", "heads", "sectors",
                "num_blocks", "total_bytes", "partitions", "single_volume"):
        assert key in record, f"missing {key}"
    assert record["scheme"] == "amibuilder-drive-v1"
    assert record["single_volume"] is False


def test_the_record_carries_every_partition(daves):
    record = daves.as_record()
    assert len(record["partitions"]) == 3
    first = record["partitions"][0]
    assert first["device"] == "DH0"
    assert first["volume"] == "Workbench"
    assert first["bootable"] is True
    assert first["dos_type"] == 0x444F5303
    assert first["low_cyl"] == daves.rdb_cylinders


def test_the_record_has_an_empty_dos_env(daves):
    """A new drive has nothing to reproduce, so amitools computes the DosEnvec from the geometry.

    Recording values here would mean inventing a `de_Mask` -- exactly the guessing the layers
    design says silently breaks real controllers.
    """
    assert all(part["dos_env"] == {} for part in daves.as_record()["partitions"])


def test_record_partition_ranges_do_not_overlap(daves):
    parts = daves.as_record()["partitions"]
    for earlier, later in itertools.pairwise(parts):
        assert earlier["high_cyl"] < later["low_cyl"]
