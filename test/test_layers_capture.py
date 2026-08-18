"""Capturing a volume, and diffing two captures.

The diff behaviour is the part of the design most likely to disappoint, so most of these
tests are about what does and does not count as a difference. The rest check that a capture
records directories explicitly and is reproducible.
"""

from __future__ import annotations

import pytest

from amibuilder.addressing import parse
from amibuilder.image import open_container
from amibuilder.layers import capture as C
from amibuilder.layers import manifest as M
from amibuilder.layers.blobs import BlobStore

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(str(tmp_path / "blobs"))


def grab(path: str, blobs: BlobStore, **kw) -> C.CaptureResult:
    with open_container(parse(path)) as container:
        return C.capture_container(container, blobs, **kw)


def fentry(path: str, blob: str = HASH_A, **kw) -> M.ManifestEntry:
    return M.ManifestEntry(path=path, kind=M.FILE, blob=blob, size=kw.pop("size", 10), **kw)


def dentry(path: str, **kw) -> M.ManifestEntry:
    return M.ManifestEntry(path=path, kind=M.DIR, **kw)


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # '*' stops at a separator, '**' crosses them. This is the distinction fnmatch
        # does not make, and the reason the translator is hand-rolled.
        ("Work:*", "Work:file", True),
        ("Work:*", "Work:dir/file", False),
        ("Work:**", "Work:dir/file", True),
        ("**/Trashcan/**", "Work:a/b/Trashcan/thing", True),
        ("**/Trashcan/**", "Work:Trashcan/thing", True),
        # A trailing '/**' also matches the directory itself, so an excluded directory is
        # not left behind empty.
        ("**/Trashcan/**", "Work:a/Trashcan", True),
        ("**/T/**", "Workbench:T", True),
        ("**/T/**", "Workbench:T/scratch", True),
        ("**/T/**", "Workbench:Tools/Calculator", False),
        ("Work:?.txt", "Work:a.txt", True),
        ("Work:?.txt", "Work:ab.txt", False),
        ("**/._*", "Work:dir/._resource", True),
    ],
)
def test_glob_semantics(pattern, path, expected):
    assert (C.Exclusions((pattern,)).matches(path) is not None) is expected


def test_matching_is_case_insensitive_like_ffs():
    excl = C.Exclusions(("**/trashcan/**",))
    assert excl.matches("Work:TRASHCAN/thing") is not None


def test_matches_returns_the_offending_pattern():
    """So the user can be told *why* something was skipped."""
    excl = C.Exclusions(("**/T/**", "**/Trashcan/**"))
    assert excl.matches("Work:Trashcan/x") == "**/Trashcan/**"


def test_no_match_returns_none():
    assert C.Exclusions(("**/T/**",)).matches("Work:C/List") is None


@pytest.mark.parametrize(
    "path",
    [
        "Workbench:S/env-archive/sys/overscan.prefs",
        "Workbench:Devs/system-configuration",
        "Workbench:S/Startup-Sequence",
        "Workbench:S/User-Startup",
        "Workbench:WBStartup/MyTool",
        # Icons are load-bearing, so *.info is never excluded generally.
        "Workbench:Tools/Calculator.info",
    ],
)
def test_defaults_do_not_exclude_configuration_or_icons(path):
    """These are plausibly the whole point of a 'my configuration' layer."""
    assert C.Exclusions().matches(path) is None


@pytest.mark.parametrize(
    "path",
    ["Workbench:T/scratch", "Work:Trashcan/deleted", "Work:Trashcan.info", "Work:.DS_Store"],
)
def test_defaults_exclude_noise(path):
    assert C.Exclusions().matches(path) is not None


def test_build_can_drop_the_defaults():
    assert C.Exclusions.build(defaults=False).patterns == ()


def test_build_appends_extra_patterns():
    excl = C.Exclusions.build(extra=("**/Games/**",))
    assert excl.matches("Work:Games/x") is not None
    assert excl.matches("Work:T/x") is not None


# ---------------------------------------------------------------------------
# Capturing a real volume
# ---------------------------------------------------------------------------


