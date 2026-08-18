"""The content-addressed blob store.

Two behaviours carry real weight: dedup, which is what makes the whole layer model cheap,
and corruption detection, because a backup tool that silently composes damaged bytes onto a
card is worse than no backup tool.
"""

from __future__ import annotations

import os

import pytest

from amibuilder.errors import ImageError
from amibuilder.layers import blobs as B

# Highly compressible, so it exercises the xz path.
SQUISHY = b"AmigaDOS " * 4096
# Deterministic pseudo-random bytes: incompressible, so xz makes it bigger and the store
# must fall back to raw. Stands in for the .lha archives Amiga software actually ships as.
import random as _random  # noqa: E402

_rng = _random.Random(1978)
INCOMPRESSIBLE = bytes(_rng.randrange(256) for _ in range(20_000))


@pytest.fixture
def store(tmp_path) -> B.BlobStore:
    return B.BlobStore(str(tmp_path / "blobs"))


# ---------------------------------------------------------------------------
# Round trip and addressing
# ---------------------------------------------------------------------------


def test_put_then_get_round_trips(store):
    result = store.put_bytes(SQUISHY)
    assert store.get(result.hash) == SQUISHY


def test_hash_is_of_the_plaintext_so_shasum_agrees(store):
    """Independent verifiability was the reason for choosing SHA-256 of the plaintext."""
    import hashlib

    result = store.put_bytes(SQUISHY)
    assert result.hash == hashlib.sha256(SQUISHY).hexdigest()


def test_empty_content_is_a_valid_blob(store):
    result = store.put_bytes(b"")
    assert result.size == 0
    assert store.get(result.hash) == b""


def test_blobs_are_sharded_by_hash_prefix(store):
    result = store.put_bytes(SQUISHY)
    found = store.find(result.hash)
    assert found is not None
    path, _codec = found
    assert os.path.basename(os.path.dirname(path)) == result.hash[:2]


def test_stored_filename_records_the_codec(store):
    """A per-blob codec is what makes changing the default a non-migration."""
    squishy = store.put_bytes(SQUISHY)
    raw = store.put_bytes(INCOMPRESSIBLE)
    assert store.find(squishy.hash)[0].endswith(".xz")
    assert store.find(raw.hash)[0].endswith(".raw")


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "A" * 64])
def test_invalid_hashes_rejected(store, bad):
    with pytest.raises(ImageError, match="not a valid blob hash"):
        store.path_for(bad)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_second_put_of_identical_content_writes_nothing(store):
    first = store.put_bytes(SQUISHY)
    second = store.put_bytes(SQUISHY)
    assert first.written is True
    assert second.written is False
    assert second.deduplicated is True
    assert first.hash == second.hash
    assert store.count() == 1


def test_dedup_still_reports_accurate_sizes(store):
    store.put_bytes(SQUISHY)
    again = store.put_bytes(SQUISHY)
    assert again.size == len(SQUISHY)
    assert again.stored_size == store.stored_size(again.hash)


def test_differing_content_produces_separate_blobs(store):
    a = store.put_bytes(b"one")
    b = store.put_bytes(b"two")
    assert a.hash != b.hash
    assert store.count() == 2


# ---------------------------------------------------------------------------
# Codec choice
# ---------------------------------------------------------------------------


def test_compressible_content_shrinks(store):
    result = store.put_bytes(SQUISHY)
    assert result.codec == "xz"
    assert result.stored_size < result.size


def test_incompressible_content_is_stored_raw_not_inflated(store):
    """Storing already-compressed data must never cost more than the data itself."""
    result = store.put_bytes(INCOMPRESSIBLE)
    assert result.codec == "raw"
    assert result.stored_size == result.size


def test_raw_blobs_read_back_intact(store):
    result = store.put_bytes(INCOMPRESSIBLE)
    assert store.get(result.hash) == INCOMPRESSIBLE


# ---------------------------------------------------------------------------
# Corruption and absence
# ---------------------------------------------------------------------------


def test_tampered_blob_is_detected(store):
    result = store.put_bytes(INCOMPRESSIBLE)  # raw, so it can be edited in place
    path, _ = store.find(result.hash)
    data = bytearray(open(path, "rb").read())
    data[0] ^= 0xFF
    with open(path, "wb") as fh:
        fh.write(bytes(data))

    with pytest.raises(ImageError, match="is corrupt"):
        store.get(result.hash)
    assert store.verify(result.hash) is False


def test_undecompressable_blob_reports_corruption_not_a_traceback(store):
    result = store.put_bytes(SQUISHY)  # xz
    path, _ = store.find(result.hash)
    with open(path, "wb") as fh:
        fh.write(b"this is not an xz stream")
    with pytest.raises(ImageError, match="cannot decompress"):
        store.get(result.hash)


