"""Host-side storage mechanics: sparse files and hole punching on APFS.

Zeroing free blocks makes an image *compress* well. Reclaiming actual disk space is a
separate operation that needs explicit hole punching, because writing zeros does not
deallocate and neither cp nor cp -c re-sparsifies.

Verified in docs/KIP-FFS-NOTES.md section 1.1. These tests are macOS-specific; the
equivalent on Linux is fallocate(FALLOC_FL_PUNCH_HOLE).
"""

from __future__ import annotations

import fcntl
import os
import struct
import subprocess
import sys

import pytest
from helpers import images

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="F_PUNCHHOLE is macOS-specific"
)

F_PUNCHHOLE = 99  # from <sys/fcntl.h>


def punch_hole(path: str, offset: int, length: int) -> None:
    """Deallocate a byte range, leaving it reading as zeros and the size unchanged.

    fpunchhole_t is { u_int32 fp_flags; u_int32 reserved; off_t fp_offset;
    off_t fp_length }.
    """
    arg = struct.pack("=IIqq", 0, 0, offset, length)
    with open(path, "r+b") as f:
        fcntl.fcntl(f.fileno(), F_PUNCHHOLE, arg)


def allocated_bytes(path: str) -> int:
    return os.stat(path).st_blocks * 512


MB = 1024 * 1024


@pytest.fixture
def dense_file(workdir):
    """A 32 MB fully-allocated file of incompressible data."""
    path = str(workdir / "dense.bin")
    with open(path, "wb") as f:
        f.write(os.urandom(32 * MB))
    assert allocated_bytes(path) >= 30 * MB
    return path


def test_writing_zeros_does_not_free_space(dense_file):
    """The trap: overwriting with zeros leaves every block allocated."""
    before = allocated_bytes(dense_file)

    with open(dense_file, "r+b") as f:
        f.write(bytes(28 * MB))

    after = allocated_bytes(dense_file)
    assert after >= before * 0.95, (
        f"zero-writing unexpectedly freed space: {before} -> {after}. If APFS gained "
        "automatic zero detection, `compact` may no longer need hole punching."
    )


def test_cp_does_not_resparsify(dense_file, workdir):
    """Neither a plain copy nor an APFS clone reclaims a zeroed region."""
    with open(dense_file, "r+b") as f:
        f.write(bytes(28 * MB))

    plain = str(workdir / "plain.bin")
    subprocess.run(["cp", dense_file, plain], check=True)
    assert allocated_bytes(plain) >= 30 * MB, "cp re-sparsified unexpectedly"

    clone = str(workdir / "clone.bin")
    subprocess.run(["cp", "-c", dense_file, clone], check=True)
    assert allocated_bytes(clone) >= 30 * MB, "cp -c re-sparsified unexpectedly"


def test_punch_hole_reclaims_space_and_preserves_size(dense_file):
    """F_PUNCHHOLE works from pure Python: no native extension needed for `compact`."""
    size_before = os.path.getsize(dense_file)
    alloc_before = allocated_bytes(dense_file)

    punch_hole(dense_file, 2 * MB, 28 * MB)

    assert os.path.getsize(dense_file) == size_before, "apparent size must not change"
    assert allocated_bytes(dense_file) < alloc_before * 0.4, (
        f"expected a large reduction: {alloc_before} -> {allocated_bytes(dense_file)}"
    )


def test_punched_region_reads_as_zeros(dense_file):
    punch_hole(dense_file, 2 * MB, 28 * MB)
    with open(dense_file, "rb") as f:
        f.seek(4 * MB)
        assert f.read(65536) == bytes(65536)


def test_punch_hole_leaves_surrounding_data_intact(workdir):
    """Only the punched range is affected; the head and tail must survive."""
    path = str(workdir / "edges.bin")
    head = b"HEAD" * 1024
    tail = b"TAIL" * 1024
    with open(path, "wb") as f:
        f.write(head)
        f.write(os.urandom(16 * MB))
        f.write(tail)

    punch_hole(path, MB, 8 * MB)

    with open(path, "rb") as f:
        assert f.read(len(head)) == head
        f.seek(-len(tail), os.SEEK_END)
        assert f.read() == tail


