"""The `recipe` and `compose` commands through the real CLI.

Two concerns here. First, whether `compose` reports its consequences accurately -- particularly
the destroys/untouched/creates-empty distinction, which is the line a person actually reads
before deciding to run it for real. Second, that each `--format` writes something the tool can
read back and `check` accepts, since a composed image that only *looks* written is the worst
possible outcome.
"""

from __future__ import annotations

import json
import os

import pytest
from helpers import images

from amibuilder.cli import main


@pytest.fixture
def store_dir(tmp_path) -> str:
    return str(tmp_path / "store")


def build(path: str, files: dict[str, bytes]) -> str:
    images.make_rdb_hdf(
        path, size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(path, files, part=0)
    return path


BASE_FILES = {
    "S/Startup-Sequence": b"C:SetPatch QUIET\n",
    "C/List": bytes(range(256)) * 4,
    "Libs/old.library": bytes(400),
}
LATER_FILES = {
    "S/Startup-Sequence": b"C:SetPatch QUIET\n",
    "C/List": bytes(range(256)) * 4,
    # old.library gone -> whiteout
    "Tools/NewApp": b"installed thing" * 40,
}


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def run_both(capsys, *argv: str) -> tuple[int, str, str]:
    """Like `run`, but also returns stderr -- refusals are reported there, not on stdout."""
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, *argv: str):
    code, text = run(capsys, *argv, "--json")
    return code, json.loads(text)


@pytest.fixture
def stack(capsys, workdir, store_dir) -> str:
    """A store holding base-os plus a committed diff layer, and a recipe over both."""
    base = build(str(workdir / "base.hdf"), BASE_FILES)
    later = build(str(workdir / "later.hdf"), LATER_FILES)
    run(capsys, "snap", "create", base, "--label", "base-os", "--store", store_dir)
    run(capsys, "snap", "diff", later, "--parent", "base-os", "--label", "newapp",
        "--store", store_dir)
    run(capsys, "snap", "commit", "newapp", "--store", store_dir)
    run(capsys, "recipe", "new", "a1200", "--layers", "base-os,newapp",
        "--description", "everyday", "--store", store_dir)
    return store_dir


# ---------------------------------------------------------------------------
# recipe
# ---------------------------------------------------------------------------


def test_recipe_new_and_show(capsys, stack):
    code, text = run(capsys, "recipe", "show", "a1200", "--store", stack)
    assert code == 0
    assert "base-os" in text and "newapp" in text
    assert "everyday" in text


def test_recipe_ls(capsys, stack):
    _code, data = run_json(capsys, "recipe", "ls", "--store", stack)
    assert [r["name"] for r in data] == ["a1200"]


def test_recipe_show_resolves_each_layer(capsys, stack):
    _code, data = run_json(capsys, "recipe", "show", "a1200", "--store", stack)
    assert [item["spec"] for item in data["resolved"]] == ["base-os", "newapp"]
    assert all(item["id"] for item in data["resolved"])


def test_recipe_follows_a_moving_label(capsys, stack, workdir):
    """A recipe stores the ref name, so re-pointing the label changes what composes."""
    _code, before = run_json(capsys, "recipe", "show", "a1200", "--store", stack)
    original = before["resolved"][0]["id"]

    other = build(str(workdir / "other.hdf"), {"C/Only": b"different"})
    run(capsys, "snap", "create", other, "--label", "base-os", "--store", stack)

    _code, after = run_json(capsys, "recipe", "show", "a1200", "--store", stack)
    assert after["resolved"][0]["id"] != original


def test_recipe_show_reports_an_unresolved_layer(capsys, stack):
    """A dangling reference must be visible rather than surfacing later as a compose failure."""
    run(capsys, "snap", "rm", "newapp", "--store", stack)
    code, text = run(capsys, "recipe", "show", "a1200", "--store", stack)
    assert code == 3
    assert "UNRESOLVED" in text


def test_recipe_new_rejects_an_unknown_layer(capsys, stack):
    code, _ = run(capsys, "recipe", "new", "bad", "--layers", "ghost", "--store", stack)
    assert code == 3


