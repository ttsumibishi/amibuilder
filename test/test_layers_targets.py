"""Writing a composed plan to each of its targets: a host directory, a plain HDF, and an RDB drive.

Two parts are worth singling out. The `.uaem` sidecar format is an interchange format read by
FS-UAE and by amitools, so it is checked against amitools' own parser rather than against my
reading of the spec. And restoring into an *existing* RDB drive is checked hard, because getting it
wrong destroys the partitions the user was trying to keep.
"""

from __future__ import annotations

import os
import shutil

import pytest

from amibuilder.addressing import parse
from amibuilder.errors import ImageError, UsageError
from amibuilder.image import open_container
from amibuilder.layers import capture as C
from amibuilder.layers import compose as CP
from amibuilder.layers import drive as D
from amibuilder.layers import manifest as M
from amibuilder.layers import store as S
from amibuilder.layers import targets as T
from amibuilder.layers.blobs import BlobStore
from helpers import images

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


# ---------------------------------------------------------------------------
# Plain HDF target
# ---------------------------------------------------------------------------


def plain_plan(store: S.Store, entries) -> CP.Plan:
    """A plan shaped like one captured from a plain HDF: one volume, no partition table."""
    drive = {
        "scheme": D.DRIVE_SCHEME,
        "kind": "hdf",
        "block_size": 512,
        "cylinders": 1280,
        "heads": 1,
        "sectors": 32,
        "num_blocks": 40960,
        "total_bytes": 20 * 1024 * 1024,
        "single_volume": True,
        "partitions": [],
    }
    make_base(store, entries, label="base", drive=drive)
    return CP.build_plan(store, ["base"])


def read_back(path: str):
    """Open a composed image with amibuilder's own reader."""
    from amibuilder.addressing import parse
    from amibuilder.image import open_container

    with open_container(parse(path)) as container:
        with container.open_volume() as vol:
            entries = []
            for _dirpath, dirs, files in vol.walk():
                entries.extend(dirs)
                entries.extend(files)
            return vol.name, {e.path: e for e in entries}, {
                e.path: vol.read_file(e.path) for e in entries if not e.is_dir
            }


def test_plain_image_is_created_and_validates(store, tmp_path):
    plan = plain_plan(store, [
        M.ManifestEntry(path="Workbench:S", kind=M.DIR),
        fentry(store, "Workbench:S/Startup-Sequence", b"C:SetPatch QUIET\n"),
    ])
    target = str(tmp_path / "out.hdf")
    result = T.write_plain(plan, store.blobs, target)

    assert os.path.isfile(target)
    assert result.files == 1 and result.dirs == 1
    name, _entries, _data = read_back(target)
    assert name == "Workbench"


def test_plain_image_size_comes_from_the_recorded_source_size(store, tmp_path):
    """A captured plain HDF has no partition record, but the image size is still a recorded fact."""
    plan = plain_plan(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out.hdf")
    result = T.write_plain(plan, store.blobs, target)
    assert result.size_bytes == 20 * 1024 * 1024
    assert "recorded size" in result.size_source
    assert os.path.getsize(target) == 20 * 1024 * 1024


def test_explicit_size_wins(store, tmp_path):
    plan = plain_plan(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out.hdf")
    result = T.write_plain(plan, store.blobs, target, size=4 * 1024 * 1024)
    assert result.size_bytes == 4 * 1024 * 1024
    assert result.size_source == "requested"


def test_no_recorded_size_and_none_given_is_refused(store, tmp_path):
    """Inventing a size is how an image ends up mysteriously full."""
    make_base(store, [fentry(store, "Workbench:a", b"1")], label="base", drive=None)
    plan = CP.build_plan(store, ["base"])
    with pytest.raises(UsageError, match="Pass --size"):
        T.write_plain(plan, store.blobs, str(tmp_path / "out.hdf"))


def test_content_survives_the_round_trip(store, tmp_path):
    payloads = {
        "Workbench:S/Startup-Sequence": b"C:SetPatch QUIET\n",
        "Workbench:C/List": bytes(range(256)) * 3,
        "Workbench:Libs/thing.library": bytes(700),
        "Workbench:Empty/keeper": b"",
    }
    entries = [
        M.ManifestEntry(path="Workbench:S", kind=M.DIR),
        M.ManifestEntry(path="Workbench:C", kind=M.DIR),
        M.ManifestEntry(path="Workbench:Libs", kind=M.DIR),
        M.ManifestEntry(path="Workbench:Empty", kind=M.DIR),
    ] + [fentry(store, path, data) for path, data in payloads.items()]

    plan = plain_plan(store, entries)
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)

    _name, _read, data = read_back(target)
    for path, expected in payloads.items():
        relative = M.split_path(path)[1]
        assert data[relative] == expected, f"content differs for {path}"


def test_deep_directories_are_created(store, tmp_path):
    """amitools' create_dir is not recursive (G19), so the chain has to be built by hand."""
    entries = [
        M.ManifestEntry(path="Workbench:a", kind=M.DIR),
        M.ManifestEntry(path="Workbench:a/b", kind=M.DIR),
        M.ManifestEntry(path="Workbench:a/b/c", kind=M.DIR),
        fentry(store, "Workbench:a/b/c/leaf", b"buried"),
    ]
    plan = plain_plan(store, entries)
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)
    _name, _read, data = read_back(target)
    assert data["a/b/c/leaf"] == b"buried"


