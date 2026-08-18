"""Writing a composed plan to a host directory, with .uaem sidecars.

The sidecar format is the interesting part: it is an interchange format read by FS-UAE and by
amitools, so these tests check it against amitools' own parser rather than against my reading of
the spec.
"""

from __future__ import annotations

import os

import pytest

from amibuilder.errors import ImageError, UsageError
from amibuilder.layers import compose as CP
from amibuilder.layers import drive as D
from amibuilder.layers import manifest as M
from amibuilder.layers import store as S
from amibuilder.layers import targets as T
from amibuilder.layers.blobs import BlobStore

from test_layers_compose_plan import (  # reuse the plan fixtures
    drive_record,
    make_base,
    partition,
)


@pytest.fixture
def store(tmp_path) -> S.Store:
    st = S.Store(str(tmp_path / "store"))
    st.init()
    return st


def fentry(store: S.Store, path: str, data: bytes = b"contents", **kw) -> M.ManifestEntry:
    return M.ManifestEntry(
        path=path, kind=M.FILE, blob=store.blobs.put_bytes(data).hash, size=len(data), **kw
    )


def plan_for(store: S.Store, entries, **kw) -> CP.Plan:
    make_base(store, entries, label="base", **kw)
    return CP.build_plan(store, ["base"])


# ---------------------------------------------------------------------------
# Name escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["List", "Startup-Sequence", "thing.library", "My File", "Sm\xf6rg\xe5s", "a+b,c"]
)
def test_ordinary_names_pass_through_untouched(name):
    """Escaping should be rare, or the host tree stops resembling the Amiga one."""
    assert T.escape_name(name) == name


@pytest.mark.parametrize("name,expected", [
    ("a/b", "a%2fb"),
    ("a:b", "a%3ab"),
    ("50%", "50%25"),
    ("q?", "q%3f"),
    ('say"hi"', "say%22hi%22"),
])
def test_troublesome_characters_are_escaped(name, expected):
    assert T.escape_name(name) == expected


def test_percent_is_escaped_so_the_mapping_stays_reversible():
    """Without this, '%2f' on the Amiga and an escaped '/' would be indistinguishable."""
    assert T.escape_name("%2f") == "%252f"


@pytest.mark.parametrize("name,expected", [("trailing.", "trailing%2e"), ("trailing ", "trailing%20")])
def test_trailing_dot_or_space_is_escaped(name, expected):
    assert T.escape_name(name) == expected


def test_interior_dots_and_spaces_are_left_alone():
    assert T.escape_name("a. b.c") == "a. b.c"


def test_control_characters_are_escaped():
    assert T.escape_name("a\x01b") == "a%01b"


# ---------------------------------------------------------------------------
# The .uaem sidecar format
# ---------------------------------------------------------------------------


def test_sidecar_layout_matches_the_documented_offsets():
    """amitools reads the timestamp at [0:19] and ticks at [20:22] after a 9-byte prefix."""
    entry = M.ManifestEntry(
        path="Work:f", kind=M.FILE, blob="a" * 64, size=1,
        protect="h-p-rwed", ts=(17389, 587, 12), comment="a note",
    )
    line = T.uaem_line(entry)
    assert line.endswith("\n")
    assert line[0:8] == "h-p-rwed"
    assert line[8] == " "
    body = line[9:]
    assert body[19] == "."
    assert body[20:22] == "12"
    assert body[23:].rstrip("\n") == "a note"


def test_amitools_parses_our_sidecar():
    """The real test: the consumer's own parser must accept what we write.

    Checks protection and comment exactly. It deliberately does not check the timestamp, because
    amitools parses through its `time.mktime`-derived epoch and so shifts the value by the host's
    January UTC offset (notes G7 / §5.7). That is amitools' bug, and asserting agreement with it
    would mean asserting the bug.
    """
    from amitools.fs.MetaInfoFSUAE import MetaInfoFSUAE

    entry = M.ManifestEntry(
        path="Work:f", kind=M.FILE, blob="a" * 64, size=1,
        # Protection order is h s p a r w e d -- 'p' belongs in slot 2, not slot 3.
        protect="hsp-rwed", ts=(17389, 587, 34), comment="round trip",
    )
    meta = MetaInfoFSUAE().parse_data(T.uaem_line(entry))
    assert meta.get_protect_str() == "hsp-rwed"
    assert meta.get_comment_unicode_str() == "round trip"


