"""`amibuilder init` through the real CLI.

The refusals get as much attention as the successes. `init`'s whole promise is that the target is
new, so the ways it can be misused all end in either a destroyed image or a half-written one.
"""

from __future__ import annotations

import json
import os

import pytest

from amibuilder.cli import main


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def run_both(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, *argv: str):
    code, text = run(capsys, *argv, "--json")
    return code, json.loads(text)


# ---------------------------------------------------------------------------
# Blank images -- the original brief
# ---------------------------------------------------------------------------


def test_size_alone_makes_a_blank_image(capsys, tmp_path):
    target = tmp_path / "blank.hdf"
    code, out = run(capsys, "init", str(target), "--size", "100M")
    assert code == 0, out
    assert target.stat().st_size == 100 * 1024 ** 2


def test_a_blank_image_has_no_partition_table_and_no_filesystem(capsys, tmp_path):
    """"Brand new, empty, blank, unformatted, unpartitioned", as asked for."""
    target = tmp_path / "blank.hdf"
    run(capsys, "init", str(target), "--size", "100M")
    assert target.read_bytes()[:512] == bytes(512)


def test_a_blank_image_is_sparse(capsys, tmp_path):
    """A 4 GB image should cost almost nothing until something fills it."""
    target = tmp_path / "big.hdf"
    run(capsys, "init", str(target), "--size", "4G")
    assert target.stat().st_size == 4 * 1024 ** 3
    assert target.stat().st_blocks * 512 < 4 * 1024 ** 2


def test_a_blank_image_says_what_to_do_next(capsys, tmp_path):
    code, out = run(capsys, "init", str(tmp_path / "blank.hdf"), "--size", "100M")
    assert code == 0
    assert "HDToolBox" in out or "--partition" in out


# ---------------------------------------------------------------------------
# Partitioned drives
# ---------------------------------------------------------------------------


def test_daves_layout_end_to_end(capsys, tmp_path):
    """The drive this command was built for."""
    target = tmp_path / "dave.hdf"
    code, out = run(capsys, "init", str(target), "--size", "4G",
                    "--partition", "Workbench=1G,bootable",
                    "--partition", "Work=2G",
                    "--partition", "Persist=rest")
    assert code == 0, out

    code, data = run_json(capsys, "partitions", str(target))
    assert code == 0
    assert [p["volume"] for p in data["partitions"]] == ["Workbench", "Work", "Persist"]


def test_a_partitioned_drive_passes_check(capsys, tmp_path):
    target = tmp_path / "dave.hdf"
    run(capsys, "init", str(target), "--size", "1G",
        "--partition", "Boot=200M,bootable", "--partition", "Rest=rest")
    code, out = run(capsys, "check", str(target))
    assert code == 0, out


def test_the_volumes_are_formatted_and_mountable(capsys, tmp_path):
    target = tmp_path / "dave.hdf"
    run(capsys, "init", str(target), "--size", "1G",
        "--partition", "Boot=200M,bootable", "--partition", "Data=rest")
    code, out = run(capsys, "ls", f"{target}:Data")
    assert code == 0, out


def test_the_bootable_flag_reaches_the_partition_table(capsys, tmp_path):
    target = tmp_path / "dave.hdf"
    run(capsys, "init", str(target), "--size", "1G",
        "--partition", "Boot=200M,bootable", "--partition", "Data=rest")
    code, data = run_json(capsys, "partitions", str(target))
    assert code == 0
    by_name = {p["volume"]: p for p in data["partitions"]}
    assert by_name["Boot"]["bootable"]
    assert not by_name["Data"]["bootable"]


def test_a_chosen_dostype_reaches_the_partition_table(capsys, tmp_path):
    target = tmp_path / "mixed.hdf"
    run(capsys, "init", str(target), "--size", "1G",
        "--partition", "Boot=200M,bootable", "--partition", "Old=rest,dostype=ffs")
    code, data = run_json(capsys, "partitions", str(target))
    by_name = {p["volume"]: p for p in data["partitions"]}
    assert by_name["Boot"]["dos_type"] == "DOS\\3"
    assert by_name["Old"]["dos_type"] == "DOS\\1"


