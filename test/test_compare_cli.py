"""CLI tests for `amibuilder diff` -- compare any two sources.

These drive the real entry point, because the value of the command is in the wiring: two
source arguments reaching capture, the path/volume alignment choosing correctly, and a
`--json` payload a script can branch on. Every fixture is built from scratch (never a real
image), and nothing is written to any store -- `diff` is read-only on both sides.
"""

from __future__ import annotations

import json
import os

from helpers import images

from amibuilder.cli import main


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def run_json(capsys, *argv: str):
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0, err or out
    return json.loads(out)


def _plain(workdir, name: str, volume: str, files: dict[str, bytes]) -> str:
    path = images.make_plain_hdf(str(workdir / name), size="10Mi", volume=volume)
    if files:
        images.write_files(path, files)
    return path


def _two_part(workdir, name: str = "rdb.hdf") -> str:
    return images.make_rdb_hdf(
        str(workdir / name),
        size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )


# ---------------------------------------------------------------------------
# The core comparison
# ---------------------------------------------------------------------------


def test_identical_content_reports_no_differences(workdir, capsys):
    files = {"keep.txt": b"hello", "notes.txt": b"echo hi\n"}
    a = _plain(workdir, "a.hdf", "Disk", files)
    b = _plain(workdir, "b.hdf", "Other", files)  # different volume name, same content
    data = run_json(capsys, "diff", a, b)
    assert data["identical"] is True
    assert data["changes"] == []
    assert data["by"] == "path"
    assert data["unchanged"] == 2


def test_reports_added_removed_and_changed(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk",
               {"keep": b"same", "changed": b"aaa", "gone": b"old"})
    b = _plain(workdir, "b.hdf", "Disk",
               {"keep": b"same", "changed": b"bbbb", "fresh": b"new"})
    data = run_json(capsys, "diff", a, b)
    assert data["identical"] is False
    by = {c["path"]: c for c in data["changes"]}
    assert by["gone"]["change"] == "removed" and by["gone"]["reason"] == "deleted"
    assert by["fresh"]["change"] == "added" and by["fresh"]["reason"] == "new"
    assert by["changed"]["change"] == "changed" and by["changed"]["reason"] == "content"
    assert data["unchanged"] == 1
    assert data["by_reason"] == {"content": 1, "deleted": 1, "new": 1}


def test_no_deletions_suppresses_removed(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"keep": b"same", "gone": b"old"})
    b = _plain(workdir, "b.hdf", "Disk", {"keep": b"same"})
    full = run_json(capsys, "diff", a, b)
    assert any(c["change"] == "removed" for c in full["changes"])
    trimmed = run_json(capsys, "diff", a, b, "--no-deletions")
    assert not any(c["change"] == "removed" for c in trimmed["changes"])
    # 'gone' was the only difference; suppressing it leaves the two identical.
    assert trimmed["identical"] is True


def test_exit_zero_even_with_differences(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"x": b"1"})
    b = _plain(workdir, "b.hdf", "Disk", {"x": b"2"})
    code, out, _err = run(capsys, "diff", a, b)  # text mode
    assert code == 0
    assert "changed" in out


def test_text_output_reports_identical(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"x": b"1"})
    code, out, _err = run(capsys, "diff", a, a)
    assert code == 0
    assert "identical -- no differences" in out


# ---------------------------------------------------------------------------
# Volume alignment: path vs volume
# ---------------------------------------------------------------------------


def test_auto_selects_path_for_single_volumes(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"x": b"1"})
    b = _plain(workdir, "b.hdf", "Other", {"x": b"1"})
    assert run_json(capsys, "diff", a, b)["by"] == "path"


def test_auto_selects_volume_for_multi_volume(workdir, capsys):
    rdb = _two_part(workdir)
    images.write_files(rdb, {"a": b"1"}, part=0)
    data = run_json(capsys, "diff", rdb, rdb)
    assert data["by"] == "volume"
    assert data["identical"] is True


def test_by_path_refused_on_multi_volume(workdir, capsys):
    rdb = _two_part(workdir)
    a = _plain(workdir, "a.hdf", "Disk", {"x": b"1"})
    code, _out, err = run(capsys, "diff", rdb, a, "--by", "path")
    assert code == 2
    assert "--by path" in err
    assert "Workbench" in err and "Work" in err


def test_by_volume_forces_volume_qualified(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"x": b"1"})
    b = _plain(workdir, "b.hdf", "Other", {"x": b"1"})  # same content, different volume name
    data = run_json(capsys, "diff", a, b, "--by", "volume")
    assert data["by"] == "volume"
    assert data["identical"] is False
    assert {c["change"] for c in data["changes"]} == {"added", "removed"}
    paths = {c["path"] for c in data["changes"]}
    assert "Disk:x" in paths and "Other:x" in paths