def test_amitools_parses_a_sidecar_with_no_comment():
    from amitools.fs.MetaInfoFSUAE import MetaInfoFSUAE

    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=1)
    meta = MetaInfoFSUAE().parse_data(T.uaem_line(entry))
    assert meta.get_protect_str() == M.DEFAULT_PROTECT


def test_empty_comment_still_leaves_the_separator():
    """amitools' own writer emits '%s %s %s\\n', so the trailing space is part of the format."""
    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=1)
    assert T.uaem_line(entry).endswith(" \n")


def test_ticks_are_always_two_digits():
    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=1, ts=(1, 1, 5))
    assert ".05 " in T.uaem_line(entry)


def test_timestamp_is_rendered_from_the_triple_not_through_unix_time():
    """Day zero is 1978-01-01, and it must render as that rather than shifted by an offset."""
    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=1, ts=(0, 0, 0))
    assert "1978-01-01 00:00:00.00" in T.uaem_line(entry)


def test_a_known_triple_renders_exactly():
    """17389 days after 1978-01-01 is 2025-08-11; 587 minutes into the day is 09:47.

    Verified independently with `datetime`, not with this project's own converter, so the
    assertion is not circular.
    """
    entry = M.ManifestEntry(
        path="Work:f", kind=M.FILE, blob="a" * 64, size=1, ts=(17389, 587, 0)
    )
    assert "2025-08-11 09:47:00.00" in T.uaem_line(entry)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_files_and_directories_land_with_sidecars(store, tmp_path):
    plan = plan_for(store, [
        M.ManifestEntry(path="Workbench:S", kind=M.DIR),
        fentry(store, "Workbench:S/Startup-Sequence", b"C:SetPatch\n"),
    ])
    target = str(tmp_path / "out")
    result = T.write_directory(plan, store.blobs, target)

    root = os.path.join(target, "Workbench")
    assert open(os.path.join(root, "S", "Startup-Sequence"), "rb").read() == b"C:SetPatch\n"
    assert os.path.isfile(os.path.join(root, "S", "Startup-Sequence.uaem"))
    # A directory's sidecar sits beside it, not inside -- as amitools writes them.
    assert os.path.isfile(os.path.join(root, "S.uaem"))
    assert result.files == 1 and result.dirs == 1 and result.sidecars == 2


def test_one_subdirectory_per_volume(store, tmp_path):
    drive = drive_record([
        partition(index=0, volume="Workbench", low=1, high=100),
        partition(index=1, volume="Work", low=101, high=200, policy=D.POLICY_MERGE),
    ])
    plan = plan_for(store, [
        fentry(store, "Workbench:a", b"1"),
        fentry(store, "Work:b", b"2"),
    ], drive=drive)
    target = str(tmp_path / "out")
    T.write_directory(plan, store.blobs, target)
    assert os.path.isfile(os.path.join(target, "Workbench", "a"))
    assert os.path.isfile(os.path.join(target, "Work", "b"))


def test_empty_directories_are_preserved(store, tmp_path):
    """The whole reason directories are recorded explicitly -- T/ and WBStartup matter."""
    plan = plan_for(store, [M.ManifestEntry(path="Workbench:WBStartup", kind=M.DIR)])
    target = str(tmp_path / "out")
    T.write_directory(plan, store.blobs, target)
    assert os.path.isdir(os.path.join(target, "Workbench", "WBStartup"))


def test_nested_directories_are_created_in_order(store, tmp_path):
    plan = plan_for(store, [
        M.ManifestEntry(path="Workbench:a", kind=M.DIR),
        M.ManifestEntry(path="Workbench:a/b", kind=M.DIR),
        fentry(store, "Workbench:a/b/c", b"deep"),
    ])
    target = str(tmp_path / "out")
    T.write_directory(plan, store.blobs, target)
    assert open(os.path.join(target, "Workbench", "a", "b", "c"), "rb").read() == b"deep"