def test_empty_directories_survive(store, tmp_path):
    plan = plain_plan(store, [M.ManifestEntry(path="Workbench:WBStartup", kind=M.DIR)])
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)
    _name, read, _data = read_back(target)
    assert "WBStartup" in read
    assert read["WBStartup"].is_dir


def test_protection_bits_survive(store, tmp_path):
    entry = M.ManifestEntry(
        path="Workbench:script", kind=M.FILE, blob=store.blobs.put_bytes(b"x").hash,
        size=1, protect="hsp-rw-d",
    )
    plan = plain_plan(store, [entry])
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)
    _name, read, _data = read_back(target)
    assert read["script"].protect_str == "hsp-rw-d"


def test_comments_survive(store, tmp_path):
    entry = M.ManifestEntry(
        path="Workbench:noted", kind=M.FILE, blob=store.blobs.put_bytes(b"x").hash,
        size=1, comment="a preserved note",
    )
    plan = plain_plan(store, [entry])
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)
    _name, read, _data = read_back(target)
    assert read["noted"].comment == "a preserved note"


def test_timestamps_survive_exactly(store, tmp_path):
    """The whole reason MetaInfo is built by hand: the triple must reach the disk unchanged.

    Going through amitools' `from_secs` would shift this by the host's January UTC offset.
    """
    from amibuilder import timestamps

    triple = (17389, 587, 34)
    entry = M.ManifestEntry(
        path="Workbench:stamped", kind=M.FILE, blob=store.blobs.put_bytes(b"x").hash,
        size=1, ts=triple,
    )
    plan = plain_plan(store, [entry])
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)

    _name, read, _data = read_back(target)
    got = read["stamped"]
    assert timestamps.to_triple(got.mod_secs, got.mod_ticks) == triple


def test_directory_timestamps_are_not_restamped(store, tmp_path):
    """Creating a child must not update its parent -- that is what update_ts=False guards."""
    from amibuilder import timestamps

    triple = (17000, 100, 10)
    entries = [
        M.ManifestEntry(path="Workbench:S", kind=M.DIR, ts=triple),
        fentry(store, "Workbench:S/child", b"x"),
    ]
    plan = plain_plan(store, entries)
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)

    _name, read, _data = read_back(target)
    got = read["S"]
    assert timestamps.to_triple(got.mod_secs, got.mod_ticks) == triple


def test_the_composed_image_passes_check(store, tmp_path, capsys):
    """A structurally invalid image would be worse than no image.

    Driven through the real `check` command rather than its internals, so this exercises the same
    five-step validator sequence a user would run (notes §5.5).
    """
    from amibuilder.cli import main

    entries = [
        M.ManifestEntry(path="Workbench:S", kind=M.DIR),
        fentry(store, "Workbench:S/Startup-Sequence", b"C:SetPatch\n"),
        # Large enough to need several data blocks and a file-extension block.
        fentry(store, "Workbench:big", bytes(50_000)),
    ]
    plan = plain_plan(store, entries)
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)

    code = main(["check", target])
    output = capsys.readouterr().out
    assert code == 0, f"check reported problems:\n{output}"
    assert "ok" in output


