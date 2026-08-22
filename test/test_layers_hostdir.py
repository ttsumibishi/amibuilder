"""Capturing a host directory as a layer, and the `.uaem` sidecar format both ways.

`snap create ./dir` and `snap diff ./dir` turn a host directory -- typically one
`compose --format dir` produced and FS-UAE then booted -- straight into a layer with no HDF in
between. The tests that matter most here are the two round trips: `escape_name`/`unescape_name`
and `uaem_line`/`parse_uaem` must invert exactly, and a tree written by `write_directory` must
read back through `DirectoryVolume` as the same manifest. Everything else is a guard rail.

Nothing here touches a real image; every fixture is built from scratch under tmp_path.
"""

from __future__ import annotations

import json
import os
import types

import pytest

from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.errors import ImageError, UsageError
from amibuilder.image import ImageKind, open_container
from amibuilder.layers import capture as C
from amibuilder.layers import compose as CP
from amibuilder.layers import manifest as M
from amibuilder.layers import store as S
from amibuilder.layers import targets as T
from amibuilder.layers import uaem
from amibuilder.layers.blobs import BlobStore
from amibuilder.layers.hostdir import DirectoryVolume
from amibuilder import timestamps
from amibuilder.commands import snap

from test_layers_compose_plan import make_base  # reuse the base-layer builder


# ---------------------------------------------------------------------------
# uaem: the shared sidecar format, both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Startup-Sequence", "thing.library", "My File", "foo*bar", "a:b", "a/b", "50%",
     "trailing ", "trailing.", "cafe\u0301"],
)
def test_escape_unescape_round_trips(name):
    """A host name has to map back to the exact Amiga name, or the capture renames files."""
    assert uaem.unescape_name(uaem.escape_name(name)) == name


def test_unescape_leaves_a_bare_percent_alone():
    """Output this module produced never has a lone '%', so a hand-made one is left verbatim."""
    assert uaem.unescape_name("100% sure") == "100% sure"


def test_uaem_line_and_parse_round_trip_the_triple():
    entry = M.ManifestEntry(
        path="Work:S/Startup-Sequence", kind=M.FILE, blob="a" * 64, size=10,
        protect="h-p-rwed", ts=(17389, 587, 12), comment="a note",
    )
    protect, secs, ticks, comment = uaem.parse_uaem(uaem.uaem_line(entry))
    assert protect == "h-p-rwed"
    assert comment == "a note"
    assert timestamps.to_triple(secs, ticks) == (17389, 587, 12)


def test_parse_recovers_a_zero_timestamp_as_zero():
    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=1, ts=(0, 0, 0))
    protect, secs, ticks, comment = uaem.parse_uaem(uaem.uaem_line(entry))
    assert (secs, ticks) == (0, 0)
    assert comment == ""


def test_parse_keeps_a_comment_with_interior_spaces():
    entry = M.ManifestEntry(path="Work:f", kind=M.FILE, blob="a" * 64, size=1,
                            comment="two  spaces and more")
    _p, _s, _t, comment = uaem.parse_uaem(uaem.uaem_line(entry))
    assert comment == "two  spaces and more"


@pytest.mark.parametrize(
    "line",
    [
        "----rwed 2020-01-01",                       # too few fields
        "notaprot 2020-01-01 00:00:00.00 c",         # first field is not 8 protect chars
        "----rwed not-a-date 00:00:00.00 c",         # unparseable date
        "----rwed 2020-13-40 99:99:99.00 c",         # out-of-range date/time
    ],
)
def test_a_corrupt_uaem_line_raises(line):
    with pytest.raises(uaem.SidecarError):
        uaem.parse_uaem(line + "\n")


def test_read_sidecar_is_none_when_absent(tmp_path):
    assert uaem.read_sidecar(str(tmp_path / "no-such-file")) is None


def test_targets_still_exposes_the_writer_helpers():
    """Callers and tests reach these as `targets.escape_name` / `targets.uaem_line`."""
    assert T.escape_name is uaem.escape_name
    assert T.uaem_line is uaem.uaem_line
    assert T.UAEM_SUFFIX == uaem.UAEM_SUFFIX


# ---------------------------------------------------------------------------
# DirectoryVolume: name resolution
# ---------------------------------------------------------------------------