def test_recipe_new_rejects_an_empty_layer_list(capsys, stack):
    code, _ = run(capsys, "recipe", "new", "bad", "--layers", ",,", "--store", stack)
    assert code == 2


def test_recipe_rm(capsys, stack):
    code, _ = run(capsys, "recipe", "rm", "a1200", "--store", stack)
    assert code == 0
    code, _ = run(capsys, "recipe", "rm", "a1200", "--store", stack)
    assert code == 1


def test_recipe_without_a_subcommand(capsys):
    code, _ = run(capsys, "recipe")
    assert code == 2


def test_recipe_ls_on_an_empty_store(capsys, tmp_path):
    code, text = run(capsys, "recipe", "ls", "--store", str(tmp_path / "empty"))
    assert code == 0
    assert "no recipes" in text


# ---------------------------------------------------------------------------
# compose: stack selection
# ---------------------------------------------------------------------------


def test_compose_by_recipe(capsys, stack):
    code, text = run(capsys, "compose", "--recipe", "a1200", "--dry-run", "--store", stack)
    assert code == 0
    assert "base-os + newapp" in text
    assert "dry run: nothing written" in text


def test_compose_by_stack(capsys, stack):
    code, text = run(capsys, "compose", "--stack", "base-os,newapp", "--dry-run",
                     "--store", stack)
    assert code == 0
    assert "base-os + newapp" in text


def test_compose_needs_exactly_one_of_recipe_or_stack(capsys, stack):
    code, _ = run(capsys, "compose", "--dry-run", "--store", stack)
    assert code == 2
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--stack", "base-os",
                  "--dry-run", "--store", stack)
    assert code == 2


def test_compose_reports_an_unknown_recipe(capsys, stack):
    code, _ = run(capsys, "compose", "--recipe", "ghost", "--dry-run", "--store", stack)
    assert code == 3


# ---------------------------------------------------------------------------
# compose: what it says it will do
# ---------------------------------------------------------------------------


def test_dry_run_to_a_new_target_destroys_nothing(capsys, stack, tmp_path):
    """The ordinary case. If the scariest line fired here it would be crying wolf."""
    target = str(tmp_path / "brand-new.hdf")
    code, data = run_json(capsys, "compose", "--recipe", "a1200", "--into", target,
                          "--dry-run", "--store", stack)
    assert code == 0
    assert data["destroys"] == []


def test_dry_run_to_an_existing_target_names_what_it_destroys(capsys, stack, workdir):
    existing = build(str(workdir / "existing.hdf"), {"C/Something": b"already here"})
    _code, data = run_json(capsys, "compose", "--recipe", "a1200", "--into", existing,
                           "--dry-run", "--store", stack)
    assert data["destroys"] == ["Workbench"]


def test_whiteout_is_applied_so_the_deleted_file_is_absent(capsys, stack):
    _code, data = run_json(capsys, "compose", "--recipe", "a1200", "--dry-run",
                           "--store", stack)
    workbench = next(v for v in data["volumes"] if v["volume"] == "Workbench")
    # base had 3 files, the diff removed one and added one
    assert workbench["files"] == 3


def test_no_deletions_keeps_the_removed_file(capsys, stack):
    _code, data = run_json(capsys, "compose", "--recipe", "a1200", "--no-deletions",
                           "--dry-run", "--store", stack)
    workbench = next(v for v in data["volumes"] if v["volume"] == "Workbench")
    assert workbench["files"] == 4


def test_volume_restricts_the_plan(capsys, stack):
    _code, data = run_json(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                           "--dry-run", "--store", stack)
    assert [v["volume"] for v in data["volumes"]] == ["Workbench"]


def test_policy_override_is_applied(capsys, stack):
    _code, data = run_json(capsys, "compose", "--recipe", "a1200",
                           "--policy", "Workbench=merge", "--dry-run", "--store", stack)
    workbench = next(v for v in data["volumes"] if v["volume"] == "Workbench")
    assert workbench["policy"] == "merge"
    assert workbench["format"] is False


