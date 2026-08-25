"""`inject` -- copy files from one Amiga image (or ADF) into another, without the host.

The properties that matter here, and why:

* **Bytes and metadata cross intact.** inject exists so an ADF or a partition can be
  folded into another volume without a host round-trip, so the point is that the copy is
  faithful: the file's contents, protection bits, comment and modification time all arrive
  unchanged. The metadata test is the one that would catch inject quietly resetting things
  the way a host copy has to.
* **Nothing is written unless everything can be.** A directory without `-r`, a `--to` that
  needs `-p`, a collision without `-f`, or a tree too big to fit is refused while the
  destination is still untouched -- the same no-half-applied rule the other writers follow.
* **The same file is refused for both sides.** Two open handles on one image, one writing,
  could corrupt the source read; v1 declines rather than risk it.

Written against the CLI, because the argument shapes, the `--from`/`--to` semantics and the
exit codes are as much of the contract as the copy itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.image import open_container

sys.path.insert(0, str(Path(__file__).parent))
from helpers import images

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


@pytest.fixture
def src(workdir: Path) -> str:
    """A source volume 'Src' with a small tree and one top-level file."""
    path = images.make_plain_hdf(str(workdir / "src.hdf"), size="10Mi", volume="Src")
    images.write_files(path, {
        "Game1/data.bin": bytes(range(256)) * 4,          # 1024 bytes
        "Game1/Docs/readme.txt": b"read me first\n",
        "loose.txt": b"a loose top-level file\n",
    })
    return path


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


# ---------------------------------------------------------------------------
# The core copy: contents, shapes, and an ADF source
# ---------------------------------------------------------------------------


def test_inject_whole_volume_contents(run, src, plain_hdf):
    """--from omitted copies the source volume's contents directly under --to."""
    code, _, err = run("inject", src, plain_hdf, "--to", "Apps", "-r", "-p")
    assert code == 0, err
    assert read(plain_hdf, "Apps/Game1/data.bin") == read(src, "Game1/data.bin")
    assert read(plain_hdf, "Apps/Game1/Docs/readme.txt") == b"read me first\n"
    assert read(plain_hdf, "Apps/loose.txt") == b"a loose top-level file\n"
    assert entry_of(plain_hdf, "Apps/Game1")["type"] == "dir"


def test_inject_named_subtree_nests_under_its_name(run, src, plain_hdf):
    code, _, err = run("inject", src, plain_hdf, "--from", "Game1", "--to", "Tools", "-r", "-p")
    assert code == 0, err
    assert read(plain_hdf, "Tools/Game1/data.bin") == read(src, "Game1/data.bin")
    assert read(plain_hdf, "Tools/Game1/Docs/readme.txt") == b"read me first\n"
    assert not exists(plain_hdf, "Tools/loose.txt")  # only the named subtree came across


def test_inject_single_file_needs_no_recursive(run, src, plain_hdf):
    code, _, err = run("inject", src, plain_hdf, "--from", "loose.txt")
    assert code == 0, err
    assert read(plain_hdf, "loose.txt") == b"a loose top-level file\n"


def test_inject_from_adf(run, adf, plain_hdf):
    """The primary use: fold an ADF's contents into a partition."""
    code, _, err = run("inject", adf, plain_hdf, "--to", "Disk", "-r", "-p")
    assert code == 0, err
    assert read(plain_hdf, "Disk/Archive/part1.bin") == read(adf, "Archive/part1.bin")


# ---------------------------------------------------------------------------
# Metadata preservation -- the reason inject differs from a host copy
# ---------------------------------------------------------------------------


def test_inject_preserves_protect_comment_and_time(run, src, plain_hdf):
    # Give the source file non-default metadata first.
    assert run("protect", src, "Game1/data.bin", "--bits", "r")[0] == 0
    assert run("comment", src, "Game1/data.bin", "--text", "the payload")[0] == 0
    before = entry_of(src, "Game1/data.bin")
    assert before["protect"] == "----r---"

    code, _, err = run("inject", src, plain_hdf, "--from", "Game1/data.bin", "--to", "Keep", "-p")
    assert code == 0, err

    after = entry_of(plain_hdf, "Keep/data.bin")
    assert after["protect"] == before["protect"]
    assert after["comment"] == before["comment"] == "the payload"
    assert after["modified_amiga_secs"] == before["modified_amiga_secs"]


