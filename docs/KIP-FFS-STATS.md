# KIP-FFS — Measured numbers

Every figure here was measured, not estimated. Anything not yet measured is listed as such in §7
rather than filled in with a guess, because the point of this file is to be quotable — in the
README, in a decision, or back at me when something regresses.

**Keeping it current:** add a dated row rather than overwriting one. A number that moved is more
interesting than a number that is merely current, and a regression is only visible against history.
Each section says how its figures were produced so they can be re-run.

**Reading it honestly:** most of this rests on a **single 797 KiB sample** of real AmigaOS. That is
enough to prove mechanisms work and nowhere near enough to forecast a 4 GB install. §6 marks which
claims are safe to repeat and which are a sample of one.

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

**Measured 2026-08-18.** Real AmigaOS 3.2 install-floppy contents (`Install3.2.adf`, 73 files,
15 directories, 797 KiB) written to a 40 MiB RDB HDF, then captured with `snap create`.

| Metric | Value |
|---|---|
| Content captured | 797 KiB (73 files, 15 dirs) |
| Blobs written | 71 |
| Blob storage used | **352 KiB** |
| Compression ratio | **44.2%** of content |
| Files deduplicated *within a single capture* | 2 |
| Whole store on disk (blobs + manifests + refs) | 580 KiB |

Two things worth drawing out. First, **44% is a floor, not a forecast**: this sample is unusually
hostile to compression, being mostly `.info` icons and 68k executables that are already dense.
Text-heavy content (`S/`, `Devs/DOSDrivers`, `Prefs`) compresses far harder, and a real install has
proportionally more of it.

Second, **2 files deduplicated before a second layer existed at all** — content addressing paying
off inside one capture, from `CLI` appearing twice and several identically-sized `.info` files. The
interesting dedup number is across layers and is not yet measured (§7).

Codec is stdlib `lzma`, recorded per blob in its filename suffix, with a raw fallback when
compression does not help. See `KIP-FFS-PLAN.md` §0 for why not `zstandard`.

---

## 3. Space overhead

**FFS block overhead**, from `amibuilder du` on the composed image:

| | Value |
|---|---|
| Apparent size | 797 KiB |
| On-disk within the volume | 855 KiB |
| Overhead | **57.9 KiB across 73 files** (~7.3%, ~812 bytes/file) |

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

**Measured 2026-08-18**, MacBook (Apple Silicon), APFS, warm cache, 797 KiB / 73 files.

| Operation | Time |
|---|---|
| `snap create` (capture, hash, compress, store) | **0.377 s** |
| `compose --format rdb` **including verification** | **0.224 s** |

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

### Booting real AmigaOS

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
  with zero filesystem errors.
- Capture → compose → re-capture is idempotent down to the layer ID.
- The DosEnvec is reproduced field for field, including `de_Mask` and `de_MaxTransfer`.
- A composed image costs roughly its content on a sparse filesystem, not its declared capacity.

**Sample of one — true as measured, do not generalise:**

- The 44% compression ratio. One 797 KiB sample, skewed toward incompressible content.
- All timings. Sub-second on a tiny image says nothing about 4 GB.
- The 7.3% FFS overhead. Depends entirely on file-size distribution.

**Not yet true at all — do not claim:**

- Anything about real hardware. ZuluSCSI and PiStorm/Emu68 have seen nothing.
- Anything about a real 4 GB install, which is the case the project exists for.
- Anything about a real Amiga booting *multiple* composed partitions at once.

---

## 7. Not yet measured

The gaps, roughly in order of how much they matter.

1. **A real AmigaOS install as a base layer** — size, compression ratio, and whether capture warns
   about links. This is the number that decides how much of the project is worth building, and the
   procedure is written up in `DAVE-FFS-TODO.md` as Job A.
2. **A real diff layer** — install one piece of software on a real drive, capture the diff, and
   compare it against the 4 GB image it came from. Job B. The headline claim of the whole project
   ("a snapshot costs kilobytes, not gigabytes") rests on this and is currently unmeasured.
3. **Cross-layer deduplication** — how much a second snapshot of a mostly-unchanged drive actually
   saves. Only intra-capture dedup has been observed.
4. **Diff noise on a real install** — whether a real AmigaOS boot restamps enough files to bury a
   real change, and whether the default exclusions are adequate.
5. **Timings at 4 GB scale**, including how long a full-image capture takes from an SD card.
6. **Real hardware boot** — ZuluSCSI first, then PiStorm/Emu68 via the MBR `0x76` target, which is
   not yet written.
7. **Multi-partition boot on a real Amiga.**
8. **SD card write volume saved** — the wear argument. Composing writes only the used blocks, so a
   restore should move far less data than a full image copy, but this has never been quantified.

---

## 8. Test suite

**As of 2026-08-18**, branch `phase3`.

| | Count | Time |
|---|---|---|
| Non-emulator | **976 passed**, 20 deselected | 8 min 23 s |
| Emulator (`test/test_emulator.py`) | **53 passed**, 1 skipped | 1 min 01 s |

Run in two halves; one combined run has repeatedly hung.

```bash
.venv/bin/python -m pytest -q -m "not emulator"
.venv/bin/python -m pytest -q test/test_emulator.py
```

| Date | Non-emulator tests | Note |
|---|---|---|
| 2026-08-19 | 976 | AmigaDOS output parsers; boot test codified (emulator suite 41 → 53) |
| 2026-08-18 | 953 | Phase 3 complete: RDB target + `compose --verify` |
| 2026-08-18 | 900 | plain HDF target |
| 2026-08-18 | 870 | dir target |
| 2026-08-18 | 812 | planner + recipe + `--dry-run` |
| 2026-08-17 | 719 | Phase 2 complete (layer capture) |
| 2026-08-17 | 403 | Phase 1 complete (read-only inspection) |
