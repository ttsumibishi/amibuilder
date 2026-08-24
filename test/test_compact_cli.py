"""`compact` -- punch holes over an image's zero runs to reclaim host disk space.

The point of compact is entirely about on-disk footprint, so that is what the tests measure
(`st_blocks`, the blocks the file actually occupies), not the apparent size. And because it
must never change what the image *contains*, the live file and structural validity are checked
to be identical afterwards -- a punched region has to read back as the zeros it already held.
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
from helpers import images  # noqa: E402

LIVE = b"LIVEDATA" * 4000   # 32 KiB, must survive byte-for-byte


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


def on_disk(path: str) -> int:
    """Bytes the file actually occupies (holes don't count). st_blocks is in 512-byte units."""
    return Path(path).stat().st_blocks * 512


def read(image: str, path: str) -> bytes:
    with open_container(parse(image)) as c:
        with c.open_addressed_volume() as vol:
            return vol.read_file(path)


@pytest.fixture
def zeroed_hdf(workdir: Path) -> str:
    """A 20 MiB image with a tiny live file and all its free space written out as real zeros.

    The zeroing is done straight through the Volume API rather than the `zerofree` CLI so the
    fixture prints nothing (which would otherwise pollute a later --json capture), and so the
    free blocks genuinely occupy disk -- the pre-compact state compact is meant to shrink.
    """
    path = images.make_plain_hdf(str(workdir / "z.hdf"), size="20Mi", volume="Work")
    images.write_files(path, {"live.bin": LIVE})
    with open_container(parse(path), writable=True) as c:
        with c.open_addressed_volume() as vol:
            vol.zero_free_blocks()
    return path


def test_compact_reclaims_disk_space(run, zeroed_hdf):
    before = on_disk(zeroed_hdf)
    assert before > 10 * 1024 * 1024        # the zeros really are occupying disk
    code, out, err = run("compact", zeroed_hdf)
    assert code == 0, err
    after = on_disk(zeroed_hdf)
    assert after < 1 * 1024 * 1024          # ...and almost all of it came back
    assert "reclaimed" in out


def test_compact_leaves_files_and_validity_intact(run, zeroed_hdf):
    before = read(zeroed_hdf, "live.bin")
    assert run("compact", zeroed_hdf)[0] == 0
    assert read(zeroed_hdf, "live.bin") == before == LIVE
    code, out, err = run("check", zeroed_hdf)
    assert code == 0, f"{out}\n{err}"


def test_compact_leaves_apparent_size_unchanged(run, zeroed_hdf):
    size = Path(zeroed_hdf).stat().st_size
    assert run("compact", zeroed_hdf)[0] == 0
    assert Path(zeroed_hdf).stat().st_size == size


def test_compact_dry_run_reclaims_nothing(run, zeroed_hdf):
    before = on_disk(zeroed_hdf)
    code, out, _ = run("compact", zeroed_hdf, "--dry-run")
    assert code == 0
    assert "would punch" in out
    assert on_disk(zeroed_hdf) == before        # untouched


def test_compact_json(run, zeroed_hdf):
    code, out, _ = run("compact", zeroed_hdf, "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["punched_bytes"] > 0
    assert payload["reclaimed_bytes"] > 0
    assert payload["dry_run"] is False


def test_compact_refuses_a_directory(run, workdir):
    d = workdir / "adir"
    d.mkdir()
    code, _, _ = run("compact", str(d))
    assert code == 2
