"""The drive record a base layer carries.

Its reason for existing is that composing with *guessed* DosEnvec values can silently
corrupt data on a real controller, so the tests that matter most check the record is
complete and verbatim, and that validation refuses a layout that would write outside its
own partition.
"""

from __future__ import annotations

import base64

import pytest

from amibuilder.addressing import parse
from amibuilder.errors import ImageError, UsageError
from amibuilder.image import DOS_ENV_FIELDS, open_container
from amibuilder.layers import drive as D


def synthetic_env(**over) -> dict[str, int]:
    env = {name: 0 for name in DOS_ENV_FIELDS}
    env.update({"block_size": 128, "surfaces": 8, "blk_per_trk": 32})
    env.update(over)
    return env


def synthetic_part(index=0, low=1, high=50, device="DH0", policy=D.POLICY_MERGE, **extra):
    part = {
        "index": index,
        "device": device,
        "low_cyl": low,
        "high_cyl": high,
        "policy": policy,
        "dos_env": synthetic_env(low_cyl=low, high_cyl=high),
    }
    part.update(extra)
    return part


def synthetic_record(partitions=None, **over) -> dict:
    rec = {
        "scheme": D.DRIVE_SCHEME,
        "kind": "rdb",
        "block_size": 512,
        "cylinders": 100,
        "heads": 8,
        "sectors": 32,
        "partitions": partitions if partitions is not None else [synthetic_part()],
    }
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# Capture from a real image
# ---------------------------------------------------------------------------


@pytest.fixture
def captured(rdb_populated: str) -> dict:
    with open_container(parse(rdb_populated)) as container:
        return D.capture(container)


def test_capture_records_geometry(captured):
    assert captured["scheme"] == D.DRIVE_SCHEME
    assert captured["kind"] == "rdb"
    assert captured["block_size"] == 512
    assert captured["cylinders"] > 0
    assert captured["heads"] > 0
    assert captured["sectors"] > 0
    assert captured["total_bytes"] == captured["num_blocks"] * captured["block_size"]


def test_capture_records_every_partition(captured):
    assert len(captured["partitions"]) == 2
    assert [p["index"] for p in captured["partitions"]] == [0, 1]


def test_capture_records_volume_names_not_just_devices(captured):
    """Volume names are what a person thinks in, and what manifest paths are keyed on."""
    assert D.volumes(captured) == ["Workbench", "Work"]


def test_capture_keeps_the_dosenvec_verbatim(captured):
    """Anything missing here silently becomes a default at compose time."""
    for part in captured["partitions"]:
        assert set(part["dos_env"]) == set(DOS_ENV_FIELDS)
        assert all(isinstance(v, int) for v in part["dos_env"].values())


def test_capture_records_the_mask_and_max_transfer(captured):
    """The specific values whose wrong defaults break PFS3 on PiStorm."""
    env = captured["partitions"][0]["dos_env"]
    assert env["mask"] > 0
    assert env["max_transfer"] > 0


def test_capture_marks_the_bootable_partition(captured):
    assert captured["partitions"][0]["bootable"] is True
    assert captured["partitions"][1]["bootable"] is False


def test_capture_suggests_replace_for_the_bootable_partition_and_merge_elsewhere(captured):
    """Defaults chosen so the non-OS volumes destroy nothing without an explicit decision."""
    assert captured["partitions"][0]["policy"] == D.POLICY_REPLACE
    assert captured["partitions"][1]["policy"] == D.POLICY_MERGE


def test_capture_records_dos_type_three_ways(captured):
    """Raw for reproduction, the Amiga signature for recognition, a name for reading."""
    part = captured["partitions"][0]
    assert part["dos_type"].startswith("0x")
    assert part["dos_type_label"].startswith("DOS")
    assert part["filesystem"] == "FFS"


def test_capture_validates_what_it_produces(captured):
    D.validate(captured)


def test_capture_of_a_plain_hdf_records_no_partition_table(plain_hdf: str):
    """A plain HDF genuinely does not constrain the target's shape, so none is invented."""
    with open_container(parse(plain_hdf)) as container:
        record = D.capture(container)
    assert record["single_volume"] is True
    assert record["partitions"] == []


# ---------------------------------------------------------------------------
# Boot blocks -- open question L2, answered by measurement
# ---------------------------------------------------------------------------


def test_boot_blocks_are_captured_for_each_partition(captured):
    for part in captured["partitions"]:
        boot = part["boot_blocks"]
        assert boot["size"] == D.BOOT_BLOCK_COUNT * 512
        assert len(boot["sha256"]) == 64


def test_boot_block_dos_type_matches_the_partition(captured):
    part = captured["partitions"][0]
    assert part["boot_blocks"]["dos_type"] == part["dos_type"]


def test_a_freshly_formatted_partition_has_no_boot_code(captured):
    """So an ordinary base layer does not carry a kilobyte of base64 zeros per partition."""
    for part in captured["partitions"]:
        assert part["boot_blocks"]["has_boot_code"] is False
        assert part["boot_blocks"]["boot_code"] is None


def test_boot_code_is_preserved_when_present():
    data = b"DOS\x03" + b"\x00" * 8 + b"\x60\x0a" + bytes(1024 - 14)
    record = D._describe_boot_blocks(data)
    assert record["has_boot_code"] is True
    assert base64.b64decode(record["boot_code"]) == data


