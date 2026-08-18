"""Shared fixtures and configuration for the amibuilder test suite."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import images  # noqa: E402


# ---------------------------------------------------------------------------
# Environment reporting
# ---------------------------------------------------------------------------


def pytest_report_header(config):
    """Show which amitools is under test, and whether the emulator tests can run."""
    lines = []
    try:
        from importlib.metadata import metadata, version

        lic = metadata("amitools").get("License-Expression") or "unknown"
        lines.append(f"amitools: {version('amitools')} ({lic})")
    except Exception as exc:  # pragma: no cover - only on a broken install
        lines.append(f"amitools: NOT IMPORTABLE ({exc})")

    fsuae = find_fsuae()
    rom = find_kickstart()
    if fsuae and rom:
        lines.append(f"fs-uae: {fsuae}")
        lines.append(f"kickstart: {Path(rom).name}")
    else:
        missing = [n for n, v in (("fs-uae", fsuae), ("kickstart ROM", rom)) if not v]
        lines.append(f"emulator tests skip: no {', '.join(missing)}")
    return lines


# ---------------------------------------------------------------------------
# Emulator configuration
# ---------------------------------------------------------------------------


REPO = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO / "source-files-do-not-add-to-git"

#: Where FS-UAE and Kickstart ROMs are looked for when the environment says nothing.
FSUAE_CANDIDATES = [
    "/Applications/FS-UAE.app/Contents/MacOS/fs-uae",
    "/usr/local/bin/fs-uae",
    "/opt/homebrew/bin/fs-uae",
]
ROM_GLOBS = ["roms/*.rom", "roms/**/*.rom", "*.rom"]


def find_fsuae() -> str | None:
    """Locate an FS-UAE binary: environment first, then the usual places."""
    env = os.environ.get("AMIBUILDER_FSUAE")
    if env:
        return env if Path(env).exists() else None
    for cand in FSUAE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return shutil.which("fs-uae")


def find_kickstart() -> str | None:
    """Locate a Kickstart ROM: environment first, then source-files-do-not-add-to-git.

    ROMs are licensed software and are never committed, so auto-detection only looks in
    the gitignored source directory.
    """
    env = os.environ.get("AMIBUILDER_KICKSTART")
    if env:
        return env if Path(env).exists() else None
    for pattern in ROM_GLOBS:
        for p in sorted(SOURCE_DIR.glob(pattern)):
            if p.is_file() and p.stat().st_size in (262_144, 524_288):
                return str(p)
    return None


@pytest.fixture(scope="session")
def fsuae_config() -> dict[str, str]:
    """Paths for the FS-UAE harness, or skip if the machine is not set up for it.

    Auto-detects FS-UAE in /Applications and a Kickstart ROM in
    source-files-do-not-add-to-git/roms/. Override either with:

        export AMIBUILDER_FSUAE=/path/to/fs-uae
        export AMIBUILDER_KICKSTART=/path/to/kick.rom

    A checkout with no Amiga ROMs present skips these and runs everything else.
    """
    binary = find_fsuae()
    rom = find_kickstart()
    if not binary:
        pytest.skip("no FS-UAE binary found (set AMIBUILDER_FSUAE)")
    if not rom:
        pytest.skip(
            "no Kickstart ROM found in source-files-do-not-add-to-git/roms/ "
            "(set AMIBUILDER_KICKSTART)"
        )
    return {"binary": binary, "rom": rom}


# ---------------------------------------------------------------------------
# Working directories
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A per-test scratch directory.

    Images can be large, so tmp_path (which pytest prunes) is used rather than a
    fixed location. On APFS these are sparse, so the apparent sizes are misleading.
    """
    return tmp_path


# ---------------------------------------------------------------------------
# Image fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plain_hdf(workdir: Path) -> str:
    """A small formatted plain HDF: DOS3, no RDB."""
    return images.make_plain_hdf(str(workdir / "plain.hdf"), size="20Mi", volume="Plain")


@pytest.fixture
def rdb_hdf(workdir: Path) -> str:
    """A 64 MiB RDB image with a single bootable DOS3 partition."""
    return images.make_rdb_hdf(
        str(workdir / "rdb.hdf"),
        size="64Mi",
        partitions=[images.Partition(dos_type="ffs+intl", bootable=True, volume="Work")],
    )


@pytest.fixture
def rdb_two_part(workdir: Path) -> str:
    """An RDB image with two partitions, for partition-isolation tests."""
    return images.make_rdb_hdf(
        str(workdir / "two.hdf"),
        size="128Mi",
        partitions=[
            images.Partition(size="32MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Extra"),
        ],
    )


