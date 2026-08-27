"""`sync` -- make a host directory and an image match, one direction, content-driven.

The properties that matter, and why each has a test:

* **It copies the right way, and only what changed.** `sync SOURCE DEST` always goes
  SOURCE -> DEST; a file is copied only when it is missing on the destination or its content
  differs, so a second run is a no-op (convergence) and a metadata-only difference does not
  churn. This is the SD-card-wear win, so "unchanged is skipped" is load-bearing, not cosmetic.
* **`--delete` is opt-in, and copies precede deletes.** Without it, a file the source dropped
  survives on the destination; with it, it is removed -- and a run that both copies and deletes
  does both, with the copy applied.
* **A folder and an image either way round, or two images; no raw devices.** Two host
  directories, one image file named twice (including two partitions of it), or a `/dev` path on
  either side are refused with a clear message and exit 2, never acted on.
* **Metadata travels in every direction, and must converge.** Image->image carries protection
  bits and comments directly; the host directions carry them in `.uaem` sidecars, written on the
  way out and read on the way back, on by default with `--no-metadata` to opt out. A
  metadata-only difference is reconciled *in place* rather than by rewriting content, and a
  second run must be a no-op -- convergence is the guard that matters most, because a direction
  that cannot converge rewrites the card on every single run.
* **A folder with no sidecars states nothing, and must not be read as stating the default.**
  Restoring from such a folder leaves the image's own protection bits alone rather than
  resetting them to `----rwed`. That is the one regression here that would be both silent and
  destructive -- it would break `Resident` on a real install -- so it has its own guard test.
* **Nothing is written unless the whole plan can be.** A folder->image sync that will not fit,
  or a path that is a file on one side and a directory on the other, is refused whole (exit 5)
  while the image is untouched.

Written against the CLI, because the argument order, the direction rule, `--delete` and the
exit codes are as much of the contract as the copy.
"""

from __future__ import annotations

import datetime as dt
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
from helpers import images

#: A small known tree used on both sides: four files across two subdirectories.
FILES = {
    "hello.txt": b"hello\n",
    "readme": b"readme contents\n",
    "S/Startup-Sequence": b'Echo "hi"\n',
    "C/List": bytes(range(64)),
}

#: Two real protection spellings, taken from a measurement of an actual Workbench 3.2 install
#: rather than invented: 742 of its 882 entries (84%) carry something other than the default.
#: `--p-rwed` is on all 83 commands in `C/` -- losing its `p` (pure) bit stops `Resident`
#: working, which is a subtly broken system with nothing obvious to point at. `-s--rw-d` is the
#: script bit, on 8 entries. Both are used as round-trip subjects below.
PURE = "--p-rwed"
SCRIPT = "-s--rw-d"

#: What a freshly created entry carries, and so what a lossy restore would flatten everything to.
DEFAULT_PROTECT = "----rwed"


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
    """Every *content* file under `root`, as {posix-relative-path: bytes}.

    `.uaem` sidecars are excluded, the same way `DirectoryVolume.walk()` excludes them: they are
    metadata describing their neighbour, not files in their own right, so a content assertion
    should not have to enumerate them. `sidecar_tree` asserts on them separately, which keeps
    "the right bytes arrived" and "the right metadata arrived" as two readable claims.
    """
    base = Path(root)
    return {
        p.relative_to(base).as_posix(): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and not p.name.endswith(".uaem")
    }