def test_a_multi_volume_plan_is_refused(store, tmp_path):
    """A plain HDF holds one volume; silently composing part of a stack would be worse."""
    plan = plan_for(store, [
        fentry(store, "Workbench:a", b"1"),
        fentry(store, "Work:b", b"2"),
    ], drive=drive_record([
        partition(index=0, volume="Workbench", low=1, high=100),
        partition(index=1, volume="Work", low=101, high=200, policy=D.POLICY_MERGE),
    ]))
    with pytest.raises(UsageError, match="--format rdb"):
        T.write_plain(plan, store.blobs, str(tmp_path / "out.hdf"))


def test_an_existing_target_is_refused_without_force(store, tmp_path):
    plan = plain_plan(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out.hdf")
    open(target, "wb").close()
    with pytest.raises(UsageError, match="--force"):
        T.write_plain(plan, store.blobs, target)


def test_force_recreates_a_replace_volume(store, tmp_path):
    drive = {
        "scheme": D.DRIVE_SCHEME, "kind": "rdb", "block_size": 512, "cylinders": 1280,
        "heads": 1, "sectors": 32, "single_volume": False,
        "partitions": [partition(volume="Workbench", num_blocks=40960)],
    }
    make_base(store, [fentry(store, "Workbench:new", b"fresh")], label="base", drive=drive)
    plan = CP.build_plan(store, ["base"], existing_volumes=["Workbench"])
    assert plan.volume("Workbench").format_volume is True

    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)
    result = T.write_plain(plan, store.blobs, target, force=True)
    assert result.cleared == ["Workbench"]


def test_merge_into_an_existing_image_keeps_what_is_there(store, tmp_path):
    """The bug this guards: unlinking an existing image would let merge destroy it."""
    target = str(tmp_path / "out.hdf")

    first = plain_plan(store, [fentry(store, "Workbench:original", b"keep me")])
    T.write_plain(first, store.blobs, target)

    store2 = S.Store(os.path.join(str(tmp_path), "store2"))
    store2.init()
    second = plain_plan(store2, [fentry(store2, "Workbench:added", b"new thing")])
    assert second.volume("Workbench").format_volume is False  # merge, no partition record

    result = T.write_plain(second, store2.blobs, target, force=True)
    assert result.cleared == []

    _name, read, data = read_back(target)
    assert data["original"] == b"keep me"
    assert data["added"] == b"new thing"


def test_merge_replaces_a_colliding_file_and_says_so(store, tmp_path):
    """amitools refuses to overwrite (G22), so a collision means delete-then-write."""
    target = str(tmp_path / "out.hdf")
    first = plain_plan(store, [fentry(store, "Workbench:f", b"old")])
    T.write_plain(first, store.blobs, target)

    store2 = S.Store(os.path.join(str(tmp_path), "store2"))
    store2.init()
    second = plain_plan(store2, [fentry(store2, "Workbench:f", b"new")])
    result = T.write_plain(second, store2.blobs, target, force=True)

    assert any("replaced an existing file" in w for w in result.warnings)
    _name, _read, data = read_back(target)
    assert data["f"] == b"new"


