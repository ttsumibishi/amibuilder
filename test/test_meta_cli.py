"""`touch`, `protect` and `comment` -- set metadata on entries already on a volume.

These are the AmigaDOS `SetDate`, `Protect` and `FileNote` verbs. They change an entry
without rewriting its data, and they share the same discipline as `cp` and `rm`: the whole
path list is validated before anything is written, so a typo in the batch leaves the disk
untouched.

Two things get the most attention, because both are places amitools would quietly do the
wrong thing:

* **A protection mask of 0 must actually clear the bits.** amitools' `change_meta_info`
  guards its protect update with `if protect:`, so resetting a locked file back to the
  all-permitted `----rwed` (mask 0) is silently skipped. `Volume.set_protect` drives the
  block directly for exactly this reason; the reset test pins it.
* **A comment can be set at all.** amitools' own `change_comment` crashes on any comment,
  on any volume, because `needs_extra_comment_block` calls `len()` on a `FileName`.
  `Volume.set_comment` drives the block directly instead; these tests are what would catch
  that regressing back to the amitools path.

Written against the CLI, because the exit codes, the preflight and the argument shapes are
as much of the contract as the write itself.
"""

from __future__ import annotations

import json

import pytest

from amibuilder import timestamps
from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.image import open_container

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
    """Run a command that must succeed, returning stdout."""

    def _ok(*argv: str) -> str:
        code, out, err = run(*argv)
        assert code == 0, f"exit {code}: {err or out}"
        return out

    return _ok


def entries(image: str, path: str = "") -> dict[str, dict]:
    """Read a directory back, keyed by name, from a freshly opened volume.

    Re-opening the container each call means every assertion also proves the change
    survived the write and was not merely reflected in an in-memory cache.
    """
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return {e.name: e.as_dict() for e in vol.listdir(path)}


def entry_of(image: str, path: str) -> dict:
    """The single entry at `path`, via its parent directory listing."""
    parent, _, leaf = path.rpartition("/")
    return entries(image, parent)[leaf]


def exists(image: str, path: str) -> bool:
    parent, _, leaf = path.rpartition("/")
    return leaf in entries(image, parent)


def volume_name(image: str) -> str:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return vol.info().name


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