def test_merge_policy_warns_about_deletions_it_cannot_apply(capsys, stack):
    _code, text = run(capsys, "compose", "--recipe", "a1200",
                      "--policy", "Workbench=merge", "--dry-run", "--store", stack)
    assert "will not take effect" in text


def test_preserve_on_a_missing_volume_reports_creation_not_destruction(capsys, stack):
    _code, text = run(capsys, "compose", "--recipe", "a1200", "--policy", "Work=preserve",
                      "--dry-run", "--store", stack)
    assert "creates empty: Work" in text
    assert "DESTROYS existing data on: Work" not in text


@pytest.mark.parametrize("bad", ["Workbench", "Workbench=", "=replace", "Workbench=often"])
def test_malformed_policy_is_rejected(capsys, stack, bad):
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--policy", bad,
                  "--dry-run", "--store", stack)
    assert code == 2


def test_dry_run_shows_the_drive_layout_including_the_mask(capsys, stack):
    """The values a wrong default would corrupt data with should be visible before writing."""
    _code, text = run(capsys, "compose", "--recipe", "a1200", "--dry-run", "--store", stack)
    assert "mask=0x" in text and "maxtr=0x" in text


# ---------------------------------------------------------------------------
# compose: refusals
# ---------------------------------------------------------------------------


def test_a_stack_missing_its_parent_warns_but_proceeds(capsys, stack):
    code, text = run(capsys, "compose", "--stack", "newapp", "--dry-run", "--store", stack)
    assert code == 0
    assert "not in this stack" in text


def test_strict_parents_refuses(capsys, stack):
    code, text = run(capsys, "compose", "--stack", "newapp", "--strict-parents",
                     "--dry-run", "--store", stack)
    assert code == 5
    assert "composition refuses" in text
    assert "nothing was written" in text


def test_a_refusal_suggests_how_to_fix_it(capsys, stack):
    _code, text = run(capsys, "compose", "--stack", "newapp", "--strict-parents",
                      "--dry-run", "--store", stack)
    assert "snap review" in text


def test_the_default_format_is_a_whole_drive_rdb(capsys, stack, tmp_path):
    """Omitting --format should give the format that boots on real hardware, not a directory."""
    target = str(tmp_path / "out.hdf")
    code, out = run(capsys, "compose", "--recipe", "a1200", "--into", target, "--store", stack)
    assert code == 0, out

    code, data = run_json(capsys, "info", target)
    assert code == 0
    assert data["kind"] == "rdb"


def test_json_reports_writability(capsys, stack):
    code, data = run_json(capsys, "compose", "--stack", "newapp", "--strict-parents",
                          "--dry-run", "--store", stack)
    assert code == 5
    assert data["writable"] is False
    assert data["problems"]


def test_both_commands_emit_valid_json(capsys, stack):
    for argv in (
        ("recipe", "ls"),
        ("recipe", "show", "a1200"),
        ("compose", "--recipe", "a1200", "--dry-run"),
        ("compose", "--stack", "base-os", "--dry-run"),
    ):
        _code, text = run(capsys, *argv, "--store", stack, "--json")
        json.loads(text)


# ---------------------------------------------------------------------------
# compose --format dir
# ---------------------------------------------------------------------------


def test_dir_target_writes_the_tree(capsys, stack, tmp_path):
    target = str(tmp_path / "composed")
    code, text = run(capsys, "compose", "--recipe", "a1200", "--into", target,
                     "--format", "dir", "--store", stack)
    assert code == 0
    assert "wrote" in text
    import os

    assert os.path.isfile(os.path.join(target, "Workbench", "S", "Startup-Sequence"))
    assert os.path.isfile(os.path.join(target, "Workbench", "Tools", "NewApp"))


def test_dir_target_applies_the_whiteout(capsys, stack, tmp_path):
    """The deleted library must be absent, which is deletion-by-omission working end to end."""
    import os

    target = str(tmp_path / "composed")
    run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "dir",
        "--store", stack)
    assert not os.path.exists(os.path.join(target, "Workbench", "Libs", "old.library"))


