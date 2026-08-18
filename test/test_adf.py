"""ADF floppy images: browsing, gzip, and staging contents onto a hard drive.

The staging case is the practical one: a download split across four floppies can be
reassembled directly onto an RDB partition instead of being fed through real disks.
Verified in docs/KIP-FFS-NOTES.md section 10.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys
from pathlib import Path

import pytest
from helpers import blocks as B
from helpers import images

# amitools is importable from the same environment that provides its CLI tools.
sys.path.insert(0, str(Path(images.XDFTOOL).parent.parent))


def _amitools():
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.FSString import FSString

    return ADFSVolume, BlkDevFactory, FSString


def _open_volume(path: str, part: int | None = None, read_only: bool = True,
                 fobj=None):
    ADFSVolume, BlkDevFactory, _ = _amitools()
    opts = {"part": part} if part is not None else None
    blkdev = BlkDevFactory().open(path, fobj=fobj, options=opts, read_only=read_only)
    vol = ADFSVolume(blkdev)
    vol.open()
    return vol


def _walk(vol, node=None, prefix=""):
    """Yield (relative_path, is_dir) for every entry under `node`."""
    if node is None:
        node = vol.get_root_dir()
    for entry in node.get_entries():
        name = entry.name.get_unicode_name()
        rel = prefix + name
        if entry.is_dir():
            yield rel, True
            yield from _walk(vol, entry, rel + "/")
        else:
            yield rel, False


def _mkdirs(vol, path: str) -> None:
    """amitools' create_dir is not recursive (G19), so emulate mkdir -p."""
    _, _, FSString = _amitools()
    parts = [p for p in path.split("/") if p]
    for i in range(len(parts)):
        sub = "/".join(parts[: i + 1])
        if vol.get_path_name(FSString(sub)) is None:
            vol.create_dir(FSString(sub))


# ---------------------------------------------------------------------------
# Basic ADF handling
# ---------------------------------------------------------------------------


def test_dd_adf_is_880_kib(adf):
    assert os.path.getsize(adf) == images.DD_ADF_BYTES == 901_120
    assert os.path.getsize(adf) // B.BLOCK_SIZE == 1760


def test_adf_contents_are_listable(adf):
    out = images.xdftool(adf, "open", "+", "list").output
    assert "Disk1" in out
    assert "part1.bin" in out


def test_adf_root_block_is_880(adf):
    root = B.read_block(adf, 880)
    assert B.get_long(root, 0) == B.T_SHORT
    assert B.get_long(root, -1) == B.ST_ROOT
    assert B.read_bstr(root, 432, 30) == "Disk1"


def test_gzipped_adf_is_readable_read_only(adf, workdir):
    """`.adf.gz` opens directly, so archived collections need no decompression step.

    It must be opened read-only; the factory refuses write access to a gzipped image
    before it even attempts to read.
    """
    gz = str(workdir / "disk.adf.gz")
    with open(adf, "rb") as fi, gzip.open(gz, "wb") as fo:
        shutil.copyfileobj(fi, fo)

    vol = _open_volume(gz, read_only=True)
    try:
        names = [n for n, _ in _walk(vol)]
        assert "Archive" in names
        assert "Archive/part1.bin" in names
    finally:
        vol.close()


def test_gzipped_image_rejects_write_access(adf, workdir):
    gz = str(workdir / "ro.adf.gz")
    with open(adf, "rb") as fi, gzip.open(gz, "wb") as fo:
        shutil.copyfileobj(fi, fo)

    with pytest.raises(Exception) as exc:
        _open_volume(gz, read_only=False)
    assert "gzip" in str(exc.value).lower()


def test_non_dos_adf_fails_safe(workdir):
    """Most game floppies are custom-format. These must be refused, not misparsed."""
    nd = str(workdir / "nondos.adf")
    with open(nd, "wb") as f:
        f.write(os.urandom(images.DD_ADF_BYTES))

    with pytest.raises(Exception) as exc:
        _open_volume(nd)
    assert "Boot Block" in str(exc.value) or "boot" in str(exc.value).lower()


def test_dms_is_not_supported(workdir):
    """DMS needs external conversion with xdms; confirm we fail rather than guess."""
    dms = str(workdir / "disk.dms")
    with open(dms, "wb") as f:
        f.write(b"DMS!" + os.urandom(4096))

    with pytest.raises(Exception):
        _open_volume(dms)


# ---------------------------------------------------------------------------
# Staging: ADF contents into an RDB partition
# ---------------------------------------------------------------------------