def sidecar_tree(root: str | Path) -> dict[str, str]:
    """Every `.uaem` sidecar under `root`, as {path-it-describes: sidecar text}."""
    base = Path(root)
    return {
        p.relative_to(base).as_posix()[: -len(".uaem")]: p.read_text("utf-8")
        for p in sorted(base.rglob("*.uaem")) if p.is_file()
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
    converges rather than copying it forever.

    The source here is a hand-made folder with no `.uaem` sidecars, so it states no metadata
    and the image's own bits are left exactly as they were -- see
    `test_restore_from_sidecarless_folder_does_not_strip_protection` for why that matters.
    Image->image reconciles such a difference instead; see
    `test_image_to_image_metadata_only_is_fixed_without_rewriting_content`.
    """
    src = images.make_tree(str(workdir / "src"), FILES)
    ok("sync", src, plain_hdf)
    ok("protect", plain_hdf, "readme", "--bits", "r")  # change only the image's metadata
    p = payload(run, "sync", src, plain_hdf)
    assert p["counts"]["copied"] == 0
    assert p["counts"]["metadata_only"] >= 1
    assert p["counts"]["metadata"] == 0
    assert entry_of(plain_hdf, "readme")["protect"] == "----r---"


# ---------------------------------------------------------------------------
# Host directions: AmigaDOS metadata travels in `.uaem` sidecars
# ---------------------------------------------------------------------------


def test_image_to_host_writes_a_sidecar_for_every_entry(ok, plain_hdf, workdir):
    """Directories included, and for entries whose metadata is the default too.

    Writing the uninformative ones as well is deliberate: it is what makes the *absence* of a
    sidecar mean exactly one thing on the way back -- "this folder was not written by us" -- which
    is the distinction `_states_metadata` depends on.
    """
    images.write_files(plain_hdf, FILES)
    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    ok("comment", plain_hdf, "readme", "--text", "read me first")
    dest = workdir / "backup"
    ok("sync", plain_hdf, str(dest))

    assert host_tree(dest) == FILES                       # content untouched by the sidecars
    cars = sidecar_tree(dest)
    assert set(cars) == set(FILES) | {"S", "C"}
    assert cars["C/List"].startswith(PURE + " ")
    assert cars["hello.txt"].startswith(DEFAULT_PROTECT + " ")
    assert cars["readme"].rstrip("\n").endswith(" read me first")


def test_sidecars_restore_protection_onto_a_fresh_image(ok, plain_hdf, workdir):
    """The whole point of the feature: card -> folder -> card keeps the metadata."""
    images.write_files(plain_hdf, FILES)
    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    ok("protect", plain_hdf, "S/Startup-Sequence", f"--bits={SCRIPT}")
    ok("protect", plain_hdf, "C", f"--bits={PURE}")
    ok("comment", plain_hdf, "readme", "--text", "read me first")
    backup = workdir / "backup"
    ok("sync", plain_hdf, str(backup))

    restored = images.make_plain_hdf(str(workdir / "restored.hdf"), size="20Mi", volume="Plain")
    ok("sync", str(backup), restored)
    assert entry_of(restored, "C/List")["protect"] == PURE
    assert entry_of(restored, "S/Startup-Sequence")["protect"] == SCRIPT
    assert entry_of(restored, "C")["protect"] == PURE       # a directory, the trap case
    assert entry_of(restored, "readme")["comment"] == "read me first"
    assert read(restored, "C/List") == FILES["C/List"]


def test_restore_from_sidecarless_folder_does_not_strip_protection(run, ok, plain_hdf, workdir):
    """The hazard this feature had to be designed around, and the reason for `_states_metadata`.

    A folder with no sidecars -- assembled by hand, or written by a version of this tool that did
    not have them -- reports the default `----rwed` for every entry, because that is all
    `DirectoryVolume` can infer from a plain file. Reading that as a *statement* would make a
    restore reset the card's protection to the default: on a real install that is 84% of entries,
    including the pure bit on every command in `C/`, destroyed by the command whose entire job is
    to put files back, with no error and nothing to point at.
    """
    src = images.make_tree(str(workdir / "src"), FILES)
    assert sidecar_tree(src) == {}, "fixture must have no sidecars or this proves nothing"
    ok("sync", src, plain_hdf)
    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    ok("protect", plain_hdf, "C", f"--bits={PURE}")
    ok("comment", plain_hdf, "readme", "--text", "keep me")

    p = payload(run, "sync", src, plain_hdf)
    assert p["counts"]["metadata"] == 0, "an entry the source never stated must not be 'fixed'"
    assert entry_of(plain_hdf, "C/List")["protect"] == PURE
    assert entry_of(plain_hdf, "C")["protect"] == PURE
    assert entry_of(plain_hdf, "readme")["comment"] == "keep me"
    # Left alone, but said out loud rather than silently skipped.
    assert "no .uaem sidecar" in ok("sync", src, plain_hdf)


def test_host_directions_converge_with_metadata(run, ok, plain_hdf, workdir):
    """A second run must be a complete no-op both ways round.

    Non-convergence would not be cosmetic: it would rewrite every sidecar on each backup and
    re-set every bit on the card on each restore, which is precisely the SD-card wear this
    command exists to avoid. Directories are in the fixture on purpose --
    `Volume.mkdir(exist_ok=True)` returns an existing directory *without* applying the metadata
    it was passed, so the folder->image path has to re-stamp them or it would re-report the same
    difference forever.
    """
    images.write_files(plain_hdf, FILES)
    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    ok("protect", plain_hdf, "C", f"--bits={PURE}")
    backup = workdir / "backup"

    ok("sync", plain_hdf, str(backup))
    second = payload(run, "sync", plain_hdf, str(backup))
    assert second["counts"]["copied"] == 0
    assert second["counts"]["metadata"] == 0, "image->folder is not converging"
    assert second["counts"]["metadata_only"] == 0

    restored = images.make_plain_hdf(str(workdir / "r.hdf"), size="20Mi", volume="Plain")
    ok("sync", str(backup), restored)
    again = payload(run, "sync", str(backup), restored)
    assert again["counts"]["copied"] == 0
    assert again["counts"]["metadata"] == 0, "folder->image is not converging"
    assert again["counts"]["metadata_only"] == 0


def test_image_to_host_metadata_fix_rewrites_only_the_sidecar(run, ok, plain_hdf, workdir):
    """A protection change updates the sidecar and does not re-copy the file."""
    images.write_files(plain_hdf, FILES)
    backup = workdir / "backup"
    ok("sync", plain_hdf, str(backup))
    target = backup / "C" / "List"
    before = target.stat().st_mtime_ns

    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    p = payload(run, "sync", plain_hdf, str(backup))
    assert p["counts"]["copied"] == 0
    assert p["copied_bytes"] == 0
    assert [m["path"] for m in p["metadata"]] == ["C/List"]
    assert sidecar_tree(backup)["C/List"].startswith(PURE + " ")
    assert target.read_bytes() == FILES["C/List"]
    assert target.stat().st_mtime_ns == before, "the file was rewritten, not just its metadata"


def test_no_metadata_writes_no_sidecars(ok, plain_hdf, workdir):
    images.write_files(plain_hdf, FILES)
    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    dest = workdir / "backup"
    ok("sync", plain_hdf, str(dest), "--no-metadata")
    assert host_tree(dest) == FILES
    assert sidecar_tree(dest) == {}


def test_no_metadata_does_not_read_sidecars_either(run, ok, plain_hdf, workdir):
    """Symmetric: the flag means "do not carry metadata", not "do not write it"."""
    images.write_files(plain_hdf, FILES)
    ok("protect", plain_hdf, "C/List", f"--bits={PURE}")
    backup = workdir / "backup"
    ok("sync", plain_hdf, str(backup))
    assert sidecar_tree(backup), "the sidecars must exist for this to be a real opt-out"

    restored = images.make_plain_hdf(str(workdir / "r.hdf"), size="20Mi", volume="Plain")
    p = payload(run, "sync", str(backup), restored, "--no-metadata")
    assert p["metadata_enabled"] is False
    assert p["counts"]["metadata"] == 0
    assert entry_of(restored, "C/List")["protect"] == DEFAULT_PROTECT


def test_delete_removes_the_sidecar_too(ok, plain_hdf, workdir):
    """An orphan would let a later sync read metadata for an entry that no longer exists.

    The directory case is the one easy to get wrong: `S`'s own sidecar is `S.uaem`, a sibling of
    the tree, so removing the tree does not take it along.
    """
    images.write_files(plain_hdf, FILES)
    backup = workdir / "backup"
    ok("sync", plain_hdf, str(backup))
    assert {"readme", "S"} <= set(sidecar_tree(backup))

    ok("rm", plain_hdf, "readme")
    ok("sync", plain_hdf, str(backup), "--delete")
    assert not (backup / "readme").exists()
    assert not (backup / "readme.uaem").exists()

    ok("rm", plain_hdf, "S", "-r")
    ok("sync", plain_hdf, str(backup), "--delete")
    assert not (backup / "S").exists()
    assert not (backup / "S.uaem").exists(), "a directory's own sidecar sits outside its tree"


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
    code, _out, _ = run("sync", plain_hdf, str(dest), "-n")
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
                      "copied", "deleted", "metadata", "counts", "copied_bytes", "warnings"}
    assert set(p["counts"]) == {"copied", "new", "changed", "deleted",
                                "unchanged", "metadata_only", "metadata"}
    assert all(c["kind"] in ("f", "d") for c in p["copied"])
    assert p["metadata_enabled"] is True
    # Every entry here is new, so it is a copy: a metadata fix is for an entry whose content
    # already matches. (And this source folder has no sidecars, so it states no metadata.)
    assert p["metadata"] == []


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
# Image -> image: the same content rules, plus metadata
# ---------------------------------------------------------------------------


@pytest.fixture
def two_images(workdir) -> tuple[str, str]:
    """A populated source image and an empty destination image."""
    src = images.make_plain_hdf(str(workdir / "src.hdf"), size="20Mi", volume="Alpha")
    dst = images.make_plain_hdf(str(workdir / "dst.hdf"), size="20Mi", volume="Beta")
    images.write_files(src, FILES)
    return src, dst


def test_image_to_image_copies_everything(ok, two_images):
    src, dst = two_images
    ok("sync", src, dst)
    for rel, data in FILES.items():
        assert read(dst, rel) == data
    assert entry_of(dst, "S")["type"] == "dir"


def test_image_to_image_carries_protection_and_comment(ok, two_images):
    """Both sides are real Amiga volumes, so a faithful copy is the only sensible one."""
    src, dst = two_images
    ok("protect", src, "readme", "--bits", "r")
    ok("comment", src, "readme", "--text", "read me first")
    ok("sync", src, dst)
    got = entry_of(dst, "readme")
    assert got["protect"] == entry_of(src, "readme")["protect"] == "----r---"
    assert got["comment"] == "read me first"


def test_image_to_image_second_run_is_a_no_op(run, ok, two_images):
    src, dst = two_images
    ok("sync", src, dst)
    p = payload(run, "sync", src, dst)
    assert p["counts"]["copied"] == 0
    assert p["counts"]["metadata"] == 0
    assert p["counts"]["unchanged"] >= len(FILES)


def test_image_to_image_metadata_only_is_fixed_without_rewriting_content(run, ok, two_images):
    """A protection change must not re-copy the file: the content already matches."""
    src, dst = two_images
    ok("sync", src, dst)
    ok("protect", src, "readme", "--bits", "r")
    p = payload(run, "sync", src, dst)
    assert p["counts"]["copied"] == 0          # no data rewritten -- the point of the split
    assert p["copied_bytes"] == 0
    assert [m["path"] for m in p["metadata"]] == ["readme"]
    assert entry_of(dst, "readme")["protect"] == "----r---"
    assert read(dst, "readme") == FILES["readme"]


def test_image_to_image_converges_after_a_metadata_fix(run, ok, two_images):
    """The guard that matters most.

    `Volume.mkdir(exist_ok=True)` returns an existing directory *without* applying the
    metadata it was passed, so reconciling by way of the create path would leave the
    difference in place and re-report it on every run, forever. This asserts the third run
    is genuinely clean.
    """
    src, dst = two_images
    ok("sync", src, dst)
    ok("protect", src, "S", "--bits", "r")          # a *directory*, the trap case
    ok("comment", src, "S", "--text", "scripts")
    first = payload(run, "sync", src, dst)
    assert first["counts"]["metadata"] >= 1
    second = payload(run, "sync", src, dst)
    assert second["counts"]["metadata"] == 0, "metadata fix did not stick -- not converging"
    assert second["counts"]["copied"] == 0
    assert entry_of(dst, "S")["protect"] == "----r---"
    assert entry_of(dst, "S")["comment"] == "scripts"


def test_image_to_image_preserves_a_directory_timestamp(ok, two_images):
    """A copied directory keeps the source's mtime, not the time its children landed.

    Writing a file into a directory re-stamps that directory -- correct AmigaDOS behaviour
    and exactly wrong when reproducing a tree, so the source time is re-applied once the
    contents exist (the reason `inject._restamp_dirs` exists). Uses a fixed old timestamp
    rather than comparing against "now", so the assertion cannot pass by coincidence.
    """
    src, dst = two_images
    old_secs = 1_000_000_000          # a fixed, unmistakably-not-now Amiga second count
    with open_container(parse(src), writable=True) as c:
        with c.open_addressed_volume() as vol:
            vol.set_times("S", old_secs, 0)
            vol.flush()

    ok("sync", src, dst)
    assert entry_of(dst, "S")["modified_amiga_secs"] == old_secs


def test_image_to_image_new_and_changed_are_the_only_copies(run, ok, two_images, workdir):
    src, dst = two_images
    ok("sync", src, dst)

    changed = workdir / "readme"                      # same name, different content
    changed.write_bytes(b"a different readme\n")
    ok("cp", str(changed), src, "--force")
    brand_new = workdir / "extra.txt"
    brand_new.write_bytes(b"brand new\n")
    ok("cp", str(brand_new), src)

    p = payload(run, "sync", src, dst)
    assert {c["path"]: c["reason"] for c in p["copied"]} == {
        "readme": "changed", "extra.txt": "new"}
    assert read(dst, "readme") == b"a different readme\n"
    assert read(dst, "extra.txt") == b"brand new\n"


def test_image_to_image_delete_removes_extra_entries(ok, two_images):
    src, dst = two_images
    ok("sync", src, dst)
    ok("rm", src, "hello.txt")
    ok("sync", src, dst, "--delete")
    assert not exists(dst, "hello.txt")
    assert read(dst, "readme") == FILES["readme"]


def test_image_to_image_without_delete_keeps_extra_entries(ok, two_images):
    src, dst = two_images
    ok("sync", src, dst)
    ok("rm", src, "hello.txt")
    ok("sync", src, dst)
    assert exists(dst, "hello.txt")


def test_image_to_image_copies_and_deletes_in_one_run(run, ok, two_images, workdir):
    src, dst = two_images
    ok("sync", src, dst)
    ok("rm", src, "hello.txt")
    extra = workdir / "new.txt"
    extra.write_bytes(b"NEW\n")
    ok("cp", str(extra), src)
    p = payload(run, "sync", src, dst, "--delete")
    assert [c["path"] for c in p["copied"]] == ["new.txt"]
    assert [d["path"] for d in p["deleted"]] == ["hello.txt"]
    assert read(dst, "new.txt") == b"NEW\n"
    assert not exists(dst, "hello.txt")


def test_image_to_image_dry_run_writes_nothing(run, two_images):
    src, dst = two_images
    code, out, _ = run("sync", src, dst, "-n")
    assert code == 0
    assert "would sync" in out
    assert not exists(dst, "hello.txt")


def test_image_to_image_json_shape(run, two_images):
    src, dst = two_images
    p = payload(run, "sync", src, dst)
    assert p["direction"] == "image-to-image"
    assert p["source"]["kind"] == "image"
    assert p["dest"]["kind"] == "image"


def test_image_to_image_rdb_partition_selectors(ok, rdb_hdf, workdir):
    """A `:selector` on each side syncs just those two partitions."""
    other = images.make_rdb_hdf(str(workdir / "other.hdf"), size="64Mi")
    images.write_files(rdb_hdf, FILES, part=0)
    ok("sync", f"{rdb_hdf}:Work", f"{other}:Test")
    for rel, data in FILES.items():
        assert read(f"{other}:Test", rel) == data


def test_image_to_image_capacity_refused_leaves_dest_untouched(run, workdir):
    src = images.make_plain_hdf(str(workdir / "big.hdf"), size="20Mi", volume="Big")
    images.write_files(src, {"huge.bin": bytes(3 * 1024 * 1024)})
    small = images.make_plain_hdf(str(workdir / "small.hdf"), size="2Mi", volume="Small")
    code, _, _ = run("sync", src, small)
    assert code == 5
    assert not exists(small, "huge.bin")


def test_image_to_image_leaves_a_valid_volume(run, ok, two_images):
    src, dst = two_images
    ok("sync", src, dst)
    code, out, err = run("check", dst)
    assert code == 0, f"{out}\n{err}"


# ---------------------------------------------------------------------------
# Refusals -- no two host directories, no devices, no single file twice
# ---------------------------------------------------------------------------


def test_refuse_two_host_directories(run, workdir):
    a = images.make_tree(str(workdir / "a"), {"x": b"1"})
    b = images.make_tree(str(workdir / "b"), {"y": b"2"})
    code, _, err = run("sync", a, b)
    assert code == 2
    assert "host directories" in err


def test_refuse_the_same_image_file(run, plain_hdf):
    code, _, err = run("sync", plain_hdf, plain_hdf)
    assert code == 2
    assert "same image file" in err


def test_refuse_two_partitions_of_one_image(run, rdb_two_part):
    """One file, two handles, one of them writing -- `inject`'s hazard exactly."""
    code, _, err = run("sync", f"{rdb_two_part}:0", f"{rdb_two_part}:1")
    assert code == 2
    assert "same image file" in err


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
    code, _, _err = run("sync", src, small)
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
# Modification time is carried across
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


def test_host_to_image_dates_a_directory_from_its_sidecar_not_the_restore(ok, plain_hdf,
                                                                         workdir):
    """A restored directory keeps its own date rather than the moment its contents were written.

    Writing a file into a directory re-stamps that directory, so the stated time has to be
    applied again after the contents exist. Fidelity rather than convergence: the diff ignores
    timestamps, so getting this wrong would not churn -- every directory on a restored install
    would just quietly be dated today, and nothing would ever report it.

    The stated time is set by hand to 1995 so that "the restore's own clock" cannot pass by
    coincidence, which it could if the assertion only compared against a value seconds old.
    """
    images.write_files(plain_hdf, FILES)
    backup = workdir / "backup"
    ok("sync", plain_hdf, str(backup))
    (backup / "C.uaem").write_text(f"{DEFAULT_PROTECT} 1995-06-15 12:34:56.00 \n")
    want, _ = timestamps.from_datetime(dt.datetime(1995, 6, 15, 12, 34, 56))

    restored = images.make_plain_hdf(str(workdir / "r.hdf"), size="20Mi", volume="Plain")
    ok("sync", str(backup), restored)
    assert entry_of(restored, "C")["modified_amiga_secs"] == want
    assert read(restored, "C/List") == FILES["C/List"]   # and the contents still arrived
