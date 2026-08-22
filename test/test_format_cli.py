"""`format` -- laying a fresh filesystem onto an existing drive's partition.

This is the most destructive verb in the tool, so the guard tests assert the *effect* of each
guard, not just its exit code: a refused format must leave the partition exactly as unmountable
as it was. That is what makes the tests mutation-resistant -- delete the `--force` check and the
"still unformatted" assertions fail, because the format would have gone through.

Every image here is built from scratch under tmp_path with `init`; nothing touches a real image
or a device.
"""

from __future__ import annotations

import json

import pytest

from amibuilder.cli import main


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def run_json(capsys, *argv: str):
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0, f"expected success, got {code}: {err}"
    return json.loads(out)


def _mountable(capsys, addr: str) -> bool:
    """True when the volume mounts -- i.e. `ls` of its root succeeds."""
    code, _out, _err = run(capsys, "ls", addr)
    return code == 0


@pytest.fixture
def unformatted_rdb(tmp_path, capsys) -> str:
    """A two-partition RDB whose partitions have no filesystem yet (init --no-format)."""
    path = str(tmp_path / "u.hdf")
    code = main(["init", path, "--size", "16M", "--partition", "Work=8M,bootable",
                 "--partition", "Data=rest", "--no-format"])
    capsys.readouterr()
    assert code == 0
    return path


@pytest.fixture
def formatted_rdb(tmp_path, capsys) -> str:
    """A two-partition RDB, formatted ffs+intl (init's default)."""
    path = str(tmp_path / "f.hdf")
    code = main(["init", path, "--size", "16M", "--partition", "Work=8M,bootable",
                 "--partition", "Data=rest"])
    capsys.readouterr()
    assert code == 0
    return path


@pytest.fixture
def plain_hdf(tmp_path, capsys) -> str:
    path = str(tmp_path / "p.hdf")
    code = main(["init", path, "--size", "4M", "--plain", "--volume", "Old"])
    capsys.readouterr()
    assert code == 0
    return path


# ---------------------------------------------------------------------------
# It works
# ---------------------------------------------------------------------------


def test_format_makes_an_unformatted_partition_mountable(capsys, unformatted_rdb):
    assert not _mountable(capsys, f"{unformatted_rdb}:0")  # precondition
    code, _out, err = run(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work",
                          "--force")
    assert code == 0, err
    assert _mountable(capsys, f"{unformatted_rdb}:0")


def test_format_leaves_the_new_volume_empty(capsys, unformatted_rdb):
    run(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work", "--force")
    # A freshly formatted volume has no entries.
    d = run_json(capsys, "ls", f"{unformatted_rdb}:0")
    assert d["listings"][0]["entries"] == []


def test_format_reuses_the_current_volume_name(capsys, formatted_rdb):
    # Partition 0 is already "Work"; formatting with no --volume keeps that name.
    code, _out, err = run(capsys, "format", f"{formatted_rdb}:Work", "--force")
    assert code == 0, err
    d = run_json(capsys, "info", f"{formatted_rdb}:0")
    assert d["partitions"][0]["volume"] == "Work"


def test_plain_hdf_formats_to_the_requested_type(capsys, plain_hdf):
    code, _out, err = run(capsys, "format", plain_hdf, "--volume", "New", "--dos-type", "ffs",
                          "--force")
    assert code == 0, err
    d = run_json(capsys, "info", plain_hdf)
    assert d["volume"]["volume"] == "New"
    assert d["volume"]["filesystem"] == "FFS"


def test_json_reports_the_format(capsys, unformatted_rdb):
    d = run_json(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work", "--force")
    assert d["formatted"] is True
    assert d["volume"] == "Work"
    assert ":0" in d["target"]


# ---------------------------------------------------------------------------
# The destructive-write guard (mutation-resistant: refusal must leave it unformatted)
# ---------------------------------------------------------------------------


def test_a_file_needs_force_and_the_refusal_changes_nothing(capsys, unformatted_rdb):
    code, _out, err = run(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work")
    assert code == 2
    assert "--force" in err
    # The guard's whole point: nothing was written.
    assert not _mountable(capsys, f"{unformatted_rdb}:0")


def test_dry_run_previews_without_writing(capsys, unformatted_rdb):
    code, out, _err = run(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work",
                          "--dry-run")
    assert code == 0
    assert "would format" in out
    assert not _mountable(capsys, f"{unformatted_rdb}:0")  # still not formatted


def test_dry_run_json_marks_it_not_formatted(capsys, unformatted_rdb):
    d = run_json(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work", "--dry-run")
    assert d["dry_run"] is True
    assert d["formatted"] is False
    assert not _mountable(capsys, f"{unformatted_rdb}:0")


# ---------------------------------------------------------------------------
# The RDB "type is not ours to change" guard
# ---------------------------------------------------------------------------


def test_rdb_refuses_a_dos_type_that_differs_from_the_partition_table(capsys, formatted_rdb):
    code, _out, err = run(capsys, "format", f"{formatted_rdb}:0", "--dos-type", "ofs",
                          "--force")
    assert code == 2
    assert "HDToolBox" in err
    # The partition table's type is unchanged, and the volume still mounts.
    d = run_json(capsys, "info", f"{formatted_rdb}:0")
    assert d["partitions"][0]["dos_type"] == "DOS\\3"


def test_rdb_allows_a_dos_type_that_matches_the_partition_table(capsys, formatted_rdb):
    code, _out, err = run(capsys, "format", f"{formatted_rdb}:0", "--dos-type", "ffs+intl",
                          "--force")
    assert code == 0, err


# ---------------------------------------------------------------------------
# Naming and target guards
# ---------------------------------------------------------------------------


def test_an_unformatted_partition_needs_a_volume_name(capsys, unformatted_rdb):
    code, _out, err = run(capsys, "format", f"{unformatted_rdb}:1", "--force")
    assert code == 2
    assert "--volume" in err


def test_a_plain_hdf_needs_a_volume_name(capsys, plain_hdf):
    code, _out, err = run(capsys, "format", plain_hdf, "--force")
    assert code == 2
    assert "--volume" in err


def test_a_directory_is_refused(capsys, tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    code, _out, err = run(capsys, "format", str(d), "--volume", "X", "--force")
    assert code == 2
    assert "directory" in err


def test_an_invalid_dos_type_is_refused(capsys, unformatted_rdb):
    code, _out, err = run(capsys, "format", f"{unformatted_rdb}:0", "--volume", "Work",
                          "--dos-type", "bogus", "--force")
    assert code == 2
    assert "dos-type" in err.lower()


def test_a_volume_name_with_a_colon_is_refused(capsys, unformatted_rdb):
    code, _out, err = run(capsys, "format", f"{unformatted_rdb}:0", "--volume", "bad:name",
                          "--force")
    assert code == 2
    assert "':'" in err or "cannot contain" in err