def test_no_format_leaves_the_partitions_without_a_filesystem(capsys, tmp_path):
    target = tmp_path / "raw.hdf"
    code, out = run(capsys, "init", str(target), "--size", "1G", "--no-format",
                    "--partition", "Boot=200M,bootable", "--partition", "Data=rest")
    assert code == 0, out

    code, data = run_json(capsys, "partitions", str(target))
    assert code == 0
    # The partition table exists...
    assert len(data["partitions"]) == 2
    # ...but nothing on it will mount, because there is no filesystem to mount.
    assert all(p["volume_error"] for p in data["partitions"])
    code, _out = run(capsys, "ls", f"{target}:Data")
    assert code != 0


def test_cylinder_rounding_is_reported(capsys, tmp_path):
    """A size that does not land on a cylinder must say so rather than quietly differ."""
    target = tmp_path / "odd.hdf"
    code, out = run(capsys, "init", str(target), "--size", "4G",
                    "--partition", "Odd=1.1G,bootable", "--partition", "Rest=rest")
    assert code == 0, out
    assert "rounding" in out


def test_an_exact_layout_reports_no_rounding(capsys, tmp_path):
    """Do not cry wolf on the common case."""
    code, out = run(capsys, "init", str(tmp_path / "exact.hdf"), "--size", "4G",
                    "--partition", "Workbench=1G,bootable", "--partition", "Rest=rest")
    assert code == 0
    assert "rounding" not in out


def test_an_unbootable_layout_is_fixed_and_reported(capsys, tmp_path):
    code, out = run(capsys, "init", str(tmp_path / "noboot.hdf"), "--size", "1G",
                    "--partition", "Data=200M", "--partition", "More=rest")
    assert code == 0
    assert "bootable" in out


# ---------------------------------------------------------------------------
# Plain images
# ---------------------------------------------------------------------------


def test_plain_makes_a_single_volume_image(capsys, tmp_path):
    target = tmp_path / "plain.hdf"
    code, out = run(capsys, "init", str(target), "--size", "100M",
                    "--plain", "--volume", "Scratch")
    assert code == 0, out

    code, data = run_json(capsys, "info", str(target))
    assert code == 0
    assert data["kind"] == "hdf", "a plain image is an hdf with no partition table"
    assert data["volume"]["volume"] == "Scratch"


def test_plain_has_no_partition_table(capsys, tmp_path):
    target = tmp_path / "plain.hdf"
    run(capsys, "init", str(target), "--size", "100M", "--plain", "--volume", "Scratch")
    code, data = run_json(capsys, "partitions", str(target))
    assert code == 0
    assert data["partitions"] == [], "a plain image has no partitions to list"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_existing_file_is_never_overwritten(capsys, tmp_path):
    """The one refusal with no escape hatch, because a --force here could only serve a mistake."""
    target = tmp_path / "precious.hdf"
    target.write_bytes(b"someone's actual drive")
    code, _out, err = run_both(capsys, "init", str(target), "--size", "100M")
    assert code == 2
    assert "never overwrites" in err
    assert target.read_bytes() == b"someone's actual drive"


def test_there_is_no_force_flag_to_defeat_that(capsys, tmp_path):
    """Pinned deliberately: adding one later would silently reintroduce the hazard.

    argparse rejects the unknown flag itself, which is why this expects SystemExit rather than a
    return code -- and is also the strongest form of the guarantee, since the option does not
    exist to be passed.
    """
    target = tmp_path / "precious.hdf"
    target.write_bytes(b"data")
    with pytest.raises(SystemExit) as caught:
        main(["init", str(target), "--size", "100M", "--force"])
    assert caught.value.code == 2
    assert target.read_bytes() == b"data"


def test_plain_and_partition_together_are_refused(capsys, tmp_path):
    code, _out, err = run_both(capsys, "init", str(tmp_path / "x.hdf"), "--size", "100M",
                               "--plain", "--volume", "V", "--partition", "P=50M")
    assert code == 2
    assert "cannot be combined" in err


