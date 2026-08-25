"""Composition planning: flattening a stack, policies, and preflight.

Nothing here writes, and that is the point being tested — every refusal has to happen while the
target is still untouched. The behaviours that carry real weight: a whiteout removes a path by
omission rather than by deleting, last-wins is reported rather than prevented, and a naming
limit is caught for the whole plan before a single block would be written.
"""

from __future__ import annotations

import pytest

from amibuilder.errors import UsageError
from amibuilder.layers import compose as CP
from amibuilder.layers import drive as D
from amibuilder.layers import manifest as M
from amibuilder.layers import store as S


@pytest.fixture
def store(tmp_path) -> S.Store:
    st = S.Store(str(tmp_path / "store"))
    st.init()
    return st


def blob(store: S.Store, data: bytes) -> str:
    return store.blobs.put_bytes(data).hash


def fentry(store: S.Store, path: str, data: bytes = b"x") -> M.ManifestEntry:
    return M.ManifestEntry(path=path, kind=M.FILE, blob=blob(store, data), size=len(data))


def dentry(path: str) -> M.ManifestEntry:
    return M.ManifestEntry(path=path, kind=M.DIR)


def env(**over) -> dict:
    from amibuilder.image import DOS_ENV_FIELDS

    base = dict.fromkeys(DOS_ENV_FIELDS, 0)
    base.update({"block_size": 128, "surfaces": 8, "blk_per_trk": 32})
    base.update(over)
    return base


def partition(index=0, volume="Workbench", low=1, high=100, policy=D.POLICY_REPLACE,
              device=None, dos_type="0x444f5303", num_blocks=25600):
    return {
        "index": index,
        "device": device or f"DH{index}",
        "volume": volume,
        "low_cyl": low,
        "high_cyl": high,
        "policy": policy,
        "dos_type": dos_type,
        "dos_type_label": "DOS\\3",
        "filesystem": "FFS",
        "bootable": index == 0,
        "automount": True,
        "num_blocks": num_blocks,
        "num_bytes": num_blocks * 512,
        "dos_env": env(low_cyl=low, high_cyl=high),
    }


def drive_record(parts=None, cylinders=4096):
    return {
        "scheme": D.DRIVE_SCHEME,
        "kind": "rdb",
        "block_size": 512,
        "cylinders": cylinders,
        "heads": 8,
        "sectors": 32,
        "single_volume": False,
        "partitions": parts if parts is not None else [partition()],
    }


#: Distinguishes "caller said nothing, use a default record" from "caller explicitly wants no
#: drive record". Using None for both silently turned two tests vacuous.
DEFAULT_DRIVE = object()


def make_base(store: S.Store, entries, *, label="base", drive=DEFAULT_DRIVE) -> S.Layer:
    layer = store.write_layer(
        entries=entries, kind=S.KIND_BASE, label=label,
        drive=drive_record() if drive is DEFAULT_DRIVE else drive,
    )
    store.set_ref(label, layer.id)
    return layer


def make_diff(store: S.Store, entries, parent: S.Layer, *, label="diff") -> S.Layer:
    layer = store.write_layer(
        entries=entries, kind=S.KIND_DIFF, label=label, parent=parent.id
    )
    store.set_ref(label, layer.id)
    return layer


# ---------------------------------------------------------------------------
# Stack resolution
# ---------------------------------------------------------------------------


def test_resolve_stack_keeps_the_given_order(store):
    a = make_base(store, [fentry(store, "Workbench:a")], label="base")
    b = make_diff(store, [fentry(store, "Workbench:b", b"bb")], a, label="top")
    assert [layer.id for layer in CP.resolve_stack(store, ["base", "top"])] == [a.id, b.id]
    assert [layer.id for layer in CP.resolve_stack(store, ["top", "base"])] == [b.id, a.id]


def test_resolve_stack_rejects_an_empty_list(store):
    with pytest.raises(UsageError, match="at least one layer"):
        CP.resolve_stack(store, [])