def test_capture_records_files_and_directories(populated_hdf: str, blobs: BlobStore):
    result = grab(populated_hdf, blobs)
    paths = {e.path for e in result.entries}
    assert "Pop:S/Startup-Sequence" in paths
    assert "Pop:TESTFILE" in paths
    assert "Pop:Libs/thing.library" in paths
    # Directories are recorded in their own right, not implied by their contents.
    assert "Pop:S" in paths
    assert "Pop:Libs" in paths


def test_captured_directories_are_dir_entries(populated_hdf: str, blobs: BlobStore):
    result = grab(populated_hdf, blobs)
    by_path = {e.path: e for e in result.entries}
    assert by_path["Pop:S"].kind == M.DIR
    assert by_path["Pop:S"].blob is None


def test_capture_does_not_record_the_volume_root(populated_hdf: str, blobs: BlobStore):
    """Composition creates the root by formatting, so recording it describes nothing useful."""
    assert all(e.relative for e in grab(populated_hdf, blobs).entries)


def test_capture_stores_content_and_it_reads_back(populated_hdf: str, blobs: BlobStore):
    result = grab(populated_hdf, blobs)
    entry = next(e for e in result.entries if e.path == "Pop:S/Startup-Sequence")
    assert blobs.get(entry.blob) == b'Echo "hello"\n'


def test_capture_records_sizes(populated_hdf: str, blobs: BlobStore):
    result = grab(populated_hdf, blobs)
    entry = next(e for e in result.entries if e.path == "Pop:TESTFILE")
    assert entry.size == 100


def test_capture_stats_add_up(populated_hdf: str, blobs: BlobStore):
    result = grab(populated_hdf, blobs)
    assert result.stats.files == 3
    assert result.stats.dirs == 2
    assert result.stats.content_bytes == len(b'Echo "hello"\n') + 100 + 256 * 8
    assert result.stats.blobs_written == 3
    assert result.stats.stored_bytes > 0


def test_capture_is_reproducible(populated_hdf: str, blobs: BlobStore, tmp_path):
    """Two captures of one image must produce identical manifests, or layer IDs are unstable."""
    first = grab(populated_hdf, blobs)
    second = grab(populated_hdf, BlobStore(str(tmp_path / "other")))
    assert M.canonical_bytes(first.entries) == M.canonical_bytes(second.entries)


def test_identical_content_deduplicates_within_one_capture(workdir, blobs: BlobStore):
    from helpers import images

    path = images.make_plain_hdf(str(workdir / "dup.hdf"), size="20Mi", volume="Dup")
    images.write_files(path, {"a": b"same bytes here", "b": b"same bytes here"})
    result = grab(path, blobs)
    assert result.stats.files == 2
    assert result.stats.blobs_written == 1
    assert result.stats.blobs_deduplicated == 1
    assert blobs.count() == 1


def test_capture_reports_progress(populated_hdf: str, blobs: BlobStore):
    seen: list[tuple[str, int]] = []
    grab(populated_hdf, blobs, on_file=lambda p, n: seen.append((p, n)))
    assert len(seen) == 3
    assert all(p.startswith("Pop:") for p, _ in seen)


def test_exclusions_are_applied_and_counted(populated_hdf: str, blobs: BlobStore):
    result = grab(populated_hdf, blobs, exclusions=C.Exclusions(("**/Libs/**",)))
    paths = {e.path for e in result.entries}
    assert "Pop:Libs" not in paths
    assert "Pop:Libs/thing.library" not in paths
    assert "Pop:TESTFILE" in paths
    assert result.stats.skipped == 2


# ---------------------------------------------------------------------------
# Capturing a whole container
# ---------------------------------------------------------------------------


def test_capture_container_covers_every_partition(rdb_populated: str, blobs: BlobStore):
    result = grab(rdb_populated, blobs)
    assert result.volumes == ["Workbench", "Work"]
    volumes = {e.volume for e in result.entries}
    assert volumes == {"Workbench"}  # the second partition is deliberately empty


def test_capture_container_qualifies_paths_by_volume(rdb_populated: str, blobs: BlobStore):
    result = grab(rdb_populated, blobs)
    assert "Workbench:S/Startup-Sequence" in {e.path for e in result.entries}