def test_plain_without_a_volume_name_is_refused(capsys, tmp_path):
    code, _out, err = run_both(capsys, "init", str(tmp_path / "x.hdf"), "--size", "100M",
                               "--plain")
    assert code == 2
    assert "--volume" in err


def test_volume_without_plain_is_refused(capsys, tmp_path):
    """Otherwise it looks like it named the drive, and it would be silently ignored."""
    code, _out, err = run_both(capsys, "init", str(tmp_path / "x.hdf"), "--size", "100M",
                               "--volume", "V")
    assert code == 2
    assert "--plain" in err


def test_no_format_without_partitions_is_refused(capsys, tmp_path):
    """Without --partition the image is already unformatted, so the flag would mean nothing."""
    code, _out, err = run_both(capsys, "init", str(tmp_path / "x.hdf"), "--size", "100M",
                               "--no-format")
    assert code == 2
    assert "--partition" in err


def test_an_out_of_range_size_is_refused(capsys, tmp_path):
    code, _out, err = run_both(capsys, "init", str(tmp_path / "x.hdf"), "--size", "64G")
    assert code == 2
    assert "range" in err


def test_nothing_is_left_behind_when_a_size_is_refused(capsys, tmp_path):
    target = tmp_path / "x.hdf"
    run_both(capsys, "init", str(target), "--size", "64G")
    assert not target.exists()


def test_a_failure_part_way_through_leaves_no_stub(capsys, tmp_path, monkeypatch):
    """A half-written image is worse than none: it looks usable and is not."""
    from amibuilder.commands import init as I

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure after the file was created")

    monkeypatch.setattr(I, "create_rdb", boom)
    target = tmp_path / "doomed.hdf"
    with pytest.raises(RuntimeError):
        main(["init", str(target), "--size", "1G", "--partition", "A=200M",
              "--partition", "B=rest"])
    assert not target.exists(), "a failed init must not leave a plausible-looking image"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_reports_the_layout(capsys, tmp_path):
    code, data = run_json(capsys, "init", str(tmp_path / "dave.hdf"), "--size", "4G",
                          "--partition", "Workbench=1G,bootable",
                          "--partition", "Work=2G",
                          "--partition", "Persist=rest")
    assert code == 0
    assert data["kind"] == "rdb"
    assert data["size_bytes"] == 4 * 1024 ** 3
    assert data["formatted"] is True
    assert [p["volume"] for p in data["partitions"]] == ["Workbench", "Work", "Persist"]


def test_json_reports_requested_against_actual(capsys, tmp_path):
    """The rounding has to be machine-readable too, not only in the text output."""
    code, data = run_json(capsys, "init", str(tmp_path / "odd.hdf"), "--size", "4G",
                          "--partition", "Odd=1.1G,bootable", "--partition", "Rest=rest")
    odd = data["partitions"][0]
    assert odd["requested_bytes"] == int(1.1 * 1024 ** 3)
    assert odd["size_bytes"] != odd["requested_bytes"]
    assert odd["rounded_bytes"] == odd["size_bytes"] - odd["requested_bytes"]


def test_json_reports_the_geometry(capsys, tmp_path):
    code, data = run_json(capsys, "init", str(tmp_path / "g.hdf"), "--size", "4G",
                          "--partition", "A=1G,bootable", "--partition", "B=rest")
    geometry = data["geometry"]
    assert geometry["block_size"] == 512
    assert geometry["cylinders"] * geometry["heads"] * geometry["sectors"] == geometry["num_blocks"]
    assert geometry["total_bytes"] == 4 * 1024 ** 3


def test_json_for_a_blank_image_has_no_partitions(capsys, tmp_path):
    code, data = run_json(capsys, "init", str(tmp_path / "blank.hdf"), "--size", "100M")
    assert code == 0
    assert data["kind"] == "blank"
    assert "partitions" not in data