def test_no_deletions_keeps_it(capsys, stack, tmp_path):
    import os

    target = str(tmp_path / "composed")
    run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "dir",
        "--no-deletions", "--store", stack)
    assert os.path.isfile(os.path.join(target, "Workbench", "Libs", "old.library"))


def test_dir_target_writes_uaem_sidecars(capsys, stack, tmp_path):
    import os

    target = str(tmp_path / "composed")
    run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "dir",
        "--store", stack)
    sidecar = os.path.join(target, "Workbench", "S", "Startup-Sequence.uaem")
    assert os.path.isfile(sidecar)
    assert open(sidecar).read().startswith("----rwed ")


def test_no_metadata_skips_sidecars(capsys, stack, tmp_path):
    import os

    target = str(tmp_path / "composed")
    run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "dir",
        "--no-metadata", "--store", stack)
    assert not os.path.exists(
        os.path.join(target, "Workbench", "S", "Startup-Sequence.uaem")
    )


def test_dir_target_prints_the_fsuae_mount_config(capsys, stack, tmp_path):
    """A composed directory is useless until mounted, so the config is part of the output."""
    target = str(tmp_path / "composed")
    _code, text = run(capsys, "compose", "--recipe", "a1200", "--into", target,
                      "--format", "dir", "--store", stack)
    assert "hard_drive_0 =" in text
    assert "hard_drive_0_label = Workbench" in text


def test_dir_target_refuses_to_clobber_without_force(capsys, stack, tmp_path):
    target = str(tmp_path / "composed")
    run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "dir",
        "--store", stack)
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--into", target,
                  "--format", "dir", "--store", stack)
    assert code == 2


def test_dir_target_force_recomposes(capsys, stack, tmp_path):
    target = str(tmp_path / "composed")
    run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "dir",
        "--store", stack)
    code, text = run(capsys, "compose", "--recipe", "a1200", "--into", target,
                     "--format", "dir", "--force", "--store", stack)
    assert code == 0
    assert "cleared first" in text


def test_into_is_required_unless_dry_run(capsys, stack):
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--format", "dir", "--store", stack)
    assert code == 2


def test_dir_target_json_reports_what_was_written(capsys, stack, tmp_path):
    target = str(tmp_path / "composed")
    code, data = run_json(capsys, "compose", "--recipe", "a1200", "--into", target,
                          "--format", "dir", "--store", stack)
    assert code == 0
    assert data["written"]["files"] > 0
    assert data["written"]["sidecars"] > 0
    assert any(line.startswith("hard_drive_0 =") for line in data["fsuae_config"])


def test_dry_run_with_dir_format_writes_nothing(capsys, stack, tmp_path):
    import os

    target = str(tmp_path / "composed")
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--into", target,
                  "--format", "dir", "--dry-run", "--store", stack)
    assert code == 0
    assert not os.path.exists(target)


# ---------------------------------------------------------------------------
# compose --format plain
# ---------------------------------------------------------------------------


def test_plain_target_refuses_a_multi_volume_stack(capsys, stack, tmp_path):
    """The fixture stack has Workbench and Work, which cannot both fit in a plain HDF."""
    code, _ = run(capsys, "compose", "--recipe", "a1200",
                  "--into", str(tmp_path / "out.hdf"), "--format", "plain", "--store", stack)
    assert code == 2


def test_plain_target_writes_one_chosen_volume(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    code, text = run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                     "--into", target, "--format", "plain", "--store", stack)
    assert code == 0
    assert "wrote" in text
    import os

    assert os.path.isfile(target)


def test_the_composed_plain_image_passes_check(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
        "--into", target, "--format", "plain", "--store", stack)
    code, text = run(capsys, "check", target)
    assert code == 0, text
    assert "ok" in text


def test_composed_plain_image_contents_match_the_layers(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
        "--into", target, "--format", "plain", "--store", stack)

    _code, listing = run(capsys, "find", target, "--type", "f")
    assert "S/Startup-Sequence" in listing
    assert "Tools/NewApp" in listing
    # The whiteout must have taken effect.
    assert "old.library" not in listing


def test_plain_target_reports_the_image_size_and_its_source(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    _code, text = run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                      "--into", target, "--format", "plain", "--store", stack)
    assert "image size" in text


