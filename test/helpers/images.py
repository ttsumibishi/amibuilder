"""Builders for the disk images the tests run against.

Everything goes through amitools' CLI tools rather than its Python API where possible,
because that is the surface most likely to change between releases and therefore the
one most worth regression-testing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# amitools installs its tools as console scripts next to the running interpreter.
_BIN = Path(sys.executable).parent

XDFTOOL = str(_BIN / "xdftool")
RDBTOOL = str(_BIN / "rdbtool")
XDFSCAN = str(_BIN / "xdfscan")


class ToolError(RuntimeError):
    """An amitools CLI invocation failed."""


@dataclass
class Result:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run_tool(*args: str, check: bool = True) -> Result:
    """Invoke an amitools CLI tool.

    amitools frequently reports failures on stdout while still exiting 0, so callers
    that care about success should inspect `output` as well as `returncode`.
    """
    proc = subprocess.run([str(a) for a in args], capture_output=True, text=True, check=False)
    res = Result(list(map(str, args)), proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise ToolError(f"{args!r} exited {proc.returncode}\n{res.output}")
    return res


def xdftool(image: str, *cmds: str, check: bool = True) -> Result:
    return run_tool(XDFTOOL, image, *cmds, check=check)


def rdbtool(image: str, *cmds: str, check: bool = True) -> Result:
    return run_tool(RDBTOOL, image, *cmds, check=check)


def xdfscan(image: str, *args: str) -> Result:
    # xdfscan uses carriage returns for progress; normalise so callers can parse it.
    res = run_tool(XDFSCAN, *args, image, check=False)
    res.stdout = res.stdout.replace("\r", "\n")
    return res


def scan_is_ok(image: str) -> bool:
    """True if xdfscan reports the image as clean.

    Note the known dircache false positive: a DOS4/DOS5 volume reports one error per
    dircache block because the validator has no dircache support. See
    docs/KIP-FFS-NOTES.md section 5.5.
    """
    out = xdfscan(image).stdout
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return False
    return lines[-1].strip().endswith("ok " + os.path.basename(image)) or " ok " in lines[-1]


# ---------------------------------------------------------------------------
# Image builders
# ---------------------------------------------------------------------------

DD_ADF_BYTES = 901_120  # 880 KiB, 1760 blocks
HD_ADF_BYTES = 1_802_240


@dataclass
class Partition:
    """A partition to create inside an RDB image."""

    size: str | None = None  # e.g. "500MiB"; None means "use remaining space"
    dos_type: str = "ffs+intl"
    bootable: bool = False
    volume: str | None = None  # format with this volume name if given


@dataclass
class RdbImage:
    path: str
    partitions: list[Partition] = field(default_factory=list)


def make_plain_hdf(path: str, size: str = "100Mi", volume: str | None = "Test",
                   dos_type: str = "ffs+intl") -> str:
    """Create a plain (non-RDB) HDF, optionally formatted."""
    if os.path.exists(path):
        os.remove(path)
    cmds = ["create", f"size={size}"]
    if volume is not None:
        cmds += ["+", "format", volume, dos_type]
    xdftool(path, *cmds)
    return path


def make_rdb_hdf(path: str, size: str = "64Mi", partitions: list[Partition] | None = None) -> str:
    """Create an RDB whole-disk image with one or more partitions.

    Note the `size=` trap: rdbtool interprets a bare value as CYLINDERS. Only a value
    ending in b/B is read as bytes, so partition sizes must be spelled "500MiB", not
    "500Mi". See docs/KIP-FFS-NOTES.md G18.
    """
    if os.path.exists(path):
        os.remove(path)
    if partitions is None:
        partitions = [Partition(bootable=True, volume="Test")]

    cmds: list[str] = ["create", f"size={size}", "+", "init"]
    for p in partitions:
        add = ["add"]
        if p.size:
            add.append(f"size={p.size}")
        add.append(f"dostype={p.dos_type}")
        if p.bootable:
            add.append("bootable")
        cmds += ["+", *add]
    rdbtool(path, *cmds)

    for idx, p in enumerate(partitions):
        if p.volume:
            xdftool(path, "open", f"part={idx}", "+", "format", p.volume, p.dos_type)
    return path


def make_adf(path: str, volume: str = "Disk", dos_type: str = "ffs",
             files: dict[str, bytes] | None = None) -> str:
    """Create and format a DD ADF, optionally populating it.

    Keys in `files` may contain '/' to create subdirectories.
    """
    if os.path.exists(path):
        os.remove(path)
    xdftool(path, "create", "+", "format", volume, dos_type)
    if files:
        write_files(path, files)
    return path


def write_files(image: str, files: dict[str, bytes], part: int | None = None) -> None:
    """Write host data into an image, creating parent directories as needed.

    amitools' create_dir is not recursive (docs/KIP-FFS-NOTES.md G19), so directories
    are created one component at a time.
    """
    import tempfile

    made: set[str] = set()
    for ami_path, data in files.items():
        parts = ami_path.split("/")
        for i in range(len(parts) - 1):
            d = "/".join(parts[: i + 1])
            if d and d not in made:
                cmds = ["open"] + ([f"part={part}"] if part is not None else [])
                xdftool(image, *cmds, "+", "makedir", d, check=False)
                made.add(d)
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            cmds = ["open"] + ([f"part={part}"] if part is not None else [])
            xdftool(image, *cmds, "+", "write", tmp, ami_path)
        finally:
            os.unlink(tmp)


def make_tree(root: str, spec: dict[str, bytes]) -> str:
    """Create a host directory tree from {relative_path: content}."""
    if os.path.exists(root):
        shutil.rmtree(root)
    for rel, data in spec.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def workbench_like_tree(root: str, seed: int = 1978, dirs: int = 8,
                       files_per_dir: int = 12) -> tuple[str, int, int]:
    """Build a tree shaped like a Workbench partition: many small files plus icons.

    Returns (root, file_count, total_bytes). Deterministic for a given seed so that
    size and timing assertions stay stable.
    """
    import random

    rnd = random.Random(seed)
    names = ["C", "S", "L", "Libs", "Devs", "Fonts", "Prefs", "Utilities",
             "Tools", "WBStartup", "Classes", "Locale"][:dirs]

    spec: dict[str, bytes] = {}
    total = 0
    for d in names:
        for i in range(files_per_dir):
            size = max(32, min(int(rnd.lognormvariate(7.5, 1.2)), 200_000))
            spec[f"{d}/file{i:03d}"] = bytes(rnd.getrandbits(8) for _ in range(size))
            total += size
            if i % 3 == 0:
                isize = rnd.randint(300, 1500)
                spec[f"{d}/file{i:03d}.info"] = bytes(
                    rnd.getrandbits(8) for _ in range(isize)
                )
                total += isize
    make_tree(root, spec)
    return root, len(spec), total


def du_bytes(path: str) -> int:
    """Actual disk usage in bytes, which differs from apparent size for sparse files."""
    return os.stat(path).st_blocks * 512


def unpack_adf(adf: str, dest: str) -> tuple[dict[str, bytes], list[str]]:
    """Extract an ADF with xdftool into `dest`. Returns (files, empty directories).

    Deliberately uses amitools rather than amibuilder: this builds *input* for tests that
    validate amibuilder, so a bug in our own extraction should not be able to shape the fixture.

    xdftool writes the tree to `dest/<VolumeName>/` and drops `.blkdev`, `.bootcode` and
    `.xdfmeta` sidecars beside it, which are not part of the volume and are excluded.

    Empty directories are returned separately because they carry no files to imply them, and
    something has to recreate them or they vanish.
    """
    os.makedirs(dest, exist_ok=True)
    xdftool(adf, "unpack", dest)

    roots = [
        os.path.join(dest, name)
        for name in sorted(os.listdir(dest))
        if os.path.isdir(os.path.join(dest, name))
    ]
    if not roots:
        raise ToolError(f"xdftool unpack produced no volume directory in {dest}")
    root = roots[0]

    files: dict[str, bytes] = {}
    empty_dirs: list[str] = []
    for current, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(current, root)
        real = [f for f in filenames if not f.endswith((".uaem", ".xdfmeta", ".blkdev",
                                                        ".bootcode"))]
        if rel_dir != "." and not real and not dirnames:
            empty_dirs.append(rel_dir)
        for name in real:
            full = os.path.join(current, name)
            with open(full, "rb") as handle:
                files[os.path.relpath(full, root)] = handle.read()
    return files, empty_dirs


@dataclass
class VolumeSpec:
    """One partition of a test drive, together with what should be on it."""

    partition: Partition
    #: Files to write, as {amiga_relative_path: content}.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Protection bits to apply after population, as {amiga_path: "hsp-rwed"}.
    protect: dict[str, str] = field(default_factory=dict)
    #: Populate from this ADF's entire contents instead of `files`.
    from_adf: str | None = None


def make_multi_volume_hd(path: str, specs: list[VolumeSpec], *, size: str = "64Mi") -> str:
    """Build an RDB hard-drive image with one or more populated partitions.

    `protect` on a spec matters more than it looks: everything xdftool writes gets identical
    default protection, so a comparison across a copy could not catch a bug that reset protection
    bits. Choose paths nothing touches while booting.

    Comments are deliberately not set -- `xdftool comment` crashes inside amitools (pinned by
    `test_comment_command_is_broken`); comment preservation is covered by the software round-trip
    tests instead.
    """
    if os.path.exists(path):
        os.remove(path)
    make_rdb_hdf(path, size=size, partitions=[spec.partition for spec in specs])

    for index, spec in enumerate(specs):
        files, empty_dirs = spec.files, []
        if spec.from_adf:
            workdir = os.path.join(
                os.path.dirname(path) or ".", f"_adf_unpack_{index}"
            )
            files, empty_dirs = unpack_adf(spec.from_adf, workdir)

        if files:
            write_files(path, files, part=index)
        for rel in empty_dirs:
            xdftool(path, "open", f"part={index}", "+", "makedir", rel, check=False)
        for target, flags in spec.protect.items():
            xdftool(path, "open", f"part={index}", "+", "protect", target, flags)
    return path


def make_bootable_hd_from_adf(
    adf: str,
    path: str,
    *,
    size: str = "40Mi",
    volume: str = "Workbench",
    dos_type: str = "ffs+intl",
    protect: dict[str, str] | None = None,
) -> str:
    """Build a single-partition bootable RDB image holding a real ADF's contents.

    This turns a real AmigaOS install floppy into something a real Amiga will boot from a hard
    drive, which is what makes an end-to-end boot test possible without a pre-built OS image.
    """
    return make_multi_volume_hd(
        path,
        [
            VolumeSpec(
                partition=Partition(dos_type=dos_type, bootable=True, volume=volume),
                from_adf=adf,
                protect=protect or {},
            )
        ],
        size=size,
    )
