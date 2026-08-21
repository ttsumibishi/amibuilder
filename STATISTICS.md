# amibuilder — measured results

Every figure here was measured, not estimated. This is the user-facing summary; the full
working record — method for each number, dated history, and the predictions this project got
wrong — lives in [`docs/KIP-FFS-STATS.md`](docs/KIP-FFS-STATS.md).

One honesty note carried over from that record, because it governs how to read everything
below: as of 2026-08-21 the storage and speed figures rest on a small number of real samples
that **disagree** with each other (compression came out at 44.2%, 53.25% and 40.14% on three
different inputs, with no relationship to size). So quote the sample whose shape matches your
question, and do not average them or extrapolate to a full 4 GB drive. Section [What is safe to
repeat](#what-is-safe-to-repeat) marks which claims generalise and which are a sample of one.

## The problem, in numbers

The starting complaint: a 4 GB HDF with a couple of hundred megabytes in use, backed up
repeatedly, each copy costing the full 4 GB, and every restore writing 4 GB to an SD card that
wears out.

| | Value |
|---|---|
| Typical image size | 4 GB |
| Typically in use | a few hundred MB |
| Cost of one conventional backup | 4 GB, regardless of use |
| Cost of ten | 40 GB |

The layer model changes the cost of a snapshot from *capacity* to *content*, and a second
snapshot of a mostly-unchanged drive costs only the difference.

## Storage

### A real AmigaOS 3.2 install (measured 2026-08-20)

A stock AmigaOS 3.2 installed in FS-UAE onto a 4 GiB drive built by `amibuilder init`, then
captured with `snap create`. Barebones, and never booted.

| Metric | Value |
|---|---|
| Content captured | 6,032,638 bytes (5.75 MiB) — 812 files, 69 dirs |
| Blob storage used | 3,212,327 bytes (3.06 MiB) |
| Compression ratio | 53.25% of content |
| Whole store on disk (blobs + manifests + refs) | 5.3 MB |
| Capture time | 5.38 s |
| Source image on disk | 7.6 MB for a 4 GiB sparse image |

### A real diff layer — the headline claim (measured 2026-08-20)

SysInfo 4.4 installed to `Work:` on a clone of the base image, captured as a diff against
`base-3.2`. This is the number the whole project rests on.

| Metric | Value |
|---|---|
| Changes | 14 — 12 files, 2 directories, all new |
| Content added | 119,103 bytes (116 KiB) |
| **Layer stored** | **47,812 bytes (46.7 KiB)** |

Three ways of counting what that snapshot costs:

| Compared against | Ratio |
|---|---|
| A conventional 4 GiB image copy | **89,830× smaller** — 0.00111% of it |
| The image's sparse footprint on APFS (7.75 MiB) | 170× smaller |
| The content on the drive (5.9 MiB) | 129× smaller |

So "a snapshot costs kilobytes, not gigabytes" is literally true as stated: 46.7 KiB against
4 GiB. The whole store — a complete AmigaOS 3.2 install *plus* this delta — is 5.4 MB. The diff
contained only the software: all 14 entries under `Work:Utilities/SysInfo`, no timestamp churn
anywhere, no deletions.

**Round trip verified by content address.** Composing `base-3.2 + sysinfo-4.4` into a fresh
4 GiB RDB, then capturing *that* image, produced a layer ID byte-identical to capturing the real
drive. The ID is a SHA-256 over the drive record plus every path, size, protection bit, comment
and content hash, so the two drives agree on all of it.

## The finding that shaped the design

Deleting files in FFS reclaims almost nothing at the image level. Measured on a 1000 MiB image
holding 236 MiB of data, after deleting four fifths of the files:

| | Before | After |
|---|---|---|
| FFS reports used | 236 MiB | 46 MiB |
| Compressed image size | 235,302,718 B | 235,279,723 B |
| On-disk (APFS sparse) | 230 MB | 230 MB |

**0.01% reclaimed.** FFS clears bitmap bits and unlinks the header; it never touches the data
blocks. Every byte ever written is still there, in blocks marked free, defeating both compression
and deduplication. This is why composed images are built into freshly formatted volumes — they
avoid the problem entirely — and why a `zerofree` pass (Phase 5) is worth having for existing
images.

## Space overhead

FFS block overhead, from `amibuilder du`:

| | Stock 3.2 install | Floppy contents |
|---|---|---|
| Apparent size | 5.8 MiB | 797 KiB |
| On-disk within the volume | 6.3 MiB | 855 KiB |
| Overhead | 605 KiB across 812 files (~763 B/file) | 57.9 KiB across 73 files (~812 B/file) |

Both land near 800 bytes per file, which is what a 512-byte block predicts: a header block plus,
on average, half a block wasted in the tail. It scales with file *count*, not size — a drive of
many tiny `.info` icons pays more than one of a few big archives. That is FFS's own cost, not
anything this tool adds.

A composed image is sparse: a 40 MiB image occupied 928 KiB on APFS. It costs roughly its content,
not its declared capacity, as long as it stays on a filesystem that supports sparse files.

## Speed

MacBook (Apple Silicon), APFS, warm cache. Both operations are sub-second to a few seconds at
these sizes, so they say nothing about a 4 GB drive; they exist as a baseline to notice a
regression against.

| Operation | Stock 3.2 install (5.75 MiB) | Floppy contents (797 KiB) |
|---|---|---|
| `snap create` (capture, hash, compress, store) | 5.38 s | 0.377 s |
| `compose --format rdb` including verification | not yet measured | 0.224 s |

## Fidelity — booting real AmigaOS

The checks that are not circular. Everything host-side validates amitools against amitools; only a
real Kickstart can catch a file written into the wrong hash bucket, which reads back perfectly
under our own reader while being invisible to AmigaDOS.

- **A composed RDB image boots real AmigaOS 3.2.3**, and AmigaDOS reports it identical to its
  source with **zero filesystem errors** — verified for a single-partition drive and for a
  three-partition drive, including the boot election choosing the flagged partition and two
  DosTypes coexisting on one drive.
- **A drive `init` created mounts on real AmigaOS with no HDToolBox step**, every partition within
  0.03% of its requested size, zero errors, and an installer floppy still winning the boot
  election against the fresh drive's empty bootable partition.
- **A never-configured install, booted and quit, comes back byte-identical** — `cmp` clean, host
  mtime untouched, `snap diff` finds nothing. So a base layer survives being booted for a look.
- **`cp`-written files read correctly under real AmigaOS**: `--preserve-times` to the second,
  directory timestamps, `--protect` bits, `--comment`, and `mkdir -p` chains all landed, with
  byte-exact round trips through AmigaDOS `Copy` including a file needing extension blocks and a
  zero-length file. The preserved date was deliberately in July, exactly where amitools' broken
  epoch would land an hour out — AmigaDOS printed the correct time, proving the bad conversion
  never reached the disk.

## Whiteouts and three-layer stacking (measured 2026-08-21)

Every diff before this one was purely additive, so the delete/modify path had never run on real
content. AmigaOS **3.2.3** installed over `base-3.2`, captured as `patch-3.2.3`:

| Change kind | Count |
|---|---|
| content (file replaced) | 126 |
| new | 36 |
| deleted (whiteout) | 1 |
| case-only rename | 1 |
| protection | 1 |
| unchanged (deduplicated) | 754 |

**686 files deduplicated against the parent** — the unchanged entries cost nothing but a manifest
line. The one deletion is recorded as an absence, not data:

```json
{"p":"Workbench:Tools/TextEditFileTypes/Default4Types","t":"w"}
```

Composing `base-3.2,patch-3.2.3` produces that directory with `Default4Types` absent while the
base still has it — deletion by omission, on real content. **Three layers stack:**
`base-3.2 + patch-3.2.3 + sysinfo-4.4` composes to 852 files across three volumes, 930 entries
verified, all volumes clean.

One number worth knowing about that layer: it stores 17.34 MiB, and **91.9% of that is a staged
installer** (`AmigaOS-3.2.3.lha` plus its unpacker), not the patch. The patch itself is 1.40 MiB.
The capture is honest — that *is* what the drive held — and the fix is not in the tool: shape the
capture with `--exclude 'Work:Installers/**'` (measured: same OS files, whiteout intact, 1.40 MiB
stored instead of 17.34), or delete the files on the disk first. The caller decides what a layer
holds, by shaping the drive or the capture before the diff.

## Mutation testing — vacuous guards found

A test that cannot fail reads as coverage while proving nothing. Mutating the code a guard protects
and requiring the guard to go red has repeatedly found tests that only looked like guards. Each is
a distinct failure mode worth recognising:

- An assertion measuring free space that returned to normal regardless of the bug, because the
  write path deletes-then-creates either way — the bug lived only in the preflight arithmetic.
- Two `update_ts=True` mutations that were *unobservable* rather than uncaught, because a correct
  re-stamp ran immediately after and overwrote amitools' wrong value.
- A block-accounting test whose fixture never exercised the OFS branch it claimed to cover.
- A read-only refusal matched so loosely (`"read-only"`) that a *later*, unrelated failure with the
  same phrase satisfied it — so it never proved *where* the refusal happened.
- A shell same-path guard whose assertion (`"same"`) matched the pytest temp directory — named after
  the test — rather than the guard's message, because the error echoed the source path. It passed
  with the guard removed entirely.

The `rm` guards (`mutate-rm-guards.py`, 9 mutations) and the shell + completion guards
(`mutate-shell-guards.py`, 19 mutations) are all killed by named tests.

## What is safe to repeat

Because an overstated claim is worse than a modest one.

**Solid — mechanism proven:**

- A composed RDB image boots real AmigaOS 3.2.3, reported identical to its source with zero
  errors, on single- and three-partition drives.
- A drive created by `init` mounts on real AmigaOS with no HDToolBox step.
- One volume can be restored to stock on a drive already in use, with the other partitions' files
  surviving byte-for-byte.
- Capture → compose → re-capture is idempotent down to the layer ID.
- A composed image costs roughly its content on a sparse filesystem, not its declared capacity.
- A software install snapshots to kilobytes (46.7 KiB against 4 GiB), the diff contains only the
  software, and the stack composes back to an identical content-addressed layer ID.
- Whiteouts and three-layer stacking work on real content (the 3.2.3 patch over the 3.2 base).

**Sample of one — true as measured, do not generalise:**

- The compression ratios, and note they disagree: 44.2% / 53.25% / 40.14%. Quote the one whose
  shape matches your question.
- All timings. 5.4 s for 5.75 MiB says little about a drive with 500 MiB in use.
- The FFS overhead figures — entirely dependent on file-size distribution.

**Not yet true at all — do not claim:**

- Anything about real hardware. ZuluSCSI and PiStorm/Emu68 have seen nothing.
- Anything about a *full* 4 GB drive with games and applications on it.
- Cross-layer deduplication and the SD-card write-volume savings — the wear argument is sound but
  unquantified.

Two of the working record's own predictions have been **falsified by later measurement and left
visible** rather than edited out (a real install compressing worse than the floppy sample, and a
first boot changing nothing where "timestamp churn" was predicted). The pattern is worth
remembering: a plausible mechanism is not a measurement.
