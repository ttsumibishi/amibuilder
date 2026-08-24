"""`zerofree` -- zero an image's free blocks so it compresses and sparsifies.

The properties that matter, and why each is here:

* **Deleted data actually disappears.** The whole point: a file removed from FFS leaves its
  bytes in blocks marked free, and every backup carries them. After `zerofree` those bytes
  are zeros. Proven by pattern-counting the raw image, not by trusting the free-block total.
* **Live files are untouched.** Zeroing free space must not disturb a single live byte. The
  built-in verify already checks this; the tests check it independently, and check that verify
  *catches* a simulated misparse and leaves the original intact.
* **Scope is right.** All of an RDB's partitions by default; one when a selector names it,
  with the others left alone.

Written against the CLI, because the flags, the safety defaults and the exit codes are the
contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.image import open_container
from amibuilder.volume import Volume

sys.path.insert(0, str(Path(__file__).parent))
from helpers import images  # noqa: E402

# The files are a small unit repeated. Search the raw image for the *unit*: FFS data blocks
# are not physically contiguous, so the whole multi-KiB blob never appears as one run, but
# the unit appears many times within each data block.
DEAD_UNIT = b"DEADBEEF"
LIVE_UNIT = b"CAFEF00D"
DEAD = DEAD_UNIT * 8000   # 64 KiB, the deleted file
LIVE = LIVE_UNIT * 4000   # 32 KiB, must survive


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


def raw_count(path: str, unit: bytes) -> int:
    with open(path, "rb") as f:
        return f.read().count(unit)


def read(image: str, path: str) -> bytes:
    with open_container(parse(image)) as c:
        with c.open_addressed_volume() as vol:
            return vol.read_file(path)


def _delete(spec: str, path: str) -> None:
    """Delete a file straight through the Volume API, so nothing is printed to stdout.

    Going via the `rm` CLI would leave its summary in capsys, which the first `run()` in a
    test then captures and prepends to the command's output -- breaking the --json parse.
    """
    with open_container(parse(spec), writable=True) as c:
        with c.open_addressed_volume() as vol:
            vol.remove(path)


@pytest.fixture
def dirty_hdf(workdir: Path) -> str:
    """A plain HDF with a live file and a deleted one whose data still lingers in free space."""
    path = images.make_plain_hdf(str(workdir / "dirty.hdf"), size="10Mi", volume="Work")
    images.write_files(path, {"dead.bin": DEAD, "live.bin": LIVE})
    _delete(path, "dead.bin")
    return path


@pytest.fixture
def dirty_rdb(workdir: Path) -> str:
    """A two-partition RDB, each partition holding a live file and a deleted one."""
    path = images.make_rdb_hdf(
        str(workdir / "dirty-rdb.hdf"), size="24Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True, volume="A"),
            images.Partition(dos_type="ffs+intl", volume="B"),
        ],
    )
    for part in (0, 1):
        images.write_files(path, {"dead.bin": DEAD, "live.bin": LIVE}, part=part)
        _delete(f"{path}:{part}", "dead.bin")
    return path


# ---------------------------------------------------------------------------
# The core: dead data gone, live data intact, volume still valid
# ---------------------------------------------------------------------------


def test_zerofree_wipes_deleted_file_data(run, dirty_hdf):
    assert raw_count(dirty_hdf, DEAD_UNIT) > 0            # the ghost is there to begin with
    code, out, err = run("zerofree", dirty_hdf)
    assert code == 0, err
    assert raw_count(dirty_hdf, DEAD_UNIT) == 0           # ...and gone afterwards
    assert raw_count(dirty_hdf, LIVE_UNIT) > 0            # the live file's bytes remain


def test_zerofree_keeps_live_files_byte_identical(run, dirty_hdf):
    before = read(dirty_hdf, "live.bin")
    assert run("zerofree", dirty_hdf)[0] == 0
    assert read(dirty_hdf, "live.bin") == before == LIVE


def test_zerofree_leaves_a_valid_volume(run, dirty_hdf):
    assert run("zerofree", dirty_hdf)[0] == 0
    code, out, err = run("check", dirty_hdf)
    assert code == 0, f"{out}\n{err}"


def test_zerofree_reports_and_verifies(run, dirty_hdf):
    code, out, _ = run("zerofree", dirty_hdf)
    assert code == 0
    assert "zeroed" in out
    assert "verified" in out


# ---------------------------------------------------------------------------
# Flags and scope
# ---------------------------------------------------------------------------


def test_zerofree_dry_run_writes_nothing(run, dirty_hdf):
    ghosts = raw_count(dirty_hdf, DEAD_UNIT)
    code, out, _ = run("zerofree", dirty_hdf, "--dry-run")
    assert code == 0
    assert "would zero" in out
    assert raw_count(dirty_hdf, DEAD_UNIT) == ghosts      # untouched


def test_zerofree_in_place(run, dirty_hdf):
    code, out, _ = run("zerofree", dirty_hdf, "--in-place")
    assert code == 0
    assert "in place" in out
    assert raw_count(dirty_hdf, DEAD_UNIT) == 0
    assert read(dirty_hdf, "live.bin") == LIVE


def test_zerofree_all_partitions_by_default(run, dirty_rdb):
    code, out, err = run("zerofree", dirty_rdb)
    assert code == 0, err
    assert "2 volume(s)" in out
    assert raw_count(dirty_rdb, DEAD_UNIT) == 0           # both partitions cleaned
    assert raw_count(dirty_rdb, LIVE_UNIT) > 0


def test_zerofree_selector_scopes_to_one_partition(run, dirty_rdb):
    # Zeroing partition 0 must leave partition 1's free space (its ghost) alone.
    code, out, err = run("zerofree", f"{dirty_rdb}:0")
    assert code == 0, err
    assert "1 volume(s)" in out
    # One DEAD ghost was cleaned (partition 0); the other (partition 1) remains.
    assert raw_count(dirty_rdb, DEAD_UNIT) > 0
    assert read(f"{dirty_rdb}:1", "live.bin") == LIVE
    assert read(f"{dirty_rdb}:0", "live.bin") == LIVE


def test_zerofree_json(run, dirty_hdf):
    import json

    code, out, _ = run("zerofree", dirty_hdf, "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["verified"] is True
    assert payload["in_place"] is False
    assert payload["zeroed_blocks"] > 0
    assert payload["zeroed_bytes"] == payload["zeroed_blocks"] * 512


def _on_disk(path: str) -> int:
    return Path(path).stat().st_blocks * 512


def test_zerofree_compact_reclaims_in_one_pass(run, dirty_hdf):
    code, out, err = run("zerofree", dirty_hdf, "--compact")
    assert code == 0, err
    assert "compacted" in out
    assert raw_count(dirty_hdf, DEAD_UNIT) == 0        # dead data zeroed
    assert read(dirty_hdf, "live.bin") == LIVE         # live file intact
    # Without --compact, zeroing leaves ~10 MiB of zeros occupying disk; with it, the image
    # is punched back down to roughly the live data.
    assert _on_disk(dirty_hdf) < 1 * 1024 * 1024


def test_zerofree_compact_json(run, dirty_hdf):
    import json

    code, out, _ = run("zerofree", dirty_hdf, "--compact", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["compacted"] is True
    assert "reclaimed_bytes" in payload


# ---------------------------------------------------------------------------
# Safety: verify catches a misparse, and the default protects the original
# ---------------------------------------------------------------------------


def _misparse(self: Volume) -> int:
    """A stand-in for zero_free_blocks that zeros EVERY block, live ones included.

    This is the worst a bitmap misparse could do -- treat the whole volume as free. It
    destroys the copy; verify must notice and refuse, leaving the original untouched.
    """
    bd = self._blkdev
    zeros = b"\x00" * bd.block_bytes
    n = 0
    for blk in range(bd.reserved, bd.num_blocks):
        bd.write_block(blk, zeros)
        n += 1
    return n


def test_verify_catches_a_misparse_and_protects_the_original(run, dirty_hdf, monkeypatch):
    before = read(dirty_hdf, "live.bin")
    ghosts = raw_count(dirty_hdf, DEAD_UNIT)
    monkeypatch.setattr(Volume, "zero_free_blocks", _misparse)

    code, out, err = run("zerofree", dirty_hdf)     # default temp-copy mode
    assert code == 5                                 # ImageError: verify failed
    assert "verify failed" in err
    # The original is byte-for-byte as it was: the temp copy was discarded.
    assert read(dirty_hdf, "live.bin") == before
    assert raw_count(dirty_hdf, DEAD_UNIT) == ghosts


def test_in_place_misparse_reports_the_image_may_be_corrupt(run, dirty_hdf, monkeypatch):
    monkeypatch.setattr(Volume, "zero_free_blocks", _misparse)
    code, out, err = run("zerofree", dirty_hdf, "--in-place")
    assert code == 5
    assert "verify failed" in err
    assert "in place" in err and "corrupt" in err   # the honest in-place warning


def test_no_verify_skips_the_safety_net(run, dirty_hdf, monkeypatch):
    # With verify off, the same misparse is NOT caught -- documents that --no-verify
    # genuinely removes the net rather than being cosmetic.
    monkeypatch.setattr(Volume, "zero_free_blocks", _misparse)
    code, _, _ = run("zerofree", dirty_hdf, "--no-verify")
    assert code == 0                                 # nothing stopped it


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_zerofree_refuses_a_directory(run, workdir):
    d = workdir / "adir"
    d.mkdir()
    code, _, _ = run("zerofree", str(d))
    assert code == 2


def test_zerofree_refuses_an_unformatted_image(run, unformatted_hdf):
    code, _, _ = run("zerofree", unformatted_hdf)
    assert code == 4                                 # UnsupportedError: nothing to zero
