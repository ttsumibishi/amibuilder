"""Content-addressed blob store.

One file's contents, stored once, addressed by the SHA-256 of its **uncompressed** bytes.
Addressing the plaintext rather than the compressed form is what makes dedup work: the same
`reqtools.library` shipped by five different installers is one blob regardless of how well
any particular codec happened to do on it, and the recorded hash stays valid if the codec
ever changes.

SHA-256 specifically, over the faster BLAKE2b that is equally available, because a user can
check a blob against `shasum -a 256` without needing this tool. For a backup utility, being
independently verifiable is worth more than the throughput.

**Compression is the secondary win here.** The primary one is not storing unchanged data at
all -- content addressing plus layers. So the codec is deliberately stdlib `lzma`, taking on
no binary dependency for a secondary concern, and it is recorded per blob in the filename
suffix. Adding zstd later is a registry entry and needs no migration, because every existing
blob already says how it was written. That also covers the case that matters most in
practice: Amiga software arrives as `.lha` archives, which are already compressed, so a blob
whose compressed form is no smaller is stored raw rather than paying to make it bigger.
"""

from __future__ import annotations

import hashlib
import lzma
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator

from ..errors import ImageError

#: Hash algorithm, and the length of its hex digest.
HASH_NAME = "sha256"
HASH_HEX_LEN = 64

#: Characters of the hash used as a subdirectory. Two gives 256 shards, which keeps any one
#: directory small enough that a plain `ls` of it stays usable.
SHARD = 2

#: xz compression level. 6 is the stdlib default and a reasonable ratio/speed balance for
#: mixed Amiga data. `PRESET_EXTREME` costs several times the CPU for very little here.
XZ_PRESET = 6

#: Blobs at or below this size are handled entirely in memory. Every file on an Amiga
#: volume is comfortably under it; the streaming path exists for host-side content such as
#: a future byte-exact whole-image capture.
IN_MEMORY_LIMIT = 8 * 1024 * 1024

CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Codec:
    """How a blob's bytes are stored on disk.

    Recorded per blob via `suffix`, so several codecs coexist in one store and switching
    the default is not a migration.
    """

    name: str
    suffix: str
    compress: Callable[[bytes], bytes]
    decompress: Callable[[bytes], bytes]


def _xz_compress(data: bytes) -> bytes:
    return lzma.compress(data, format=lzma.FORMAT_XZ, preset=XZ_PRESET)


def _xz_decompress(data: bytes) -> bytes:
    return lzma.decompress(data, format=lzma.FORMAT_XZ)


def _identity(data: bytes) -> bytes:
    return data


XZ = Codec("xz", ".xz", _xz_compress, _xz_decompress)
RAW = Codec("raw", ".raw", _identity, _identity)

#: Every codec a store can read. Order matters only for lookup cost.
CODECS: tuple[Codec, ...] = (XZ, RAW)
CODECS_BY_SUFFIX = {c.suffix: c for c in CODECS}

#: What new blobs are written with, when compression helps.
DEFAULT_CODEC = XZ


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PutResult:
    """Outcome of storing content."""

    hash: str
    size: int
    #: Bytes actually occupied on disk, after compression.
    stored_size: int
    codec: str
    #: False when the blob was already present, which is the common case during a diff
    #: capture and is the whole point of content addressing.
    written: bool

    @property
    def deduplicated(self) -> bool:
        return not self.written


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def hash_bytes(data: bytes) -> str:
    return hashlib.new(HASH_NAME, data).hexdigest()


def is_valid_hash(value: str) -> bool:
    return len(value) == HASH_HEX_LEN and all(c in "0123456789abcdef" for c in value)


