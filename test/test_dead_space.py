"""The finding that motivates the whole project: deleting FFS files reclaims nothing.

FFS deletion clears bitmap bits and unlinks the header. It never touches the data
blocks. So an image carries the full historical high-water mark of everything ever
written to it, and both compression and deduplication see that as live data.

Measured originally at 0.01% of compressed size reclaimed after deleting 4/5 of the
files. See docs/KIP-FFS-NOTES.md section 1. These tests pin the behaviour so that a
future `zerofree` implementation has something to prove itself against.
"""

from __future__ import annotations

import os
import zlib

from helpers import blocks as B
from helpers import images


def _compressed_size(path: str) -> int:
    """Compressed size via zlib, streamed so multi-GB images stay cheap.

    zlib rather than zstd so the test needs no external binary; the ratio between
    before and after is what matters, not the absolute figure.
    """
    comp = zlib.compressobj(6)
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 << 20)
            if not chunk:
                break
            total += len(comp.compress(chunk))
    total += len(comp.flush())
    return total


def test_deleted_file_contents_remain_on_disk(workdir):
    """The clearest form of the finding: the bytes are still there after deletion."""
    path = images.make_plain_hdf(str(workdir / "ghost.hdf"), size="10Mi", volume="Ghost")
    marker = b"AMIBUILDER-GHOST-MARKER-" * 16  # 320 bytes, fits one data block

    images.write_files(path, {"secret.bin": marker})
    with open(path, "rb") as f:
        assert marker in f.read(), "sanity: marker should be present before deletion"

    images.xdftool(path, "open", "+", "delete", "secret.bin")

    # The directory entry is gone...
    assert "secret.bin" not in images.xdftool(path, "open", "+", "list").output

    # ...but the data block was never overwritten.
    with open(path, "rb") as f:
        assert marker in f.read(), (
            "deleted file contents should still be on disk -- this is the whole point "
            "of needing zerofree"
        )


def test_bitmap_marks_the_block_free_while_data_persists(workdir):
    """Confirms the mechanism: the bitmap bit flips, the block content does not."""
    path = images.make_plain_hdf(str(workdir / "bm.hdf"), size="10Mi", volume="BM")
    marker = b"Z" * 400
    images.write_files(path, {"gone.bin": marker})

    # Locate the file's data block before deleting.
    hdr_blk = None
    for blk in B.find_blocks_by_type(path, B.T_SHORT, B.ST_FILE):
        data = B.read_block(path, blk)
        if B.read_bstr(data, 432, 30) == "gone.bin":
            hdr_blk = blk
            data_blk = B.get_long(data, 4)
            break
    assert hdr_blk is not None, "could not find the file header"

    images.xdftool(path, "open", "+", "delete", "gone.bin")

    num_blocks = os.path.getsize(path) // B.BLOCK_SIZE
    root_blk = B.expected_root_block(num_blocks)

    assert B.is_block_free(path, data_blk, root_blk), (
        "bitmap should now mark the block FREE (bit set)"
    )
    assert B.read_block(path, data_blk)[:400] == marker, (
        "the freed block still holds the old contents"
    )


def test_deletion_reclaims_almost_no_compressed_size(workdir):
    """Quantifies the finding: deleting most files barely shrinks the compressed image."""
    path = images.make_plain_hdf(str(workdir / "bulk.hdf"), size="40Mi", volume="Bulk")

    # Incompressible payload, so any reduction must come from real reclamation.
    payload = {f"d{i}/blob{j}": os.urandom(200_000) for i in range(4) for j in range(8)}
    images.write_files(path, payload)

    before = _compressed_size(path)
    used_before = _used_blocks(path)

    for i in range(3):  # delete 3 of the 4 directories
        images.xdftool(path, "open", "+", "delete", f"d{i}", "all")

    after = _compressed_size(path)
    used_after = _used_blocks(path)

    # FFS accounting shows a large reduction...
    assert used_after < used_before * 0.45, (
        f"expected FFS to report far fewer used blocks: {used_before} -> {used_after}"
    )

    # ...while the compressed image is essentially unchanged.
    reclaimed = (before - after) / before
    assert reclaimed < 0.05, (
        f"expected under 5% compressed reclamation, got {reclaimed:.1%} "
        f"({before} -> {after} bytes)"
    )


def test_zeroing_free_blocks_would_reclaim_the_space(workdir):
    """Proves the fix works, using the bitmap to drive the zeroing.

    This is a prototype of the `zerofree` command: read the bitmap, zero every block
    marked free, and confirm the image then compresses to roughly its live size while
    every file remains byte-identical and the volume still validates.
    """
    path = images.make_plain_hdf(str(workdir / "zf.hdf"), size="40Mi", volume="ZF")
    keep = {f"keep/blob{j}": os.urandom(100_000) for j in range(4)}
    drop = {f"drop/blob{j}": os.urandom(200_000) for j in range(12)}
    images.write_files(path, {**keep, **drop})
    images.xdftool(path, "open", "+", "delete", "drop", "all")

    before = _compressed_size(path)
    zeroed = _zero_free_blocks(path)
    after = _compressed_size(path)

    assert zeroed > 0, "expected some free blocks to zero"
    assert after < before * 0.5, (
        f"zeroing free blocks should shrink the compressed image markedly: "
        f"{before} -> {after} bytes after zeroing {zeroed} blocks"
    )

    # The safety property that matters: no file content changed.
    for name, data in keep.items():
        out = str(workdir / "out.bin")
        images.xdftool(path, "open", "+", "read", name, out)
        with open(out, "rb") as f:
            assert f.read() == data, f"{name} changed during zerofree"

    assert images.scan_is_ok(path), "volume must still validate after zeroing"


# ---------------------------------------------------------------------------
# Helpers that prototype zerofree
# ---------------------------------------------------------------------------


def _used_blocks(path: str) -> int:
    out = images.xdftool(path, "open", "+", "info").output
    return int(next(ln for ln in out.splitlines() if ln.startswith("used:")).split()[1])


def _zero_free_blocks(path: str, reserved: int = 2) -> int:
    """Zero every block the FFS bitmap marks free. Returns the count zeroed.

    Deliberately reads the bitmap through amitools rather than by hand, because getting
    the bitmap chain right (including extension blocks) is exactly the part that must
    not be reimplemented casually.
    """
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(images.XDFTOOL)))

    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory

    blkdev = BlkDevFactory().open(path, read_only=True)
    vol = ADFSVolume(blkdev)
    vol.open()
    bitmap = vol.bitmap
    num_blocks = blkdev.num_blocks
    free = [n for n in range(reserved, num_blocks) if bitmap.get_bit(n)]
    vol.close()

    zeros = bytes(B.BLOCK_SIZE)
    count = 0
    with open(path, "r+b") as f:
        for n in free:
            f.seek(n * B.BLOCK_SIZE)
            if f.read(B.BLOCK_SIZE) != zeros:
                f.seek(n * B.BLOCK_SIZE)
                f.write(zeros)
                count += 1
    return count