def test_merge_to_a_missing_target_creates_it_and_explains(store, tmp_path):
    """Otherwise the plan's 'write into existing' line reads as a contradiction."""
    plan = plain_plan(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out.hdf")
    result = T.write_plain(plan, store.blobs, target)
    assert any("target does not exist, so it was created" in w for w in result.warnings)


def test_dos_type_defaults_are_reported(store, tmp_path):
    plan = plain_plan(store, [fentry(store, "Workbench:a", b"1")])
    result = T.write_plain(plan, store.blobs, str(tmp_path / "out.hdf"))
    assert any("defaulting to DOS3" in w for w in result.warnings)


def test_recorded_dos_type_is_honoured(store, tmp_path):
    """Writing with the wrong hash variant makes files invisible to AmigaDOS (G3)."""
    drive = {
        "scheme": D.DRIVE_SCHEME, "kind": "rdb", "block_size": 512, "cylinders": 1280,
        "heads": 1, "sectors": 32, "single_volume": False,
        "partitions": [partition(volume="Workbench", num_blocks=40960,
                                 dos_type="0x444f5301")],  # DOS1, plain FFS, no intl
    }
    make_base(store, [fentry(store, "Workbench:a", b"1")], label="base", drive=drive)
    plan = CP.build_plan(store, ["base"])
    target = str(tmp_path / "out.hdf")
    T.write_plain(plan, store.blobs, target)

    from amibuilder.addressing import parse
    from amibuilder.image import open_container

    with open_container(parse(target)) as container:
        assert container.boot_dos_type.raw == 0x444F5301


def test_plain_dry_run_writes_nothing(store, tmp_path):
    plan = plain_plan(store, [fentry(store, "Workbench:a", b"1")])
    target = str(tmp_path / "out.hdf")
    result = T.write_plain(plan, store.blobs, target, dry_run=True)
    assert not os.path.exists(target)
    assert result.files == 1


def test_plain_refuses_an_unwritable_plan(store, tmp_path):
    plan = plain_plan(store, [fentry(store, "Workbench:" + "x" * 40, b"1")])
    with pytest.raises(UsageError, match="unresolved problems"):
        T.write_plain(plan, store.blobs, str(tmp_path / "out.hdf"))


def test_meta_info_carries_the_exact_triple(store):
    """Unit-level guard on the thing that makes the write faithful."""
    from amitools.fs.TimeStamp import TimeStamp

    entry = M.ManifestEntry(
        path="Work:f", kind=M.FILE, blob="a" * 64, size=1, ts=(12345, 678, 9),
        protect="h-p-rwed", comment="note",
    )
    meta = T.meta_info_for(entry)
    stamp: TimeStamp = meta.get_mod_ts()
    assert (stamp.days, stamp.mins, stamp.ticks) == (12345, 678, 9)
    assert meta.get_protect_str() == "h-p-rwed"
    assert meta.get_comment_unicode_str() == "note"


# ---------------------------------------------------------------------------
# Restoring into an existing RDB drive
#
# The bug these cover: `write_rdb` used to rebuild the whole drive from the record every time, so
# restoring one volume left every other partition unformatted -- reported as a warning, with exit
# code 0 and a "verified" line underneath it.
# ---------------------------------------------------------------------------


def _three_volume_store(tmp_path) -> tuple[S.Store, str]:
    """A store holding a three-volume drive record, plus the source image it came from."""
    source = str(tmp_path / "source.hdf")
    images.make_multi_volume_hd(
        source,
        [
            images.VolumeSpec(
                partition=images.Partition(size="8MiB", dos_type="ffs+intl", bootable=True,
                                           volume="Boot"),
                files={"S/Startup-Sequence": b"C:Version\n", "C/Thing": bytes(600)},
            ),
            images.VolumeSpec(
                partition=images.Partition(size="6MiB", dos_type="ffs+intl", volume="Data"),
                files={"keep/me.txt": b"data volume content\n"},
            ),
            images.VolumeSpec(
                partition=images.Partition(dos_type="ffs", volume="Extra"),
                files={"third": b"third volume\n"},
            ),
        ],
        size="24Mi",
    )
    store = S.Store(str(tmp_path / "store"))
    store.init()
    with open_container(parse(source)) as container:
        record = D.capture(container)
        result = C.capture_container(container, store.blobs)
    layer = store.write_layer(
        entries=result.entries, kind=S.KIND_BASE, label="three", drive=record
    )
    store.set_ref("three", layer.id)
    return store, source


def test_restoring_one_volume_leaves_the_others_alone(tmp_path):
    """The headline fix. Previously Data: and Extra: were left unformatted."""
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)

    plan = CP.build_plan(store, ["three"], only_volumes=["Boot"],
                         existing_volumes=["Boot", "Data", "Extra"])
    result = T.write_rdb(plan, store.blobs, target, force=True)

    assert result.in_place
    assert sorted(result.untouched) == ["DH1", "DH2"]
    with open_container(parse(target)) as container:
        names = {p.volume_name for p in container.partitions(probe_volumes=True)}
    assert names == {"Boot", "Data", "Extra"}


