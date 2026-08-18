"""The layer store: identity, refs, candidates, recipes, integrity and gc.

The property that most needs defending is what a layer ID does and does not depend on.
Getting that wrong in either direction is expensive: too much in the hash and an identical
re-capture looks like a new layer, too little and two genuinely different layers collide.
"""

from __future__ import annotations

import json
import os

import pytest

from amibuilder.errors import ImageError, NotFoundError, UsageError
from amibuilder.layers import manifest as M
from amibuilder.layers import store as S


@pytest.fixture
def store(tmp_path) -> S.Store:
    st = S.Store(str(tmp_path / "store"))
    st.init()
    return st


def file_entry(store: S.Store, path: str, data: bytes = b"contents") -> M.ManifestEntry:
    """A file entry whose blob is really in the store, as capture would leave it."""
    result = store.blobs.put_bytes(data)
    return M.ManifestEntry(path=path, kind=M.FILE, blob=result.hash, size=len(data))


def simple_entries(store: S.Store) -> list[M.ManifestEntry]:
    return [
        file_entry(store, "Workbench:C/List", b"list command"),
        M.ManifestEntry(path="Workbench:Libs", kind=M.DIR),
    ]


DRIVE = {
    "block_size": 512,
    "cylinders": 1024,
    "heads": 8,
    "sectors": 32,
    "partitions": [{"name": "DH0", "volume": "Workbench", "low_cyl": 1, "high_cyl": 1000}],
}


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_init_creates_the_expected_layout(tmp_path):
    st = S.Store(str(tmp_path / "s"))
    assert st.exists is False
    st.init()
    for sub in ("blobs", "layers", "refs", "recipes", "candidates"):
        assert os.path.isdir(os.path.join(st.root, sub))
    assert st.exists is True


def test_reading_an_absent_store_is_not_an_error(tmp_path):
    st = S.Store(str(tmp_path / "nothing"))
    assert st.list_layers() == []
    assert list(st.iter_refs()) == []
    assert list(st.iter_recipes()) == []
    assert list(st.iter_candidates()) == []


def test_default_store_path_honours_the_environment(monkeypatch):
    monkeypatch.setenv(S.STORE_ENV_VAR, "/tmp/some-store")
    assert S.default_store_path() == "/tmp/some-store"


def test_default_store_path_expands_home(monkeypatch):
    monkeypatch.delenv(S.STORE_ENV_VAR, raising=False)
    assert S.default_store_path().startswith(os.path.expanduser("~"))


# ---------------------------------------------------------------------------
# Layer identity -- what the hash covers
# ---------------------------------------------------------------------------


def test_write_layer_returns_a_hashed_id_and_materialises_the_files(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="base")
    assert len(layer.id) == 64
    assert os.path.isfile(os.path.join(store.layer_dir(layer.id), "layer.json"))
    assert os.path.isfile(os.path.join(store.layer_dir(layer.id), "manifest.jsonl"))


def test_identical_content_is_idempotent(store):
    first = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="a")
    second = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="a")
    assert first.id == second.id
    assert len(store.list_layers()) == 1


def test_label_and_creation_time_do_not_affect_identity(store):
    """Re-capturing the same content under a different name must not fork the store."""
    a = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="first")
    b = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="second")
    assert a.id == b.id


def test_source_path_does_not_affect_identity(store):
    a = store.write_layer(
        entries=simple_entries(store), kind=S.KIND_BASE, source={"path": "/one.hdf"}
    )
    b = store.write_layer(
        entries=simple_entries(store), kind=S.KIND_BASE, source={"path": "/two.hdf"}
    )
    assert a.id == b.id


def test_parent_affects_identity(store):
    """The same files mean something different composed onto a different base."""
    a = store.write_layer(entries=simple_entries(store), kind=S.KIND_DIFF, parent="a" * 64)
    b = store.write_layer(entries=simple_entries(store), kind=S.KIND_DIFF, parent="b" * 64)
    assert a.id != b.id


def test_kind_affects_identity(store):
    a = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    b = store.write_layer(entries=simple_entries(store), kind=S.KIND_DIFF)
    assert a.id != b.id


