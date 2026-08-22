"""Recorded policy intent: a per-volume compose policy stored in a recipe.

A save-games volume looks exactly like a work volume from outside, so `preserve` can only ever
be stated, never inferred. Recording it in the recipe -- rewritable, and outside the layer
identity hash where the drive record's policy lives -- lets `compose --recipe` apply it without
the user re-typing `--policy` every time. Precedence is CLI `--policy` > recipe > the drive
record's default > merge, and the one failure that must never be silent is a stated `preserve`
that does not apply.

Nothing here writes an image; the build_plan tests are pure planning and the CLI tests dry-run.
"""

from __future__ import annotations

import json

import pytest

from amibuilder.cli import main
from amibuilder.errors import UsageError
from amibuilder.layers import compose as CP
from amibuilder.layers import drive as D

from test_layers_compose_plan import (  # reuse the plan fixtures
    drive_record,
    fentry,
    make_base,
    partition,
    store,
)


# ---------------------------------------------------------------------------
# drive.parse_policies -- shared by compose --policy and recipe new --policy
# ---------------------------------------------------------------------------


def test_parse_policies_reads_pairs():
    assert D.parse_policies(["Work=preserve", "Games=merge"]) == {
        "Work": "preserve", "Games": "merge"
    }


def test_parse_policies_lowercases_and_strips():
    assert D.parse_policies([" Work = PRESERVE "]) == {"Work": "preserve"}


def test_parse_policies_is_empty_for_none():
    assert D.parse_policies(None) == {}


@pytest.mark.parametrize("bad", ["Work", "=preserve", "Work=", "Work=nonsense"])
def test_parse_policies_rejects_malformed(bad):
    with pytest.raises(UsageError):
        D.parse_policies([bad])


# ---------------------------------------------------------------------------
# store: the recipe carries policies
# ---------------------------------------------------------------------------


def _two_partition_base(store):
    drive = drive_record([
        partition(index=0, volume="Workbench", low=1, high=100, policy=D.POLICY_REPLACE),
        partition(index=1, volume="Work", low=101, high=200, policy=D.POLICY_MERGE),
    ])
    make_base(
        store,
        [fentry(store, "Workbench:a"), fentry(store, "Work:b")],
        label="base",
        drive=drive,
    )