def test_content_on_the_untouched_volumes_survives(tmp_path):
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)

    plan = CP.build_plan(store, ["three"], only_volumes=["Boot"],
                         existing_volumes=["Boot", "Data", "Extra"])
    T.write_rdb(plan, store.blobs, target, force=True)

    with open_container(parse(target)) as container:
        with container.open_volume(1) as data:
            assert data.read_file("keep/me.txt") == b"data volume content\n"
        with container.open_volume(2) as extra:
            assert extra.read_file("third") == b"third volume\n"


def test_the_partition_table_is_not_rewritten(tmp_path):
    """Rewriting it would move partitions whose contents are deliberately being kept."""
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)

    with open_container(parse(source)) as container:
        before = [(p.device_name, p.low_cyl, p.high_cyl, p.dos_type.raw)
                  for p in container.partitions()]

    plan = CP.build_plan(store, ["three"], only_volumes=["Boot"],
                         existing_volumes=["Boot", "Data", "Extra"])
    T.write_rdb(plan, store.blobs, target, force=True)

    with open_container(parse(target)) as container:
        after = [(p.device_name, p.low_cyl, p.high_cyl, p.dos_type.raw)
                 for p in container.partitions()]
    assert after == before


def test_a_restore_into_an_existing_drive_needs_force(tmp_path):
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)

    plan = CP.build_plan(store, ["three"], only_volumes=["Boot"],
                         existing_volumes=["Boot", "Data", "Extra"])
    with pytest.raises(UsageError, match="--force"):
        T.write_rdb(plan, store.blobs, target)


def test_the_refusal_names_the_volume_that_would_be_replaced(tmp_path):
    """So the confirmation is informed rather than a reflex."""
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)

    plan = CP.build_plan(store, ["three"], only_volumes=["Boot"],
                         existing_volumes=["Boot", "Data", "Extra"])
    with pytest.raises(UsageError, match="Boot:"):
        T.write_rdb(plan, store.blobs, target)


def test_a_drive_whose_layout_disagrees_is_refused(tmp_path):
    """Restoring into a differently-partitioned drive would write to the wrong places."""
    store, _source = _three_volume_store(tmp_path)
    other = str(tmp_path / "other.hdf")
    images.make_multi_volume_hd(
        other,
        [images.VolumeSpec(
            partition=images.Partition(dos_type="ffs+intl", bootable=True, volume="Boot"),
            files={"S/Startup-Sequence": b"C:Version\n"},
        )],
        size="24Mi",
    )

    plan = CP.build_plan(store, ["three"], existing_volumes=["Boot"])
    with pytest.raises(UsageError, match="does not match"):
        T.write_rdb(plan, store.blobs, other, force=True)


def test_the_layout_refusal_lists_every_difference(tmp_path):
    """Reporting only the first would hide that it is a different drive in several ways."""
    store, _source = _three_volume_store(tmp_path)
    other = str(tmp_path / "other.hdf")
    images.make_multi_volume_hd(
        other,
        [images.VolumeSpec(
            partition=images.Partition(dos_type="ffs+intl", bootable=True, volume="Boot"),
        )],
        size="32Mi",
    )

    plan = CP.build_plan(store, ["three"], existing_volumes=["Boot"])
    with pytest.raises(UsageError) as caught:
        T.write_rdb(plan, store.blobs, other, force=True)
    message = str(caught.value)
    assert "partition count" in message
    assert "cylinders" in message


def test_the_layout_refusal_says_how_to_proceed(tmp_path):
    """A refusal that does not say what to do next just moves the problem."""
    store, _source = _three_volume_store(tmp_path)
    other = str(tmp_path / "other.hdf")
    images.make_multi_volume_hd(
        other,
        [images.VolumeSpec(
            partition=images.Partition(dos_type="ffs+intl", bootable=True, volume="Boot"),
        )],
        size="24Mi",
    )
    plan = CP.build_plan(store, ["three"], existing_volumes=["Boot"])
    with pytest.raises(UsageError, match="Delete"):
        T.write_rdb(plan, store.blobs, other, force=True)