@pytest.fixture
def populated_hdf(workdir: Path) -> str:
    """A plain HDF holding a small known file set, used by structural tests."""
    path = images.make_plain_hdf(str(workdir / "pop.hdf"), size="20Mi", volume="Pop")
    images.write_files(
        path,
        {
            "S/Startup-Sequence": b'Echo "hello"\n',
            "TESTFILE": b"A" * 100,
            "Libs/thing.library": bytes(range(256)) * 8,
        },
    )
    return path


@pytest.fixture
def adf(workdir: Path) -> str:
    """A DD ADF holding one file inside a directory."""
    return images.make_adf(
        str(workdir / "disk.adf"),
        volume="Disk1",
        files={"Archive/part1.bin": b"\x01\x02\x03" * 1000},
    )


# ---------------------------------------------------------------------------
# Skip logic for tools that may be absent
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _require_amitools():
    """Fail loudly and early if the amitools CLI tools are missing."""
    for tool in (images.XDFTOOL, images.RDBTOOL, images.XDFSCAN):
        if not Path(tool).exists() and shutil.which(Path(tool).name) is None:
            pytest.exit(
                f"amitools tool not found: {tool}\n"
                "Install the dev environment first:  uv sync --extra dev",
                returncode=1,
            )


# ---------------------------------------------------------------------------
# Fixtures for the amibuilder package itself
# ---------------------------------------------------------------------------

#: A tree shaped like a real Workbench partition, small enough to build quickly.
WORKBENCH_FILES = {
    "S/Startup-Sequence": b"C:SetPatch QUIET\nC:Version >NIL:\nEndCLI >NIL:\n",
    "S/Shell-Startup": b'Prompt "%N.%S> "\n',
    "C/List": bytes(range(256)) * 8,
    "C/Dir": bytes(700),
    "Devs/DOSDrivers/CD0": b"FileSystem = L:CDFileSystem\n",
    "Tools/Calculator": bytes(4096),
    "Tools/Calculator.info": bytes(1200),
    "Prefs/Env-Archive/Sys/overscan.prefs": bytes(96),
}


@pytest.fixture
def rdb_populated(workdir: Path) -> str:
    """A two-partition RDB whose first partition holds WORKBENCH_FILES.

    Partition 1 is left empty on purpose: several tests need to prove that an operation
    on one partition leaves the other untouched.
    """
    path = images.make_rdb_hdf(
        str(workdir / "populated.hdf"),
        size="32Mi",
        partitions=[
            images.Partition(size="10MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(path, WORKBENCH_FILES, part=0)
    return path


@pytest.fixture
def amiga_card(workdir: Path, rdb_populated: str) -> tuple[str, int]:
    """A PiStorm-shaped card: a FAT slot, then a type 0x76 slot holding an RDB.

    Returns (path, amiga_slot). This is the layout Emu68 presents as Amiga drive units,
    and the one where an offset bug would destroy the emulator's own boot partition.
    """
    from helpers.mbr import PTYPE_AMIGA_VIRTUAL, build_mbr

    card = str(workdir / "card.img")
    fat_sectors = 2048
    inner = Path(rdb_populated).stat().st_size
    with open(card, "wb") as f:
        f.write(build_mbr([
            (0x0C, 1, fat_sectors - 1),
            (PTYPE_AMIGA_VIRTUAL, fat_sectors, inner // 512),
        ]))
        f.truncate(fat_sectors * 512)
        f.seek(fat_sectors * 512)
        with open(rdb_populated, "rb") as src:
            shutil.copyfileobj(src, f)
    return card, 1


@pytest.fixture
def dircache_hdf(workdir: Path) -> str:
    """A DOS5 (dircache) volume, which amitools' validator cannot check correctly.

    Used to prove that `check` classifies the resulting bitmap findings as validator
    limitations rather than reporting a healthy volume as corrupt (notes section 5.5).
    """
    path = images.make_plain_hdf(
        str(workdir / "dircache.hdf"), size="20Mi", volume="DC", dos_type="ffs+dircache"
    )
    images.write_files(path, {f"D{i}/f{j:03d}": bytes(200) for i in range(4)
                              for j in range(20)})
    return path


@pytest.fixture
def unformatted_hdf(workdir: Path) -> str:
    """A blank, unpartitioned, unformatted HDF -- what `init` will eventually produce."""
    path = workdir / "blank.hdf"
    with open(path, "wb") as f:
        f.truncate(8 * 1024 * 1024)
    return str(path)