def test_capture_container_captures_the_whole_known_tree(rdb_populated: str, blobs: BlobStore):
    from conftest import WORKBENCH_FILES

    result = grab(rdb_populated, blobs)
    captured = {e.path for e in result.entries if e.kind == M.FILE}
    assert captured == {f"Workbench:{p}" for p in WORKBENCH_FILES}


def test_capture_container_on_a_plain_hdf(plain_hdf: str, blobs: BlobStore):
    result = grab(plain_hdf, blobs)
    assert result.volumes == ["Plain"]
    assert result.warnings == []


def test_unmountable_partition_becomes_a_warning_not_a_failure(
    rdb_populated: str, blobs: BlobStore, monkeypatch
):
    """One PFS3 partition must not make a four-partition drive uncapturable."""
    from dataclasses import replace as dc_replace

    from amibuilder.image import Container

    original = Container.partitions

    def patched(self, probe_volumes=True):
        parts = original(self, probe_volumes=probe_volumes)
        return [
            dc_replace(parts[0], volume_name=None, volume_error="unsupported filesystem PFS3"),
            *parts[1:],
        ]

    monkeypatch.setattr(Container, "partitions", patched)
    result = grab(rdb_populated, blobs)
    assert any("PFS3" in w for w in result.warnings)
    assert result.volumes == ["Work"]


# ---------------------------------------------------------------------------
# Diff -- what counts as a difference
# ---------------------------------------------------------------------------


def test_new_file_is_a_difference():
    result = C.diff([], [fentry("Work:new")])
    assert [c.reason for c in result.changes] == [C.REASON_NEW]


def test_changed_content_is_a_difference():
    result = C.diff([fentry("Work:f", HASH_A)], [fentry("Work:f", HASH_B)])
    assert [c.reason for c in result.changes] == [C.REASON_CONTENT]


def test_changed_protection_is_a_difference():
    before = fentry("Work:f")
    after = M.replace(before, protect="h---rwed")
    assert [c.reason for c in C.diff([before], [after]).changes] == [C.REASON_PROTECTION]


def test_changed_comment_is_a_difference():
    before = fentry("Work:f")
    after = M.replace(before, comment="now annotated")
    assert [c.reason for c in C.diff([before], [after]).changes] == [C.REASON_COMMENT]


def test_timestamp_alone_is_not_a_difference():
    """The decision that keeps a 40-file layer from becoming 400 entries."""
    before = fentry("Work:f", ts=(100, 0, 0))
    after = M.replace(before, ts=(200, 30, 10))
    result = C.diff([before], [after])
    assert result.is_empty
    assert result.unchanged == 1


def test_timestamps_significant_makes_it_a_difference():
    before = fentry("Work:f", ts=(100, 0, 0))
    after = M.replace(before, ts=(200, 30, 10))
    result = C.diff([before], [after], timestamps_significant=True)
    assert [c.reason for c in result.changes] == [C.REASON_TIMESTAMP]


def test_kind_change_is_a_difference():
    result = C.diff([fentry("Work:thing")], [dentry("Work:thing")])
    assert C.REASON_KIND in result.changes[0].reasons


def test_case_only_rename_is_one_change_not_a_delete_plus_add():
    """Otherwise the pair would compose into deleting the file being added."""
    result = C.diff([fentry("Work:Thing")], [fentry("Work:thing")])
    assert len(result.changes) == 1
    assert result.changes[0].reason == C.REASON_CASE


def test_several_reasons_are_reported_together():
    before = fentry("Work:f", HASH_A)
    after = M.replace(before, blob=HASH_B, protect="h---rwed")
    change = C.diff([before], [after]).changes[0]
    assert set(change.reasons) == {C.REASON_CONTENT, C.REASON_PROTECTION}


def test_unchanged_entries_are_counted_not_recorded():
    entries = [fentry("Work:a"), fentry("Work:b", HASH_B), dentry("Work:d")]
    result = C.diff(entries, entries)
    assert result.is_empty
    assert result.unchanged == 3


