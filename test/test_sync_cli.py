"""`sync` -- make a host directory and an image match, one direction, content-driven.

The properties that matter, and why each has a test:

* **It copies the right way, and only what changed.** `sync SOURCE DEST` always goes
  SOURCE -> DEST; a file is copied only when it is missing on the destination or its content
  differs, so a second run is a no-op (convergence) and a metadata-only difference does not
  churn. This is the SD-card-wear win, so "unchanged is skipped" is load-bearing, not cosmetic.
* **`--delete` is opt-in, and copies precede deletes.** Without it, a file the source dropped
  survives on the destination; with it, it is removed -- and a run that both copies and deletes
  does both, with the copy applied.
* **Exactly one host directory and one image; no raw devices.** Two directories, two images, or
  a `/dev` path on either side are refused with a clear message and exit 2, never acted on.
* **Nothing is written unless the whole plan can be.** A folder->image sync that will not fit,
  or a path that is a file on one side and a directory on the other, is refused whole (exit 5)
  while the image is untouched.

Written against the CLI, because the argument order, the direction rule, `--delete` and the
exit codes are as much of the contract as the copy.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from amibuilder import timestamps
from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.image import open_container

sys.path.insert(0, str(Path(__file__).parent))
from helpers import images  # noqa: E402


#: A small known tree used on both sides: four files across two subdirectories.
FILES = {
    "hello.txt": b"hello\n",
    "readme": b"readme contents\n",
    "S/Startup-Sequence": b'Echo "hi"\n',
    "C/List": bytes(range(64)),
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


@pytest.fixture
def ok(run):
    def _ok(*argv: str) -> str:
        code, out, err = run(*argv)
        assert code == 0, f"exit {code}: {err or out}"
        return out

    return _ok


def entries(image: str, path: str = "") -> dict[str, dict]:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return {e.name: e.as_dict() for e in vol.listdir(path)}


def entry_of(image: str, path: str) -> dict:
    parent, _, leaf = path.rpartition("/")
    return entries(image, parent)[leaf]


def exists(image: str, path: str) -> bool:
    parent, _, leaf = path.rpartition("/")
    return leaf in entries(image, parent)


def read(image: str, path: str) -> bytes:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return vol.read_file(path)


def host_tree(root: str | Path) -> dict[str, bytes]:
    """Every file under `root`, as {posix-relative-path: bytes}. Directories are implied."""
    base = Path(root)
    return {
        p.relative_to(base).as_posix(): p.read_bytes()
        for p in sorted(base.rglob("*")) if p.is_file()
    }


def payload(run, *argv: str) -> dict:
    code, out, err = run(*argv, "--json")
    assert code == 0, f"exit {code}: {err or out}"
    return json.loads(out)


# ---------------------------------------------------------------------------
# The core copy, both directions
# ---------------------------------------------------------------------------


def test_image_to_host_copies_everything(ok, plain_hdf, workdir):
    images.write_files(plain_hdf, FILES)
    dest = workdir / "backup"
    ok("sync", plain_hdf, str(dest))
    assert host_tree(dest) == FILES


def test_host_to_image_copies_everything(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    for rel, data in FILES.items():
        assert read(plain_hdf, rel) == data
    assert entry_of(plain_hdf, "S")["type"] == "dir"


def test_image_to_host_creates_missing_dest_dir(ok, plain_hdf, workdir):
    images.write_files(plain_hdf, FILES)
    (workdir / "does").mkdir()  # the immediate parent must exist; the backup dir need not
    ok("sync", plain_hdf, str(workdir / "does" / "backup"))
    assert (workdir / "does" / "backup" / "hello.txt").read_bytes() == b"hello\n"


def test_rdb_partition_selector(ok, rdb_hdf, workdir):
    """A `:selector` syncs just that partition against the folder."""
    images.write_files(rdb_hdf, FILES, part=0)
    dest = workdir / "backup"
    ok("sync", f"{rdb_hdf}:Work", str(dest))
    assert host_tree(dest) == FILES


# ---------------------------------------------------------------------------
# Content-driven: convergence, new and changed
# ---------------------------------------------------------------------------


def test_second_run_is_a_no_op(run, ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    p = payload(run, "sync", src, plain_hdf)
    assert p["counts"]["copied"] == 0
    assert p["counts"]["deleted"] == 0
    assert p["counts"]["unchanged"] >= len(FILES)


def test_new_and_changed_are_the_only_copies(run, ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    (Path(src) / "readme").write_bytes(b"a different readme\n")   # changed
    (Path(src) / "extra.txt").write_bytes(b"brand new\n")          # new
    p = payload(run, "sync", src, plain_hdf)
    assert {c["path"]: c["reason"] for c in p["copied"]} == {
        "readme": "changed", "extra.txt": "new"}
    assert read(plain_hdf, "readme") == b"a different readme\n"
    assert read(plain_hdf, "extra.txt") == b"brand new\n"


def test_metadata_only_difference_does_not_recopy(run, ok, plain_hdf, workdir):
    """A file identical in content but with different protection is left alone, so sync
    converges rather than copying it forever (v1 does not carry metadata)."""
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    ok("protect", plain_hdf, "readme", "--bits", "r")  # change only the image's metadata
    p = payload(run, "sync", src, plain_hdf)
    assert p["counts"]["copied"] == 0
    assert p["counts"]["metadata_only"] >= 1


# ---------------------------------------------------------------------------
# --delete
# ---------------------------------------------------------------------------


def test_without_delete_extra_dest_file_survives(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    (Path(src) / "hello.txt").unlink()
    ok("sync", src, plain_hdf)               # no --delete
    assert exists(plain_hdf, "hello.txt")     # still there


def test_delete_removes_extra_dest_file(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    (Path(src) / "hello.txt").unlink()
    ok("sync", src, plain_hdf, "--delete")
    assert not exists(plain_hdf, "hello.txt")
    assert read(plain_hdf, "readme") == FILES["readme"]  # others intact


def test_delete_removes_extra_directory_recursively(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    shutil.rmtree(Path(src) / "S")
    ok("sync", src, plain_hdf, "--delete")
    assert not exists(plain_hdf, "S")


def test_delete_on_host_side_removes_extra(ok, plain_hdf, workdir):
    """image -> host with --delete prunes the host folder too."""
    images.write_files(plain_hdf, FILES)
    dest = workdir / "backup"
    ok("sync", plain_hdf, str(dest))
    (dest / "stale.txt").write_bytes(b"left over\n")   # not on the image
    ok("sync", plain_hdf, str(dest), "--delete")
    assert not (dest / "stale.txt").exists()
    assert (dest / "hello.txt").exists()


def test_one_run_copies_and_deletes(run, ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    (Path(src) / "hello.txt").unlink()               # a deletion
    (Path(src) / "new.txt").write_bytes(b"NEW\n")     # and a copy, same run
    p = payload(run, "sync", src, plain_hdf, "--delete")
    assert [c["path"] for c in p["copied"]] == ["new.txt"]
    assert [d["path"] for d in p["deleted"]] == ["hello.txt"]
    assert read(plain_hdf, "new.txt") == b"NEW\n"
    assert not exists(plain_hdf, "hello.txt")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_to_image_writes_nothing(run, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    code, out, _ = run("sync", src, plain_hdf, "-n")
    assert code == 0
    assert "would sync" in out
    assert not exists(plain_hdf, "hello.txt")


def test_dry_run_to_host_creates_no_directory(run, plain_hdf, workdir):
    images.write_files(plain_hdf, FILES)
    dest = workdir / "backup"
    code, out, _ = run("sync", plain_hdf, str(dest), "-n")
    assert code == 0
    assert not dest.exists()


# ---------------------------------------------------------------------------
# JSON, exclusions
# ---------------------------------------------------------------------------


def test_json_shape(run, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    p = payload(run, "sync", src, plain_hdf)
    assert p["direction"] == "to-image"
    assert p["source"]["kind"] == "host directory"
    assert p["dest"]["kind"] == "image"
    assert set(p) >= {"source", "dest", "direction", "delete", "dry_run",
                      "copied", "deleted", "counts", "copied_bytes", "warnings"}
    assert set(p["counts"]) == {"copied", "new", "changed", "deleted",
                                "unchanged", "metadata_only"}
    assert all(c["kind"] in ("f", "d") for c in p["copied"])


def test_exclude_skips_matching_paths(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), {**FILES, "notes.tmp": b"scratch"})
    ok("sync", src, plain_hdf, "--exclude", "**/notes.tmp")
    assert not exists(plain_hdf, "notes.tmp")
    assert exists(plain_hdf, "hello.txt")


def test_default_excludes_skip_temp_dir(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), {**FILES, "T/scratch": b"tmp"})
    ok("sync", src, plain_hdf)
    assert not exists(plain_hdf, "T")


def test_no_default_excludes_syncs_temp_dir(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), {**FILES, "T/scratch": b"tmp"})
    ok("sync", src, plain_hdf, "--no-default-excludes")
    assert read(plain_hdf, "T/scratch") == b"tmp"


# ---------------------------------------------------------------------------
# Refusals -- exactly one directory and one image, no devices
# ---------------------------------------------------------------------------


def test_refuse_two_host_directories(run, workdir):
    a = images.make_tree(str(workdir / "a"), {"x": b"1"})
    b = images.make_tree(str(workdir / "b"), {"y": b"2"})
    code, _, err = run("sync", a, b)
    assert code == 2
    assert "host directories" in err


def test_refuse_two_images(run, plain_hdf, workdir):
    other = images.make_plain_hdf(str(workdir / "other.hdf"), size="10Mi", volume="Other")
    code, _, err = run("sync", plain_hdf, other)
    assert code == 2
    assert "inject" in err  # points at the right tool for image->image


def test_refuse_device_source(run, workdir):
    dest = images.make_tree(str(workdir / "d"), {"x": b"1"})
    code, _, err = run("sync", "/dev/rdisk99", dest)
    assert code == 2
    assert "raw devices" in err


def test_refuse_device_dest(run, plain_hdf):
    code, _, err = run("sync", plain_hdf, "/dev/rdisk99")
    assert code == 2
    assert "raw devices" in err


def test_refuse_missing_source(run, plain_hdf, workdir):
    code, _, _ = run("sync", str(workdir / "nope"), plain_hdf)
    assert code == 2  # AddressError: no such file


# ---------------------------------------------------------------------------
# Whole-or-nothing guards, and a valid volume afterwards
# ---------------------------------------------------------------------------


def test_capacity_refused_leaves_image_untouched(run, workdir):
    small = images.make_plain_hdf(str(workdir / "small.hdf"), size="2Mi", volume="Small")
    src = images.make_tree(str(workdir / "big"), {"huge.bin": bytes(3 * 1024 * 1024)})
    code, _, err = run("sync", src, small)
    assert code == 5  # ImageError: capacity
    assert not exists(small, "huge.bin")


def test_kind_conflict_refused_whole(run, ok, plain_hdf, workdir):
    """A path that is a directory on the image and a file in the folder is refused, and the
    image is left as it was."""
    ok("mkdir", plain_hdf, "Clash")                       # a directory on the image
    src = images.make_tree(str(workdir / "src"), {"Clash": b"but a file here"})
    code, _, err = run("sync", src, plain_hdf)
    assert code == 5
    assert "file on one side and a directory" in err
    assert entry_of(plain_hdf, "Clash")["type"] == "dir"  # untouched


def test_leaves_a_valid_volume(run, ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    code, out, err = run("check", plain_hdf)
    assert code == 0, f"{out}\n{err}"


# ---------------------------------------------------------------------------
# Modification time is carried across (content + mtime, per the v1 contract)
# ---------------------------------------------------------------------------


def test_host_to_image_preserves_mtime(ok, plain_hdf, workdir):
    src = images.make_tree(str(workdir / "src"), {"dated.txt": b"when\n"})
    host_file = Path(src) / "dated.txt"
    import os
    stamp = 1_600_000_000  # a fixed 2020 mtime
    os.utime(host_file, (stamp, stamp))
    ok("sync", src, plain_hdf)
    expected_secs, _ = timestamps.from_unix(stamp)
    assert entry_of(plain_hdf, "dated.txt")["modified_amiga_secs"] == expected_secs
