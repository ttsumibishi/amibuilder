# KIP-FFS — Investigation Notes

**Date:** 2026-08-17 (verified via `date -u`)
**Status:** Investigation only. No product code written.
**Machine:** macOS / Apple Silicon, APFS, Python 3.12.12, rustc 1.93.0, `uv` 0.11.32, zstd 1.5.7. No Go installed.
**Probe environment:** amitools 0.8.1 in a throwaway venv at `/tmp/amitools-probe` (deleted test images afterwards).

Everything below is labelled either **VERIFIED** (I ran it or read the implementation this session) or
**UNVERIFIED** (plausible, needs checking). I have tried hard not to blur the two.

---

## 1. The headline finding: deleting files in FFS reclaims nothing

This is the most important thing I found, and it reframes the whole project.

I built a 1000 MiB FFS image, packed 236 MiB of data into it, then deleted 4/5 of the files.
AmigaDOS-side accounting dropped from 236 MiB used to 46 MiB used, exactly as expected. Then I
measured what that actually bought:

| Measurement | Before delete | After deleting 4/5 of files | Reclaimed |
|---|---|---|---|
| FFS reports used | 236 MiB | 46 MiB | 81% |
| `zstd -3` compressed size | 235,302,718 bytes | 235,279,723 bytes | **0.01%** |
| Sparse on-disk size (APFS `du`) | 230 MB | 230 MB | **0%** |

**VERIFIED.** Deleting a file in FFS clears its bitmap bits and unlinks its header. It does not touch
the data blocks. Every byte of every file ever written to that partition is still sitting there in
blocks marked "free". So an HDF carries the full historical high-water mark of everything ever
installed on it, forever, and both compression and deduplication see that garbage as real data.

That is almost certainly why the backups are enormous. It is not that 4 GB images are inherently
expensive to store; it is that the free space is full of ghosts.

For contrast, a freshly formatted 4 GB image compresses to **133,541 bytes** with `zstd -19 --long`
(**VERIFIED**), because its free space genuinely is zeros.

**Consequence for the design:** a `zerofree` command that walks the FFS allocation bitmap and
overwrites every unallocated block with zeros is the single highest-value feature in this project.
It costs one pass over the image, needs no new on-disk format, and makes every downstream
mechanism (compression, dedup, sparse storage, block-level snapshots) work as intended. It also
happens to be a privacy win: deleted files stop being recoverable from the image.

### 1.1 Reclaiming the space on APFS requires explicit hole punching

Zeroing alone shrinks the *compressed* size but not the on-disk footprint:

| Action on a 100 MB file | On-disk size after |
|---|---|
| Write 100 MB random | 100 MB |
| Overwrite 90 MB of it with zeros | 100 MB (**no change**) |
| `cp` the result | 100 MB (**not re-sparsified**) |
| `cp -c` (APFS clone) | 100 MB (**not re-sparsified**) |
| `fcntl(fd, F_PUNCHHOLE, ...)` over the zeroed range | **10 MB** |

**VERIFIED.** macOS `F_PUNCHHOLE` (command value `99`) works from pure-Python `fcntl` with a
`struct.pack("=IIqq", 0, 0, offset, length)` argument. The file's apparent size stays at 100 MB and
the punched region reads back as zeros. No native extension or C code needed.

So `zerofree` and `compact` are two distinct operations worth separating:

- `zerofree` — write zeros over unallocated blocks. Makes the image compress and dedup well. Keeps
  the image byte-for-byte valid and directly usable.
- `compact` — punch holes over runs of zero blocks. Reclaims local disk space. Purely a host-side
  storage optimisation; the image contents are unchanged.

**Caveat (UNVERIFIED but near-certain):** exFAT and FAT32 do not support sparse files, so a compacted
image expands back to full apparent size the moment it is copied to an SD card. Compaction helps the
Mac-side backup store, not the card.

---

## 2. Corrections to the Gemini technical brief

I checked the brief's structural claims against the amitools 0.8.1 implementation
(`amitools/fs/block/*.py`). **The offset table in that brief is substantially wrong and would not
produce a working parser.** It looks like two different layouts got merged: the header offsets are
missing the 20-byte prefix, and the tail offsets are 8 bytes low.

Do not use the brief as a specification. Corrected values are in section 3.

| Item | Brief claims | Actual (VERIFIED) |
|---|---|---|
| Header block checksum | offset 4 | **offset 20** (longword 5) |
| Hash table location | offset 8 | **offset 24** |
| Hash table size | 296 bytes | **288 bytes** (72 longwords) |
| Hash table entry count | 72, fixed | `block_longs − 56` → 72 at 512 B, **200 at 1024 B** |
| Root block position | `(blocks − 1) / 2` → 879 on a DD floppy | **`blocks // 2`** → **880**, the correct floppy value |
| File size field | offset 316 | **offset 324** |
| Filename length / chars | offset 424 / 425 | **offset 432 / 433** |
| Next-hash link | offset 488 | **offset 496** |
| Parent pointer | offset 492 | **offset 500** |
| Extension pointer | offset 496 | **offset 504** |
| Data block table | "from offset 504 walking backwards" | **offset 308 descending to 24** |
| Block size | "hardcoded to 512 bytes" | **from RDB `de_SizeBlock` × 4**; 1024/2048/4096 are legal |
| HDF structure | "raw image of a disk partition" | **two distinct flavours** — plain single-filesystem, or whole-disk with an `RDSK` block. Both are common. |

The brief's "offset 504 walking backwards" claim is the most dangerous one: offsets 320–508 hold
protection bits, file size, comment, timestamps, filename and the parent/extension pointers. A
parser writing data block pointers there would destroy the file metadata it just wrote.

Three things the brief got **right**, worth keeping: the checksum algorithm for header blocks (sum
all longwords with the checksum slot zeroed, then negate); big-endian throughout; and the inverted
bitmap convention where a set bit means *free*.

One thing it got half right: "checksum at offset 4" is true for the **boot block** and offset 0 for
**bitmap blocks** and offset 8 for **RDB-family blocks**. It conflated four different conventions
into one.

---

## 3. Verified FFS structural reference

Source: `amitools/fs/block/*.py`, read directly. Offsets are byte offsets within a 512-byte block.
`block_longs` = block_bytes / 4 = 128 for 512-byte blocks.

### 3.1 Checksum locations and algorithms

| Block type | Checksum at | Algorithm |
|---|---|---|
| Root, User Dir, File Header, File List, Comment, DirCache | offset **20** (longword 5) | sum all longwords except the slot, negate, mask to 32 bits |
| Bitmap block | offset **0** (longword 0) | same |
| Bitmap **extension** block | **none** | — |
| Boot block | offset **4** (longword 1) | **different**: one's-complement addition (add carry back in) over all boot blocks, then bitwise NOT |
| `RDSK`, `PART`, `FSHD`, `LSEG`, `BADB` | offset **8** (longword 2) | sum-to-zero over `rdb_SummedLongs` longwords |

The boot block checksum spans *all* boot blocks (normally 2), not just the first.

### 3.2 Root block (secondary type 1)

| Offset | Field |
|---|---|
| 0 | primary type = 2 (`T_SHORT`) |
| 12 | `ht_size` = `block_longs − 56` (72 at 512 B) |
| 20 | checksum |
| 24 – 311 | hash table, 72 longwords |
| 312 | bitmap valid flag (`0xFFFFFFFF` = valid) |
| 316 – 415 | `bm_pages[25]` — bitmap block pointers |
| 416 | first bitmap **extension** block |
| 420 / 424 / 428 | last-modified days / mins / ticks |
| 432 / 433 | volume name length / 30 chars |
| 468 | blocks used (**DOS6/DOS7 only**) |
| 472 / 476 / 480 | volume last-altered days / mins / ticks |
| 484 / 488 / 492 | filesystem creation days / mins / ticks |
| 496 | filesystem type (**DOS6/DOS7 only**; 0 otherwise) |
| 504 | extension |
| 508 | secondary type = 1 |

