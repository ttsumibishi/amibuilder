# KIP-FFS — Measured numbers

Every figure here was measured, not estimated. Anything not yet measured is listed as such in §7
rather than filled in with a guess, because the point of this file is to be quotable — in the
README, in a decision, or back at me when something regresses.

**Keeping it current:** add a dated row rather than overwriting one. A number that moved is more
interesting than a number that is merely current, and a regression is only visible against history.
Each section says how its figures were produced so they can be re-run.

**Reading it honestly:** as of 2026-08-20 there are **two** samples — a 797 KiB set of floppy
contents, and a real 5.75 MiB stock AmigaOS 3.2 install. They **disagree** about compression (44.2%
vs 53.25%), and the larger one did worse, which is worth remembering before quoting either as
typical. Most of the fidelity work below was measured against the smaller one. §6 marks which claims
are safe to repeat and which are a sample of one.

**This file has been wrong twice, both times in the same way.** §2 predicted a real install would
compress *better* than the floppy sample; it compressed worse. §7 predicted a first boot would produce
"a handful of genuine changes and a great deal of timestamp churn"; it produced neither — the image
came back byte-identical. Both predictions were confident, both rested on assumptions nobody had
checked, and both were cheap to check. The retractions are left in place rather than edited out,
because a document that quietly deletes its bad predictions cannot be trusted about its good ones.

The pattern is worth naming, since the next prediction will be tempting too: **a plausible mechanism
is not a measurement.**

---

## 1. The problem, in numbers

The starting complaint: a 4 GB HDF with a couple of hundred megabytes in use, backed up repeatedly,
each copy costing the full 4 GB, and every restore writing 4 GB to an SD card that wears out.

| | Value |
|---|---|
| Typical image size | 4 GB |
| Typically in use | a few hundred MB |
| Cost of one conventional backup | 4 GB, regardless of use |
| Cost of ten | 40 GB |

What the layer model changes is that a snapshot costs *content*, not *capacity* — and a second
snapshot of a mostly-unchanged drive costs only the difference.

---

## 2. Storage: deduplication and compression

### A real AmigaOS 3.2 install

**Measured 2026-08-20.** The number this project was waiting for. A stock AmigaOS 3.2 installed in
FS-UAE onto a 4 GiB drive built by `amibuilder init`, then captured with `snap create`. Barebones
deliberately: no GlowIcons, no CPU libraries, no extras. **Never booted** — the installer was quit
rather than allowed to restart, so nothing a first boot might rewrite has been touched.

| Metric | Value |
|---|---|
| Content captured | **6,032,638 bytes** (5.75 MiB) — 812 files, 69 dirs |
| Blobs written | 754 |
| Blob storage used | **3,212,327 bytes** (3.06 MiB) |
| Compression ratio | **53.25%** of content |
| Saved by compression | 2,820,311 bytes (2.69 MiB) |
| Files deduplicated *within the single capture* | 58 (7.1% of files) |
| Whole store on disk (blobs + manifests + refs) | 5.3 MB |
| Links encountered | **0** |
| Entries skipped | 1 — `Workbench:T`, the temp directory, a default exclude |
| Capture time | **5.38 s** |
| Source image on disk | 7.6 MB for a 4 GiB sparse image |

**⚠️ This falsifies a prediction made in this document.** The earlier version of this section
claimed *"44% is a floor, not a forecast — this sample is unusually hostile to compression, being
mostly `.info` icons and 68k executables… a real install has proportionally more [text]."* A real
install compresses to **53.25%, which is worse, not better**. The reasoning was wrong in both
directions: the 797 KiB floppy sample is largely installer scripts and text, and a full install is
largely dense binary — `Libs/`, `Classes/`, 60 commands in `C/`, and 606 KiB of outline font data in
`Fonts/_bullet*`. `Locale/Help` is only 932 KiB of 5.75 MiB, nowhere near enough to dominate. The
lesson is not about compression, it is that a ratio was predicted from an assumption about file
composition that nobody had looked at. Per-directory ratios would settle it and have not been
measured.