def test_a_plain_hdf_target_is_rebuilt_rather_than_restored_into(tmp_path):
    """A plain HDF has no partition table, so it cannot be restored into partition by partition."""
    store, _source = _three_volume_store(tmp_path)
    other = str(tmp_path / "other.hdf")
    images.make_plain_hdf(other, size="24Mi", volume="Plain")

    plan = CP.build_plan(store, ["three"], existing_volumes=["Boot"])
    result = T.write_rdb(plan, store.blobs, other, force=True)
    assert not result.in_place, "a non-RDB target must be rebuilt, not restored into"


def test_a_garbage_target_is_rebuilt_rather_than_restored_into(tmp_path):
    """An unreadable file is not something to restore into, and must not be mistaken for one."""
    store, _source = _three_volume_store(tmp_path)
    target = tmp_path / "garbage.hdf"
    target.write_bytes(b"definitely not a disk image")

    plan = CP.build_plan(store, ["three"])
    result = T.write_rdb(plan, store.blobs, str(target), force=True)
    assert not result.in_place
    with open_container(parse(str(target))) as container:
        assert len(list(container.partitions())) == 3


def test_merge_into_an_existing_volume_adds_without_clearing(tmp_path):
    """`merge` on an existing drive must open the volume, not recreate it."""
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)

    # Write something the layer does not know about, then merge the layer back in.
    with open_container(parse(target)) as container:
        pass
    images.write_files(target, {"amiga-made": b"written later\n"}, part=1)

    plan = CP.build_plan(store, ["three"], only_volumes=["Data"],
                         policies={"Data": "merge"},
                         existing_volumes=["Boot", "Data", "Extra"])
    T.write_rdb(plan, store.blobs, target, force=True)

    with open_container(parse(target)) as container:
        with container.open_volume(1) as data:
            assert data.read_file("amiga-made") == b"written later\n"
            assert data.read_file("keep/me.txt") == b"data volume content\n"


def test_replace_into_an_existing_volume_does_clear_it(tmp_path):
    """The other half: `replace` must not silently become a merge."""
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)
    images.write_files(target, {"doomed": b"should not survive\n"}, part=1)

    plan = CP.build_plan(store, ["three"], only_volumes=["Data"],
                         policies={"Data": "replace"},
                         existing_volumes=["Boot", "Data", "Extra"])
    result = T.write_rdb(plan, store.blobs, target, force=True)

    assert "Data" in result.cleared
    with open_container(parse(target)) as container:
        with container.open_volume(1) as data:
            names = {entry.path for _d, _dirs, files in data.walk() for entry in files}
    assert "doomed" not in names
    assert "keep/me.txt" in names


def test_preserve_leaves_an_existing_volume_completely_alone(tmp_path):
    store, source = _three_volume_store(tmp_path)
    target = str(tmp_path / "drive.hdf")
    shutil.copy2(source, target)
    images.write_files(target, {"untouched-marker": b"still here\n"}, part=1)

    plan = CP.build_plan(store, ["three"], only_volumes=["Data"],
                         policies={"Data": "preserve"},
                         existing_volumes=["Boot", "Data", "Extra"])
    result = T.write_rdb(plan, store.blobs, target, force=True)

    assert "DH1" in result.untouched
    with open_container(parse(target)) as container:
        with container.open_volume(1) as data:
            assert data.read_file("untouched-marker") == b"still here\n"


def test_a_fresh_drive_still_warns_about_partitions_it_cannot_fill(tmp_path):
    """The in-place change must not silence the fresh-drive case, where the warning is right."""
    store, _source = _three_volume_store(tmp_path)
    target = str(tmp_path / "new.hdf")

    plan = CP.build_plan(store, ["three"], only_volumes=["Boot"])
    result = T.write_rdb(plan, store.blobs, target)

    assert not result.in_place
    assert any("left unformatted" in w for w in result.warnings)
    assert result.untouched == []