def test_byte_counts_are_reported(store, tmp_path):
    plan = plan_for(store, [
        fentry(store, "Workbench:a", b"12345"),
        fentry(store, "Workbench:b", b"678"),
    ])
    result = T.write_directory(plan, store.blobs, str(tmp_path / "out"))
    assert result.bytes_written == 8


def test_no_metadata_skips_the_sidecars(store, tmp_path):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out")
    result = T.write_directory(plan, store.blobs, target, metadata=False)
    assert result.sidecars == 0
    assert not os.path.exists(os.path.join(target, "Workbench", "a.uaem"))


def test_progress_callback_fires_per_file(store, tmp_path):
    plan = plan_for(store, [
        fentry(store, "Workbench:a", b"1"),
        fentry(store, "Workbench:b", b"22"),
    ])
    seen: list[tuple[str, int]] = []
    T.write_directory(plan, store.blobs, str(tmp_path / "out"),
                      on_file=lambda p, n: seen.append((p, n)))
    assert [n for _p, n in seen] == [1, 2]


def test_escaped_names_are_reported(store, tmp_path):
    """Silent escaping would make the host tree quietly disagree with the Amiga one."""
    plan = plan_for(store, [fentry(store, "Workbench:fifty%", b"1")])
    result = T.write_directory(plan, store.blobs, str(tmp_path / "out"))
    assert any("escaped as %XX" in w for w in result.warnings)


def test_a_file_whose_parent_was_not_recorded_is_reported(store, tmp_path):
    """A missing directory entry means the capture lost that directory's metadata."""
    plan = plan_for(store, [fentry(store, "Workbench:orphan/file", b"1")])
    target = str(tmp_path / "out")
    result = T.write_directory(plan, store.blobs, target)
    assert os.path.isfile(os.path.join(target, "Workbench", "orphan", "file"))
    assert any("not recorded in the manifest" in w for w in result.warnings)


def test_a_missing_blob_is_reported_clearly(store, tmp_path):
    entry = fentry(store, "Workbench:a", b"gone")
    plan = plan_for(store, [entry])
    store.blobs.delete(entry.blob)
    with pytest.raises(ImageError, match="blob missing"):
        T.write_directory(plan, store.blobs, str(tmp_path / "out"))


# ---------------------------------------------------------------------------
# Policies and refusals
# ---------------------------------------------------------------------------


def test_replace_refuses_a_non_empty_directory_without_force(store, tmp_path):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out")
    os.makedirs(os.path.join(target, "Workbench"))
    open(os.path.join(target, "Workbench", "precious"), "w").close()

    with pytest.raises(UsageError, match="Pass --force"):
        T.write_directory(plan, store.blobs, target)
    # The refusal must leave the existing content alone.
    assert os.path.isfile(os.path.join(target, "Workbench", "precious"))


def test_the_refusal_suggests_merge_as_the_alternative(store, tmp_path):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out")
    os.makedirs(os.path.join(target, "Workbench"))
    open(os.path.join(target, "Workbench", "precious"), "w").close()
    with pytest.raises(UsageError, match="--policy Workbench=merge"):
        T.write_directory(plan, store.blobs, target)


def test_force_clears_the_volume_first(store, tmp_path):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out")
    os.makedirs(os.path.join(target, "Workbench"))
    open(os.path.join(target, "Workbench", "stale"), "w").close()

    result = T.write_directory(plan, store.blobs, target, force=True)
    assert result.cleared == ["Workbench"]
    assert not os.path.exists(os.path.join(target, "Workbench", "stale"))
    assert os.path.isfile(os.path.join(target, "Workbench", "a"))


def test_replace_into_an_empty_directory_needs_no_force(store, tmp_path):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out")
    os.makedirs(os.path.join(target, "Workbench"))
    T.write_directory(plan, store.blobs, target)
    assert os.path.isfile(os.path.join(target, "Workbench", "a"))