def test_stack_from_recipe(store):
    a = make_base(store, [fentry(store, "Workbench:a")], label="base")
    make_diff(store, [fentry(store, "Workbench:b", b"bb")], a, label="top")
    store.write_recipe("everyday", ["base", "top"])
    assert [layer.label for layer in CP.stack_from_recipe(store, "everyday")] == ["base", "top"]


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def test_later_layer_wins(store):
    base = make_base(store, [fentry(store, "Workbench:f", b"old")])
    top = make_diff(store, [fentry(store, "Workbench:f", b"new")], base)
    resolved, _conflicts = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert resolved["workbench:f"].source == top.id
    assert store.blobs.get(resolved["workbench:f"].entry.blob) == b"new"


def test_collision_is_reported_with_the_winner(store):
    base = make_base(store, [fentry(store, "Workbench:Libs/reqtools.library", b"v1")])
    top = make_diff(store, [fentry(store, "Workbench:Libs/reqtools.library", b"v2")], base)
    _resolved, conflicts = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert len(conflicts) == 1
    assert conflicts[0].layers == (base.id, top.id)
    assert conflicts[0].winner == top.id


def test_no_conflict_when_paths_do_not_overlap(store):
    base = make_base(store, [fentry(store, "Workbench:a")])
    top = make_diff(store, [fentry(store, "Workbench:b", b"bb")], base)
    _resolved, conflicts = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert conflicts == []


def test_shadowed_sources_are_recorded(store):
    base = make_base(store, [fentry(store, "Workbench:f", b"v1")])
    mid = make_diff(store, [fentry(store, "Workbench:f", b"v2")], base, label="mid")
    top = make_diff(store, [fentry(store, "Workbench:f", b"v3")], mid, label="top")
    resolved, _ = CP.flatten([
        (base, store.read_manifest(base.id)),
        (mid, store.read_manifest(mid.id)),
        (top, store.read_manifest(top.id)),
    ])
    assert resolved["workbench:f"].shadowed == (base.id, mid.id)


def test_whiteout_removes_by_omission(store):
    """Deletion is 'never written', not 'deleted' -- which is why fresh composition is safe."""
    base = make_base(store, [fentry(store, "Workbench:Libs/old.library", b"obsolete")])
    top = make_diff(store, [M.whiteout("Workbench:Libs/old.library")], base)
    resolved, _ = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert "workbench:libs/old.library" not in resolved


def test_whiteout_on_a_directory_removes_its_subtree(store):
    """Otherwise a later layer could leave an orphan inside a removed directory."""
    base = make_base(store, [
        dentry("Workbench:Old"),
        fentry(store, "Workbench:Old/thing", b"1"),
        fentry(store, "Workbench:Old/deeper/thing", b"2"),
        fentry(store, "Workbench:Keep", b"3"),
    ])
    top = make_diff(store, [M.whiteout("Workbench:Old")], base)
    resolved, _ = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert set(resolved) == {"workbench:keep"}


def test_whiteout_does_not_remove_a_similarly_named_sibling(store):
    """'Old' must not take 'Older' with it."""
    base = make_base(store, [
        fentry(store, "Workbench:Old/thing", b"1"),
        fentry(store, "Workbench:Older/thing", b"2"),
    ])
    top = make_diff(store, [M.whiteout("Workbench:Old")], base)
    resolved, _ = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert set(resolved) == {"workbench:older/thing"}


def test_deletions_disabled_keeps_the_parents_version(store):
    base = make_base(store, [fentry(store, "Workbench:f", b"kept")])
    top = make_diff(store, [M.whiteout("Workbench:f")], base)
    resolved, _ = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))],
        deletions=False,
    )
    assert store.blobs.get(resolved["workbench:f"].entry.blob) == b"kept"


def test_a_layer_can_re_add_what_an_earlier_whiteout_removed(store):
    base = make_base(store, [fentry(store, "Workbench:f", b"v1")])
    mid = make_diff(store, [M.whiteout("Workbench:f")], base, label="mid")
    top = make_diff(store, [fentry(store, "Workbench:f", b"v3")], mid, label="top")
    resolved, _ = CP.flatten([
        (base, store.read_manifest(base.id)),
        (mid, store.read_manifest(mid.id)),
        (top, store.read_manifest(top.id)),
    ])
    assert store.blobs.get(resolved["workbench:f"].entry.blob) == b"v3"