def test_boot_blocks_can_be_skipped(rdb_populated: str):
    with open_container(parse(rdb_populated)) as container:
        record = D.capture(container, boot_blocks=False)
    assert "boot_blocks" not in record["partitions"][0]


def test_unreadable_boot_blocks_are_recorded_not_fatal(rdb_populated: str):
    """One bad partition must not make the whole drive uncapturable."""
    with open_container(parse(rdb_populated)) as container:
        bad = synthetic_env(low_cyl=10**9, high_cyl=10**9)
        with pytest.raises(ImageError, match="image ends before"):
            D.read_boot_blocks(container, bad)


def test_unusable_partition_geometry_is_rejected(rdb_populated: str):
    with open_container(parse(rdb_populated)) as container:
        with pytest.raises(ImageError, match="geometry is unusable"):
            D.read_boot_blocks(container, synthetic_env(surfaces=0))


# ---------------------------------------------------------------------------
# Validation -- the guards against writing into a neighbouring partition
# ---------------------------------------------------------------------------


def test_validate_accepts_a_sane_record():
    D.validate(synthetic_record())


def test_validate_rejects_a_foreign_scheme():
    with pytest.raises(ImageError, match="scheme"):
        D.validate(synthetic_record(scheme="something-else"))


def test_validate_rejects_inverted_cylinders():
    with pytest.raises(ImageError, match="above high_cyl"):
        D.validate(synthetic_record([synthetic_part(low=50, high=10)]))


def test_validate_rejects_a_partition_claiming_cylinder_zero():
    """Cylinder 0 holds the RDB that describes the partition."""
    with pytest.raises(ImageError, match="cylinder 0 holds the RDB"):
        D.validate(synthetic_record([synthetic_part(low=0, high=10)]))


def test_validate_rejects_a_partition_past_the_end_of_the_drive():
    with pytest.raises(ImageError, match="beyond the drive"):
        D.validate(synthetic_record([synthetic_part(low=1, high=500)]))


def test_validate_rejects_overlapping_partitions():
    parts = [
        synthetic_part(index=0, low=1, high=50, device="DH0"),
        synthetic_part(index=1, low=40, high=90, device="DH1"),
    ]
    with pytest.raises(ImageError, match="overlap"):
        D.validate(synthetic_record(parts))


def test_validate_accepts_adjacent_partitions():
    parts = [
        synthetic_part(index=0, low=1, high=50, device="DH0"),
        synthetic_part(index=1, low=51, high=90, device="DH1"),
    ]
    D.validate(synthetic_record(parts))


def test_validate_rejects_an_unknown_policy():
    with pytest.raises(UsageError, match="unknown policy"):
        D.validate(synthetic_record([synthetic_part(policy="whatever")]))


def test_validate_rejects_an_incomplete_dosenvec():
    part = synthetic_part()
    del part["dos_env"]["mask"]
    with pytest.raises(ImageError, match="missing DosEnvec fields"):
        D.validate(synthetic_record([part]))


# ---------------------------------------------------------------------------
# Lookup and policy editing
# ---------------------------------------------------------------------------


def test_find_partition_by_index(captured):
    assert D.find_partition(captured, 1)["device"] == "DH1"
    assert D.find_partition(captured, "1")["device"] == "DH1"


def test_find_partition_by_volume_name(captured):
    assert D.find_partition(captured, "Workbench")["index"] == 0
    assert D.find_partition(captured, "Workbench:")["index"] == 0


def test_find_partition_by_volume_name_is_case_insensitive(captured):
    assert D.find_partition(captured, "workbench")["index"] == 0


def test_find_partition_by_device_name(captured):
    assert D.find_partition(captured, "DH1")["index"] == 1


def test_find_partition_error_lists_what_is_available(captured):
    with pytest.raises(UsageError, match="DH0=Workbench"):
        D.find_partition(captured, "Nope")


def test_find_partition_unknown_index(captured):
    with pytest.raises(UsageError, match="no partition 9"):
        D.find_partition(captured, 9)


def test_set_policy_returns_a_copy(captured):
    updated = D.set_policy(captured, "Work", D.POLICY_PRESERVE)
    assert D.find_partition(updated, "Work")["policy"] == D.POLICY_PRESERVE
    assert D.find_partition(captured, "Work")["policy"] == D.POLICY_MERGE


def test_set_policy_rejects_an_unknown_policy(captured):
    with pytest.raises(UsageError, match="unknown policy"):
        D.set_policy(captured, "Work", "sometimes")


@pytest.mark.parametrize(
    "bootable,expected", [(True, D.POLICY_REPLACE), (False, D.POLICY_MERGE)]
)
def test_default_policy(bootable, expected):
    assert D.default_policy(bootable=bootable) == expected


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_summary_shows_the_values_that_break_controllers(captured):
    text = "\n".join(D.summary_lines(captured))
    assert "mask=0x" in text and "maxtr=0x" in text
    assert "Workbench:" in text
    assert "policy=replace" in text


def test_summary_flags_a_bootable_partition(captured):
    assert "bootable pri" in "\n".join(D.summary_lines(captured))


def test_summary_of_a_single_volume_record(plain_hdf: str):
    with open_container(parse(plain_hdf)) as container:
        record = D.capture(container)
    assert "single volume" in "\n".join(D.summary_lines(record))


def test_summary_flags_a_custom_boot_block(captured):
    captured["partitions"][0]["boot_blocks"]["has_boot_code"] = True
    assert "custom boot block" in "\n".join(D.summary_lines(captured))