def test_stage_multiple_adfs_into_one_target_path(workdir, rdb_hdf):
    """Three ADFs reassembled under one path, as a split archive would need.

    This is the operation `snap create-from-adf` and `cp <adf> <image>` both rely on.
    """
    _, _, FSString = _amitools()

    parts: dict[str, bytes] = {}
    adfs: list[str] = []
    for n in (1, 2, 3):
        payload = os.urandom(60_000)
        parts[f"big.part{n}"] = payload
        adfs.append(
            images.make_adf(
                str(workdir / f"d{n}.adf"),
                volume=f"Disk{n}",
                files={f"Archive/big.part{n}": payload},
            )
        )

    target_path = "Install/BigArchive"
    tgt = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        _mkdirs(tgt, target_path)
        copied = 0
        for a in adfs:
            src = _open_volume(a)
            try:
                for rel, is_dir in _walk(src):
                    dest = f"{target_path}/{rel}"
                    if is_dir:
                        _mkdirs(tgt, dest)
                    else:
                        data = src.read_file(FSString(rel))
                        tgt.write_file(bytes(data), FSString(dest))
                        copied += 1
            finally:
                src.close()
    finally:
        tgt.close()

    assert copied == 3

    listing = images.xdftool(rdb_hdf, "open", "part=0", "+", "list").output
    for n in (1, 2, 3):
        assert f"big.part{n}" in listing

    assert images.scan_is_ok(rdb_hdf), "staging must leave a valid volume"

    # Byte-identity of every staged part.
    vol = _open_volume(rdb_hdf, part=0)
    try:
        for name, payload in parts.items():
            got = bytes(vol.read_file(FSString(f"{target_path}/Archive/{name}")))
            assert got == payload, f"{name} differs after staging"
    finally:
        vol.close()


def test_write_refuses_to_overwrite_an_existing_file(rdb_hdf):
    """amitools never overwrites: it raises NAME_ALREADY_EXISTS and leaves the original.

    This is a consequential design fact, not a quirk. "Last-wins" composition semantics
    therefore need an explicit delete-then-write, which means `merge` policy and ADF
    staging depend on the delete path -- they are not purely additive. See G22.

    Failing safe is the right default; it just has to be handled deliberately.
    """
    _, _, FSString = _amitools()

    vol = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        vol.write_file(b"FIRST", FSString("same.bin"))
    finally:
        vol.close()

    vol = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        with pytest.raises(Exception) as exc:
            vol.write_file(b"SECOND", FSString("same.bin"))
        assert "already exists" in str(exc.value).lower()
    finally:
        vol.close()

    vol = _open_volume(rdb_hdf, part=0)
    try:
        assert bytes(vol.read_file(FSString("same.bin"))) == b"FIRST", (
            "the original content must survive a refused overwrite"
        )
    finally:
        vol.close()


def test_overwrite_requires_delete_then_write(rdb_hdf):
    """The supported way to replace a file, which composition will have to use."""
    _, _, FSString = _amitools()

    vol = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        vol.write_file(b"FIRST", FSString("replaceme.bin"))
    finally:
        vol.close()

    vol = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        vol.delete(FSString("replaceme.bin"))
        vol.write_file(b"SECOND", FSString("replaceme.bin"))
    finally:
        vol.close()

    vol = _open_volume(rdb_hdf, part=0)
    try:
        assert bytes(vol.read_file(FSString("replaceme.bin"))) == b"SECOND"
    finally:
        vol.close()

    assert images.scan_is_ok(rdb_hdf)


def test_staging_collisions_are_detectable_before_writing(workdir, rdb_hdf):
    """A collision must be detected up front so it can be reported, not discovered mid-copy.

    Multi-part archives normally have distinct names per disk, so a collision usually
    means the wrong disks were combined (G21).
    """
    _, _, FSString = _amitools()

    a1 = images.make_adf(str(workdir / "c1.adf"), volume="C1",
                         files={"same.bin": b"FIRST"})
    a2 = images.make_adf(str(workdir / "c2.adf"), volume="C2",
                         files={"same.bin": b"SECOND"})

    planned: dict[str, bytes] = {}
    collisions: list[str] = []
    for a in (a1, a2):
        src = _open_volume(a)
        try:
            for rel, is_dir in _walk(src):
                if is_dir:
                    continue
                if rel in planned:
                    collisions.append(rel)
                planned[rel] = bytes(src.read_file(FSString(rel)))
        finally:
            src.close()

    assert collisions == ["same.bin"]
    assert planned["same.bin"] == b"SECOND", "plan resolves last-wins before any write"

    # Applying the resolved plan touches each path exactly once, so no overwrite occurs.
    tgt = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        for rel, data in planned.items():
            tgt.write_file(data, FSString(rel))
    finally:
        tgt.close()

    vol = _open_volume(rdb_hdf, part=0)
    try:
        assert bytes(vol.read_file(FSString("same.bin"))) == b"SECOND"
    finally:
        vol.close()
    assert images.scan_is_ok(rdb_hdf)


def test_create_dir_is_not_recursive(rdb_hdf):
    """Pins G19: a missing intermediate directory is a hard error, not auto-created."""
    _, _, FSString = _amitools()

    vol = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        with pytest.raises(Exception) as exc:
            vol.write_file(b"x", FSString("A/B/C/deep.bin"))
        assert "Parent" in str(exc.value) or "parent" in str(exc.value)
    finally:
        vol.close()


def test_write_file_takes_data_before_path(rdb_hdf):
    """Pins the argument order, which is easy to get backwards."""
    _, _, FSString = _amitools()

    vol = _open_volume(rdb_hdf, part=0, read_only=False)
    try:
        vol.write_file(b"payload", FSString("ordered.bin"))
    finally:
        vol.close()

    vol = _open_volume(rdb_hdf, part=0)
    try:
        assert bytes(vol.read_file(FSString("ordered.bin"))) == b"payload"
    finally:
        vol.close()