### 3.3 File header block (secondary type −3 / `0xFFFFFFFD`)

| Offset | Field |
|---|---|
| 0 | primary type = 2 |
| 4 | `own_key` — this block's own number; **must equal the block number or the block is invalid** |
| 8 | `block_count` (number of data pointers in this block) |
| 16 | `first_data` |
| 20 | checksum |
| **308 descending to 24** | data block pointers, max `block_longs − 56` = 72, **stored in reverse**: entry *i* lives at longword `block_longs − 51 − i`, so the *first* data block is at offset 308 and the table grows downward |
| 320 | protection bits |
| 324 | file size in bytes |
| 328 | comment length, comment follows |
| 420 / 424 / 428 | days / mins / ticks |
| 432 / 433 | name length / 30 chars (110 for LNFS) |
| 496 | next entry with same hash |
| 500 | parent directory |
| 504 | extension block (`FileListBlock`, primary type 16 = `T_LIST`) |
| 508 | secondary type = −3 |

A user directory block is the same shape with secondary type 2 and a hash table in place of the data
block table.

### 3.4 Block type constants

`T_SHORT = 2`, `T_DATA = 8`, `T_LIST = 16`, `T_DIR_CACHE = 33`, `T_COMMENT = 64`.
Secondary: `ST_ROOT = 1`, `ST_USERDIR = 2`, `ST_FILE = −3`.

### 3.5 Filename hashing

```
h = len(upper(name))
for each byte c in upper(name):
    h = (h * 13 + c) & 0x7FF
h = h % hash_size          # hash_size = block_longs - 56
```

In **international** mode (DOS2–DOS7) the uppercase step additionally maps bytes 224–254 down by 32,
**excluding 247** (`÷`). Using the wrong variant does not corrupt anything, but files land in the
wrong hash bucket and become **invisible to AmigaDOS** while still occupying space. This is a silent
failure mode. The DosType must be read and honoured on every write.

Illegal filename characters: `:` and `/`. Length limit **30** bytes, or **110** with LNFS.

### 3.6 Allocation bitmap

- Bit **set = FREE**, clear = allocated. Inverted relative to most filesystems.
- Bit for block *n*: `off = n − reserved`, longword index `off // 32`, bit index `off % 32`,
  mask `1 << bit`. So **bit 0 (LSB) of the first longword maps to block `reserved`** (normally 2, the
  first block after the boot blocks) — LSB-first bit order inside a big-endian longword.
- One bitmap block maps `(block_longs − 1) × 32` = **4064 blocks** at 512 B.
- The root block holds **25** bitmap pointers → 101,600 blocks ≈ 49.6 MiB.
- Beyond that, **bitmap extension blocks are mandatory**, each holding `block_longs − 1` = 127
  pointers and chaining via offset 508.

**VERIFIED at scale:** a 3.5 GiB FFS partition consumed 1,831 metadata blocks = 1,812 bitmap blocks
+ 15 bitmap extension blocks + root + 2 boot blocks + 1. amitools handles this correctly. Any
from-scratch implementation that ignores extension blocks will silently corrupt anything over ~50 MB.

### 3.7 DosType values

| Value | Meaning |
|---|---|
| `DOS\0` 0x444F5300 | OFS |
| `DOS\1` 0x444F5301 | FFS |
| `DOS\2` 0x444F5302 | OFS + international |
| `DOS\3` 0x444F5303 | FFS + international |
| `DOS\4` 0x444F5304 | OFS + intl + **dircache** |
| `DOS\5` 0x444F5305 | FFS + intl + **dircache** |
| `DOS\6` 0x444F5306 | OFS + intl + **long filenames** |
| `DOS\7` 0x444F5307 | FFS + intl + **long filenames** |

Mask bits: FFS = 1, INTL = 2, DIRCACHE = 4. `PFS\3` (0x50465303) and `SFS\0` are different
filesystems entirely — recognisable in the RDB, not readable at file level by any FFS parser.

### 3.8 RDB partition geometry

A partition's `DosEnvec` gives: `de_SizeBlock` (in **longwords** — 128 means 512 bytes),
`de_Surfaces`, `de_BlocksPerTrack`, `de_SectorPerBlock`, `de_Reserved`, `de_PreAlloc`, `de_LowCyl`,
`de_HighCyl`, `de_Mask`, `de_MaxTransfer`, `de_BootPri`, `de_DosType`.

Partition start block = `surfaces × blocks_per_track × low_cyl`.
Partition length in cylinders = `high_cyl − low_cyl + 1`.
Root block index within the partition = `num_blocks // 2` (**VERIFIED**; gives 880 for a DD floppy).

**UNVERIFIED:** whether `num_blocks // 2` matches AmigaDOS exactly when `de_Reserved ≠ 2` or
`de_PreAlloc ≠ 0`. The classic AmigaDOS formula is usually written
`(de_Reserved + highBlock) / 2`, which agrees with `num_blocks // 2` for the common case
(2 + 1759) / 2 = 880 but would diverge for unusual reserved values. Worth confirming before trusting
any hand-rolled parser on a third-party image. A robust reader should validate the computed root
block (type 2 / subtype 1 / good checksum) and fall back to scanning.

When amitools created a 4 GiB image it chose `heads=8, sectors=32`, giving 256 blocks (128 KiB) per
cylinder and 32,768 cylinders, and reserved cylinder 0 (blocks 0–255) for the RDB itself. Partition
sizes must be whole cylinders.

---

## 4. Measured performance — Python is fast enough

The main reason to reach for Go or Rust would be speed. I measured instead of assuming.

| Operation | Result |
|---|---|
| Create 4 GiB RDB HDF + init | **0.09 s**, 16 KB on disk (sparse) |
| Format 500 MiB FFS partition | 0.09 s |
| Format 3.5 GiB FFS partition | 0.10 s |
| Pack 236 MB / 5,950 files (host → HDF) | **2.61 s** (~90 MB/s, ~2,270 files/s) |
| Unpack 236 MB / 5,950 files (HDF → host) | **1.74 s** (~136 MB/s) |
| Round-trip byte verification (`diff -r`) | **all 5,950 files byte-identical** |
| `xdfscan` full validation of a 4 GiB image | 0.19 s |

**VERIFIED.** A realistic Workbench-sized partition round-trips in a few seconds. The performance
argument for a compiled language does not survive contact with the numbers. The bulk work
(`hashlib`, file I/O, `struct`) is already C inside CPython.

Sparse behaviour is excellent on APFS: a 4 GiB image with two formatted partitions occupied
**1.2 MB** on disk.

---

## 5. amitools assessment

**Verdict: use it. It is the fastest path to a tool that will not eat irreplaceable Amiga disks.**

| Property | Value |
|---|---|
| Version | 0.8.1 |
| **License** | **GPL-2.0-or-later** ← see §5.3 |
| Runtime dependencies | **none** (only `machine68k` for the unrelated `vamos` extra) |
| Python | ≥ 3.8, 3.13 supported |
| Maintenance | active; commits as recent as Dec 2025 |

### 5.1 What it already does (VERIFIED by running it)