def test_deletion_becomes_a_whiteout():
    result = C.diff([fentry("Work:gone")], [])
    assert len(result.changes) == 1
    assert result.changes[0].entry.kind == M.WHITEOUT
    assert result.changes[0].reason == C.REASON_DELETED
    assert result.changes[0].previous is not None


def test_deletions_can_be_disabled():
    result = C.diff([fentry("Work:gone")], [], deletions=False)
    assert result.is_empty


def test_previous_entry_is_carried_for_explanation():
    before = fentry("Work:f", HASH_A)
    after = fentry("Work:f", HASH_B)
    assert C.diff([before], [after]).changes[0].previous == before


def test_by_reason_summarises():
    result = C.diff(
        [fentry("Work:gone"), fentry("Work:changed", HASH_A)],
        [fentry("Work:changed", HASH_B), fentry("Work:added")],
    )
    assert result.by_reason() == {
        C.REASON_CONTENT: 1,
        C.REASON_DELETED: 1,
        C.REASON_NEW: 1,
    }


def test_diff_entries_are_sorted():
    result = C.diff([], [fentry("Work:zeta"), fentry("Work:alpha", HASH_B)])
    assert [e.path for e in result.entries] == ["Work:alpha", "Work:zeta"]


def test_diff_across_volumes_keeps_them_separate():
    result = C.diff([fentry("Workbench:f")], [fentry("Work:f")])
    reasons = {c.entry.path: c.reason for c in result.changes}
    assert reasons == {"Work:f": C.REASON_NEW, "Workbench:f": C.REASON_DELETED}


# ---------------------------------------------------------------------------
# End-to-end diff against real images
# ---------------------------------------------------------------------------


def test_diff_of_a_real_added_file(workdir, blobs: BlobStore):
    from helpers import images

    before_path = images.make_plain_hdf(str(workdir / "before.hdf"), size="20Mi", volume="V")
    images.write_files(before_path, {"C/List": b"original"})
    parent = grab(before_path, blobs).entries

    after_path = images.make_plain_hdf(str(workdir / "after.hdf"), size="20Mi", volume="V")
    images.write_files(after_path, {"C/List": b"original", "C/NewTool": b"brand new"})
    current = grab(after_path, blobs).entries

    result = C.diff(parent, current)
    added = [c for c in result.changes if c.reason == C.REASON_NEW]
    assert [c.entry.path for c in added] == ["V:C/NewTool"]


def test_diff_of_an_identical_image_is_empty(workdir, blobs: BlobStore):
    """Timestamps differ between two builds, so this only passes because they are excluded."""
    from helpers import images

    files = {"C/List": b"same", "S/Startup-Sequence": b"boot"}
    a = images.make_plain_hdf(str(workdir / "a.hdf"), size="20Mi", volume="V")
    images.write_files(a, files)
    b = images.make_plain_hdf(str(workdir / "b.hdf"), size="20Mi", volume="V")
    images.write_files(b, files)

    result = C.diff(grab(a, blobs).entries, grab(b, blobs).entries)
    assert result.is_empty, f"unexpected differences: {[c.as_dict() for c in result.changes]}"


# ---------------------------------------------------------------------------
# Review: dropping and keeping
# ---------------------------------------------------------------------------


def test_apply_drops_removes_matches_and_reports_counts():
    changes = C.diff([], [fentry("Work:T/tmp"), fentry("Work:C/List", HASH_B)]).changes
    kept, dropped = C.apply_drops(changes, ["**/T/**"])
    assert [c.entry.path for c in kept] == ["Work:C/List"]
    assert dropped == {"**/T/**": 1}


def test_apply_drops_reports_a_pattern_that_matched_nothing():
    """So a typo'd pattern is visible instead of silently doing nothing."""
    changes = C.diff([], [fentry("Work:C/List")]).changes
    _kept, dropped = C.apply_drops(changes, ["**/Nope/**"])
    assert dropped == {"**/Nope/**": 0}


def test_apply_keeps_is_the_inverse():
    changes = C.diff([], [fentry("Work:T/tmp"), fentry("Work:C/List", HASH_B)]).changes
    kept = C.apply_keeps(changes, ["**/T/**"])
    assert [c.entry.path for c in kept] == ["Work:T/tmp"]