# ---------------------------------------------------------------------------
# Addressing: a partition selector narrows to one volume
# ---------------------------------------------------------------------------


def test_partition_selector_captures_only_that_volume(workdir, capsys):
    rdb = _two_part(workdir)
    images.write_files(rdb, {"wbfile": b"the workbench file"}, part=0)
    images.write_files(rdb, {"workfile": b"the work file"}, part=1)
    folder = images.make_tree(str(workdir / "dir"), {"workfile": b"the work file"})

    # rdb:Work captures ONLY Work (single volume) -> matches the folder byte-for-byte.
    data = run_json(capsys, "diff", f"{rdb}:Work", folder)
    assert data["by"] == "path"
    assert data["a"]["volumes"] == ["Work"]
    assert data["identical"] is True

    # rdb:Workbench against the same folder differs, proving the selector picks the right one.
    other = run_json(capsys, "diff", f"{rdb}:Workbench", folder)
    assert other["a"]["volumes"] == ["Workbench"]
    assert other["identical"] is False


def test_whole_rdb_source_captures_every_volume(workdir, capsys):
    rdb = _two_part(workdir)
    images.write_files(rdb, {"a": b"1"}, part=0)
    images.write_files(rdb, {"b": b"2"}, part=1)
    data = run_json(capsys, "diff", rdb, rdb)
    assert sorted(data["a"]["volumes"]) == ["Work", "Workbench"]


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_timestamps_significant_flags_a_timestamp_only_change(workdir, capsys):
    # Two directories with byte-identical content but different mtimes. A host directory
    # surfaces its files' mtimes as the Amiga timestamp, so this is a clean timestamp-only
    # difference with nothing else moving.
    da = images.make_tree(str(workdir / "da"), {"f": b"same bytes"})
    db = images.make_tree(str(workdir / "db"), {"f": b"same bytes"})
    os.utime(os.path.join(da, "f"), (900_000_000, 900_000_000))   # ~1998
    os.utime(os.path.join(db, "f"), (946_684_800, 946_684_800))   # 2000-01-01

    assert run_json(capsys, "diff", da, db)["identical"] is True  # ignored by default
    strict = run_json(capsys, "diff", da, db, "--timestamps-significant")
    assert strict["identical"] is False
    assert strict["by_reason"] == {"timestamp": 1}
    assert strict["changes"][0]["reason"] == "timestamp"
    assert strict["changes"][0]["change"] == "changed"


# ---------------------------------------------------------------------------
# ADF sources
# ---------------------------------------------------------------------------


def test_adf_vs_adf(workdir, capsys):
    a = images.make_adf(str(workdir / "a.adf"), volume="Disk1",
                        files={"Docs/readme": b"v1", "keep": b"x"})
    b = images.make_adf(str(workdir / "b.adf"), volume="Disk2",
                        files={"Docs/readme": b"v1", "keep": b"x", "extra": b"new"})
    data = run_json(capsys, "diff", a, b)
    assert data["by"] == "path"
    by = {c["path"]: c for c in data["changes"]}
    assert by["extra"]["change"] == "added"
    assert all(c["change"] != "removed" for c in data["changes"])


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_exclude_drops_matching_paths(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"keep": b"1", "sub/x": b"aaa"})
    b = _plain(workdir, "b.hdf", "Disk", {"keep": b"2", "sub/x": b"bbb"})
    full = {c["path"] for c in run_json(capsys, "diff", a, b)["changes"]}
    assert "keep" in full and "sub/x" in full
    excl = {c["path"] for c in
            run_json(capsys, "diff", a, b, "--exclude", "**/sub/**")["changes"]}
    assert "keep" in excl and "sub/x" not in excl


def test_default_excludes_hide_temp_then_no_default_reveals(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"keep": b"1", "T/junk": b"aaa"})
    b = _plain(workdir, "b.hdf", "Disk", {"keep": b"1", "T/junk": b"bbb"})
    # T/ is a default exclusion, so the only difference is hidden.
    assert run_json(capsys, "diff", a, b)["identical"] is True
    revealed = run_json(capsys, "diff", a, b, "--no-default-excludes")
    assert any("T/junk" in c["path"] for c in revealed["changes"])


# ---------------------------------------------------------------------------
# JSON contract
# ---------------------------------------------------------------------------


def test_json_shape(workdir, capsys):
    a = _plain(workdir, "a.hdf", "Disk", {"x": b"1"})
    b = _plain(workdir, "b.hdf", "Other", {"x": b"2"})
    data = run_json(capsys, "diff", a, b)
    assert set(data) >= {"a", "b", "by", "identical", "changes", "by_reason", "unchanged"}
    for side in ("a", "b"):
        assert set(data[side]) >= {"spec", "path", "kind", "volumes", "entries", "warnings"}
    change = data["changes"][0]
    assert set(change) >= {"change", "reason", "path", "kind"}