def test_merge_keeps_what_is_already_there(store, tmp_path):
    drive = drive_record([partition(policy=D.POLICY_MERGE)])
    plan = plan_for(store, [fentry(store, "Workbench:new", b"1")], drive=drive)
    target = str(tmp_path / "out")
    os.makedirs(os.path.join(target, "Workbench"))
    open(os.path.join(target, "Workbench", "existing"), "w").close()

    result = T.write_directory(plan, store.blobs, target)
    assert result.cleared == []
    assert os.path.isfile(os.path.join(target, "Workbench", "existing"))
    assert os.path.isfile(os.path.join(target, "Workbench", "new"))


def test_preserve_creates_the_directory_and_writes_nothing(store, tmp_path):
    drive = drive_record([partition(volume="Saves", policy=D.POLICY_PRESERVE)])
    plan = plan_for(store, [fentry(store, "Saves:game.sav", b"1")], drive=drive)
    target = str(tmp_path / "out")
    result = T.write_directory(plan, store.blobs, target)
    assert os.path.isdir(os.path.join(target, "Saves"))
    assert os.listdir(os.path.join(target, "Saves")) == []
    assert result.files == 0


def test_dry_run_writes_nothing_at_all(store, tmp_path):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out")
    result = T.write_directory(plan, store.blobs, target, dry_run=True)
    assert not os.path.exists(target)
    assert result.files == 1  # still reports what it would do
    assert result.dry_run is True


def test_an_unwritable_plan_is_refused(store, tmp_path):
    """Preflight problems must stop the write, not just be printed alongside it."""
    plan = plan_for(store, [fentry(store, "Workbench:" + "x" * 40, b"1")])
    assert not plan.is_writable
    with pytest.raises(UsageError, match="unresolved problems"):
        T.write_directory(plan, store.blobs, str(tmp_path / "out"))


def test_an_empty_target_is_refused(store):
    plan = plan_for(store, [fentry(store, "Workbench:a", b"1")])
    with pytest.raises(UsageError, match="no target directory"):
        T.write_directory(plan, store.blobs, "")


# ---------------------------------------------------------------------------
# FS-UAE configuration
# ---------------------------------------------------------------------------


def test_fsuae_config_lists_the_bootable_volume_first(store, tmp_path):
    """Otherwise the wrong volume can win the boot election."""
    drive = drive_record([
        partition(index=0, volume="Work", low=1, high=100, policy=D.POLICY_MERGE),
        partition(index=1, volume="Workbench", low=101, high=200),
    ])
    # Only index 0 is bootable in the helper, so make Workbench the bootable one.
    drive["partitions"][0]["bootable"] = False
    drive["partitions"][1]["bootable"] = True

    plan = plan_for(store, [
        fentry(store, "Work:a", b"1"), fentry(store, "Workbench:b", b"2"),
    ], drive=drive)
    target = str(tmp_path / "out")
    result = T.write_directory(plan, store.blobs, target)
    lines = T.fsuae_config_lines(result, plan)
    assert lines[0].endswith("Workbench")
    assert "hard_drive_0_label = Workbench" in lines
    assert "hard_drive_0_priority = 0" in lines


def test_fsuae_config_covers_every_written_volume(store, tmp_path):
    drive = drive_record([
        partition(index=0, volume="Workbench", low=1, high=100),
        partition(index=1, volume="Work", low=101, high=200, policy=D.POLICY_MERGE),
    ])
    plan = plan_for(store, [
        fentry(store, "Workbench:a", b"1"), fentry(store, "Work:b", b"2"),
    ], drive=drive)
    result = T.write_directory(plan, store.blobs, str(tmp_path / "out"))
    lines = T.fsuae_config_lines(result, plan)
    assert sum(1 for line in lines if line.startswith("hard_drive_")) >= 4


def test_volume_dir_escapes_the_volume_name():
    assert T.volume_dir("/tmp/out", "My:Volume").endswith("My%3aVolume")