def test_touch_restamps_existing_to_now(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    before = timestamps.now()[0]
    code, _out, _ = run("touch", img, "S/Startup-Sequence")
    assert code == 0
    stamped = entry_of(img, "S/Startup-Sequence")["modified_amiga_secs"]
    after = timestamps.now()[0]
    # touch calls now() once at the start, so the stored second sits in [before, after].
    assert before - 2 <= stamped <= after + 2


def test_touch_creates_empty_file(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, out, _ = run("touch", img, "Freshly.txt")
    assert code == 0
    made = entry_of(img, "Freshly.txt")
    assert made["type"] == "file"
    assert made["size"] == 0
    assert made["protect"] == "----rwed"
    assert "1 created" in out


def test_touch_no_create_skips_missing(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, out, _ = run("touch", img, "Nope.txt", "-c")
    assert code == 0
    assert not exists(img, "Nope.txt")
    assert "skipped" in out
    assert "touched 0 entries" in out


def test_touch_refuses_bad_name_without_writing(run, rdb_populated):
    """A bad name anywhere in the batch is refused before any entry is created."""
    img = f"{rdb_populated}:Workbench"
    long_name = "x" * 40  # a DOS3 volume allows 30 bytes
    code, _, _err = run("touch", img, "GoodNew.txt", long_name)
    assert code == 2  # UsageError
    assert not exists(img, "GoodNew.txt")


def test_touch_refuses_missing_parent(run, rdb_populated):
    """touch does not create parent directories, and refuses the whole batch if one lacks."""
    img = f"{rdb_populated}:Workbench"
    code, _, _err = run("touch", img, "GoodNew.txt", "NoSuchDir/child.txt")
    assert code == 3  # NotFoundError
    assert not exists(img, "GoodNew.txt")
    assert not exists(img, "NoSuchDir")


def test_touch_dry_run_creates_nothing(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, out, _ = run("touch", img, "Ghost.txt", "-n")
    assert code == 0
    assert "would create" in out
    assert not exists(img, "Ghost.txt")


def test_touch_root_refused(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _err = run("touch", img, "")
    assert code == 2  # UsageError -- the volume root has no timestamp of its own


# ---------------------------------------------------------------------------
# protect
# ---------------------------------------------------------------------------


def test_protect_sets_bits(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _out, _ = run("protect", img, "C/List", "--bits", "r")
    assert code == 0
    assert entry_of(img, "C/List")["protect"] == "----r---"


def test_protect_reset_to_default_clears_a_locked_file(run, rdb_populated):
    """Mask 0 (----rwed) must clear bits -- the amitools change_protect quirk this exists for.

    Setting a restrictive mask and then resetting to the all-permitted default proves the
    direct-block path applies mask 0, which amitools' `if protect:` guard would skip.
    """
    img = f"{rdb_populated}:Workbench"
    run("protect", img, "C/Dir", "--bits", "r")
    assert entry_of(img, "C/Dir")["protect"] == "----r---"

    code, _, _ = run("protect", img, "C/Dir", "--bits=----rwed")
    assert code == 0
    got = entry_of(img, "C/Dir")
    assert got["protect"] == "----rwed"
    assert got["protect_bits"] == 0


def test_protect_multiple_paths_at_once(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _ = run("protect", img, "C/List", "C/Dir", "--bits", "rw")
    assert code == 0
    assert entry_of(img, "C/List")["protect"] == "----rw--"
    assert entry_of(img, "C/Dir")["protect"] == "----rw--"


def test_protect_on_a_directory(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _ = run("protect", img, "S", "--bits", "r")
    assert code == 0
    assert entry_of(img, "S")["protect"] == "----r---"


def test_protect_missing_path_exit3_leaves_others(run, rdb_populated):
    """A missing path in the batch is refused whole; a sibling stays at its default."""
    img = f"{rdb_populated}:Workbench"
    code, _, _ = run("protect", img, "C/List", "Nope", "--bits", "r")
    assert code == 3  # NotFoundError
    assert entry_of(img, "C/List")["protect"] == "----rwed"  # untouched


def test_protect_bad_spec_exit2(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _err = run("protect", img, "C/List", "--bits", "zqx")
    assert code == 2  # UsageError, before the image is even opened
    assert entry_of(img, "C/List")["protect"] == "----rwed"


def test_protect_dry_run_changes_nothing(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, out, _ = run("protect", img, "C/List", "--bits", "r", "-n")
    assert code == 0
    assert "would set" in out
    assert entry_of(img, "C/List")["protect"] == "----rwed"


def test_protect_on_dircache_volume(run, dircache_hdf):
    """set_protect mirrors the mask into the parent's dircache record on a DOS5 volume."""
    code, _, _ = run("protect", dircache_hdf, "D0/f000", "--bits", "r")
    assert code == 0
    assert entry_of(dircache_hdf, "D0/f000")["protect"] == "----r---"
    # ...and back to the default, exercising the mask-0 path on dircache too.
    code, _, _ = run("protect", dircache_hdf, "D0/f000", "--bits=----rwed")
    assert code == 0
    assert entry_of(dircache_hdf, "D0/f000")["protect_bits"] == 0


def test_protect_json(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, out, _ = run("protect", img, "C/List", "--bits", "r", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["entries"][0] == {"path": "C/List", "protect": "----r---"}


# ---------------------------------------------------------------------------
# comment
# ---------------------------------------------------------------------------


def test_comment_sets_text(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _ = run("comment", img, "C/List", "--text", "the list command")
    assert code == 0
    assert entry_of(img, "C/List")["comment"] == "the list command"


def test_comment_clears_with_empty_text(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    run("comment", img, "C/List", "--text", "temporary note")
    assert entry_of(img, "C/List")["comment"] == "temporary note"

    code, out, _ = run("comment", img, "C/List", "--text", "")
    assert code == 0
    assert entry_of(img, "C/List")["comment"] == ""
    assert "cleared" in out


def test_comment_at_the_79_byte_limit_is_allowed(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    note = "n" * 79
    code, _, _ = run("comment", img, "C/List", "--text", note)
    assert code == 0
    assert entry_of(img, "C/List")["comment"] == note


def test_comment_too_long_exit2(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _err = run("comment", img, "C/List", "--text", "z" * 80)
    assert code == 2  # UsageError, before any write
    assert entry_of(img, "C/List")["comment"] == ""


def test_comment_missing_path_exit3(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, _, _ = run("comment", img, "Nope", "--text", "hi")
    assert code == 3  # NotFoundError


def test_comment_dry_run_changes_nothing(run, rdb_populated):
    img = f"{rdb_populated}:Workbench"
    code, out, _ = run("comment", img, "C/List", "--text", "unwritten", "-n")
    assert code == 0
    assert "would set" in out
    assert entry_of(img, "C/List")["comment"] == ""


def test_comment_on_dircache_volume(run, dircache_hdf):
    """set_comment must work on DOS5 too -- the amitools len() crash was volume-agnostic."""
    code, _, _ = run("comment", dircache_hdf, "D0/f000", "--text", "dircache note")
    assert code == 0
    assert entry_of(dircache_hdf, "D0/f000")["comment"] == "dircache note"
    code, _, _ = run("comment", dircache_hdf, "D0/f000", "--text", "")
    assert code == 0
    assert entry_of(dircache_hdf, "D0/f000")["comment"] == ""


# ---------------------------------------------------------------------------
# The volume stays valid after a batch of metadata changes
# ---------------------------------------------------------------------------


def test_volume_still_valid_after_meta_ops(run, rdb_populated):
    """A round of touch/protect/comment leaves an FFS volume amitools' validator accepts."""
    img = f"{rdb_populated}:Workbench"
    assert run("protect", img, "C/List", "C/Dir", "--bits", "rw")[0] == 0
    assert run("comment", img, "C/List", "--text", "checked")[0] == 0
    assert run("touch", img, "S/Startup-Sequence", "Marker.txt")[0] == 0
    # check the whole image, both partitions, not just the one we edited.
    code, out, err = run("check", rdb_populated)
    assert code == 0, f"{out}\n{err}"


# ---------------------------------------------------------------------------
# relabel
# ---------------------------------------------------------------------------


def test_relabel_renames_volume(run, plain_hdf):
    code, out, _ = run("relabel", plain_hdf, "Renamed")
    assert code == 0
    assert volume_name(plain_hdf) == "Renamed"
    assert "relabelled 'Plain' -> 'Renamed'" in out


def test_relabel_dry_run_changes_nothing(run, plain_hdf):
    code, out, _ = run("relabel", plain_hdf, "Whatever", "-n")
    assert code == 0
    assert "would relabel" in out
    assert volume_name(plain_hdf) == "Plain"


def test_relabel_accepts_30_bytes(run, plain_hdf):
    name = "V" * 30
    code, _, _ = run("relabel", plain_hdf, name)
    assert code == 0
    assert volume_name(plain_hdf) == name


@pytest.mark.parametrize("bad", ["Bad:Name", "a/b", "V" * 31, ""])
def test_relabel_rejects_invalid_names(run, plain_hdf, bad):
    code, _, _ = run("relabel", plain_hdf, bad)
    assert code == 2  # UsageError
    assert volume_name(plain_hdf) == "Plain"  # untouched


def test_relabel_json(run, plain_hdf):
    code, out, _ = run("relabel", plain_hdf, "Jsonned", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload == {"old": "Plain", "new": "Jsonned", "dry_run": False}


def test_relabel_leaves_rdb_device_name(run, rdb_populated):
    """On an RDB drive relabel changes the FFS volume name, not the partition device name."""
    before = json.loads(run("partitions", rdb_populated, "--json")[1])["partitions"][0]
    assert before["device"] == "DH0"
    assert before["volume"] == "Workbench"

    code, _, _ = run("relabel", f"{rdb_populated}:0", "Rebranded")
    assert code == 0

    after = json.loads(run("partitions", rdb_populated, "--json")[1])["partitions"][0]
    assert after["device"] == "DH0"       # device name unchanged
    assert after["volume"] == "Rebranded"  # volume name changed