**Dedup is doing real work before a second layer exists.** 58 of 812 files are duplicates *within
one capture* — 7.1%, against 2.7% in the floppy sample. Cross-layer dedup, which is the number that
matters for the actual workflow, is still unmeasured (§7).

### The floppy-contents sample

**Measured 2026-08-18.** Kept for comparison, and because most of the fidelity work below was
measured against it. `Install3.2.adf` contents (73 files, 15 directories, 797 KiB) written to a
40 MiB RDB HDF, then captured.

| Metric | Value |
|---|---|
| Content captured | 797 KiB (73 files, 15 dirs) |
| Blobs written | 71 |
| Blob storage used | **352 KiB** |
| Compression ratio | **44.2%** of content |
| Files deduplicated *within a single capture* | 2 |
| Whole store on disk (blobs + manifests + refs) | 580 KiB |

Codec is stdlib `lzma`, recorded per blob in its filename suffix, with a raw fallback when
compression does not help. See `KIP-FFS-PLAN.md` §0 for why not `zstandard`.

---

## 3. Space overhead

**FFS block overhead**, from `amibuilder du`. The install is the better sample of the two:

| | Stock 3.2 install | Floppy contents |
|---|---|---|
| Apparent size | 5.8 MiB | 797 KiB |
| On-disk within the volume | 6.3 MiB | 855 KiB |
| Overhead | **605 KiB across 812 files** (~763 bytes/file) | 57.9 KiB across 73 files (~812 bytes/file) |

Both land near 800 bytes per file, which is what a 512-byte block size predicts: a file header block
plus, on average, half a block wasted in the tail. It scales with file *count*, so the cost is driven
by how many tiny `.info` icons and small commands a volume holds rather than by total size.

That is FFS's own cost — file headers and partial trailing blocks — not anything this tool adds.
It scales with file *count*, so a drive of many tiny icons pays more than one of a few big archives.

**Host sparseness**, same image:

| | Value |
|---|---|
| Image apparent size | 40 MiB |
| Actually occupying disk (APFS) | **928 KiB** |

A composed image costs roughly its content, not its declared capacity, as long as it stays on a
filesystem that supports sparse files. This is why `compact` is worth building (Phase 5) — an image
that has been *used* loses this property as deleted blocks stay allocated.

---

## 4. Speed

MacBook (Apple Silicon), APFS, warm cache.

| Operation | Stock 3.2 install (5.75 MiB, 812 files) | Floppy contents (797 KiB, 73 files) |
|---|---|---|
| `snap create` (capture, hash, compress, store) | **5.38 s** | 0.377 s |
| `compose --format rdb` **including verification** | not yet measured | 0.224 s |

7.4× the content took 14× the time, so capture is not scaling linearly on this evidence — expected,
since `lzma` cost rises with the volume of *compressible* data and the install holds more of it in
absolute terms. Two points is not a curve, and neither says much about a drive with 500 MiB in use.

Both sub-second, so nothing here says anything about how the tool behaves on a 4 GB image — the
figures exist as a baseline to notice a regression against, not as a performance claim. Compose
being *faster* than capture is expected: capture compresses, compose only decompresses.

---

## 5. Fidelity

### Round trip, in software

A two-partition RDB captured and composed straight back is identical in: geometry, every partition,
the DosEnvec **field for field**, the bootable flag, every path, every file byte for byte,
protection bits, comments, and timestamps to the tick. Re-capturing the composed image yields the
same manifest bytes **and the same layer ID**, and `snap diff` finds nothing to record.

Covered by `test/test_layers_roundtrip.py` (40 tests).

### Booting real AmigaOS, three partitions

**Measured 2026-08-19.** One drive, three volumes, composed from a layer store and booted. Full
method in `KIP-FFS-LAYERS.md` §14.

| Volume | DosType | used / free, source | used / free, composed | Errs | Entries |
|---|---|---|---|---|---|
| Workbench (bootable) | DOS\3 | 1758 / 59680 | 1758 / 59680 | **0** | 89, injected script only |
| Work | DOS\3 | 33 / 30685 | 33 / 30685 | **0** | 10, identical |
| Saves | **DOS\1** | 23 / 30663 | 23 / 30663 | **0** | 5, identical |