def test_drive_record_affects_identity(store):
    """A base layer's whole point is carrying the drive layout, so it must be in the hash."""
    a = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, drive=DRIVE)
    other = dict(DRIVE, cylinders=2048)
    b = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, drive=other)
    assert a.id != b.id


def test_entry_order_does_not_affect_identity(store):
    entries = simple_entries(store)
    a = store.write_layer(entries=entries, kind=S.KIND_BASE)
    b = store.write_layer(entries=list(reversed(entries)), kind=S.KIND_BASE)
    assert a.id == b.id


def test_unknown_kind_rejected(store):
    with pytest.raises(UsageError, match="unknown layer kind"):
        S.compute_layer_id(manifest_bytes=b"", kind="middling", parent=None, drive=None)


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def test_layer_round_trips_through_json(store):
    written = store.write_layer(
        entries=simple_entries(store),
        kind=S.KIND_BASE,
        label="base-os",
        source={"kind": "rdb-hdf", "path": "wb.hdf", "size": 123},
        drive=DRIVE,
    )
    read = store.read_layer(written.id)
    assert read == written
    assert read.is_base is True
    assert read.drive == DRIVE


def test_stats_are_recorded_so_listing_need_not_read_manifests(store):
    entries = simple_entries(store) + [M.whiteout("Workbench:gone")]
    layer = store.write_layer(entries=entries, kind=S.KIND_DIFF, parent=None)
    assert layer.stats.entries == 3
    assert layer.stats.files == 1
    assert layer.stats.dirs == 1
    assert layer.stats.whiteouts == 1
    assert layer.stats.content_size == len(b"list command")
    assert layer.stats.stored_size > 0


def test_stats_count_shared_content_once(store):
    """Two paths with identical content occupy that content once."""
    shared = store.blobs.put_bytes(b"same bytes").hash
    entries = [
        M.ManifestEntry(path="Work:a", kind=M.FILE, blob=shared, size=10),
        M.ManifestEntry(path="Work:b", kind=M.FILE, blob=shared, size=10),
    ]
    layer = store.write_layer(entries=entries, kind=S.KIND_BASE)
    assert layer.stats.stored_size == store.blobs.stored_size(shared)