def test_volume_name_comes_from_the_override(tmp_path):
    (tmp_path / "whatever").mkdir()
    assert DirectoryVolume(str(tmp_path / "whatever"), name="Workbench").name == "Workbench"


def test_volume_name_defaults_to_the_unescaped_basename(tmp_path):
    d = tmp_path / "Games"
    d.mkdir()
    assert DirectoryVolume(str(d)).name == "Games"
    # A basename that was itself escaped is decoded, so a compose --format dir subdir round-trips.
    e = tmp_path / uaem.escape_name("My*Vol")
    e.mkdir()
    assert DirectoryVolume(str(e)).name == "My*Vol"


def test_a_name_with_a_colon_is_refused(tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(UsageError, match="pass --volume"):
        DirectoryVolume(str(tmp_path / "d"), name="bad:name")


def test_a_non_directory_is_refused(tmp_path):
    f = tmp_path / "file"
    f.write_bytes(b"x")
    with pytest.raises(ImageError, match="not a directory"):
        DirectoryVolume(str(f))


# ---------------------------------------------------------------------------
# DirectoryVolume: walk and read
# ---------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path):
    """A small host tree with one sidecar, one escaped name, and an empty directory."""
    root = tmp_path / "vol"
    (root / "S").mkdir(parents=True)
    (root / "Empty").mkdir()
    (root / "S" / "Startup-Sequence").write_bytes(b"echo hi\n")
    (root / "readme.txt").write_bytes(b"hello")
    (root / "readme.txt.uaem").write_bytes(b"h--prwed 2025-08-11 09:47:00.00 a comment\n")
    # Amiga name "pic*.iff" is stored on the host as "pic%2a.iff".
    (root / uaem.escape_name("pic*.iff")).write_bytes(b"IFF!")
    return root


def _walk_entries(dv: DirectoryVolume) -> dict[str, object]:
    seen: dict[str, object] = {}
    for _dirpath, dirs, files in dv.walk():
        for entry in list(dirs) + list(files):
            seen[entry.path] = entry
    return seen


def test_walk_records_files_and_directories(tree):
    seen = _walk_entries(DirectoryVolume(str(tree), name="Work"))
    assert seen["S"].is_dir
    assert seen["Empty"].is_dir  # empty directories are load-bearing on AmigaOS
    assert not seen["S/Startup-Sequence"].is_dir
    assert not seen["readme.txt"].is_dir


def test_walk_skips_the_sidecar_files_as_content(tree):
    seen = _walk_entries(DirectoryVolume(str(tree), name="Work"))
    assert "readme.txt.uaem" not in seen


def test_walk_unescapes_host_names(tree):
    seen = _walk_entries(DirectoryVolume(str(tree), name="Work"))
    assert "pic*.iff" in seen


def test_sidecar_metadata_is_used_when_present(tree):
    seen = _walk_entries(DirectoryVolume(str(tree), name="Work"))
    e = seen["readme.txt"]
    assert e.protect_str == "h--prwed"
    assert e.comment == "a comment"
    assert timestamps.to_triple(e.mod_secs, e.mod_ticks) == (17389, 587, 0)


def test_without_a_sidecar_the_host_mtime_and_default_protection_are_used(tree):
    seen = _walk_entries(DirectoryVolume(str(tree), name="Work"))
    e = seen["S/Startup-Sequence"]
    assert e.protect_str == M.DEFAULT_PROTECT
    assert e.mod_secs > 0  # taken from the host file, not left blank
    assert e.comment == ""


def test_read_file_maps_an_escaped_name_back_to_its_host_file(tree):
    dv = DirectoryVolume(str(tree), name="Work")
    assert dv.read_file("pic*.iff") == b"IFF!"
    assert dv.read_file("S/Startup-Sequence") == b"echo hi\n"


def test_a_corrupt_sidecar_warns_and_falls_back(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    (root / "f").write_bytes(b"x")
    (root / "f.uaem").write_bytes(b"this is not a uaem line\n")
    dv = DirectoryVolume(str(root), name="Work")
    seen = _walk_entries(dv)
    assert seen["f"].protect_str == M.DEFAULT_PROTECT
    assert any("corrupt" in w and "f" in w for w in dv.warnings)


def test_a_symlink_is_skipped_with_a_warning(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    (root / "real").write_bytes(b"x")
    os.symlink(str(root / "real"), str(root / "link"))
    dv = DirectoryVolume(str(root), name="Work")
    seen = _walk_entries(dv)
    assert "link" not in seen
    assert "real" in seen
    assert any("symlink skipped" in w for w in dv.warnings)


# ---------------------------------------------------------------------------
# capture_container: the DIRECTORY branch
# ---------------------------------------------------------------------------


def _capture(path: str, blobs: BlobStore, **kw) -> C.CaptureResult:
    with open_container(parse(path)) as container:
        return C.capture_container(container, blobs, **kw)


def test_capturing_a_directory_names_the_volume(tree, tmp_path):
    blobs = BlobStore(str(tmp_path / "blobs"))
    result = _capture(str(tree), blobs, directory_volume_name="Work")
    assert result.volumes == ["Work"]
    paths = {e.path for e in result.entries}
    assert "Work:S/Startup-Sequence" in paths
    assert "Work:Empty" in paths  # the empty dir survived


def test_capturing_a_directory_defaults_the_volume_to_the_basename(tmp_path):
    d = tmp_path / "Workbench"
    d.mkdir()
    (d / "a").write_bytes(b"1")
    blobs = BlobStore(str(tmp_path / "blobs"))
    result = _capture(str(d), blobs)
    assert result.volumes == ["Workbench"]
    assert {e.path for e in result.entries} == {"Workbench:a"}


def test_an_empty_directory_capture_warns(tmp_path):
    d = tmp_path / "Empty"
    d.mkdir()
    blobs = BlobStore(str(tmp_path / "blobs"))
    result = _capture(str(d), blobs, directory_volume_name="Empty")
    assert result.entries == []
    assert any("holds no files" in w for w in result.warnings)


def test_default_exclusions_apply_to_a_directory_source(tmp_path):
    d = tmp_path / "Work"
    (d / "T").mkdir(parents=True)
    (d / "T" / "scratch").write_bytes(b"junk")
    (d / "keep").write_bytes(b"1")
    blobs = BlobStore(str(tmp_path / "blobs"))
    result = _capture(str(d), blobs, directory_volume_name="Work")
    paths = {e.path for e in result.entries}
    assert "Work:keep" in paths
    assert not any(p.startswith("Work:T") for p in paths)  # T/ excluded like an image


# ---------------------------------------------------------------------------
# The round trip: write_directory -> capture back
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> S.Store:
    st = S.Store(str(tmp_path / "store"))
    st.init()
    return st


def test_write_directory_then_capture_is_faithful(store, tmp_path):
    """A tree written by the directory target reads back as the same manifest.

    This is the property the whole feature rests on: compose to a directory, boot it, capture
    it back, and the recorded protection, timestamps, comments and content are unchanged.
    """
    entries = [
        M.ManifestEntry(path="Workbench:S", kind=M.DIR, protect="----rwed", ts=(17389, 587, 0)),
        M.ManifestEntry(path="Workbench:WBStartup", kind=M.DIR, ts=(17389, 590, 25)),
        M.ManifestEntry(
            path="Workbench:S/Startup-Sequence", kind=M.FILE,
            blob=store.blobs.put_bytes(b"C:SetPatch QUIET\n").hash, size=17,
            protect="h--prwed", ts=(17389, 587, 12), comment="the boot script",
        ),
        M.ManifestEntry(
            path="Workbench:libs/thing.library", kind=M.FILE,
            blob=store.blobs.put_bytes(bytes(range(256))).hash, size=256,
            ts=(17400, 100, 0),
        ),
        M.ManifestEntry(path="Workbench:libs", kind=M.DIR, ts=(17400, 100, 0)),
    ]
    make_base(store, entries, label="base")
    plan = CP.build_plan(store, ["base"])

    target = str(tmp_path / "out")
    T.write_directory(plan, store.blobs, target)

    # write_directory lays the volume out under <target>/<volume>/, so that subdirectory *is*
    # the volume when captured back.
    result = _capture(os.path.join(target, "Workbench"), store.blobs,
                      directory_volume_name="Workbench")
    assert result.warnings == []

    def norm(es):
        return {
            e.path: (e.kind, e.protect, tuple(e.ts), e.comment, e.blob)
            for e in es
        }

    assert norm(result.entries) == norm(entries)


# ---------------------------------------------------------------------------
# The --volume guard
# ---------------------------------------------------------------------------


def test_volume_option_is_refused_on_a_non_directory_source():
    args = types.SimpleNamespace(volume="Foo")
    container = types.SimpleNamespace(kind=ImageKind.RDB)
    with pytest.raises(UsageError, match="only applies when the source is a host directory"):
        snap._check_volume_opt(args, container)


def test_volume_option_is_accepted_on_a_directory_source():
    args = types.SimpleNamespace(volume="Foo")
    container = types.SimpleNamespace(kind=ImageKind.DIRECTORY)
    snap._check_volume_opt(args, container)  # must not raise


def test_no_volume_option_is_fine_on_any_source():
    args = types.SimpleNamespace(volume=None)
    for kind in (ImageKind.RDB, ImageKind.DIRECTORY, ImageKind.PLAIN_HDF):
        snap._check_volume_opt(args, types.SimpleNamespace(kind=kind))


# ---------------------------------------------------------------------------
# End to end through the real CLI
# ---------------------------------------------------------------------------


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def run_json(capsys, *argv: str):
    code, text = run(capsys, *argv, "--json")
    assert code in (0, 6), text
    return code, json.loads(text)


@pytest.fixture
def src_dir(tmp_path):
    d = tmp_path / "staging"
    (d / "S").mkdir(parents=True)
    (d / "S" / "Startup-Sequence").write_bytes(b"C:SetPatch\n")
    (d / "S" / "Startup-Sequence.uaem").write_bytes(
        b"h--prwed 2025-08-11 09:47:00.00 boot\n"
    )
    (d / "readme").write_bytes(b"hello")
    return str(d)


def test_cli_create_from_a_directory(capsys, src_dir, tmp_path):
    store_dir = str(tmp_path / "store")
    code, text = run(capsys, "snap", "create", src_dir, "--label", "dirbase",
                     "--volume", "Work", "--store", store_dir)
    assert code == 0, text
    _c, data = run_json(capsys, "snap", "show", "dirbase", "--files", "--store", store_dir)
    entries = {e["p"]: e for e in data["entries"]}
    assert set(entries) == {"Work:S", "Work:S/Startup-Sequence", "Work:readme"}
    assert entries["Work:S/Startup-Sequence"]["pr"] == "h--prwed"
    assert entries["Work:S/Startup-Sequence"]["c"] == "boot"
    assert data["layer"]["source"]["kind"] == "directory"
    assert data["layer"]["drive"]["single_volume"] is True


def test_cli_create_defaults_the_volume_name_to_the_directory(capsys, src_dir, tmp_path):
    store_dir = str(tmp_path / "store")
    run(capsys, "snap", "create", src_dir, "--label", "dirbase", "--store", store_dir)
    _c, data = run_json(capsys, "snap", "show", "dirbase", "--files", "--store", store_dir)
    # src_dir's basename is "staging".
    assert all(e["p"].startswith("staging:") for e in data["entries"])


def test_cli_diff_of_a_directory_against_a_directory_base(capsys, src_dir, tmp_path):
    store_dir = str(tmp_path / "store")
    run(capsys, "snap", "create", src_dir, "--label", "base", "--volume", "Work",
        "--store", store_dir)
    # Add a file to the staging directory, then diff.
    with open(os.path.join(src_dir, "newfile"), "wb") as fh:
        fh.write(b"new content")
    code, data = run_json(capsys, "snap", "diff", src_dir, "--parent", "base",
                          "--label", "cand", "--volume", "Work", "--store", store_dir)
    assert code == 0
    paths = {c["path"] for c in data["changes"]}
    assert "Work:newfile" in paths


def test_cli_create_from_an_empty_directory_reports_nothing_captured(capsys, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    store_dir = str(tmp_path / "store")
    code = main(["snap", "create", str(empty), "--label", "z", "--volume", "Empty",
                 "--store", store_dir])
    captured = capsys.readouterr()
    # UsageError -> exit 2, and the reason is on stderr (the CLI prints AmibuilderError there).
    assert code == 2
    assert "nothing was captured" in captured.err