class BlobStore:
    """The `blobs/` directory of a layer store."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    # -- addressing ---------------------------------------------------------

    def _check_hash(self, blob_hash: str) -> str:
        if not is_valid_hash(blob_hash):
            raise ImageError(
                f"not a valid blob hash: {blob_hash!r} "
                f"(expected {HASH_HEX_LEN} lowercase hex characters)"
            )
        return blob_hash

    def shard_dir(self, blob_hash: str) -> str:
        return os.path.join(self.root, blob_hash[:SHARD])

    def path_for(self, blob_hash: str, codec: Codec = DEFAULT_CODEC) -> str:
        """Where a blob written with `codec` would live."""
        self._check_hash(blob_hash)
        return os.path.join(self.shard_dir(blob_hash), blob_hash + codec.suffix)

    def find(self, blob_hash: str) -> tuple[str, Codec] | None:
        """Locate a stored blob whatever codec wrote it."""
        self._check_hash(blob_hash)
        shard = self.shard_dir(blob_hash)
        for codec in CODECS:
            candidate = os.path.join(shard, blob_hash + codec.suffix)
            if os.path.exists(candidate):
                return candidate, codec
        return None

    def has(self, blob_hash: str) -> bool:
        return self.find(blob_hash) is not None

    def stored_size(self, blob_hash: str) -> int:
        """On-disk size, which is the number that matters for 'how big is this layer'."""
        found = self.find(blob_hash)
        if found is None:
            raise ImageError(f"blob not in store: {blob_hash}")
        return os.path.getsize(found[0])

    # -- writing ------------------------------------------------------------

    def put_bytes(self, data: bytes) -> PutResult:
        """Store content, returning its hash. Idempotent.

        Chooses between the default codec and raw storage by trying the codec and keeping
        whichever is smaller, so already-compressed content costs no more than its own
        size.
        """
        blob_hash = hash_bytes(data)
        existing = self.find(blob_hash)
        if existing is not None:
            path, codec = existing
            return PutResult(
                hash=blob_hash,
                size=len(data),
                stored_size=os.path.getsize(path),
                codec=codec.name,
                written=False,
            )

        packed = DEFAULT_CODEC.compress(data)
        codec = DEFAULT_CODEC if len(packed) < len(data) else RAW
        payload = packed if codec is DEFAULT_CODEC else data

        target = self.path_for(blob_hash, codec)
        self._write_atomic(target, payload)
        return PutResult(
            hash=blob_hash,
            size=len(data),
            stored_size=len(payload),
            codec=codec.name,
            written=True,
        )

    def put_file(self, path: str) -> PutResult:
        """Store a host file's contents.

        Small files go through `put_bytes`. Larger ones are hashed and compressed in
        chunks so memory does not scale with the file, which is what a byte-exact
        whole-image capture will need.
        """
        size = os.path.getsize(path)
        if size <= IN_MEMORY_LIMIT:
            with open(path, "rb") as fh:
                return self.put_bytes(fh.read())
        with open(path, "rb") as fh:
            return self.put_stream(fh, size_hint=size)

    def put_stream(self, stream: BinaryIO, *, size_hint: int | None = None) -> PutResult:
        """Store a seekable binary stream without holding it all in memory.

        Two passes: the first hashes and compresses into a temporary file, and if
        compression did not pay, the second copies the plaintext instead. Requires a
        seekable stream, which every caller so far has.
        """
        start = stream.tell()
        hasher = hashlib.new(HASH_NAME)
        compressor = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=XZ_PRESET)

        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".put-", suffix=".part")
        raw_size = 0
        packed_size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    raw_size += len(chunk)
                    hasher.update(chunk)
                    block = compressor.compress(chunk)
                    if block:
                        packed_size += len(block)
                        out.write(block)
                tail = compressor.flush()
                if tail:
                    packed_size += len(tail)
                    out.write(tail)

            blob_hash = hasher.hexdigest()
            existing = self.find(blob_hash)
            if existing is not None:
                return PutResult(
                    hash=blob_hash,
                    size=raw_size,
                    stored_size=os.path.getsize(existing[0]),
                    codec=existing[1].name,
                    written=False,
                )

            if packed_size < raw_size:
                target = self.path_for(blob_hash, DEFAULT_CODEC)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(tmp, target)
                tmp = ""  # consumed by the rename
                return PutResult(
                    hash=blob_hash,
                    size=raw_size,
                    stored_size=packed_size,
                    codec=DEFAULT_CODEC.name,
                    written=True,
                )

            # Compression did not pay. Copy the plaintext across instead.
            stream.seek(start)
            target = self.path_for(blob_hash, RAW)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd2, tmp2 = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".put-", suffix=".part")
            try:
                with os.fdopen(fd2, "wb") as out:
                    shutil.copyfileobj(stream, out, CHUNK)
                os.replace(tmp2, target)
                tmp2 = ""
            finally:
                if tmp2:
                    _quiet_unlink(tmp2)
            return PutResult(
                hash=blob_hash,
                size=raw_size,
                stored_size=raw_size,
                codec=RAW.name,
                written=True,
            )
        finally:
            if tmp:
                _quiet_unlink(tmp)

    def _write_atomic(self, target: str, payload: bytes) -> None:
        """Write via a temporary file and rename.

        The rename is what makes a blob either absent or complete, never truncated -- an
        interrupted capture must not leave content that hashes wrongly. There is
        deliberately no `fsync` per blob: a base layer writes thousands of small files and
        the cost is significant, while the failure it would guard against is machine loss
        during capture, which `snap verify` detects and a re-capture fixes.
        """
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".put-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, target)
            tmp = ""
        finally:
            if tmp:
                _quiet_unlink(tmp)

    # -- reading ------------------------------------------------------------

    def get(self, blob_hash: str, *, verify: bool = True) -> bytes:
        """Read a blob's plaintext.

        Verification is on by default. It costs one hash of data already in memory, and
        the failure it catches -- a blob that no longer matches its own name -- is exactly
        the silent corruption a backup tool exists to prevent. Composition would otherwise
        write the damaged bytes onto a card and report success.
        """
        found = self.find(blob_hash)
        if found is None:
            raise ImageError(
                f"blob missing from store: {blob_hash} -- "
                "the layer references content that is not present (try `snap verify`)"
            )
        path, codec = found
        with open(path, "rb") as fh:
            stored = fh.read()
        try:
            data = codec.decompress(stored)
        except lzma.LZMAError as e:
            raise ImageError(f"blob {blob_hash} is corrupt: cannot decompress ({e})") from e
        if verify:
            actual = hash_bytes(data)
            if actual != blob_hash:
                raise ImageError(
                    f"blob {blob_hash} is corrupt: contents hash to {actual}. "
                    f"Stored at {path}"
                )
        return data

    def verify(self, blob_hash: str) -> bool:
        """Whether a blob is present and hashes correctly."""
        try:
            self.get(blob_hash, verify=True)
        except ImageError:
            return False
        return True

    # -- enumeration and removal -------------------------------------------

    def iter_hashes(self) -> Iterator[str]:
        """Every blob in the store. Order is filesystem order, not sorted."""
        if not os.path.isdir(self.root):
            return
        for shard in sorted(os.listdir(self.root)):
            shard_path = os.path.join(self.root, shard)
            if not os.path.isdir(shard_path) or len(shard) != SHARD:
                continue
            for name in sorted(os.listdir(shard_path)):
                stem, ext = os.path.splitext(name)
                if ext in CODECS_BY_SUFFIX and is_valid_hash(stem):
                    yield stem

    def count(self) -> int:
        return sum(1 for _ in self.iter_hashes())

    def total_stored_size(self) -> int:
        total = 0
        for blob_hash in self.iter_hashes():
            found = self.find(blob_hash)
            if found is not None:
                total += os.path.getsize(found[0])
        return total

    def delete(self, blob_hash: str) -> bool:
        """Remove a blob. Returns whether anything was removed."""
        found = self.find(blob_hash)
        if found is None:
            return False
        os.unlink(found[0])
        shard = self.shard_dir(blob_hash)
        # Tidy an emptied shard so the store does not accumulate 256 empty directories.
        try:
            os.rmdir(shard)
        except OSError:
            pass
        return True

    def clean_partials(self) -> int:
        """Delete leftover `.part` files from interrupted writes."""
        removed = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                if name.startswith(".put-") and name.endswith(".part"):
                    _quiet_unlink(os.path.join(base, name))
                    removed += 1
        return removed


def _quiet_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = [
    "BlobStore",
    "Codec",
    "CODECS",
    "DEFAULT_CODEC",
    "HASH_NAME",
    "HashOnlyBlobStore",
    "PutResult",
    "RAW",
    "XZ",
    "hash_bytes",
    "is_valid_hash",
]


class HashOnlyBlobStore(BlobStore):
    """Hashes content without storing it, for reading an image you do not want to keep.

    Verification needs each file's hash, which is what a capture produces -- but a capture also
    stores every blob it hashes. Verifying a composed drive through an ordinary store would write
    the entire image into it a second time, and any *unexpected* content would land as blobs
    nothing references, turning a read-only check into something `gc` has to clean up after.

    Lookups deliberately answer "absent" rather than consulting a store. If `has()` could return
    True, a caller that skips work for content it already holds would skip the very read the
    verification depends on -- and a verify that quietly reads nothing is worse than no verify.
    This store is therefore for hashing during a read-only capture only; it cannot serve content
    back, so nothing that needs `get()` should be given one.
    """

    def __init__(self, root: str = ""):
        # The root is never touched, so it does not have to exist.
        self.root = root

    def put_bytes(self, data: bytes) -> PutResult:
        return PutResult(
            hash=hash_bytes(data),
            size=len(data),
            stored_size=0,
            codec=DEFAULT_CODEC.name,
            written=False,
        )

    def put_stream(self, stream: BinaryIO, *, size_hint: int | None = None) -> PutResult:
        digest = hashlib.new(HASH_NAME)
        size = 0
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        return PutResult(
            hash=digest.hexdigest(),
            size=size,
            stored_size=0,
            codec=DEFAULT_CODEC.name,
            written=False,
        )

    def put_file(self, path: str) -> PutResult:
        with open(path, "rb") as handle:
            return self.put_stream(handle, size_hint=os.path.getsize(path))

    def has(self, blob_hash: str) -> bool:
        return False

    def find(self, blob_hash: str) -> tuple[str, Codec] | None:
        return None