def test_inject_preserves_directory_timestamps(run, src, plain_hdf):
    src_dir_time = entry_of(src, "Game1")["modified_amiga_secs"]
    assert run("inject", src, plain_hdf, "--from", "Game1", "--to", "X", "-r", "-p")[0] == 0
    assert entry_of(plain_hdf, "X/Game1")["modified_amiga_secs"] == src_dir_time


# ---------------------------------------------------------------------------
# The no-half-applied guards
# ---------------------------------------------------------------------------


def test_inject_directory_without_recursive_refused(run, src, plain_hdf):
    code, _, _ = run("inject", src, plain_hdf, "--from", "Game1")
    assert code == 2  # UsageError
    assert not exists(plain_hdf, "Game1")


def test_inject_missing_to_without_parents_refused(run, src, plain_hdf):
    code, _, _ = run("inject", src, plain_hdf, "--from", "loose.txt", "--to", "Nope")
    assert code == 2  # UsageError
    assert not exists(plain_hdf, "Nope")


def test_inject_missing_from_path(run, src, plain_hdf):
    code, _, _ = run("inject", src, plain_hdf, "--from", "NoSuchThing")
    assert code == 3  # NotFoundError


def test_inject_collision_needs_force(run, src, plain_hdf):
    assert run("inject", src, plain_hdf, "--from", "loose.txt")[0] == 0
    code, _, _ = run("inject", src, plain_hdf, "--from", "loose.txt")
    assert code == 5  # ImageError: already exists
    assert read(plain_hdf, "loose.txt") == b"a loose top-level file\n"  # untouched


def test_inject_force_replaces_with_source_content(run, src, plain_hdf):
    # Seed the destination with a different file of the same name.
    images.write_files(plain_hdf, {"loose.txt": b"OLD CONTENT"})
    assert read(plain_hdf, "loose.txt") == b"OLD CONTENT"

    code, _, err = run("inject", src, plain_hdf, "--from", "loose.txt", "-f")
    assert code == 0, err
    assert read(plain_hdf, "loose.txt") == b"a loose top-level file\n"


def test_inject_capacity_refused_leaves_dest_untouched(run, workdir):
    """A source too big for the destination is refused whole."""
    big = images.make_plain_hdf(str(workdir / "big.hdf"), size="10Mi", volume="Big")
    images.write_files(big, {"huge.bin": bytes(3 * 1024 * 1024)})  # 3 MiB
    small = images.make_plain_hdf(str(workdir / "small.hdf"), size="2Mi", volume="Small")

    code, _, _ = run("inject", big, small, "--from", "huge.bin")
    assert code == 5  # ImageError: capacity
    assert not exists(small, "huge.bin")


def test_inject_same_file_refused(run, src):
    code, _, err = run("inject", src, src, "--from", "Game1", "-r")
    assert code == 2  # UsageError
    assert "same image" in err


# ---------------------------------------------------------------------------
# dry run, validity and JSON
# ---------------------------------------------------------------------------


def test_inject_dry_run_writes_nothing(run, src, plain_hdf):
    code, out, _ = run("inject", src, plain_hdf, "--to", "Fresh", "-r", "-p", "-n")
    assert code == 0
    assert "would inject" in out
    assert "nothing was changed" in out
    assert not exists(plain_hdf, "Fresh")


def test_inject_leaves_a_valid_volume(run, src, plain_hdf):
    assert run("inject", src, plain_hdf, "--to", "Apps", "-r", "-p")[0] == 0
    code, out, err = run("check", plain_hdf)
    assert code == 0, f"{out}\n{err}"


def test_inject_json(run, src, plain_hdf):
    code, out, _ = run("inject", src, plain_hdf, "--from", "loose.txt", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["files"] == 1
    assert payload["directories"] == 0
    assert payload["into"] == ""
    assert payload["entries"][0]["path"] == "loose.txt"
    assert payload["entries"][0]["source"] == "loose.txt"