- Reads and writes **ADF, HDF, plain and RDB images**, gzipped images (read only), and — usefully —
  **raw block/character devices**, because `BlkDevFactory` explicitly handles `S_ISBLK`/`S_ISCHR`.
  So `/dev/rdiskN` SD card access already works with no extra code.
- Auto-detects image type from an `RDSK` magic at block 0, falling back to file extension.
- Full **DOS0–DOS7** support including international hashing and **dircache maintenance** on add,
  remove and update.
- **Bitmap extension blocks** at multi-GB scale.
- RDB partition create / delete / resize / list / **JSON output**, bootable flag, boot priority,
  DosType, and `fsadd` to embed a filesystem binary as `FSHD`/`LSEG` blocks.
- `pack` / `unpack` whole volumes with metadata preserved two ways (§5.2).
- `xdfscan` validator (fsck-equivalent) — with one caveat, §5.4.
- Fails safe on things it cannot handle: a `PFS\3` partition is listed correctly by `rdbtool` but
  `xdftool` refuses it with `FSError: Invalid Boot Block` rather than misparsing it.

### 5.2 Metadata round-trip already solved — and this is a bigger deal than it looks

Amiga files carry protection bits, a comment of up to 79 characters, and a datestamp, none of which
survive a naive copy to a macOS directory. amitools has two answers:

**`.xdfmeta`** — one flat text file per volume, written alongside the output directory:

```
Workbench:DOS3,17.08.2026 09:46:39.00,17.08.2026 09:47:03.00,17.08.2026 09:47:03.00
S:rwed,17.08.2026 09:47:03.00,
S/Startup-Sequence:s,17.08.2026 09:47:03.00,
test.bin:rwed,17.08.2026 09:47:03.00,
```

**`.uaem` sidecars** (`unpack <dir> fsuae`) — one small file per entry:

```
----rwed 2026-08-17 09:47:03.00 <comment>
```

**VERIFIED:** a full `unpack` → `pack` round-trip restored volume name, DosType, protection flags,
timestamps and byte-identical content.

The `.uaem` format is the **WinUAE / FS-UAE directory-hard-drive convention**. That means an
unpacked backup directory is *directly mountable as a hard drive in an emulator* with no conversion
step. Test a snapshot in FS-UAE before ever writing it to a card. That is a significant practical
win and it is free.

The two formats suit different workflows: `.xdfmeta` is a single diffable text file, which is ideal
if the backup directory is a git repo; `.uaem` is per-file, which is what an emulator wants. Both are
cheap to support.

### 5.3 The licensing constraint

**amitools is GPL-2.0-or-later.** Importing it as a library makes the resulting tool a derivative
work, so it must also be GPL-2.0-or-later if distributed.