def test_recipe_round_trips_policies(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    store.write_recipe("r", ["base"], policies={"Work": "preserve"})
    assert store.read_recipe("r")["policies"] == {"Work": "preserve"}


def test_recipe_without_policies_records_an_empty_map(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    store.write_recipe("r", ["base"])
    assert store.read_recipe("r")["policies"] == {}


# ---------------------------------------------------------------------------
# build_plan: a policies map overrides the recorded default, and is flagged as stated
# ---------------------------------------------------------------------------


def test_recorded_default_is_used_when_no_policy_is_given(store):
    _two_partition_base(store)
    plan = CP.build_plan(store, ["base"])
    assert plan.volume("Workbench").policy == D.POLICY_REPLACE
    assert plan.volume("Work").policy == D.POLICY_MERGE
    # Nothing was stated, so neither volume is flagged.
    assert not plan.volume("Workbench").policy_stated
    assert not plan.volume("Work").policy_stated


def test_a_policy_overrides_the_recorded_default_and_is_flagged(store):
    _two_partition_base(store)
    plan = CP.build_plan(store, ["base"], policies={"Work": "preserve"})
    work = plan.volume("Work")
    assert work.policy == D.POLICY_PRESERVE
    assert work.policy_stated is True
    # The volume that was not named keeps its recorded default and stays unflagged.
    assert plan.volume("Workbench").policy == D.POLICY_REPLACE
    assert plan.volume("Workbench").policy_stated is False


def test_a_stated_policy_for_an_absent_volume_warns(store):
    _two_partition_base(store)
    plan = CP.build_plan(store, ["base"], policies={"Saves": "preserve"})
    assert any("saves" in w.lower() and "not applied" in w for w in plan.warnings)


def test_a_matched_policy_produces_no_unapplied_warning(store):
    _two_partition_base(store)
    plan = CP.build_plan(store, ["base"], policies={"Work": "preserve"})
    assert not any("not applied" in w for w in plan.warnings)


def test_policy_matching_is_case_insensitive_like_ffs(store):
    _two_partition_base(store)
    plan = CP.build_plan(store, ["base"], policies={"work": "preserve"})
    assert plan.volume("Work").policy == D.POLICY_PRESERVE
    assert not any("not applied" in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# The CLI: recipe new --policy, and compose --recipe applying it
# ---------------------------------------------------------------------------


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def run_json(capsys, *argv: str):
    code, text = run(capsys, *argv, "--json")
    assert code in (0, 6), text
    return code, json.loads(text)


@pytest.fixture
def store_with_base(store):
    """A store holding a two-partition base layer, plus its path for --store."""
    _two_partition_base(store)
    return store


def _volume(data, name):
    return next(v for v in data["volumes"] if v["volume"] == name)


def test_recipe_new_records_the_policy(capsys, store_with_base):
    st = store_with_base.root
    code, _ = run(capsys, "recipe", "new", "r", "--layers", "base",
                  "--policy", "Work=preserve", "--store", st)
    assert code == 0
    _c, data = run_json(capsys, "recipe", "show", "r", "--store", st)
    assert data["policies"] == {"Work": "preserve"}


def test_recipe_new_rejects_a_bad_policy(capsys, store_with_base):
    st = store_with_base.root
    code, _ = run(capsys, "recipe", "new", "r", "--layers", "base",
                  "--policy", "Work=bogus", "--store", st)
    assert code == 2


def test_compose_applies_a_recipe_policy(capsys, store_with_base):
    st = store_with_base.root
    run(capsys, "recipe", "new", "r", "--layers", "base", "--policy", "Work=preserve",
        "--store", st)
    _c, data = run_json(capsys, "compose", "--recipe", "r", "--dry-run", "--format", "dir",
                        "--store", st)
    work = _volume(data, "Work")
    assert work["policy"] == "preserve"
    assert work["policy_stated"] is True
    assert _volume(data, "Workbench")["policy"] == "replace"  # recorded default, untouched


def test_cli_policy_overrides_the_recipe(capsys, store_with_base):
    st = store_with_base.root
    run(capsys, "recipe", "new", "r", "--layers", "base", "--policy", "Work=preserve",
        "--store", st)
    _c, data = run_json(capsys, "compose", "--recipe", "r", "--policy", "Work=replace",
                        "--dry-run", "--format", "dir", "--store", st)
    assert _volume(data, "Work")["policy"] == "replace"


def test_compose_warns_when_a_recipe_policy_matches_no_volume(capsys, store_with_base):
    st = store_with_base.root
    run(capsys, "recipe", "new", "r", "--layers", "base", "--policy", "Saves=preserve",
        "--store", st)
    _c, data = run_json(capsys, "compose", "--recipe", "r", "--dry-run", "--format", "dir",
                        "--store", st)
    assert any("not applied" in w for w in data["warnings"])


def test_a_recipe_predating_this_feature_still_composes(capsys, store_with_base):
    """A recipe written before policies existed has no 'policies' key; it must be tolerated."""
    st = store_with_base.root
    # Write a recipe file by hand, omitting the policies key entirely.
    import os
    os.makedirs(store_with_base.recipes_root, exist_ok=True)
    with open(os.path.join(store_with_base.recipes_root, "old.json"), "w") as fh:
        json.dump({"name": "old", "layers": ["base"], "description": "",
                   "created": "2020-01-01T00:00:00Z"}, fh)
    _c, data = run_json(capsys, "compose", "--recipe", "old", "--dry-run", "--format", "dir",
                        "--store", st)
    # Falls back to recorded defaults; nothing stated, nothing unapplied.
    assert _volume(data, "Work")["policy"] == "merge"
    assert not any("not applied" in w for w in data["warnings"])