def test_read_manifest_returns_sorted_entries(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    paths = [e.path for e in store.read_manifest(layer.id)]
    assert paths == sorted(paths, key=M.fold)


def test_iter_manifest_streams(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    assert len(list(store.iter_manifest(layer.id))) == 2


def test_read_layer_rejects_a_mismatched_id(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    path = os.path.join(store.layer_dir(layer.id), "layer.json")
    data = json.load(open(path))
    data["id"] = "f" * 64
    json.dump(data, open(path, "w"))
    with pytest.raises(ImageError, match="store is inconsistent"):
        store.read_layer(layer.id)


def test_read_missing_layer_raises_not_found(store):
    with pytest.raises(NotFoundError, match="no such layer"):
        store.read_layer("e" * 64)


def test_corrupt_layer_json_reports_clearly(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    with open(os.path.join(store.layer_dir(layer.id), "layer.json"), "w") as fh:
        fh.write("{ not json")
    with pytest.raises(ImageError, match="not valid JSON"):
        store.read_layer(layer.id)


# ---------------------------------------------------------------------------
# Labels and refs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["base", "base-os-3.2.3", "games_common", "v1.2+patch", "a"])
def test_valid_labels_accepted(good):
    assert S.check_label(good) == good


@pytest.mark.parametrize(
    "bad", ["", "-leading", ".hidden", "has space", "has/slash", "x" * 65, "café"]
)
def test_invalid_labels_rejected(bad):
    with pytest.raises(UsageError, match="invalid label"):
        S.check_label(bad)


def test_label_that_looks_like_an_id_is_rejected():
    """Otherwise `resolve` would have a genuine ambiguity to guess at."""
    with pytest.raises(UsageError, match="looks like a layer ID"):
        S.check_label("a" * 64)


def test_ref_set_get_delete(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("base-os", layer.id)
    assert store.get_ref("base-os") == layer.id
    assert store.delete_ref("base-os") is True
    assert store.get_ref("base-os") is None
    assert store.delete_ref("base-os") is False


def test_ref_cannot_point_at_an_unknown_layer(store):
    with pytest.raises(NotFoundError, match="unknown layer"):
        store.set_ref("dangling", "c" * 64)


def test_a_layer_can_carry_several_refs(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("stable", layer.id)
    store.set_ref("latest", layer.id)
    assert sorted(store.refs_for(layer.id)) == ["latest", "stable"]


def test_moving_a_ref_overwrites_it(store):
    a = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    b = store.write_layer(entries=simple_entries(store), kind=S.KIND_DIFF)
    store.set_ref("current", a.id)
    store.set_ref("current", b.id)
    assert store.get_ref("current") == b.id


def test_garbage_in_a_ref_file_is_reported(store):
    store.init()
    with open(os.path.join(store.refs_root, "broken"), "w") as fh:
        fh.write("not-a-hash\n")
    with pytest.raises(ImageError, match="does not contain a layer id"):
        store.get_ref("broken")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_by_ref(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("base-os", layer.id)
    assert store.resolve("base-os") == layer.id


def test_resolve_by_full_id(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    assert store.resolve(layer.id) == layer.id


def test_resolve_by_unique_prefix(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    assert store.resolve(layer.id[:10]) == layer.id


def test_resolve_rejects_too_short_a_prefix(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    with pytest.raises(NotFoundError):
        store.resolve(layer.id[:2])


def test_resolve_reports_an_ambiguous_prefix(store, monkeypatch):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    # Fake a sibling sharing the prefix, which is the situation the message exists for.
    twin = layer.id[:6] + ("0" if layer.id[6] != "0" else "1") + layer.id[7:]
    os.makedirs(store.layer_dir(twin), exist_ok=True)
    import shutil as _sh

    _sh.copy(
        os.path.join(store.layer_dir(layer.id), "manifest.jsonl"),
        os.path.join(store.layer_dir(twin), "manifest.jsonl"),
    )
    data = json.load(open(os.path.join(store.layer_dir(layer.id), "layer.json")))
    data["id"] = twin
    json.dump(data, open(os.path.join(store.layer_dir(twin), "layer.json"), "w"))

    with pytest.raises(UsageError, match="ambiguous"):
        store.resolve(layer.id[:6])


def test_resolve_unknown_spec(store):
    with pytest.raises(NotFoundError, match="no layer, ref or id prefix"):
        store.resolve("nope")


def test_resolve_empty_spec(store):
    with pytest.raises(UsageError, match="no layer specified"):
        store.resolve("")


def test_resolve_reports_a_ref_pointing_at_a_missing_layer(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("orphan", layer.id)
    import shutil as _sh

    _sh.rmtree(store.layer_dir(layer.id))
    with pytest.raises(ImageError, match="store is inconsistent"):
        store.resolve("orphan")


# ---------------------------------------------------------------------------
# Candidates and the review gate
# ---------------------------------------------------------------------------


def test_candidate_is_not_a_layer_until_committed(store):
    store.write_candidate("games", entries=simple_entries(store), kind=S.KIND_DIFF)
    assert store.has_candidate("games") is True
    assert list(store.iter_candidates()) == ["games"]
    assert store.list_layers() == []


def test_candidate_round_trips(store):
    entries = simple_entries(store)
    store.write_candidate("games", entries=entries, kind=S.KIND_DIFF, parent="a" * 64)
    layer, read = store.read_candidate("games")
    assert layer.kind == S.KIND_DIFF
    assert layer.parent == "a" * 64
    assert read == M.sort_entries(entries)


def test_missing_candidate_message_points_at_snap_diff(store):
    with pytest.raises(NotFoundError, match="snap diff"):
        store.read_candidate("absent")


def test_update_candidate_replaces_the_manifest_and_keeps_metadata(store):
    """This is what `snap review --drop` does."""
    entries = simple_entries(store)
    store.write_candidate("games", entries=entries, kind=S.KIND_DIFF, parent="a" * 64, drive=DRIVE)
    store.update_candidate("games", entries[:1])
    layer, read = store.read_candidate("games")
    assert len(read) == 1
    assert layer.parent == "a" * 64
    assert layer.drive == DRIVE


def test_rewriting_a_candidate_does_not_accumulate(store):
    store.write_candidate("games", entries=simple_entries(store), kind=S.KIND_DIFF)
    store.write_candidate("games", entries=simple_entries(store)[:1], kind=S.KIND_DIFF)
    _layer, read = store.read_candidate("games")
    assert len(read) == 1


def test_commit_creates_the_layer_and_the_ref_and_clears_the_candidate(store):
    store.write_candidate("games", entries=simple_entries(store), kind=S.KIND_DIFF)
    layer = store.commit_candidate("games")
    assert store.has_layer(layer.id)
    assert store.get_ref("games") == layer.id
    assert store.has_candidate("games") is False


def test_commit_can_use_a_different_ref_name(store):
    store.write_candidate("wip", entries=simple_entries(store), kind=S.KIND_DIFF)
    layer = store.commit_candidate("wip", ref="games-common")
    assert store.get_ref("games-common") == layer.id
    assert store.get_ref("wip") is None


def test_delete_candidate(store):
    store.write_candidate("games", entries=simple_entries(store), kind=S.KIND_DIFF)
    assert store.delete_candidate("games") is True
    assert store.delete_candidate("games") is False


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def test_recipe_round_trips(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("base-os", layer.id)
    store.write_recipe("a1200", ["base-os"], description="everyday setup")
    recipe = store.read_recipe("a1200")
    assert recipe["layers"] == ["base-os"]
    assert recipe["description"] == "everyday setup"
    assert list(store.iter_recipes()) == ["a1200"]


def test_recipe_stores_ref_names_so_it_follows_a_moving_label(store):
    a = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("base-os", a.id)
    store.write_recipe("a1200", ["base-os"])
    b = store.write_layer(entries=simple_entries(store)[:1], kind=S.KIND_BASE)
    store.set_ref("base-os", b.id)
    assert store.read_recipe("a1200")["layers"] == ["base-os"]
    assert store.resolve(store.read_recipe("a1200")["layers"][0]) == b.id


def test_recipe_rejects_an_unknown_layer_at_write_time(store):
    with pytest.raises(NotFoundError):
        store.write_recipe("bad", ["does-not-exist"])


def test_recipe_rejects_an_empty_layer_list(store):
    with pytest.raises(UsageError, match="at least one layer"):
        store.write_recipe("empty", [])


def test_delete_recipe(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("base-os", layer.id)
    store.write_recipe("a1200", ["base-os"])
    assert store.delete_recipe("a1200") is True
    assert store.delete_recipe("a1200") is False


def test_read_missing_recipe(store):
    with pytest.raises(NotFoundError, match="no recipe named"):
        store.read_recipe("ghost")


# ---------------------------------------------------------------------------
# Ancestry
# ---------------------------------------------------------------------------


def test_chain_returns_base_first(store):
    base = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="base")
    mid = store.write_layer(
        entries=[file_entry(store, "Work:mid", b"m")], kind=S.KIND_DIFF, parent=base.id, label="mid"
    )
    top = store.write_layer(
        entries=[file_entry(store, "Work:top", b"t")], kind=S.KIND_DIFF, parent=mid.id, label="top"
    )
    assert [layer.label for layer in store.chain(top.id)] == ["base", "mid", "top"]


def test_chain_detects_a_cycle(store):
    """Impossible to create through the API, but a hand-edited store is a real thing."""
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    path = os.path.join(store.layer_dir(layer.id), "layer.json")
    data = json.load(open(path))
    data["parent"] = layer.id
    json.dump(data, open(path, "w"))
    with pytest.raises(ImageError, match="cycle"):
        store.chain(layer.id)


def test_children_finds_dependents(store):
    base = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    child = store.write_layer(
        entries=[file_entry(store, "Work:c", b"c")], kind=S.KIND_DIFF, parent=base.id
    )
    assert [layer.id for layer in store.children(base.id)] == [child.id]


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_verify_clean_layer_reports_nothing(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    assert store.verify_layer(layer.id) == []


def test_verify_detects_an_edited_manifest(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    with open(os.path.join(store.layer_dir(layer.id), "manifest.jsonl"), "ab") as fh:
        fh.write(b'{"p":"Work:sneaky","t":"d"}\n')
    problems = store.verify_layer(layer.id)
    assert any("does not match the layer id" in p for p in problems)


def test_verify_detects_a_missing_blob(store):
    entries = simple_entries(store)
    layer = store.write_layer(entries=entries, kind=S.KIND_BASE)
    store.blobs.delete(entries[0].blob)
    assert any("blob missing" in p for p in store.verify_layer(layer.id))


def test_verify_detects_a_corrupt_blob(store):
    data = b"\x00\xff" * 5000  # compresses, so tampering breaks decompression or the hash
    entry = file_entry(store, "Work:f", data)
    layer = store.write_layer(entries=[entry], kind=S.KIND_BASE)
    path, _ = store.blobs.find(entry.blob)
    with open(path, "r+b") as fh:
        fh.seek(0)
        fh.write(b"\x00\x00\x00\x00")
    assert any("blob corrupt" in p for p in store.verify_layer(layer.id))


def test_verify_detects_a_missing_parent(store):
    base = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    child = store.write_layer(
        entries=[file_entry(store, "Work:c", b"c")], kind=S.KIND_DIFF, parent=base.id
    )
    import shutil as _sh

    _sh.rmtree(store.layer_dir(base.id))
    assert any("parent layer" in p for p in store.verify_layer(child.id))


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------


def test_gc_keeps_referenced_blobs(store):
    entries = simple_entries(store)
    store.write_layer(entries=entries, kind=S.KIND_BASE)
    count, freed = store.gc()
    assert (count, freed) == (0, 0)
    assert store.blobs.has(entries[0].blob)


def test_gc_removes_unreferenced_blobs(store):
    store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    orphan = store.blobs.put_bytes(b"nobody references this")
    count, freed = store.gc()
    assert count == 1
    assert freed > 0
    assert store.blobs.has(orphan.hash) is False


def test_gc_treats_candidates_as_roots(store):
    """A candidate is unreviewed work; reclaiming its blobs would silently empty it."""
    entries = simple_entries(store)
    store.write_candidate("wip", entries=entries, kind=S.KIND_DIFF)
    count, _freed = store.gc()
    assert count == 0
    assert store.blobs.has(entries[0].blob)


def test_gc_dry_run_reports_without_deleting(store):
    orphan = store.blobs.put_bytes(b"orphan")
    count, freed = store.gc(dry_run=True)
    assert count == 1 and freed > 0
    assert store.blobs.has(orphan.hash) is True


def test_gc_clears_interrupted_writes(store):
    os.makedirs(store.blobs.root, exist_ok=True)
    open(os.path.join(store.blobs.root, ".put-xyz.part"), "w").close()
    store.gc()
    assert not os.path.exists(os.path.join(store.blobs.root, ".put-xyz.part"))


def test_unreferenced_blobs_lists_orphans(store):
    store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    orphan = store.blobs.put_bytes(b"orphan")
    assert store.unreferenced_blobs() == [orphan.hash]


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def test_remove_layer_deletes_it_and_its_refs(store):
    layer = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.set_ref("base-os", layer.id)
    removed = store.remove_layer(layer.id)
    assert removed == ["base-os"]
    assert store.has_layer(layer.id) is False
    assert store.get_ref("base-os") is None


def test_remove_layer_refuses_while_a_child_depends_on_it(store):
    base = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE, label="base")
    store.write_layer(
        entries=[file_entry(store, "Work:c", b"c")],
        kind=S.KIND_DIFF,
        parent=base.id,
        label="child",
    )
    with pytest.raises(UsageError, match="parent of 1"):
        store.remove_layer(base.id)


def test_remove_layer_force_overrides(store):
    base = store.write_layer(entries=simple_entries(store), kind=S.KIND_BASE)
    store.write_layer(
        entries=[file_entry(store, "Work:c", b"c")], kind=S.KIND_DIFF, parent=base.id
    )
    store.remove_layer(base.id, force=True)
    assert store.has_layer(base.id) is False


def test_removing_a_layer_leaves_its_blobs_for_gc(store):
    """Removal is cheap and reversible-ish; reclaiming space is a separate, explicit step."""
    entries = simple_entries(store)
    layer = store.write_layer(entries=entries, kind=S.KIND_BASE)
    store.remove_layer(layer.id)
    assert store.blobs.has(entries[0].blob) is True
    assert store.gc()[0] >= 1