For a personal utility this is a non-issue — the GPL imposes obligations on *distribution*, not on
private use. It only matters if the tool is published under a permissive licence. In that case the
options are: publish under GPL-2.0-or-later; invoke `xdftool`/`rdbtool` as subprocesses (widely
accepted as arm's-length, though it forecloses fine-grained control and is slower); or write the FFS
layer from scratch.

**This needs a decision, not a default.** See the open questions in the plan.

### 5.4 Bugs and rough edges found this session

All **VERIFIED** by reproduction.

1. **`xdftool comment` is broken.** Setting any comment crashes:
   `TypeError: object of type 'FileName' has no len()`, from a long-filename length check that
   passes a `FileName` object where a string is expected. Comments are part of the metadata worth
   preserving, so this needs a patch or an upstream fix. Easy fix.

2. **`rdbtool free` crashes when no free space remains.**
   `TypeError: 'NoneType' object is not iterable` — `get_free_cyl_ranges()` returns `None` instead
   of an empty list.

3. **`protect` replaces rather than modifies, despite the `+` sigil.** `protect a.bin +s` turned
   `----rwed` into `-s------`, silently stripping read/write/execute/delete. `protect b.bin rwed+s`
   correctly gave `-s--rwed`. The argument is an absolute specification. A wrapper must always pass
   the complete desired flag set, never a delta.

4. **`xdfscan` reports false-positive errors on dircache volumes.** See below — this one is worth
   spelling out because I got it wrong twice before pinning it down.

The following five came out of building Phase 1 against the Python API rather than the CLI tools.
All **VERIFIED**, all pinned in `test/test_amitools_regressions.py`, all absorbed inside
`amibuilder/volume.py` or `amibuilder/image.py`.

5. **`FileName.__str__` and `__repr__` both raise `TypeError`.** Both return an `FSString`, and
   Python requires a `str`. So `str(node.get_file_name())` — the obvious way to get a filename —
   crashes, as does printing a node. Correct accessor: `get_unicode_name()`. Note `FSString.__str__`
   works fine; only the `FileName` wrapper is broken, which makes the failure look inconsistent.

6. **`ADFSFile.get_blocks(with_data=True)` returns no data blocks on FFS volumes.** `read()` only
   appends to `self.data_blks` in its OFS branch; the FFS branch reads raw blocks straight into the
   output buffer. A 4 KB file reports 1 block instead of 9; a 200 KB file is short by 391. **This is
   the most dangerous of the set** — it silently under-reports block ownership, which is exactly what
   the byte-exact and `zerofree` work in later phases depends on. `data_blk_nums` is populated
   correctly and is what to use.

7. **`TimeStamp`'s epoch constant is timezone-dependent.**
   `amiga_epoch = time.mktime(time.strptime("01.01.1978 00:00:00", ts_format))` interprets the date
   as *local* time, giving 252489600 in UTC-8 against 252460800 for true UTC. Consequences: values
   written in summer are off by the DST difference from January's offset, and an image written under
   one timezone renders shifted under another. Measured: wall clock 16:48:59, bytes on disk decode to
   15:48:59. amitools displays 16:48:59 by re-applying the same error in reverse. See §5.7.

8. **`BlkDevFactory.open()` on an RDB image returns partition 0, not the disk.** It resolves
   `options["part"]` (default `"0"`) and hands back a `PartBlockDevice`, so `num_blocks` is the
   partition's. Enumerating partitions requires driving `RawBlockDevice` + `RDisk` directly. Not a
   bug — but it is a trap, because the returned object looks like the image.

9. **`PartitionBlock.dos_env.block_size` counts longwords, not bytes.** A 512-byte block reports
   128. Reporting it verbatim is wrong by a factor of four.

Also worth recording as a limitation rather than a bug: **`find_partition_by_string` matches
AmigaDOS device names and indexes only, never volume names.** `"DH0"` and `"0"` resolve; `"Workbench"`
returns `None`. Since a user thinks in volume names, amibuilder resolves them itself by mounting each
partition on a miss.

### 5.5 The dircache false positive, and a methodology warning

`xdfscan` reported **92 errors (`E092 NOK`)** on a `ffs+dircache` image that amitools itself had just
created, while reporting **`ok`** for an `ffs+intl` image holding the identical 1,190 files.

My first instinct was a writer bug. It is not. The chain of evidence:

- The validator has **zero dircache awareness** — `grep` for `dircache|DirCache|T_DIR_CACHE` across
  the whole `amitools/fs/validate/` package returns nothing.
- I independently scanned the raw image for blocks with primary type 33 (`T_DIR_CACHE`) and a valid
  header checksum: **99 blocks**. The control plain-FFS image had **0**.
- Summing the bits across all 92 findings where the bitmap says "used" but the validator expects
  "free" gives **exactly 99 blocks**.

An exact match. The writer allocates 99 legitimate dircache blocks and records them correctly in the
bitmap; the validator's block classifier never recognises them, so its tree walk concludes they
should be free. **The errors are artefacts of an incomplete validator, not disk corruption.**

The fix is small and well understood: teach `BlockScan.read_block` to classify `T_DIR_CACHE` and
`DirScan` to traverse the chain.

**Methodology warning, recorded because it cost me time and nearly produced a wrong conclusion in
this document.** My first probe script drove the validator directly and called
`scan_boot → scan_root → scan_dir_tree → scan_bitmap`. That omits **`scan_files()`**, which is what
populates the block map for *data* blocks. Without it, every data block in every image looks
unallocated, and the validator reported thousands of errors on perfectly healthy plain-FFS images. I
briefly believed amitools' write path was broken in general. It is not — `bench.hdf` and `big.hdf`
validate clean once the correct sequence runs.

Two lessons, both of which generalise: **when a validator disagrees with a writer, suspect the
harness before the subject**; and **read how the real tool drives an API before driving it yourself**
(`xdfscan.py` spells the sequence out plainly, numbered 1–5). Any wrapper built on this validator
must reproduce the full sequence or it will produce confident nonsense.

### 5.6 What amitools does *not* do

These are the gaps the new tool has to fill:

- No **sync** in either direction — nothing incremental, no `--delete`, no change detection.
- No **`mv` / rename**, no **`cp`** within an image.
- No **interactive shell** — and `cd` is meaningless in a one-shot CLI, so this matters.
- No **`zerofree`** and no **`compact`** — the highest-value features per §1.
- No **snapshots** or content-addressed storage.
- No **diff** between an image and a directory, or between two images.
- `pack`/`unpack` are whole-volume only: they rewrite everything every time.

### 5.7 Timestamps: why amibuilder deliberately disagrees with amitools

**VERIFIED by measurement.** AmigaDOS stores a timestamp as three numbers — days since
1978-01-01, minutes into the day, ticks (1/50 s) into the minute — and performs **no timezone
arithmetic at all**. The value is naive local wall clock.

amitools converts through Unix time using the constant in bug 7 above. Because `mktime` reads
1978-01-01 as local time, three different conversions of the same on-disk triple give three
different answers. Measured on 2026-08-17 in US/Pacific, for `days=17760 mins=948 ticks=2950`:

| Method | Result |
|---|---|
| Host wall clock when amitools wrote the file | 16:48:59 |
| `localtime(secs + amitools' amiga_epoch)` — what amitools displays | 16:48:59 |
| `1978-01-01 + secs` — plain arithmetic on the stored triple | **15:48:59** |
| `fromtimestamp(secs + true-UTC offset)` | 08:48:59 |

The bytes on disk say 15:48:59. A real Amiga reads the triple and renders it directly, so 15:48:59
is what it would show. amitools wrote a value an hour adrift (January's -08:00 applied during
August's -07:00) and then displays it "correctly" by re-applying the same error backwards.

**Decision: amibuilder renders from the triple and never round-trips through Unix time.** The
number it prints is the number an Amiga would print. `amibuilder ls -l` can therefore differ from
`xdftool list` by an hour or more, and that difference is amitools'.

`--json` emits an ISO-8601 string with **no timezone designator** — deliberate, because the value
genuinely has none and attaching one would invent information — plus the raw
`modified_amiga_secs` and `modified_ticks`, which are portable ground truth.

Two knock-on effects:

- **Vindicates keeping timestamps out of the default layer-diff comparison key**
  (`KIP-FFS-LAYERS.md` §5). A key including them would report differences arising purely from
  which machine wrote the image.
- **`get --preserve-times` interprets the Amiga value as host local time**, which is the only
  reading that keeps the displayed time the same on both sides of the copy.

---

## 6. Gotcha catalogue

Numbered for cross-reference, in the house style of MASTER-VPC-FLIP-PLAN.

**G1 — Filenames longer than 30 characters cannot exist on classic FFS.**
**VERIFIED:** writing a 61-character name fails cleanly with `FSError: Invalid File Name` and writes
nothing. This will hit constantly when syncing from macOS. The sync command must **pre-flight the
entire tree** and report every offending path *before* writing a single block, otherwise a long run
aborts halfway and leaves a partially-updated image. Policy options: skip and report, deterministic
truncation with collision suffixes, or refuse the whole operation. Refusing by default is safest.
LNFS (DOS6/DOS7) raises the limit to 110 but changes the on-disk format.

**G2 — Comments are capped and `:` and `/` are illegal in names.** Comment limit is 79 characters
(80-byte field). Both are silent data loss if truncated without warning.

**G3 — The international-hashing flag changes where files live.** Writing with the wrong hash variant
produces files that occupy space but are **invisible to AmigaDOS**. Always read the DosType from the
volume and honour it. Never assume DOS1.

**G4 — Protection bits are inverted for RWED.** A *set* bit means *denied*. amitools displays
`----rwed` for a file with all four bits clear. Combined with bug §5.4.3 (`protect` replacing rather
than modifying) this is a good way to silently make a Workbench install unbootable.

**G5 — `de_MaxTransfer` and `de_Mask` are real corruption footguns on physical hardware.** amitools
defaults to `max_transfer=0xffffff`, `mask=0x7ffffffe`. Some controllers require a much smaller
`MaxTransfer` (`0x1fe00` is the classic value) and corrupt data silently above it. **UNVERIFIED for
the specific SD adapters in use** — must be checked against whatever is actually in these machines
before creating partitions from scratch. When cloning the layout of a working drive, copy its
existing values rather than using defaults.

**G6 — A 16 GB image cannot be one FFS partition.** Classic FFS is widely documented as limited to
4 GB per partition, with 2 GB barriers in older filesystem and `scsi.device` versions
(**UNVERIFIED** in this session — reported, not tested). A 16 GB image must be an RDB disk carrying
several partitions of 4 GB or less. The `--size` grammar needs to account for this, and `--init`
should refuse or loudly warn when asked for a single partition above 4 GB.

**G7 — Amiga timestamps are days/minutes/ticks since 1978-01-01 in *local* time, with no timezone.**
Ticks are 1/50 s. Any "newer than" comparison against macOS mtime needs an explicit timezone policy
and a tolerance, or sync will thrash. Storing the raw Amiga datestamp in the manifest avoids the
whole problem for round-trips.

**G8 — Dircache volumes (DOS4/DOS5) must have their cache blocks maintained on every write.**
amitools does this correctly. A from-scratch implementation that ignores dircache will produce
volumes that look corrupt on a real Amiga. If unsure, **refuse to write to DOS4/DOS5** rather than
write partially. Note also §5.5: `xdfscan` cannot currently validate these volumes.

**G9 — macOS is case-insensitive and Unicode-normalising; Amiga is case-insensitive Latin-1.** Case
insensitivity happens to match, which is lucky. Encoding does not: Amiga filenames are byte strings
in a Latin-1-like charset, macOS paths are UTF-8 and APFS may normalise. Needs an explicit mapping
and an escaping scheme for bytes that are illegal or ambiguous on either side. The UAE `%XX`
convention is the natural choice since it matches the `.uaem` ecosystem.

**G10 — macOS pollutes directories.** `.DS_Store`, `._` AppleDouble files, extended attributes and
Spotlight indexing all need excluding by default, in both directions. Consider dropping a
`.metadata_never_index` file in the backup root.

**G11 — `.info` icon files are binary and load-bearing.** Never text-translate anything. Never
"helpfully" convert line endings; some Workbench files are scripts, others are binaries, and the
tool cannot reliably tell them apart.

**G12 — FFS has both hard links (secondary type −4) and soft links (type 3).** Sync needs an explicit
policy. Silently dereferencing a hard link turns one block of data into two.

**G13 — Writing zeros does not free disk space, and `cp -c` does not re-sparsify.** See §1.1. Use
`F_PUNCHHOLE`.

**G14 — exFAT/FAT32 on the SD card cannot store sparse files.** A compacted image expands on copy.
The card always pays full apparent size.

**G15 — In-place partial writes to an image on the card are far kinder to flash than rewriting it.**
Syncing only changed files to an image sitting on a mounted SD card touches only the dirty blocks,
instead of rewriting 4 GB. This directly addresses the stated wear concern, and it is a good argument
for supporting a target path under `/Volumes/`.

**G16 — Always shut the Amiga down cleanly before pulling the card.** AmigaDOS caches writes. An
image captured from a card yanked out of a running machine can be inconsistent through no fault of
this tool.

**G17 — A bootable RDB partition needs more than the right files.** Bootability depends on
`PBFB_BOOTABLE`, `de_BootPri`, and the ROM being able to mount the DosType — possibly needing a
filesystem binary embedded in the RDB as `FSHD`/`LSEG` for non-ROM filesystems. **UNVERIFIED:**
exactly what a 3.1 ROM requires for DOS3 on these specific controllers. A file-level restore into an
already-correctly-partitioned drive sidesteps most of this, which is an argument for keeping
"restore files" and "create a bootable drive from nothing" as separate, separately-tested features.

**G18 — RDB partition sizes are cylinder-granular.** A byte size that is not a whole number of
cylinders will be rounded. Report the rounding rather than hiding it. Note also that `rdbtool`'s
`size=` parameter means **cylinders** unless the value ends in `b`/`B` — `size=500Mi` is silently
interpreted as 500 Mi *cylinders* and fails, while `size=500MiB` works. A wrapper should never expose
that ambiguity to the user.

---

## 7. Verified vs unverified summary

**VERIFIED this session:** every block offset in §3 (read from the implementation); the root block
formula giving 880 for a DD floppy; the hash function including intl handling; bitmap bit order and
inverted convention; bitmap extension blocks working at 3.5 GiB; all performance numbers in §4; the
0.01% compression finding in §1; `F_PUNCHHOLE` working from Python; the full metadata round-trip; the
30-character filename limit failing safe; PFS3 failing safe; all four amitools bugs in §5.4; the
99-block dircache false-positive arithmetic.

**UNVERIFIED — needs checking before relying on it:**

- The root block formula for unusual `de_Reserved` / `de_PreAlloc` values (§3.8).
- Correct `de_MaxTransfer` / `de_Mask` for the actual SD adapters (G5). Now largely sidestepped by
  capturing them from a working drive — see `KIP-FFS-LAYERS.md` §4.
- What a bootable DOS3 RDB partition requires on this specific hardware (G17). Same mitigation.
- Whether hard and soft links round-trip correctly through amitools' `pack`/`unpack` — I did not test
  links at all.
- Whether LNFS (DOS6/DOS7) write support is as solid as DOS0–DOS5.
- **Nothing has been tested against a real Amiga or an emulator.** Every claim of correctness here
  rests on amitools' own round-tripping plus its validator. Booting a restored image in FS-UAE or
  vAmiga is the decisive test and it has not been done.

**RESOLVED by decisions taken 2026-08-17** (previously open questions Q1–Q5, Q7):

- **Targets are ZuluSCSI (latest revision) and PiStorm (68k, bare metal / Emu68)**, plus WinUAE and
  FS-UAE for fast software installs. See §9 — this changes the recommended image format.
- **No dircache.** DirCache (DOS4/DOS5) exists to speed directory reads on slow media and buys
  nothing on SD-backed storage. This retires G8 as a concern and avoids the `xdfscan` false positive
  in §5.5 entirely. **Target DosType is DOS3 (`ffs+intl`)**, or DOS1 where international hashing is
  not wanted.
- **No partition over 4 GB.** Retires G6 as a practical concern, though `init` should still refuse or
  warn to keep the guard rail.
- **Private utility, possibly published later.** amitools' GPL-2.0-or-later is therefore acceptable
  now. Keeping the FFS layer behind a narrow interface preserves the option to publish under a
  permissive licence later without a rewrite from a standing start.
- **Restore composes into a fresh image, not in place** (a consequence of adopting the layered
  model). This removes in-place mutation from the critical path — see `KIP-FFS-LAYERS.md` §1.

---

## 8. Reproducing the key experiments

```bash
uv venv /tmp/amitools-probe --python 3.12
uv pip install --python /tmp/amitools-probe/bin/python amitools
export PATH=/tmp/amitools-probe/bin:$PATH

# 4 GiB RDB image, two partitions, one bootable. Note the 'B' suffix (G18).
rdbtool t.hdf create size=4Gi + init \
  + add size=500MiB dostype=ffs+intl bootable \
  + add dostype=ffs+intl
rdbtool t.hdf list
xdftool t.hdf open part=0 + format Workbench ffs+intl
xdftool t.hdf open part=0 + info
du -h t.hdf            # ~1.2 MB on disk for a 4 GiB image

# Metadata round-trip, both flavours
xdftool t.hdf open part=0 + unpack out          # -> out/ plus out.xdfmeta
xdftool t.hdf open part=0 + unpack out2 fsuae   # -> out2/ with .uaem sidecars

# The dead-data demonstration
xdftool big.hdf create size=1000Mi + format Big ffs+intl + pack <some-tree>
zstd -3 big.hdf -o before.zst
xdftool big.hdf open + delete <most-of-it> all
zstd -3 big.hdf -o after.zst    # within 0.01% of before.zst

# Validation — the full 5-step sequence matters (§5.5)
xdfscan t.hdf
```

---

## 9. Target hardware and image formats

Investigated 2026-08-17 after the targets were confirmed as **ZuluSCSI (latest revision)** and
**PiStorm (68k, bare metal)**, with WinUAE and FS-UAE used for faster software installs.

### 9.1 The headline correction: plain HDF is the *least* portable choice

The working assumption was that plain HDF is the most portable format. The evidence points the other
way for this particular hardware mix.

| Target | What it accepts | Plain HDF (no RDB) | RDB whole-disk image |
|---|---|---|---|
| **ZuluSCSI** | Raw hard-drive image files on a FAT32/exFAT SD card | Presents as a disk with no partition table; the Amiga finds nothing to mount without a manual mountlist | **Works** — `scsi.device` reads the RDB and mounts the partitions |
| **PiStorm / Emu68** | **No HDF support whatsoever** | ✗ | Not as a file — but the bytes go into an MBR `0x76` partition, which is `dd`-equivalent to an RDB image |
| **WinUAE** | Both | Works (geometry supplied manually) | Works — "Partitionable Hard Drive Image (RDB)" |
| **FS-UAE** | Both, plus directory hard drives | Works | Works |

**VERIFIED from primary sources.** The ZuluSCSI documentation states it uses raw hard-drive image
files stored on a FAT32 or exFAT SD card, commonly called `.hda` files — i.e. whole-disk images, so
the Amiga's own `scsi.device` does the partition discovery and an RDB is required for anything to
mount normally.

The Emu68 SD-card preparation guide is blunter, and it is the finding that matters most: Emu68
*"attempts to emulate as little as possible. Therefore, you will not find a way to attach a HDF image
file to the system and let Emu68 pretend that this is a hard drive."* Instead PiStorm gains a Zorro III
card whose ROM carries a microSD driver, and the card itself becomes a block device.

**Conclusion: an RDB whole-disk image is the universal interchange format here.** It works directly
on ZuluSCSI, works in both emulators, and is byte-compatible with the contents of a PiStorm `0x76`
partition. A plain HDF works only under emulation. This argues for promoting RDB support from "later"
to "core", which the revised plan does.

### 9.2 How PiStorm / Emu68 actually exposes storage

Worth recording in detail, because it determines what the tool has to be able to address.

- The microSD driver is **`brcm-sdhc.device`** on Pi Zero 2, 3A+, 3B and 3B+, and
  **`brcm-emmc.device`** on Pi 4B+ and CM4. HDToolBox has to be retargeted from `scsi.device` to
  whichever applies.
- The card **must use MBR**. GPT is not supported by the SDHC driver.
- The driver presents **unit 0 as the whole physical card**, including the FAT32 boot partition
  holding Emu68 itself. The guide is emphatic about not repartitioning unit 0.
- **MBR primary partitions of type `0x76`** appear as separate Amiga drive units (1, 2, …). Each one
  then carries **its own RDB**, created with HDToolBox from inside AmigaOS.
- Critically for this project, the guide notes these partitions can be moved on the card or copied
  with `dd` to other cards **or to a hardfile**. So a `0x76` partition's contents are exactly an RDB
  disk image. That is the bridge between the PiStorm workflow and the ZuluSCSI/emulator workflow.

There is also a real-world confirmation of the `de_Mask` footgun (G5) in that guide: PFS3 fails on
Emu68 when HDToolBox's suggested mask of `0xffffff` is used, because that confines filesystem buffers
to the first 16 MB while most Emu68 RAM sits above it. This is the class of value that must be copied
from a working drive rather than defaulted.

### 9.3 VERIFIED: reading an RDB image inside an MBR `0x76` partition

The PiStorm workflow requires addressing a byte range inside `/dev/rdiskN` rather than a whole file.
I built and ran a proof:

1. Created a 64 MiB RDB image with one bootable `ffs+intl` partition and wrote
   `S/Startup-Sequence` into it.
2. Embedded it at byte offset 8 MiB inside a larger container, and wrote a valid MBR with entry 0 as
   type `0x0c` (FAT32) and entry 1 as type **`0x76`** pointing at the embedded image.
3. Parsed the MBR back out (signature `0x55AA` at 510, 16-byte entries from offset 446, type byte at
   entry offset 4, LBA start and count as little-endian at offsets 8 and 12) and located the `0x76`
   entry.
4. Wrapped the byte range in a small file-like slice object and passed it to amitools as the `fobj`
   argument to `BlkDevFactory.open()`.

Result: the volume opened correctly (`name='Inner'`, dostype `0x444f5303`) and
`S/Startup-Sequence` read back byte-identical.

**No amitools modifications are required.** `BlkDevFactory.open()` already accepts a file object and
derives size from it. The MBR parser plus the slice wrapper is roughly 60 lines. This makes the
PiStorm path a first-class target rather than a research problem.

Safety note: this same capability points at `/dev/rdiskN`, so it needs the guard rails from
`KIP-FFS-IDEAS.md` §4.7 — `diskutil unmountDisk` first, an explicit `--device` flag, typed
confirmation showing identifier and size, read-only by default, and a hard refusal on the boot disk.
Writing to the wrong MBR entry on a PiStorm card destroys the Emu68 boot partition.

### 9.4 Consequences for the plan

- **RDB is core, not deferred.** `init` and `compose` must produce RDB images by default.
- **Raw device plus MBR `0x76` addressing is core**, not a nice-to-have, because it is the only way to
  reach a PiStorm card's Amiga partitions.
- **Plain HDF stays supported** as an emulator convenience target.
- **The `dir` target matters more than expected.** FS-UAE directory hard drives combined with the
  `.uaem` metadata convention (§5.2) mean a software install can be iterated with no image at all.
- **Capture the drive layout, do not synthesise it.** See `KIP-FFS-LAYERS.md` §4.

---

## 10. ADF support and staging (VERIFIED)

Investigated 2026-08-17 after ADF handling was added to the requirements: browse ADF images and copy
their contents into an RDB at a chosen path, so that multi-disk downloads can be staged straight onto
a hard drive instead of being fed through real floppies.

Short version: **amitools handles this well, and the staging path works without a host temp
directory.**

### 10.1 What works

| Capability | Result |
|---|---|
| Create / format / populate a DD ADF | **Works.** 901,120 bytes (880 KiB) |
| Accepted ADF sizes | `DD_SECS × 512` and `DD_SECS × 513`, likewise for HD. The 513 variant covers images carrying per-sector error info, which some rippers emit |
| **Gzipped ADF** (`.adf.gz`) | **Works, read-only.** Read a 120,000-byte file out of a gzipped image successfully |
| Non-DOS ADF (raw/bootblock game disk) | **Fails safe** — `FSError: Invalid Boot Block`, no misparse |
| **Multi-ADF staging into an RDB HDF** | **Works.** Three ADFs merged into `Work:Install/BigArchive/Archive/`, `xdfscan` reported **ok**, staged file byte-identical to origin |

The staging test opened the source ADF volume and the target RDB partition simultaneously in one
process and copied file-by-file through the API. No temporary directory, no `unpack`/`pack` round
trip. Directory structure from each ADF was recreated beneath the chosen target path, and the three
disks' contents merged into one tree as intended.

This means the "archive split across four floppies" case is a natural fit: point the tool at
`Disk1.adf … Disk4.adf` with one target path and it reassembles the tree on the hard drive.

### 10.2 Relevant API details

Worth recording because two of them cost time:

- `ADFSVolume.write_file(data, ami_path)` takes **data first, path second**. Easy to get backwards.
- `ADFSVolume.create_dir()` is **not recursive**. A `mkdir -p` equivalent has to be written — walk the
  path components and create any that `get_path_name()` reports missing. About 6 lines, but it is a
  hard failure (`FSError: Invalid Parent Directory`) if forgotten.
- Directory entry names come from `FileName.get_unicode_name()`, not `get_unicode()`.
- Gzipped images must be opened with `read_only=True`; otherwise the factory raises
  `can't write gzip'ed image files!` before it gets as far as reading.

### 10.3 Out of scope

**DMS** (DiskMasher) is a compressed Amiga disk image format still common in download archives.
amitools does not handle it (**verified by absence** — no DMS support in the image factory). The
standard route is to convert externally with `xdms` first. Worth documenting as a prerequisite rather
than reimplementing, since `xdms` is small and widely packaged.

**Non-DOS disks** — many game floppies are custom-format with no filesystem. These cannot be browsed
at file level by anything, and the tool should say so plainly rather than appearing broken.

### 10.4 New gotchas

**G19 — `create_dir` is not recursive.** See §10.2. Any write path needs its own `mkdir -p`.

**G20 — ADF filename limits are the same 30 characters as FFS** (G1), so staging an ADF whose
contents came from a long-filename filesystem can fail mid-copy. Pre-flight the whole ADF tree before
writing, exactly as for host-directory sync.

**G21 — Staging merges silently by default.** Copying several ADFs into one target path is the
desired behaviour, but it means a filename collision between disks resolves to last-wins with no
warning unless the tool reports it. Multi-part archives normally have distinct names per disk, so
collisions usually signal that the wrong disks were combined. Report them.

---

## 11. Later findings (2026-08-17, during test suite construction)

### 11.1 amitools never overwrites an existing file

**VERIFIED, and consequential.** Both `ADFSVolume.write_file()` and `xdftool write` raise
`FSError: Name already exists(10)` when the target already exists, and the original
content is preserved untouched.

So "last-wins" composition semantics require an **explicit delete-then-write**. That
matters for the phase plan: `merge` volume policy and ADF direct injection are therefore
*not* purely additive — they depend on the delete path for the collision case. Two ways to
handle it, and both are worth having:

1. **Resolve the plan before writing.** Collapse all sources to a final path→content map
   first, so each path is written exactly once and no overwrite ever occurs. This is the
   right default for composition and staging, and it also makes collisions reportable
   before anything touches the image.
2. **Delete then write** for genuine in-place replacement, where the target already exists
   on disk from an earlier run.

Failing safe is the correct default; it simply has to be handled deliberately rather than
discovered at runtime.

**G22 — Overwriting a file requires deleting it first.** A write to an existing path fails
cleanly. Composition and staging should resolve conflicts in the plan rather than relying
on overwrite semantics that do not exist.

### 11.2 A sum-to-zero checksum cannot identify its own slot

**VERIFIED, and a genuine trap for block scanners.** Because the stored checksum forces
the sum of all longwords to zero, the value in *any* longword k equals the negated sum of
the others. So "the checksum validates" is true for every candidate slot, and a parser
cannot locate the checksum position empirically — it must know the block type first, from
the type fields at byte 0 and byte 508.

Practical consequence: identifying a block by "its checksum is valid" is meaningless on
its own. The checksum still detects corruption correctly; it just carries no information
about layout. This is pinned by
`test_ffs_structure.py::test_sum_to_zero_checksum_validates_at_every_slot`.

### 11.3 `format` writes no boot block checksum; `boot install` does

**VERIFIED.** After `format`, the boot block carries the DosType at byte 0 and the root
block pointer at byte 8, but the checksum at byte 4 is **zero**. Running
`xdftool <img> open + boot install` writes real m68k boot code and a valid checksum.

This is correct rather than a bug: on a hard disk partition the boot block is not what
boots the machine, so `Format` has no reason to checksum it. Worth knowing because a zero
there looks like corruption if you expect a checksum.

amitools ships the boot code as two small m68k blobs, `boot1x.bin` (38 bytes) and
`boot2x3x.bin` (82 bytes).

### 11.4 The bitmap spans multiple pages sooner than intuition suggests

One bitmap block maps 4064 blocks, i.e. about 2 MiB. So on any realistic volume the great
majority of blocks live beyond `bm_pages[0]`, including the root block itself. A helper
that consults only the first bitmap page appears to work and silently checks nothing —
which is exactly what happened in the first draft of this test suite, producing a
green-but-vacuous assertion. Pinned by
`test_ffs_structure.py::test_bitmap_spans_multiple_pages_on_a_modest_volume`.

### 11.5 `pack` recreates the volume, discarding a prior `format`

**VERIFIED.** `xdftool img create ... + format X ffs+dircache` followed by a *separate*
`xdftool img open + pack tree` loses the dircache DosType, because `pack` creates the
volume itself (reading `.xdfmeta` if present, otherwise using defaults). Chaining
`create + format + pack` in one invocation preserves it.

So `pack` is a whole-volume operation that owns volume creation. Any wrapper that wants a
specific DosType must either pass it through the same invocation or supply a `.xdfmeta`.

### 11.6 vamos exists and installs, which matters for installer-script emulation

`amitools[vamos]` pulls in `machine68k` 0.3.0, a compiled m68k CPU emulator that **built
and installed cleanly on Apple Silicon**. `vamos` is an AmigaOS API-level emulator that
runs m68k Amiga CLI binaries on the host; it is what lets people run Amiga cross-compilers
natively.

This is the plausible escape hatch for `(run ...)` and `(execute ...)` in Installer
scripts. **UNVERIFIED:** I confirmed `vamos` initialises and prints usage, but I had no
Amiga executable to hand, so I have *not* seen it execute one. Treat "vamos can run the
installer's helper binaries" as promising rather than established.

Note it is an optional extra, so the base dependency stays at zero runtime packages.

---

## 12. Findings from the AmigaOS 3.2 CD (2026-08-17)

Full analysis in `KIP-FFS-INSTALLERS.md`. Recorded here are the facts that generalise
beyond installer scripts.

### 12.1 The read path works on real Amiga media

**VERIFIED.** `Install3.2.adf` from the AmigaOS 3.2 CD lists correctly with the project's
own tooling and `xdfscan` reports **`boot ok`**. This is the first validation against media
not produced by amitools. Latin-1 filenames (`Español.info`, `Türkçe.info`,
`Français.info`) render correctly, and real-world protection bits appear including the pure
bit (`--p-rwed`) on commands and the script bit (`-s--rw-d`) on scripts.

### 12.2 Install media and installed system use different hash variants

**VERIFIED and worth watching.** The AmigaOS 3.2 install disk is **`DOS1:ffs`** — plain
FFS with *no* international hashing — while this project targets DOS3 (`ffs+intl`). So a
tool copying from install media to an installed system crosses a hash-variant boundary.

That is fine as long as each volume is hashed according to its own DosType, which is the
existing rule (G3). It does mean the rule gets exercised in normal use rather than only in
unusual cases, so it is worth a test rather than trust.

### 12.3 New gotchas

**G23 — `grep` silently suppresses matches in files with high Latin-1 bytes.** The
AmigaOS 3.2 install script contains bytes like `0xA0` (non-breaking space), so `grep`
classifies it as binary and prints nothing at all rather than reporting matches or warning.
`grep -a` fixes it. This cost real time during investigation: a search for `(run` returned
empty while the file contained 30 of them, which looks like the pattern being wrong rather
than the tool declining to answer. Any script that greps Amiga text files should pass `-a`.

**G24 — Amiga `.info` icon files need a `DiskObject` parser and amitools has none.** Icon
files are binary and carry both cosmetic data (position in a drawer) and functional data
(tooltypes such as stack size). The AmigaOS installer manipulates them via the external
`IconPos` and `CopyToolTypes` commands. Anything reproducing an install at file level needs
either an icon parser, or to accept default positions and tooltypes. The format is
documented; the work is bounded but real.

### 12.4 A useful precedent: installers already treat ADFs as virtual floppies

The AmigaOS 3.2 installer mounts its own ADFs with `DAControl` rather than asking for
physical disks, and wraps that in `MOUNTADF` / `UNMOUNTADF` procedures. So the "read an ADF
as a source volume" model this project needs is the model the official installer already
uses. That is a good sign for the ADF staging design generally, not just for script
simulation.

---

## 13. Findings from building layer capture (2026-08-18)

### 13.1 amitools cannot represent AmigaDOS links at all

**VERIFIED, and it bounds what any file-level capture can record.** G12 records that FFS has
hard links (secondary type −4) and soft links (type 3) and that sync needs a policy for them.
The stronger fact is that **amitools has no way to express either**:

- `amitools/fs/block/Block.py` defines only `ST_ROOT = 1`, `ST_USERDIR = 2` and `ST_FILE = −3`.
  There is no `ST_LINKFILE`, `ST_LINKDIR` or `ST_SOFTLINK`.
- The string "Link" does not appear anywhere in `amitools/fs/`. There is no link node class.

So a link's *target* is unreachable through amitools, and it is not obvious what its reader
does when it meets one on real media — it may raise, or it may present the block as an ordinary
file. **UNVERIFIED**, because creating a link needs a real Amiga or a hand-built block, and
neither was to hand.

Two consequences, both now recorded in code:

1. `amibuilder/layers/capture.py` records a warning naming each link and skips it, rather than
   inventing a target that composition would later act on. The manifest format already carries
   the `h` and `s` kinds and a `link_target` field, so only the reading side is missing.
2. `amibuilder/volume.py` derives `link_kind` by testing whether the amitools node's class name
   contains "Link". Since no such class exists, **that branch is currently unreachable** — the
   comment beside it claiming amitools models links as distinct node classes was wrong. It is
   left in place because it costs nothing and would start working the day amitools grows link
   support, which is what `test_amitools_regressions.py::test_amitools_has_no_link_support`
   watches for.

**This is the one open risk for capturing a real AmigaOS install.** If 3.2.3 ships any links,
the base layer is incomplete in a way that only bites at compose time — but the warning names
each one, so the cost is known rather than silent.

**G25 — amitools cannot read or write AmigaDOS links, so link targets are unrecoverable.**
Distinct from G12, which is about policy: this is about capability. Any tool built on amitools
must either warn and skip, or read the link blocks itself at the raw-block level.

### 13.2 `format` leaves no boot code, which makes boot-block capture cheap

**VERIFIED.** §11.3 established that `format` writes no boot block *checksum*. Capturing boot
blocks across amitools-formatted partitions confirms the related point: the body beyond the
12-byte header is **entirely zero** unless `boot install` has been run. So recording a
partition's boot blocks costs a few dozen bytes for the DosType, checksum and a hash, and only
grows to a kilobyte when there is genuinely custom boot code to preserve.

That makes the L2 insurance in `KIP-FFS-LAYERS.md` essentially free, and it gives a cheap
detector: a partition whose boot-block body is non-zero has had something deliberate done to
it, and `snap show` flags it as `custom boot block`. Whether the real ZuluSCSI and PiStorm
drives carry any is now one read-only `snap create` away.

### 13.3 Two separately built images with identical contents differ in every datestamp

**VERIFIED, and it is the evidence behind the diff comparison key.** Building the same file set
into two images seconds apart produces manifests whose every entry differs — timestamps have
tick resolution, so nothing collides. Diffing them with the default comparison key yields
**zero** changes; with `--timestamps-significant` every entry is reported as changed.

This is a synthetic stand-in for the real risk in `KIP-FFS-LAYERS.md` §5, not a substitute for
it. It proves the mechanism excludes timestamp noise. It does not prove the default exclusion
list is right, because that depends on what a real AmigaOS boot writes — which remains
unmeasured.

---

## 14. Findings from building `cp` and `mkdir` (2026-08-20)

The first commands that write host bytes into an image. All verified on real AmigaOS 3.2 —
`KIP-FFS-STATS.md` §5 has the AmigaDOS output.

### 14.1 New gotchas

**G26 — amitools keeps `read_only` in a different place on every block-device class.**
`RawBlockDevice` and `HDFBlockDevice` hold it on `self.img_file` (an `ImageFile`);
`ADFBlockDevice` holds it directly; `PartBlockDevice`, which is what an RDB partition is
mounted through, does not carry it at all. So `getattr(blkdev, "read_only", True)` looks
correct and is wrong for the most common case — it reported every RDB partition as read-only.
Writability has to be tracked by the caller that opened the device. `amibuilder.image.Container`
already knows, so it passes the flag down rather than having `Volume` sniff for it.

**G27 — `ProtectFlags.parse_full` and `parse` disagree about both input and exception type.**
`parse_full` requires exactly 8 characters and raises **`ValueError`** on anything else.
`parse` accepts a short form naming only the permitted bits (`rwed`) plus `+`/`-` runs, and
raises **`FSError`**. Sibling methods on the same class, two different exception hierarchies, so
a caller wrapping one will miss the other. The short form is genuinely useful: argparse reads a
bare `--protect ----rwed` as another option and refuses it, whereas `--protect rwed` needs no
escaping.

**G28 — `change_meta_info` silently ignores a protection mask of 0.** The guard is
`if protect and hasattr(self.block, "protect")`, and 0 is falsy. Since AmigaDOS protection bits
are **inverted**, 0 is the perfectly ordinary `----rwed` — so the one value meaning "everything
permitted" cannot be set through `change_meta_info`. Not currently load-bearing, because `cp`
supplies protection at create time through `MetaInfo`, where `blocks_create_new` writes the
field directly. It will bite whoever adds a standalone `protect` command.

**G30 — `BlkDevTools` lives in `amitools.util`, not `amitools.fs.blkdev`.** It is imported
*by* `ImageFile` and `BlkDevFactory`, both of which are in `amitools/fs/blkdev/`, so that is
where it appears to belong — and it is not there. Getting it wrong is not subtle in effect but
is very easy to miss in practice: `amibuilder.image._device_size` had this wrong from the day it
was written (2026-08-18) until 2026-08-20, which meant **every raw-device operation failed at
`Container` construction** with `cannot determine device size: cannot import name
'BlkDevTools'` — before touching any device, so harmlessly, but the whole ZuluSCSI/PiStorm path
was dead. No test caught it because every card fixture is a *file*, and `Address.is_device` is
false for those, so `_device_size` was never called. Found by running `cp` against a bogus
`/dev/rdisk99` while checking the write guard rails. Now pinned by a test that calls
`_device_size` on an ordinary file and asserts the failure is an `OSError` from the ioctl rather
than an `ImportError` — no device required, which is why it is cheap to keep.

**G29 — `ADFSVolume.close()` is what flushes the allocation bitmap.** Not a separate `flush`,
and not each write. So close order matters: the volume must close before the block device
underneath it, or the bitmap never reaches the disk and the volume reports free blocks that are
in use. `open_adfs_volume` appends `vol.close` last to a list that `Volume.close` walks in
reverse, which puts it first.

### 14.2 Writing into a directory stamps the directory, which fights fidelity

**VERIFIED on real AmigaOS.** AmigaDOS updates a directory's datestamp when an entry is added to
it, and reproducing that is correct behaviour for an interactive copy. It is exactly wrong when
the point of the copy is to reproduce a host tree: a directory created early in a `cp -r` has
been restamped by its own children by the time the copy finishes, so every directory ends up
carrying the copy time rather than the source's.

`cp --preserve-times` therefore re-applies directory timestamps in a final pass. Confirmed by
AmigaDOS itself printing `14-Jul-99 15:09:26` for both `tree` and `tree/nested`.

Worth noting that `change_meta_info` on a child does **not** restamp its parent — it writes only
the node's own block, plus a dircache record on DOS4/DOS5 — so the final pass needs no particular
ordering.

### 14.3 A cheap, exact block-cost calculation, rather than an estimate

`layers/compose.py` deliberately estimates block usage and says so. For `cp` the exact figure is
available and worth having, because it is what decides whether a copy is refused:

```
data_blocks = ceil(size / (block_bytes if FFS else block_bytes - 24))
per_table   = block_bytes / 4 - 56          # 72 pointers in a 512-byte block
ext_blocks  = ceil(max(0, data_blocks - per_table) / per_table)
total       = 1 + data_blocks + ext_blocks  # header, data, extension
```

That mirrors `ADFSFile.blocks_get_create_num` exactly, and a test pins the two together across
sizes that straddle every boundary — including the OFS branch, which an FFS-only fixture cannot
reach. OFS spends 24 bytes of every data block on a header, so the two filesystems need
different data-block counts for the same file.

### 14.4 The byte-versus-character name limit is unobservable under Latin-1

The on-disk filename field is a byte count, so measuring the encoded length is the *correct* way
to express the 30-character limit. It is not, however, an observable difference: Latin-1 is one
byte per character, and anything outside it is replaced with a single byte. A test asserting that
bytes are measured rather than characters therefore cannot fail.

Recorded because the reasoning looks sound right up until you try to write the test. Keep the
byte framing — it is right, and it would matter under any other encoding — but do not claim a
guard for it.