`SYS:` resolved to `Workbench:` on both drives, so the boot election chose the flagged partition
rather than a data volume. Two DosTypes coexisted on one drive and both mounted.

### A drive `init` created, mounted by real AmigaOS

**Measured 2026-08-20.** The claim `init` makes is that a drive it creates is usable on real
hardware with **no HDToolBox step**. Nothing host-side can establish that — our reader could
happily agree with our writer about a layout AmigaOS rejects — so a 200 MiB drive was created
from scratch and attached to a real AmigaOS 3.2 booted from the install floppy. This is also
exactly the install workflow: fresh drive, boot the installer, install onto it.

```
amibuilder init drive.hdf --size 200M \
  --partition Boot=60M,bootable --partition Games=80M --partition Keep=rest
```

| Volume | Asked for | Unit AmigaDOS gave it | Free blocks | Free vs asked | Errs | Entries |
|---|---|---|---|---|---|---|
| Boot (bootable) | 60 MiB | `DH0` | 122845 | 99.97% | **0** | 0 |
| Games | 80 MiB | **`DH1_0`** | 163795 | 99.97% | **0** | 0 |
| Keep | rest → 60 MiB | `DH2` | 122813 | 99.97% | **0** | 0 |

All three mounted Read/Write, empty, no errors, and each within 0.03% of its requested size
(the shortfall is FFS's own root block and bitmap). No HDToolBox was involved at any point.

Two findings worth keeping:

**The installer floppy wins the boot election.** `init` marks the first partition bootable, so a
fresh drive presents a *bootable but empty* volume — if that won, the machine would not boot at
all and an installer could never run against a new drive. `SYS:` resolved to the floppy. This is
load-bearing for the whole workflow, so it is now a test of its own.

**AmigaDOS renamed a colliding device.** `init` hands out `DH0..DHn` unconditionally; the
harness's own RESULTS volume claimed `DH1` first, so AmigaOS silently renamed our second
partition's device to `DH1_0` while leaving its volume name alone. Not a defect, and harmless
here, but it means `init` has no way to avoid a collision on a machine that already has a drive
using the same prefix — a configurable device prefix is a known gap (§7).

### Booting an install that `init` made, and what a boot actually changes

**Measured 2026-08-20.** The stock 3.2 image (§2) booted under FS-UAE from a clone, taken to
Workbench, left idle for over a minute, then quit.

| | Result |
|---|---|
| Boot outcome | reached Workbench, **no requesters, no filesystem complaints** |
| Image after the boot | **byte-identical** to before — `cmp` clean, host mtime untouched |
| `snap diff` against the base | **no differences**, 881/881 entries unchanged, 0 new blobs |
| Same with `--timestamps-significant` | also no differences |

**This is the first time anything has booted *from* a partition `init` created.** The emulator test
in §5 proved only that a real Amiga would *mount* them — it booted from a floppy. A clean boot to
Workbench is independent confirmation that the RDB, the DosEnvec, the boot block and the filesystem
are all genuinely correct rather than merely self-consistent with our own reader. `init` → install →
boot, with no HDToolBox at any point.

**⚠️ A second prediction falsified.** The `boot-hdf.sh` script was written asserting that "AmigaOS
rewrites Env-Archive, datestamps directories and touches plenty else on the way up, so a boot is a
destructive edit even if you touch nothing." Measurably false: a bare boot wrote **zero bytes**. The
copy-before-boot default was kept, but on honest grounds — it is free, and anything you actually *do*
(saving a preference, moving an icon and rewriting its `.info`, anything in `WBStartup`) does write.

Two caveats on how far this generalises:

- The session ended with a hard quit (`Cmd+Q`), which cannot flush a dirty FFS buffer. The host mtime
  proves nothing was ever written *to the file*, so this is not "written then lost" at the host level
  — but a minute of idle time is the evidence that AmigaOS had nothing pending, not a proof.
- This is a **never-configured** install. Once a drive is in real use the answer will differ, and the
  interesting version of this measurement is a boot of a drive that has been used.

The useful consequence: **a base layer is stable across boots**, so booting one to check something
does not silently invalidate it. It also means diff noise from booting — listed below as an unmeasured
risk — is zero in this configuration, which is a more comfortable answer than expected.

### Booting real AmigaOS, single partition

**Measured 2026-08-18.** FS-UAE 3.2.35, A1200, Kickstart 47.96 / Workbench 47.2. Full method in
`KIP-FFS-LAYERS.md` §13.

| As reported by AmigaDOS itself | Source drive | Composed drive |
|---|---|---|
| `Info` DH0 size / used / free / full | 39M / 1762 / 80124 / 2% | 39M / 1762 / 80124 / 2% |
| `Info` DH0 **errors** | **0** | **0** |
| `List SYS: ALL` totals | 73 files, 797K, 15 dirs, 1742 blocks | 73 files, 797K, 15 dirs, 1742 blocks |
| Entries listed | 85 | 85 |
| Missing / extra | — | **0 / 0** |
| Differing | — | 2, both the harness's own injected `S/Startup-Sequence` |

FS-UAE's own log independently parsed the RDB we wrote:
`RDSK at 0, C=2560 S=32 H=1` · `LC: 1 HC: 2559` · `Partition 'DH0' Dostype=444F5303 (DOS\3)
Flags: 00000001`. Every value matches what was written, read back by a parser with no connection to
this codebase.

**Cross-check worth noting:** AmigaDOS counted 73 files / 797K / 15 directories, which is exactly
what `snap create` reported. Two unrelated implementations agreeing on the inventory.

---

## 6. What is safe to repeat, and what is a sample of one

Because these will end up in a README, and an overstated claim is worse than a modest one.

**Solid — mechanism proven, would be surprising to see fail:**

- A composed RDB image boots real AmigaOS 3.2.3 and AmigaDOS reports it identical to its source,
  with zero filesystem errors. True for a three-partition drive as well as a single one, including
  the boot election choosing the flagged partition and two DosTypes coexisting.
- **A drive created by `init` mounts on real AmigaOS with no HDToolBox step**, every partition at
  the requested size, zero filesystem errors, and an installer floppy still winning the boot
  election against the fresh drive's empty bootable partition.
- **One volume can be restored to stock on a drive already in use**, with files the Amiga itself
  wrote to the other partitions surviving byte-for-byte and block-for-block. This is the
  "I broke my OS" workflow, and it is verified by booting the drive afterwards.
- Capture → compose → re-capture is idempotent down to the layer ID.
- The DosEnvec is reproduced field for field, including `de_Mask` and `de_MaxTransfer`.
- A composed image costs roughly its content on a sparse filesystem, not its declared capacity.

- **A stock AmigaOS 3.2 install captures to 53.25% of its content in 5.4 seconds**, with zero links
  and one deliberate exclusion. Measured on a real install, not a fixture.
- **An AmigaOS install created by `init` boots on a real Amiga** — reaching Workbench with no
  requesters, from a partition table and filesystem this tool wrote, with no HDToolBox step anywhere.
- **A bare boot of a never-configured install changes the image not at all** — byte-identical
  afterwards. So a base layer survives being booted for a look. Do not extend this to a drive in
  actual use.

**Sample of one — true as measured, do not generalise:**

- **Both compression ratios, and note they disagree**: 44.2% on 797 KiB of floppy contents, 53.25%
  on a 5.75 MiB install. Two samples, one order of magnitude apart, and the larger one compressed
  *worse* — so quote the 53% figure for an install and do not extrapolate either to a full 4 GB
  drive with games and applications on it.
- All timings. 5.4 s for 5.75 MiB says little about a drive with 500 MiB in use.
- The FFS overhead figures. ~812 bytes/file on the sample, ~763 bytes/file on the install, both
  entirely dependent on file-size distribution.

**Not yet true at all — do not claim:**

- Anything about real hardware. ZuluSCSI and PiStorm/Emu68 have seen nothing.
- **Anything about a diff layer**, which is the headline claim of the project ("a snapshot costs
  kilobytes, not gigabytes"). A base layer now exists to diff against; nothing has been diffed.
- Anything about a *full* drive. The install is 5.75 MiB; Dave's real drives hold games and
  applications, and the interesting case is a 4 GB drive with a few hundred MB in use.

---

## 7. Not yet measured

The gaps, roughly in order of how much they matter.

1. ~~**A real AmigaOS install as a base layer**~~ **Done 2026-08-20**; see §2. 5.75 MiB of content
   to 3.06 MiB stored, 53.25%, no links, 5.4 s. It also falsified this document's own prediction that
   a real install would compress better than the floppy sample.
2. **A real diff layer** — install one piece of software on top of the base layer, capture the diff,
   and compare it against the full image. Job B, and now unblocked: `base-3.2` exists to diff
   against. The headline claim of the whole project ("a snapshot costs kilobytes, not gigabytes")
   rests on this and is **still unmeasured**. This is now the single most valuable measurement
   outstanding.
2a. ~~**A first boot as a diff layer.**~~ **Done 2026-08-20**, and the answer was "nothing" — see §5.
   Worth noting the prediction recorded here was that a boot "should produce a handful of genuine
   changes and a great deal of timestamp churn." It produced neither. Two for two on this document
   guessing wrong about measurements it had not taken yet.
3. **Cross-layer deduplication** — how much a second snapshot of a mostly-unchanged drive actually
   saves. Only intra-capture dedup has been observed.
4. **Diff noise on a real install** — *partially answered 2026-08-20, see §5.* A bare boot of a
   never-configured install restamps nothing, because it writes nothing, so there is no noise to bury
   anything. That is the easy case though: the question is still open for a drive in real use, where
   icons have been moved and preferences saved, and it is that case the default exclusions exist for.
5. **Timings at 4 GB scale**, including how long a full-image capture takes from an SD card.
6. **Real hardware boot** — ZuluSCSI first, then PiStorm/Emu68 via the MBR `0x76` target, which is
   not yet written.
7. **SD card write volume saved** — the wear argument. Composing writes only the used blocks, so a
   restore should move far less data than a full image copy, but this has never been quantified.
8. ~~**A partition-granular restore on a booting drive.**~~ Done 2026-08-19; see
   `KIP-FFS-LAYERS.md` §15. It found a data-loss bug.
9. **Whether an AmigaOS installer will actually install onto an `init` drive.** Mounting is
   proven (§5); completing an install is not. The installer is interactive, so the harness cannot
   drive it — this needs a person at the keyboard.
10. **Device-name collisions on a real machine.** `init` assigns `DH0..DHn` with no way to change
    them, and AmigaOS was observed silently renaming a colliding unit to `DH1_0` (§5). Untested
    against a second real drive, and a configurable prefix is unbuilt.

---

## 8. Test suite

**As of 2026-08-20**, branch `phase3`.

| | Count | Time |
|---|---|---|
| Non-emulator | **1154 passed**, 50 deselected | 9 min 12 s |
| Emulator (`test/test_emulator.py`) | **83 passed**, 1 skipped | 1 min 26 s |

Run in two halves; one combined run has repeatedly hung.

```bash
.venv/bin/python -m pytest -q -m "not emulator"
.venv/bin/python -m pytest -q test/test_emulator.py
```

| Date | Non-emulator tests | Note |
|---|---|---|
| 2026-08-20 | 1154 | `amibuilder init`; verified on real AmigaOS (emulator 76 → 83). Mutation testing found the partial-image cleanup guard wholly untested |
| 2026-08-19 | 1003 | In-place partition-granular restore; a data-loss bug fixed (emulator 66 → 76) |
| 2026-08-19 | 989 | Multi-partition boot codified (emulator suite 53 → 66) |
| 2026-08-19 | 976 | AmigaDOS output parsers; boot test codified (emulator suite 41 → 53) |
| 2026-08-18 | 953 | Phase 3 complete: RDB target + `compose --verify` |
| 2026-08-18 | 900 | plain HDF target |
| 2026-08-18 | 870 | dir target |
| 2026-08-18 | 812 | planner + recipe + `--dry-run` |
| 2026-08-17 | 719 | Phase 2 complete (layer capture) |
| 2026-08-17 | 403 | Phase 1 complete (read-only inspection) |