# ---------------------------------------------------------------------------
# A failed init must not leave a partial image behind
#
# These exist because mutation testing found the cleanup guard in cmd_init entirely
# uncovered: removing it left the whole suite green. The guard is the difference between
# "init failed, nothing was created" and a plausible-looking stub that a later `compose
# --into` or an FS-UAE config would happily accept, so it is worth pinning properly.
#
# The failure is injected into the creation call rather than contrived from bad input,
# because the guard's job is specifically to handle an exception raised *after* the file
# exists -- which is what a real mid-write failure looks like.
# ---------------------------------------------------------------------------


class _Boom(RuntimeError):
    """A mid-write failure, of the kind that leaves bytes on disk."""


def _fail_after_creating(target: str):
    """Return a stand-in that creates a partial file and then fails, like a real bad write."""

    def stub(*_args, **_kwargs):
        with open(target, "wb") as fh:
            fh.write(b"\x00" * 4096)  # enough to look like the start of an image
        raise _Boom("write failed halfway")

    return stub


@pytest.mark.parametrize(
    "which, argv_extra",
    [
        ("create_blank", []),
        ("create_rdb", ["--partition", "Work=40M"]),
        ("create_plain", ["--plain", "--volume", "Work"]),
    ],
)
def test_a_failed_init_leaves_no_partial_image(capsys, tmp_path, monkeypatch, which,
                                               argv_extra):
    """Every creation path cleans up after itself, not just the one that was easiest to test."""
    from amibuilder.commands import init as init_mod

    target = tmp_path / "doomed.hdf"
    monkeypatch.setattr(init_mod, which, _fail_after_creating(str(target)))

    with pytest.raises(_Boom):
        main(["init", str(target), "--size", "100M", *argv_extra])

    assert not target.exists(), (
        f"{which} failed but left a partial image behind. A stub like this looks usable to "
        "every other command and is not"
    )


def test_the_cleanup_does_not_swallow_the_failure(capsys, tmp_path, monkeypatch):
    """Cleaning up must not turn a failure into a success -- an exit 0 would be worse.

    Deleting the partial file and then reporting nothing wrong would be the most dangerous
    possible outcome: no image, no error, and a caller that carries on.
    """
    from amibuilder.commands import init as init_mod

    target = tmp_path / "doomed.hdf"
    monkeypatch.setattr(init_mod, "create_blank", _fail_after_creating(str(target)))

    with pytest.raises(_Boom):
        main(["init", str(target), "--size", "100M"])


def test_cleanup_tolerates_a_failure_that_created_nothing(capsys, tmp_path, monkeypatch):
    """The common case: it failed before touching the disk, so there is nothing to remove.

    The guard must not itself raise here, or a clear error would be replaced by a confusing
    FileNotFoundError from the cleanup path.
    """
    from amibuilder.commands import init as init_mod

    def stub(*_args, **_kwargs):
        raise _Boom("failed before writing anything")

    target = tmp_path / "never-made.hdf"
    monkeypatch.setattr(init_mod, "create_blank", stub)

    with pytest.raises(_Boom):
        main(["init", str(target), "--size", "100M"])
    assert not target.exists()


def test_an_existing_file_is_not_deleted_by_a_later_failure(capsys, tmp_path, monkeypatch):
    """The cleanup must never be reachable for a file init did not create.

    init refuses an existing target, so this should be impossible -- but the cleanup deletes
    unconditionally, so if the refusal above it were ever weakened this test says so loudly
    rather than letting the two guards combine into a data-loss path.
    """
    from amibuilder.commands import init as init_mod

    target = tmp_path / "precious.hdf"
    target.write_bytes(b"IRREPLACEABLE" * 100)
    original = target.read_bytes()

    monkeypatch.setattr(init_mod, "create_blank", _fail_after_creating(str(target)))

    code, out, err = run_both(capsys, "init", str(target), "--size", "100M")
    assert code == 2, f"expected the pre-existing target to be refused, got {code}: {out}{err}"
    assert target.exists(), "init deleted a file it refused to overwrite"
    assert target.read_bytes() == original, "init modified a file it refused to overwrite"