def test_flatten_matches_case_insensitively(store):
    base = make_base(store, [fentry(store, "Workbench:Thing", b"v1")])
    top = make_diff(store, [fentry(store, "Workbench:thing", b"v2")], base)
    resolved, _ = CP.flatten(
        [(base, store.read_manifest(base.id)), (top, store.read_manifest(top.id))]
    )
    assert len(resolved) == 1
    assert store.blobs.get(resolved["workbench:thing"].entry.blob) == b"v2"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_warns_when_the_parent_is_absent(store):
    base = make_base(store, [fentry(store, "Workbench:a")], label="base")
    top = make_diff(store, [fentry(store, "Workbench:b", b"bb")], base, label="top")
    warnings = CP.check_provenance([top])
    assert len(warnings) == 1
    assert "not in this stack" in warnings[0]


def test_provenance_is_quiet_when_the_parent_is_present(store):
    base = make_base(store, [fentry(store, "Workbench:a")], label="base")
    top = make_diff(store, [fentry(store, "Workbench:b", b"bb")], base, label="top")
    assert CP.check_provenance([base, top]) == []


def test_strict_parents_turns_the_warning_into_a_refusal(store):
    base = make_base(store, [fentry(store, "Workbench:a")], label="base")
    make_diff(store, [fentry(store, "Workbench:b", b"bb")], base, label="top")
    plan = CP.build_plan(store, ["top"], strict_parents=True)
    assert not plan.is_writable
    assert any("not in this stack" in p.message for p in plan.blocking)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


