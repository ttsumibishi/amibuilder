"""The `recipe` and `compose` commands through the real CLI.

`compose` has no write path yet, so these tests are mostly about whether it reports the
consequences accurately — particularly the destroys/untouched/creates-empty distinction, which
is the line a person will actually read before deciding to run it for real.
"""

from __future__ import annotations

import json

import pytest

from amibuilder.cli import main
from helpers import images


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


def test_writing_is_refused_clearly_while_unimplemented(capsys, stack, tmp_path):
    """Better an explicit refusal than a partial write that cannot finish."""
    code, _ = run(capsys, "compose", "--recipe", "a1200",
                  "--into", str(tmp_path / "out.hdf"), "--store", stack)
    assert code == 4  # UnsupportedError


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


@pytest.mark.parametrize("fmt", ["rdb", "plain"])
def test_image_formats_still_refuse_clearly(capsys, stack, tmp_path, fmt):
    code, _ = run(capsys, "compose", "--recipe", "a1200",
                  "--into", str(tmp_path / f"out.{fmt}"), "--format", fmt, "--store", stack)
    assert code == 4


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