def test_verify_passes_for_intact_blob(store):
    result = store.put_bytes(SQUISHY)
    assert store.verify(result.hash) is True


def test_missing_blob_names_the_recovery_command(store):
    with pytest.raises(ImageError, match="snap verify"):
        store.get("c" * 64)


def test_verification_can_be_skipped(store):
    """Opt-out exists, and must still return the bytes."""
    result = store.put_bytes(SQUISHY)
    assert store.get(result.hash, verify=False) == SQUISHY


# ---------------------------------------------------------------------------
# Host files and streaming
# ---------------------------------------------------------------------------


def test_put_file_matches_put_bytes(store, tmp_path):
    path = tmp_path / "content.bin"
    path.write_bytes(SQUISHY)
    assert store.put_file(str(path)).hash == B.hash_bytes(SQUISHY)


def test_streaming_path_produces_the_same_hash(store, tmp_path, monkeypatch):
    """Exercised with a lowered threshold, so the test stays fast."""
    monkeypatch.setattr(B, "IN_MEMORY_LIMIT", 1024)
    path = tmp_path / "big.bin"
    path.write_bytes(SQUISHY)
    result = store.put_file(str(path))
    assert result.hash == B.hash_bytes(SQUISHY)
    assert result.codec == "xz"
    assert store.get(result.hash) == SQUISHY


def test_streaming_falls_back_to_raw_for_incompressible_content(store, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "IN_MEMORY_LIMIT", 1024)
    path = tmp_path / "big.bin"
    path.write_bytes(INCOMPRESSIBLE)
    result = store.put_file(str(path))
    assert result.codec == "raw"
    assert result.stored_size == len(INCOMPRESSIBLE)
    assert store.get(result.hash) == INCOMPRESSIBLE


def test_streaming_dedups_against_an_existing_blob(store, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "IN_MEMORY_LIMIT", 1024)
    store.put_bytes(SQUISHY)
    path = tmp_path / "big.bin"
    path.write_bytes(SQUISHY)
    assert store.put_file(str(path)).written is False
    assert store.count() == 1


def test_streaming_leaves_no_temporary_files(store, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "IN_MEMORY_LIMIT", 1024)
    for payload in (SQUISHY, INCOMPRESSIBLE):
        path = tmp_path / "big.bin"
        path.write_bytes(payload)
        store.put_file(str(path))
    leftovers = [
        name
        for _base, _dirs, files in os.walk(store.root)
        for name in files
        if name.startswith(".put-")
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Enumeration, removal, housekeeping
# ---------------------------------------------------------------------------


def test_iter_hashes_lists_everything_stored(store):
    wanted = {store.put_bytes(f"content {i}".encode()).hash for i in range(5)}
    assert set(store.iter_hashes()) == wanted


def test_iter_hashes_on_absent_store_is_empty(tmp_path):
    assert list(B.BlobStore(str(tmp_path / "nope")).iter_hashes()) == []


def test_iter_hashes_ignores_foreign_files(store):
    store.put_bytes(SQUISHY)
    stray = os.path.join(store.root, "zz")
    os.makedirs(stray, exist_ok=True)
    open(os.path.join(stray, "not-a-blob.txt"), "w").close()
    assert store.count() == 1


def test_total_stored_size_sums_on_disk_bytes(store):
    a = store.put_bytes(SQUISHY)
    b = store.put_bytes(INCOMPRESSIBLE)
    expected = store.stored_size(a.hash) + store.stored_size(b.hash)
    assert store.total_stored_size() == expected


def test_delete_removes_the_blob_and_reports_it(store):
    result = store.put_bytes(SQUISHY)
    assert store.delete(result.hash) is True
    assert store.has(result.hash) is False
    assert store.delete(result.hash) is False


def test_delete_tidies_the_emptied_shard(store):
    result = store.put_bytes(SQUISHY)
    shard = store.shard_dir(result.hash)
    store.delete(result.hash)
    assert not os.path.isdir(shard)


def test_delete_keeps_a_shard_that_still_holds_blobs(store):
    """Two blobs can share a shard; removing one must not orphan the other."""
    kept = store.put_bytes(SQUISHY)
    shard = store.shard_dir(kept.hash)
    sibling = os.path.join(shard, "f" * 64 + ".raw")
    os.makedirs(shard, exist_ok=True)
    open(sibling, "wb").close()
    store.delete(kept.hash)
    assert os.path.exists(sibling)


def test_clean_partials_removes_interrupted_writes(store):
    store.put_bytes(SQUISHY)
    os.makedirs(store.root, exist_ok=True)
    open(os.path.join(store.root, ".put-abc.part"), "w").close()
    assert store.clean_partials() == 1
    assert store.clean_partials() == 0


def test_stored_size_of_missing_blob_raises(store):
    with pytest.raises(ImageError, match="not in store"):
        store.stored_size("d" * 64)