def test_freshly_created_image_is_sparse(workdir):
    """amitools creates images sparsely, so a 1 GiB HDF costs almost nothing."""
    path = str(workdir / "sparse.hdf")
    images.rdbtool(path, "create", "size=1Gi", "+", "init")

    assert os.path.getsize(path) == 1024**3
    assert allocated_bytes(path) < 4 * MB, (
        f"expected a sparse image, {allocated_bytes(path)} bytes allocated"
    )


def test_zerofree_then_compact_reclaims_deleted_file_space(workdir):
    """End-to-end prototype of `zerofree` followed by `compact`.

    This is the sequence that solves the original backup-size problem: zero the blocks
    the bitmap reports free, then punch holes over the resulting zero runs.
    """
    path = images.make_plain_hdf(str(workdir / "zc.hdf"), size="60Mi", volume="ZC")
    keep = {f"keep/f{i}": os.urandom(50_000) for i in range(4)}
    drop = {f"drop/f{i}": os.urandom(400_000) for i in range(24)}
    images.write_files(path, {**keep, **drop})
    images.xdftool(path, "open", "+", "delete", "drop", "all")

    alloc_before = allocated_bytes(path)

    zeroed = _zero_free_blocks(path)
    assert zeroed > 0

    punched = _punch_zero_runs(path)
    assert punched > 0, "expected zero runs to punch after zerofree"

    alloc_after = allocated_bytes(path)
    assert alloc_after < alloc_before * 0.6, (
        f"expected reclamation: {alloc_before} -> {alloc_after} bytes allocated"
    )

    # Safety: every retained file must be byte-identical, and the volume must validate.
    for name, data in keep.items():
        out = str(workdir / "chk.bin")
        images.xdftool(path, "open", "+", "read", name, out)
        with open(out, "rb") as f:
            assert f.read() == data, f"{name} changed"
    assert images.scan_is_ok(path)


# ---------------------------------------------------------------------------
# Prototypes of the eventual zerofree / compact implementation
# ---------------------------------------------------------------------------


def _zero_free_blocks(path: str, reserved: int = 2) -> int:
    """Zero every block the FFS bitmap marks free. Returns how many were changed."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(images.XDFTOOL)))
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory

    blkdev = BlkDevFactory().open(path, read_only=True)
    vol = ADFSVolume(blkdev)
    vol.open()
    try:
        free = [n for n in range(reserved, blkdev.num_blocks) if vol.bitmap.get_bit(n)]
    finally:
        vol.close()

    zeros = bytes(512)
    changed = 0
    with open(path, "r+b") as f:
        for n in free:
            f.seek(n * 512)
            if f.read(512) != zeros:
                f.seek(n * 512)
                f.write(zeros)
                changed += 1
    return changed


def _punch_zero_runs(path: str, min_run: int = 1 << 20) -> int:
    """Punch holes over runs of zeros at least `min_run` bytes long.

    Returns the number of bytes punched. A minimum run length avoids thousands of tiny
    punches, which would be slower and would fragment the file's extent map.
    """
    size = os.path.getsize(path)
    chunk = 1 << 20
    zeros = bytes(chunk)

    punched = 0
    run_start = None
    with open(path, "rb") as f:
        pos = 0
        while pos < size:
            f.seek(pos)
            data = f.read(min(chunk, size - pos))
            is_zero = data == zeros[: len(data)]
            if is_zero and run_start is None:
                run_start = pos
            elif not is_zero and run_start is not None:
                length = pos - run_start
                if length >= min_run:
                    punch_hole(path, run_start, length)
                    punched += length
                run_start = None
            pos += len(data)

    if run_start is not None:
        length = size - run_start
        if length >= min_run:
            punch_hole(path, run_start, length)
            punched += length
    return punched
