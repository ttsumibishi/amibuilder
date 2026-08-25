"""The `snap` command family, driven through the real CLI entry point.

These are the tests that would catch a wiring mistake the unit tests cannot see: an argument
that never reaches its handler, a `--json` payload that is not valid JSON, an exit code that
does not distinguish "worked" from "found problems".
"""

from __future__ import annotations

import json

import pytest
from helpers import images

from amibuilder.cli import main


@pytest.fixture
def store_dir(tmp_path) -> str:
    return str(tmp_path / "store")


@pytest.fixture
def base_image(workdir) -> str:
    """A two-partition RDB with a small Workbench tree, plus noise under T/."""
    path = images.make_rdb_hdf(
        str(workdir / "base.hdf"),
        size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(
        path,
        {
            "S/Startup-Sequence": b"C:SetPatch QUIET\n",
            "C/List": bytes(range(256)) * 4,
            "Libs/thing.library": bytes(900),
            "T/scratch": b"temporary junk",
        },
        part=0,
    )
    return path


@pytest.fixture
def rebuilt_image(workdir) -> str:
    """Byte-for-byte the same *contents* as `base_image`, built separately.

    Deliberately a second build rather than the same file: two builds are seconds apart, so
    their Amiga datestamps differ at tick resolution. Diffing this against a layer captured
    from `base_image` is therefore a real test of the timestamp decision, where diffing the
    original file against itself would pass for the trivial reason that nothing moved.
    """
    path = images.make_rdb_hdf(
        str(workdir / "rebuilt.hdf"),
        size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(
        path,
        {
            "S/Startup-Sequence": b"C:SetPatch QUIET\n",
            "C/List": bytes(range(256)) * 4,
            "Libs/thing.library": bytes(900),
            "T/scratch": b"temporary junk",
        },
        part=0,
    )
    return path


@pytest.fixture
def changed_image(workdir) -> str:
    """The same drive after 'installing' something: one edit, two new files, new noise."""
    path = images.make_rdb_hdf(
        str(workdir / "later.hdf"),
        size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(
        path,
        {
            "S/Startup-Sequence": b"C:SetPatch QUIET\n",
            "C/List": bytes(range(256)) * 4,
            "Libs/thing.library": bytes(900) + b"patched",
            "Tools/NewApp": b"a freshly installed application" * 20,
            "Tools/NewApp.info": bytes(500),
            "T/scratch": b"different junk",
        },
        part=0,
    )
    return path


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def run_json(capsys, *argv: str):
    code, text = run(capsys, *argv, "--json")
    assert code in (0, 6), text
    return code, json.loads(text)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_makes_a_base_layer(capsys, base_image, store_dir):
    code, text = run(capsys, "snap", "create", base_image, "--label", "base-os",
                     "--store", store_dir)
    assert code == 0
    assert "created base layer" in text
    assert "base-os" in text


def test_create_reports_the_drive_layout(capsys, base_image, store_dir):
    """The values whose wrong defaults corrupt data should be visible without --json."""
    _code, text = run(capsys, "snap", "create", base_image, "--label", "base-os",
                      "--store", store_dir)
    assert "Workbench:" in text
    assert "mask=0x" in text
    assert "policy=replace" in text
    assert "bootable" in text


def test_create_excludes_temporary_paths_by_default(capsys, base_image, store_dir):
    run(capsys, "snap", "create", base_image, "--label", "base-os", "--store", store_dir)
    _code, data = run_json(capsys, "snap", "show", "base-os", "--files",
                           "--store", store_dir)
    paths = {e["p"] for e in data["entries"]}
    assert "Workbench:S/Startup-Sequence" in paths
    assert not any(p.startswith("Workbench:T") for p in paths)


def test_no_default_excludes_captures_everything(capsys, base_image, store_dir):
    run(capsys, "snap", "create", base_image, "--label", "everything",
        "--no-default-excludes", "--store", store_dir)
    _code, data = run_json(capsys, "snap", "show", "everything", "--files",
                           "--store", store_dir)
    paths = {e["p"] for e in data["entries"]}
    assert "Workbench:T/scratch" in paths


def test_exclude_is_repeatable(capsys, base_image, store_dir):
    run(capsys, "snap", "create", base_image, "--label", "trimmed",
        "--exclude", "**/Libs/**", "--exclude", "**/C/**", "--store", store_dir)
    _code, data = run_json(capsys, "snap", "show", "trimmed", "--files",
                           "--store", store_dir)
    paths = {e["p"] for e in data["entries"]}
    assert not any("Libs" in p or ":C" in p for p in paths)
    assert "Workbench:S/Startup-Sequence" in paths


def test_create_records_the_full_dosenvec_in_json(capsys, base_image, store_dir):
    run(capsys, "snap", "create", base_image, "--label", "base-os", "--store", store_dir)
    _code, data = run_json(capsys, "snap", "show", "base-os", "--store", store_dir)
    env = data["layer"]["drive"]["partitions"][0]["dos_env"]
    assert env["mask"] > 0 and env["max_transfer"] > 0
    assert "pre_alloc" in env and "surfaces" in env


def test_create_rejects_a_bad_label(capsys, base_image, store_dir):
    code, _ = run(capsys, "snap", "create", base_image, "--label", "has space",
                  "--store", store_dir)
    assert code == 2


def test_create_is_idempotent(capsys, base_image, store_dir):
    """Re-capturing identical content must not fork the store."""
    run(capsys, "snap", "create", base_image, "--label", "base-os", "--store", store_dir)
    run(capsys, "snap", "create", base_image, "--label", "again", "--store", store_dir)
    _code, data = run_json(capsys, "snap", "ls", "--store", store_dir)
    assert len(data) == 1
    assert sorted(data[0]["refs"]) == ["again", "base-os"]


# ---------------------------------------------------------------------------
# diff / review / commit
# ---------------------------------------------------------------------------


@pytest.fixture
def with_base(capsys, base_image, store_dir) -> str:
    run(capsys, "snap", "create", base_image, "--label", "base-os", "--store", store_dir)
    return store_dir


def test_diff_finds_the_install(capsys, with_base, changed_image):
    code, data = run_json(capsys, "snap", "diff", changed_image, "--parent", "base-os",
                          "--label", "newapp", "--store", with_base)
    assert code == 0
    paths = {c["path"] for c in data["changes"]}
    assert "Workbench:Tools/NewApp" in paths
    assert "Workbench:Libs/thing.library" in paths
    assert data["by_reason"]["new"] == 3   # Tools/, NewApp, NewApp.info
    assert data["by_reason"]["content"] == 1


def test_diff_ignores_unchanged_files(capsys, with_base, changed_image):
    _code, data = run_json(capsys, "snap", "diff", changed_image, "--parent", "base-os",
                           "--label", "newapp", "--store", with_base)
    paths = {c["path"] for c in data["changes"]}
    assert "Workbench:S/Startup-Sequence" not in paths
    assert data["unchanged"] >= 4


def test_diff_deduplicates_unchanged_content(capsys, with_base, changed_image):
    """Content already in the store from the base must not be stored twice."""
    _code, data = run_json(capsys, "snap", "diff", changed_image, "--parent", "base-os",
                           "--label", "newapp", "--store", with_base)
    assert data["capture"]["blobs_deduplicated"] >= 2


def test_diff_of_a_rebuilt_but_identical_drive_is_empty(capsys, with_base, rebuilt_image):
    """The headline case: same contents, different build, so every datestamp differs."""
    _code, data = run_json(capsys, "snap", "diff", rebuilt_image, "--parent", "base-os",
                           "--label", "noop", "--store", with_base)
    assert data["changes"] == [], f"expected no changes, got {data['changes']}"


def test_empty_diff_suggests_discarding_not_committing(capsys, with_base, base_image):
    """Suggesting a commit after 'no differences' would contradict itself."""
    _code, text = run(capsys, "snap", "diff", base_image, "--parent", "base-os",
                      "--label", "noop", "--store", with_base)
    assert "no differences" in text
    assert "snap discard noop" in text
    assert "snap commit noop" not in text


def test_diff_does_not_create_a_layer(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "ls", "--store", with_base)
    assert len(data) == 1  # still just the base


def test_diff_requires_a_known_parent(capsys, with_base, changed_image):
    code, _ = run(capsys, "snap", "diff", changed_image, "--parent", "nope",
                  "--label", "x", "--store", with_base)
    assert code == 3  # NotFoundError


def test_candidates_are_listed_separately(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "ls", "--candidates", "--store", with_base)
    assert [c["label"] for c in data] == ["newapp"]


def test_review_lists_the_candidate_contents(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, text = run(capsys, "snap", "review", "newapp", "--store", with_base)
    assert "Workbench:Tools/NewApp" in text


def test_review_drop_prunes_the_candidate(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "review", "newapp",
                           "--drop", "**/*.info", "--store", with_base)
    paths = {e["p"] for e in data["entries"]}
    assert "Workbench:Tools/NewApp.info" not in paths
    assert "Workbench:Tools/NewApp" in paths
    assert data["dropped"]["**/*.info"] == 1
    assert data["modified"] is True


def test_review_drop_persists(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "review", "newapp", "--drop", "**/*.info", "--store", with_base)
    _code, data = run_json(capsys, "snap", "review", "newapp", "--store", with_base)
    assert not any(e["p"].endswith(".info") for e in data["entries"])


def test_review_reports_a_pattern_that_matched_nothing(capsys, with_base, changed_image):
    """A typo'd pattern must be visible rather than silently doing nothing."""
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, text = run(capsys, "snap", "review", "newapp",
                      "--drop", "**/Nonexistent/**", "--store", with_base)
    assert "matched nothing" in text


def test_review_keep_restricts_to_matches(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "review", "newapp",
                           "--keep", "**/Tools/**", "--store", with_base)
    assert all("Tools" in e["p"] for e in data["entries"])


def test_commit_creates_a_layer_and_a_ref(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    code, text = run(capsys, "snap", "commit", "newapp", "--store", with_base)
    assert code == 0
    assert "committed" in text
    _code, data = run_json(capsys, "snap", "ls", "--store", with_base)
    assert {r for layer in data for r in layer["refs"]} == {"base-os", "newapp"}


def test_commit_clears_the_candidate(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "commit", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "ls", "--candidates", "--store", with_base)
    assert data == []


def test_commit_can_rename_the_ref(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "wip", "--store", with_base)
    run(capsys, "snap", "commit", "wip", "--ref", "games-common", "--store", with_base)
    _code, data = run_json(capsys, "snap", "ls", "--store", with_base)
    assert {r for layer in data for r in layer["refs"]} == {"base-os", "games-common"}


def test_commit_refuses_an_empty_candidate(capsys, with_base, base_image):
    run(capsys, "snap", "diff", base_image, "--parent", "base-os",
        "--label", "noop", "--store", with_base)
    code, _ = run(capsys, "snap", "commit", "noop", "--store", with_base)
    assert code == 2


def test_allow_empty_overrides(capsys, with_base, base_image):
    run(capsys, "snap", "diff", base_image, "--parent", "base-os",
        "--label", "noop", "--store", with_base)
    code, _ = run(capsys, "snap", "commit", "noop", "--allow-empty", "--store", with_base)
    assert code == 0


def test_discard_removes_a_candidate(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    code, _ = run(capsys, "snap", "discard", "newapp", "--store", with_base)
    assert code == 0
    code, _ = run(capsys, "snap", "discard", "newapp", "--store", with_base)
    assert code == 1


def test_no_deletions_suppresses_whiteouts(capsys, with_base, workdir):
    """A drive missing a file records a whiteout by default, and nothing with the flag."""
    reduced = images.make_rdb_hdf(
        str(workdir / "reduced.hdf"),
        size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(reduced, {"S/Startup-Sequence": b"C:SetPatch QUIET\n"}, part=0)

    _code, data = run_json(capsys, "snap", "diff", reduced, "--parent", "base-os",
                           "--label", "shrunk", "--store", with_base)
    assert data["by_reason"].get("deleted", 0) > 0

    _code, data = run_json(capsys, "snap", "diff", reduced, "--parent", "base-os",
                           "--label", "shrunk2", "--no-deletions", "--store", with_base)
    assert "deleted" not in data["by_reason"]


def test_timestamps_significant_makes_a_rebuild_noisy(capsys, with_base, rebuilt_image):
    """Proof the default is doing real work rather than passing for a trivial reason.

    Same contents, rebuilt: silent by default, and every entry becomes a difference once
    timestamps count. That gap is exactly the noise the design set out to avoid.
    """
    _code, quiet = run_json(capsys, "snap", "diff", rebuilt_image, "--parent", "base-os",
                            "--label", "a", "--store", with_base)
    _code, noisy = run_json(capsys, "snap", "diff", rebuilt_image, "--parent", "base-os",
                            "--label", "b", "--timestamps-significant",
                            "--store", with_base)
    assert quiet["changes"] == []
    assert len(noisy["changes"]) > 0
    assert all(c["reason"] == "timestamp" for c in noisy["changes"])


# ---------------------------------------------------------------------------
# ls / show / verify / gc / rm
# ---------------------------------------------------------------------------


def test_ls_on_an_empty_store_says_so(capsys, tmp_path):
    code, text = run(capsys, "snap", "ls", "--store", str(tmp_path / "nothing"))
    assert code == 0
    assert "no layers" in text


def test_show_reports_the_stack_for_a_diff_layer(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "commit", "newapp", "--store", with_base)
    _code, text = run(capsys, "snap", "show", "newapp", "--store", with_base)
    assert "stack" in text
    assert "base-os" in text


def test_show_accepts_an_id_prefix(capsys, with_base):
    _code, data = run_json(capsys, "snap", "ls", "--store", with_base)
    prefix = data[0]["id"][:10]
    code, _ = run(capsys, "snap", "show", prefix, "--store", with_base)
    assert code == 0


def test_show_unknown_ref(capsys, with_base):
    code, _ = run(capsys, "snap", "show", "ghost", "--store", with_base)
    assert code == 3


def test_verify_passes_on_a_healthy_store(capsys, with_base):
    code, text = run(capsys, "snap", "verify", "--store", with_base)
    assert code == 0
    assert "no problems" in text


def test_verify_reports_a_missing_blob_and_exits_nonzero(capsys, with_base):
    from amibuilder.layers.store import Store

    store = Store(with_base)
    layer_id = next(store.iter_layer_ids())
    entry = next(e for e in store.read_manifest(layer_id) if e.blob)
    store.blobs.delete(entry.blob)

    code, text = run(capsys, "snap", "verify", "--store", with_base)
    assert code == 6
    assert "blob missing" in text


def test_gc_reclaims_a_discarded_candidates_blobs(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "discard", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "gc", "--store", with_base)
    assert data["blobs"] > 0
    assert data["bytes"] > 0


def test_gc_keeps_blobs_a_candidate_still_needs(capsys, with_base, changed_image):
    """Unreviewed work is a gc root -- reclaiming it would silently empty the capture."""
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    _code, data = run_json(capsys, "snap", "gc", "--store", with_base)
    assert data["blobs"] == 0


def test_gc_dry_run_deletes_nothing(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "discard", "newapp", "--store", with_base)
    _code, first = run_json(capsys, "snap", "gc", "--dry-run", "--store", with_base)
    _code, second = run_json(capsys, "snap", "gc", "--dry-run", "--store", with_base)
    assert first["blobs"] == second["blobs"] > 0


def test_rm_removes_a_layer_and_its_ref(capsys, with_base):
    code, text = run(capsys, "snap", "rm", "base-os", "--store", with_base)
    assert code == 0
    assert "removed" in text
    _code, data = run_json(capsys, "snap", "ls", "--store", with_base)
    assert data == []


def test_rm_refuses_while_a_child_depends_on_it(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "commit", "newapp", "--store", with_base)
    code, _ = run(capsys, "snap", "rm", "base-os", "--store", with_base)
    assert code == 2


def test_rm_force_overrides(capsys, with_base, changed_image):
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    run(capsys, "snap", "commit", "newapp", "--store", with_base)
    code, _ = run(capsys, "snap", "rm", "base-os", "--force", "--store", with_base)
    assert code == 0


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_snap_without_a_subcommand_is_a_usage_error(capsys):
    code, _ = run(capsys, "snap")
    assert code == 2


def test_store_location_comes_from_the_environment(capsys, base_image, tmp_path, monkeypatch):
    from amibuilder.layers.store import STORE_ENV_VAR

    target = tmp_path / "env-store"
    monkeypatch.setenv(STORE_ENV_VAR, str(target))
    code, _ = run(capsys, "snap", "create", base_image, "--label", "base-os")
    assert code == 0
    assert (target / "layers").is_dir()


def test_every_subcommand_emits_valid_json(capsys, with_base, changed_image):
    """`--json` is part of the interface, so a command that forgets it is a defect."""
    run(capsys, "snap", "diff", changed_image, "--parent", "base-os",
        "--label", "newapp", "--store", with_base)
    for argv in (
        ("snap", "ls"),
        ("snap", "ls", "--candidates"),
        ("snap", "show", "base-os"),
        ("snap", "show", "base-os", "--files"),
        ("snap", "review", "newapp"),
        ("snap", "verify"),
        ("snap", "gc", "--dry-run"),
    ):
        _code, text = run(capsys, *argv, "--store", with_base, "--json")
        json.loads(text)  # raises if a command emitted text instead