def test_plain_target_honours_an_explicit_size(capsys, stack, tmp_path):
    import os

    target = str(tmp_path / "out.hdf")
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                  "--into", target, "--format", "plain", "--size", "8M", "--store", stack)
    assert code == 0
    assert os.path.getsize(target) == 8 * 1024 * 1024


def test_plain_target_refuses_an_existing_image_without_force(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
        "--into", target, "--format", "plain", "--store", stack)
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                  "--into", target, "--format", "plain", "--store", stack)
    assert code == 2


def test_plain_target_json_reports_the_size(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    code, data = run_json(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                          "--into", target, "--format", "plain", "--store", stack)
    assert code == 0
    assert data["written"]["size_bytes"] > 0
    assert data["written"]["files"] > 0


def test_rdb_format_writes_an_image(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    code, out = run(capsys, "compose", "--recipe", "a1200",
                    "--into", target, "--format", "rdb", "--store", stack)
    assert code == 0, out
    assert os.path.exists(target)


def test_rdb_output_is_readable_as_an_rdb(capsys, stack, tmp_path):
    """The composed file has to be openable by the tool's own inspection path, not just exist."""
    target = str(tmp_path / "out.hdf")
    code, _ = run(capsys, "compose", "--recipe", "a1200",
                  "--into", target, "--format", "rdb", "--store", stack)
    assert code == 0

    code, data = run_json(capsys, "info", target)
    assert code == 0
    assert data["kind"] == "rdb"
    assert len(data["partitions"]) >= 1


def test_rdb_output_passes_check(capsys, stack, tmp_path):
    target = str(tmp_path / "out.hdf")
    code, _ = run(capsys, "compose", "--recipe", "a1200",
                  "--into", target, "--format", "rdb", "--store", stack)
    assert code == 0

    code, out = run(capsys, "check", target)
    assert code == 0, f"check found problems in the composed image:\n{out}"


def test_rdb_json_reports_what_it_wrote(capsys, stack, tmp_path):
    code, data = run_json(capsys, "compose", "--recipe", "a1200",
                          "--into", str(tmp_path / "out.hdf"), "--format", "rdb",
                          "--store", stack)
    assert code == 0
    written = data["written"]
    assert written["files"] > 0
    assert written["size_bytes"] > 0
    assert written["volumes"]


def test_rdb_refuses_an_existing_target_without_force(capsys, stack, tmp_path):
    """Whole-disk writes are the most destructive thing the tool does; --force must be explicit."""
    target = tmp_path / "out.hdf"
    target.write_bytes(b"not empty")
    code, _out, err = run_both(capsys, "compose", "--recipe", "a1200",
                               "--into", str(target), "--format", "rdb", "--store", stack)
    assert code == 2
    assert "--force" in err
    assert target.read_bytes() == b"not empty", "the existing file must be left untouched"


def test_rdb_overwrites_with_force(capsys, stack, tmp_path):
    target = tmp_path / "out.hdf"
    target.write_bytes(b"not empty")
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--force",
                  "--into", str(target), "--format", "rdb", "--store", stack)
    assert code == 0
    assert target.read_bytes() != b"not empty"


def test_rdb_dry_run_writes_nothing(capsys, stack, tmp_path):
    target = tmp_path / "out.hdf"
    code, _ = run(capsys, "compose", "--recipe", "a1200", "--dry-run",
                  "--into", str(target), "--format", "rdb", "--store", stack)
    assert code == 0
    assert not target.exists()


# ---------------------------------------------------------------------------
# Verification through the CLI
# ---------------------------------------------------------------------------


def test_verification_runs_by_default(capsys, stack, tmp_path):
    """On by default is the point: a restore you have to remember to check is one you won't."""
    code, out = run(capsys, "compose", "--recipe", "a1200",
                    "--into", str(tmp_path / "out.hdf"), "--format", "rdb", "--store", stack)
    assert code == 0
    assert "verifying" in out
    assert "verified" in out


def test_no_verify_skips_it(capsys, stack, tmp_path):
    code, out = run(capsys, "compose", "--recipe", "a1200", "--no-verify",
                    "--into", str(tmp_path / "out.hdf"), "--format", "rdb", "--store", stack)
    assert code == 0
    assert "verifying" not in out


def test_verification_runs_for_the_plain_format_too(capsys, stack, tmp_path):
    code, out = run(capsys, "compose", "--recipe", "a1200", "--volume", "Workbench",
                    "--into", str(tmp_path / "out.hdf"), "--format", "plain", "--store", stack)
    assert code == 0
    assert "verified" in out


def test_verification_is_skipped_for_a_directory_target(capsys, stack, tmp_path):
    """A directory is plain host files; re-reading it would mean writing a sidecar parser."""
    code, out = run(capsys, "compose", "--recipe", "a1200",
                    "--into", str(tmp_path / "out"), "--format", "dir", "--store", stack)
    assert code == 0
    assert "verifying" not in out


def test_json_includes_the_verdict(capsys, stack, tmp_path):
    code, data = run_json(capsys, "compose", "--recipe", "a1200",
                          "--into", str(tmp_path / "out.hdf"), "--format", "rdb",
                          "--store", stack)
    assert code == 0
    assert data["verify"]["clean"] is True
    assert data["verify"]["faults"] == 0
    assert data["verify"]["matched"] > 0


def test_a_failed_verification_exits_6_and_says_not_to_trust_it(
    capsys, stack, tmp_path, monkeypatch
):
    """Detection is tested directly elsewhere; this covers the reporting and the exit code."""
    from amibuilder.layers import compose as CP

    def dirty(plan, target, **kwargs):
        return CP.VerifyResult(
            target=target,
            volumes=[CP.VolumeVerdict(
                volume="Workbench", policy="replace",
                missing=("Workbench:S/Startup-Sequence",),
                wrong=(("Workbench:C/List", "content"),),
                matched=3,
            )],
        )

    monkeypatch.setattr(CP, "verify_written", dirty)
    code, out = run(capsys, "compose", "--recipe", "a1200",
                    "--into", str(tmp_path / "out.hdf"), "--format", "rdb", "--store", stack)
    assert code == 6
    assert "VERIFICATION FAILED" in out
    assert "do not trust this image" in out
    assert "S/Startup-Sequence" in out
    assert "C/List" in out


def test_a_failed_verification_in_json_exits_6(capsys, stack, tmp_path, monkeypatch):
    from amibuilder.layers import compose as CP

    monkeypatch.setattr(CP, "verify_written", lambda plan, target, **kw: CP.VerifyResult(
        target=target,
        volumes=[CP.VolumeVerdict(volume="Workbench", policy="replace",
                                  missing=("Workbench:gone",))],
    ))
    code, data = run_json(capsys, "compose", "--recipe", "a1200",
                          "--into", str(tmp_path / "out.hdf"), "--format", "rdb",
                          "--store", stack)
    assert code == 6
    assert data["verify"]["clean"] is False
    assert data["verify"]["faults"] == 1


def test_merge_onto_a_missing_target_does_not_claim_to_write_into_existing(
    capsys, stack, tmp_path
):
    """The action column has to describe what will actually happen.

    Composing a whole drive from scratch takes the merge path for every non-bootable volume, so
    "write into existing" was the wrong description of the *common* case, not an edge case.
    """
    code, out = run(capsys, "compose", "--recipe", "a1200", "--dry-run",
                    "--into", str(tmp_path / "absent.hdf"), "--store", stack)
    assert code == 0
    assert "write into existing" not in out


def test_merge_onto_an_existing_volume_still_says_into_existing(capsys, stack, tmp_path):
    """The other half of the distinction, so the fix did not simply delete the wording."""
    target = str(tmp_path / "present.hdf")
    assert run(capsys, "compose", "--recipe", "a1200", "--into", target, "--format", "rdb",
               "--store", stack)[0] == 0

    code, out = run(capsys, "compose", "--recipe", "a1200", "--dry-run",
                    "--policy", "Workbench=merge", "--policy", "Work=merge",
                    "--into", target, "--store", stack)
    assert code == 0
    assert "write into existing" in out