def test_replace_formats_and_writes(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    vol = CP.build_plan(store, ["base"], existing_volumes=["Workbench"]).volume("Workbench")
    assert vol.policy == D.POLICY_REPLACE
    assert (vol.format_volume, vol.write) == (True, True)
    assert vol.destroys_existing_data is True


def test_merge_writes_without_formatting(store):
    drive = drive_record([partition(policy=D.POLICY_MERGE)])
    make_base(store, [fentry(store, "Workbench:a")], label="base", drive=drive)
    vol = CP.build_plan(store, ["base"]).volume("Workbench")
    assert (vol.format_volume, vol.write) == (False, True)
    assert vol.destroys_existing_data is False


def test_preserve_writes_nothing_and_formats_only_a_missing_volume(store):
    drive = drive_record([partition(policy=D.POLICY_PRESERVE, volume="Saves")])
    make_base(store, [fentry(store, "Saves:game.sav")], label="base", drive=drive)

    absent = CP.build_plan(store, ["base"]).volume("Saves")
    assert (absent.format_volume, absent.write) == (True, False)
    assert absent.entries == ()

    present = CP.build_plan(store, ["base"], existing_volumes=["Saves"]).volume("Saves")
    assert (present.format_volume, present.write) == (False, False)


def test_policy_can_be_overridden_per_volume(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    plan = CP.build_plan(store, ["base"], policies={"Workbench": D.POLICY_MERGE})
    assert plan.volume("Workbench").policy == D.POLICY_MERGE
    assert plan.volume("Workbench").format_volume is False


def test_policy_override_accepts_a_trailing_colon(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    plan = CP.build_plan(store, ["base"], policies={"Workbench:": D.POLICY_MERGE})
    assert plan.volume("Workbench").policy == D.POLICY_MERGE


def test_unknown_policy_is_rejected(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    with pytest.raises(UsageError, match="unknown policy"):
        CP.build_plan(store, ["base"], policies={"Workbench": "occasionally"})


@pytest.fixture
def three_volume_stack(store):
    drive = drive_record([
        partition(index=0, volume="Workbench", policy=D.POLICY_REPLACE, low=1, high=100),
        partition(index=1, volume="Work", policy=D.POLICY_MERGE, low=101, high=200),
        partition(index=2, volume="Saves", policy=D.POLICY_PRESERVE, low=201, high=300),
    ])
    make_base(store, [
        fentry(store, "Workbench:a"),
        fentry(store, "Work:b", b"bb"),
        fentry(store, "Saves:c", b"ccc"),
    ], label="base", drive=drive)
    return store


def test_plan_names_what_it_destroys_on_an_existing_drive(three_volume_stack):
    store = three_volume_stack
    plan = CP.build_plan(
        store, ["base"], existing_volumes=["Workbench", "Work", "Saves"]
    )
    assert plan.destroyed_volumes == ["Workbench"]
    assert plan.untouched_volumes == ["Saves"]
    assert plan.created_empty_volumes == []


def test_composing_to_a_new_target_destroys_nothing(three_volume_stack):
    """Formatting a volume that does not exist yet loses nothing, and saying otherwise is a lie.

    This is the ordinary case -- building a fresh image -- so getting it wrong would make the
    scariest line in the output fire on the safest operation.
    """
    store = three_volume_stack
    plan = CP.build_plan(store, ["base"], existing_volumes=[])
    assert plan.destroyed_volumes == []
    assert plan.volume("Workbench").format_volume is True


def test_preserve_on_a_new_drive_is_creation_not_destruction(three_volume_stack):
    """`preserve` formats a missing volume precisely so it exists; that is not destruction."""
    store = three_volume_stack
    plan = CP.build_plan(store, ["base"], existing_volumes=["Workbench", "Work"])
    saves = plan.volume("Saves")
    assert saves.format_volume is True
    assert saves.destroys_existing_data is False
    assert plan.created_empty_volumes == ["Saves"]
    assert "Saves" not in plan.destroyed_volumes


def test_replace_on_an_existing_volume_does_destroy(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    plan = CP.build_plan(store, ["base"], existing_volumes=["Workbench"])
    assert plan.volume("Workbench").destroys_existing_data is True


def test_merge_never_reports_destruction(store):
    drive = drive_record([partition(policy=D.POLICY_MERGE)])
    make_base(store, [fentry(store, "Workbench:a")], label="base", drive=drive)
    plan = CP.build_plan(store, ["base"], existing_volumes=["Workbench"])
    assert plan.destroyed_volumes == []


# ---------------------------------------------------------------------------
# Whiteouts under merge -- the case that must not be silent
# ---------------------------------------------------------------------------


def test_merge_reports_deletions_it_cannot_apply(store):
    """A merge does not delete, so silence would look like the removal had happened."""
    drive = drive_record([partition(policy=D.POLICY_MERGE)])
    base = make_base(store, [fentry(store, "Workbench:old", b"obsolete")],
                     label="base", drive=drive)
    make_diff(store, [M.whiteout("Workbench:old")], base, label="patch")

    plan = CP.build_plan(store, ["base", "patch"])
    vol = plan.volume("Workbench")
    assert vol.ineffective_whiteouts == ("Workbench:old",)
    assert any("will not take effect" in w for w in plan.warnings)


def test_replace_does_not_report_ineffective_whiteouts(store):
    """Formatting removes everything anyway, so there is nothing that fails to apply."""
    base = make_base(store, [fentry(store, "Workbench:old", b"obsolete")], label="base")
    make_diff(store, [M.whiteout("Workbench:old")], base, label="patch")
    vol = CP.build_plan(store, ["base", "patch"]).volume("Workbench")
    assert vol.ineffective_whiteouts == ()


# ---------------------------------------------------------------------------
# Preflight: names
# ---------------------------------------------------------------------------


def test_long_name_is_blocking(store):
    long_name = "x" * 31
    make_base(store, [fentry(store, f"Workbench:{long_name}")], label="base")
    plan = CP.build_plan(store, ["base"])
    assert not plan.is_writable
    assert any("limit is 30" in p.message for p in plan.blocking)


def test_a_long_component_deep_in_a_path_is_caught(store):
    make_base(store, [fentry(store, "Workbench:a/" + "y" * 40 + "/c")], label="base")
    assert not CP.build_plan(store, ["base"]).is_writable


def test_thirty_characters_is_allowed(store):
    make_base(store, [fentry(store, "Workbench:" + "x" * 30)], label="base")
    assert CP.build_plan(store, ["base"]).is_writable


def test_long_filename_volumes_allow_110(store):
    """DOS7 raises the limit, so the check must read it from the partition's DosType."""
    drive = drive_record([partition(dos_type="0x444f5307")])
    make_base(store, [fentry(store, "Workbench:" + "x" * 100)], label="base", drive=drive)
    assert CP.build_plan(store, ["base"]).is_writable


def test_long_filename_volumes_still_have_a_limit(store):
    drive = drive_record([partition(dos_type="0x444f5307")])
    make_base(store, [fentry(store, "Workbench:" + "x" * 120)], label="base", drive=drive)
    assert not CP.build_plan(store, ["base"]).is_writable


@pytest.mark.parametrize("dos_type,expected", [
    ("0x444f5301", CP.NAME_LIMIT),
    ("0x444f5303", CP.NAME_LIMIT),
    ("0x444f5305", CP.NAME_LIMIT),
    ("0x444f5306", CP.NAME_LIMIT_LNFS),
    ("0x444f5307", CP.NAME_LIMIT_LNFS),
])
def test_name_limit_per_dos_type(dos_type, expected):
    assert CP.name_limit_for(partition(dos_type=dos_type)) == expected


def test_name_limit_defaults_to_classic_without_a_partition():
    assert CP.name_limit_for(None) == CP.NAME_LIMIT


def test_over_long_comment_is_blocking(store):
    entry = M.ManifestEntry(
        path="Workbench:f", kind=M.FILE, blob=blob(store, b"x"), size=1,
        comment="c" * 80,
    )
    make_base(store, [entry], label="base")
    plan = CP.build_plan(store, ["base"])
    assert any("comment is 80" in p.message for p in plan.blocking)


def test_seventy_nine_character_comment_is_allowed(store):
    entry = M.ManifestEntry(
        path="Workbench:f", kind=M.FILE, blob=blob(store, b"x"), size=1,
        comment="c" * 79,
    )
    make_base(store, [entry], label="base")
    assert CP.build_plan(store, ["base"]).is_writable


def test_problems_name_the_offending_path(store):
    make_base(store, [fentry(store, "Workbench:" + "x" * 40)], label="base")
    problem = CP.build_plan(store, ["base"]).blocking[0]
    assert problem.path.startswith("Workbench:")


def test_every_offender_is_reported_not_just_the_first(store):
    """Preflight exists so a long run does not abort halfway; a partial report defeats that."""
    make_base(store, [
        fentry(store, "Workbench:" + "a" * 40),
        fentry(store, "Workbench:" + "b" * 40, b"2"),
        fentry(store, "Workbench:" + "c" * 40, b"3"),
    ], label="base")
    assert len(CP.build_plan(store, ["base"]).blocking) == 3


# ---------------------------------------------------------------------------
# Preflight: capacity
# ---------------------------------------------------------------------------


def test_estimate_counts_a_header_block_per_entry(store):
    entries = [CP.PlanEntry(entry=dentry("Work:d"), source="x")]
    assert CP.estimate_blocks(entries, 512) == 1


def test_estimate_rounds_file_data_up_to_whole_blocks(store):
    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=513)
    entries = [CP.PlanEntry(entry=entry, source="x")]
    assert CP.estimate_blocks(entries, 512) == 1 + 2


def test_contents_too_large_for_the_partition_is_blocking(store):
    drive = drive_record([partition(num_blocks=4)])
    big = M.ManifestEntry(
        path="Workbench:big", kind=M.FILE, blob=blob(store, b"x" * 100), size=100_000
    )
    make_base(store, [big], label="base", drive=drive)
    plan = CP.build_plan(store, ["base"])
    assert not plan.is_writable
    assert any("but the partition holds" in p.message for p in plan.blocking)


def test_a_tight_fit_is_advisory_not_blocking(store):
    drive = drive_record([partition(num_blocks=22)])
    entry = M.ManifestEntry(
        path="Workbench:f", kind=M.FILE, blob=blob(store, b"x"), size=512 * 20
    )
    make_base(store, [entry], label="base", drive=drive)
    plan = CP.build_plan(store, ["base"])
    assert plan.is_writable
    assert any(p.severity == CP.ADVISORY for p in plan.problems)


def test_capacity_is_not_checked_without_a_partition_record(store):
    make_base(store, [fentry(store, "Workbench:f")], label="base", drive=None)
    assert CP.build_plan(store, ["base"]).is_writable


# ---------------------------------------------------------------------------
# Whole-plan shape
# ---------------------------------------------------------------------------


def test_plan_groups_entries_by_volume(store):
    drive = drive_record([
        partition(index=0, volume="Workbench", low=1, high=100),
        partition(index=1, volume="Work", low=101, high=200, policy=D.POLICY_MERGE),
    ])
    make_base(store, [
        fentry(store, "Workbench:a"),
        fentry(store, "Work:b", b"bb"),
        fentry(store, "Work:c", b"ccc"),
    ], label="base", drive=drive)
    plan = CP.build_plan(store, ["base"])
    assert [v.volume for v in plan.volumes] == ["Workbench", "Work"]
    assert plan.volume("Work").file_count == 2


def test_volume_order_follows_the_drive_record(store):
    drive = drive_record([
        partition(index=0, volume="Zeta", low=1, high=100),
        partition(index=1, volume="Alpha", low=101, high=200),
    ])
    make_base(store, [fentry(store, "Zeta:a"), fentry(store, "Alpha:b", b"bb")],
              label="base", drive=drive)
    assert [v.volume for v in CP.build_plan(store, ["base"]).volumes] == ["Zeta", "Alpha"]


def test_entries_within_a_volume_are_sorted(store):
    make_base(store, [
        fentry(store, "Workbench:zeta"),
        fentry(store, "Workbench:alpha", b"2"),
    ], label="base")
    paths = [e.entry.path for e in CP.build_plan(store, ["base"]).volume("Workbench").entries]
    assert paths == ["Workbench:alpha", "Workbench:zeta"]


def test_a_volume_with_no_partition_is_warned_about(store):
    drive = drive_record([partition(volume="Workbench")])
    make_base(store, [fentry(store, "Workbench:a"), fentry(store, "Elsewhere:b", b"bb")],
              label="base", drive=drive)
    plan = CP.build_plan(store, ["base"])
    assert any("does not define" in w for w in plan.warnings)


def test_a_stack_without_a_drive_record_warns(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base", drive=None)
    plan = CP.build_plan(store, ["base"])
    assert plan.drive is None
    assert any("no layer in this stack carries a drive record" in w for w in plan.warnings)


def test_only_volumes_restricts_the_blast_radius(store):
    """Partition-granular restore: touch one volume and leave the others alone."""
    drive = drive_record([
        partition(index=0, volume="Workbench", low=1, high=100),
        partition(index=1, volume="Work", low=101, high=200),
    ])
    make_base(store, [fentry(store, "Workbench:a"), fentry(store, "Work:b", b"bb")],
              label="base", drive=drive)
    plan = CP.build_plan(store, ["base"], only_volumes=["Workbench"])
    assert [v.volume for v in plan.volumes] == ["Workbench"]


def test_a_plan_covering_nothing_is_blocking(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    plan = CP.build_plan(store, ["base"], only_volumes=["Nonexistent"])
    assert not plan.is_writable
    assert any("nothing to compose" in p.message for p in plan.blocking)


def test_plan_totals(store):
    make_base(store, [
        fentry(store, "Workbench:a", b"1234"),
        fentry(store, "Workbench:b", b"56789"),
        dentry("Workbench:d"),
    ], label="base")
    plan = CP.build_plan(store, ["base"])
    assert plan.total_files == 2
    assert plan.total_content_bytes == 9


def test_plan_json_is_complete(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    data = CP.build_plan(store, ["base"]).as_dict()
    for key in ("layers", "volumes", "total_files", "destroys", "untouched",
                "created_empty", "conflicts", "warnings", "problems", "writable"):
        assert key in data


def test_volume_lookup_rejects_an_unknown_name(store):
    make_base(store, [fentry(store, "Workbench:a")], label="base")
    with pytest.raises(UsageError, match="no volume named"):
        CP.build_plan(store, ["base"]).volume("Nope")


def test_base_layer_supplies_the_drive_record_even_when_not_first(store):
    """A stack given out of order should still find its layout."""
    base = make_base(store, [fentry(store, "Workbench:a")], label="base")
    top = make_diff(store, [fentry(store, "Workbench:b", b"bb")], base, label="top")
    plan = CP.build_plan(store, ["top", "base"])
    assert plan.drive is not None
    assert CP.base_layer([top, base]).id == base.id
