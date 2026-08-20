# KIP-FFS — Plan

**Date:** 2026-08-17 (revised after target hardware and the layered model were settled)
**Status:** Proposed. Nothing built.

Companion docs: `KIP-FFS-NOTES.md` (verified findings), `KIP-FFS-LAYERS.md` (layer model design),
`KIP-FFS-IDEAS.md` (option analysis).

---

## 0. Session state — read this first when resuming

**Last updated: 2026-08-20.** Written as a resume point, so a fresh session can pick up without
re-deriving anything. Where this section disagrees with the phase descriptions below, this section is
newer.

### Where the work stands

| | State |
|---|---|
| Phase 1 — read-only inspection | ✅ **Complete, on `main`** |
| Phase 2 — layer capture | ✅ **Complete, on `main`.** One thing outstanding: the measurement below |
| Phase 3 — composition | ✅ **Complete, on branch `phase3`.** All four targets write; verification is on by default |
| `amibuilder init` | ✅ **Built 2026-08-20, on `phase3`.** Verified on real AmigaOS; see stats §5 |
| Tests | **1154 passing**, 50 deselected (non-emulator) · 83 passing, 1 skipped (emulator) |
| Git | `main` clean at `8b4bc0d`; branch `phase3` is 12 commits ahead. No remote |

Run the suite in two halves — one long run has repeatedly hung:

```bash
.venv/bin/python -m pytest -q -m "not emulator"     # 1154 tests, ~9.2 min
.venv/bin/python -m pytest -q test/test_emulator.py  # 83 tests, ~1.5 min, no window appears
```

**`main` is deliberately parked at `8b4bc0d`** so the Job A/B measurements in `DAVE-FFS-TODO.md`
run against a clean core. Phase 3 lives on its own branch until those numbers exist.

Commits on `main`: `5a7ead3` Phase 1 · `f3bb55d` emulator outcomes · `fb90f21` restore requester ·
`ffb8b54` crash dialog · `1f4d769` plan §0 · `231bd93` hidden FS-UAE window · `96ce4ab`
manifest+blobs · `76b1479` store · `f453f79` drive record · `7df2790` capture+diff ·
`cc30e11` snap CLI · `8b4bc0d` DAVE-FFS-TODO.

On `phase3`: `8bef9c3` planner · `a7b6d07` recipe + `compose --dry-run` · `98f183c` dir target ·
`102eadf` plain HDF target · `dc8fd25` RDB target · `bfaf2ca` `compose --verify` · `a70b185` Phase 3
notes · `6f36e8c` first real boot · `2cec025` stats doc · `925f658` boot test codified ·
`4df6644` multi-partition boot · `35e9d55` **partition-granular restore data-loss fix**.

### The one thing neither phase has done

**Nothing has been run against a real AmigaOS install.** Everything is fixture-scale, so there
is still no measured base-layer size, no diff-layer size, and no compression ratio for real
Amiga content — which is the number that decides how much of this project is worth building.
See `KIP-FFS-LAYERS.md` §11 for what was measured, what was not, and the two risks specific to
capturing a real install (it may contain links, which amitools cannot represent).

Capture is read-only, so the downside of trying is a failed capture rather than a damaged image.

**`init` closes the front half of this.** There is now a way to make the empty drive to install
onto, verified to mount on a real Amiga, so the base layer no longer depends on imaging an
existing misconfigured drive. What remains is interactive and cannot be automated: someone has to
sit through the AmigaOS 3.2 installer once. Stats §7 items 1, 2 and 9.

### Things established this session that must not be re-derived

Each of these cost real time. They are documented in full where noted.

1. **Timestamps must be rendered from the on-disk `(days, mins, ticks)` triple, never through Unix
   time.** amitools' epoch constant is built with `time.mktime`, so it carries the host's *January*
   UTC offset; it wrote 15:48:59 to disk while the wall clock read 16:48:59, then displays 16:48:59 by
   re-applying the same error backwards. amibuilder reports what the bytes say. Notes §5.7,
   `amibuilder/timestamps.py`. **This is why Phase 2 manifests store the raw triple.**
2. **`test/helpers/blocks.py` is the independent oracle and must NOT be imported by the package.**
   The package has its own `blocks.py`; `test_blocks.py` pins the two together. Importing the helper
   would make the structural tests circular.
3. **Nine amitools bugs and traps are pinned** in `test_amitools_regressions.py`. The two that would
   have caused silent data errors: `FileName.__str__`/`__repr__` raise `TypeError` (use
   `get_unicode_name()`), and `get_blocks(with_data=True)` **omits every data block on FFS volumes**
   (use `data_blk_nums`). Also: `BlkDevFactory.open()` on an RDB returns *partition 0*, not the disk.
4. **FS-UAE has no headless *video driver*, but it does have a hidden window.**
   `video_driver = none`/`dummy`/`null` and `SDL_VIDEODRIVER=dummy` all segfault in ~0.85 s — never
   use them as a test trigger; use a stub binary that exits non-zero. **`window_hidden = 1` is the
   working answer**: emulation is unaffected, the AmigaOS boot test passes, and focus is never
   taken. It is now the default; `AMIBUILDER_FSUAE_VISIBLE=1` brings the window back, which window
   capture requires. `window_minimized = 1` is silently ignored, and the SDL bundled with 3.2.35
   predates the `SDL_WINDOW_NO_ACTIVATION_WHEN_SHOWN` hint.
5. **Two different macOS dialogs block or pollute emulator runs**, both self-inflicted, both fixed.
   Only the window-restore requester actually blocks a launch. See `test/emulator/README.md` and
   `.kiro/steering/amibuilder-operations.md`.
6. **Window capture works and is the tool that found (5).** `test/emulator/window_capture.py`, needs
   Screen Recording permission (granted) and `pyobjc-framework-Quartz` (in the `dev` extra).
7. **Volume-name partition selection is amibuilder's own** — amitools' `find_partition_by_string`
   matches device names and indexes only, so `card.hdf:Workbench` mounts each partition on a miss.

### Loose ends, in priority order

1. ~~**`zstandard` is installed but not declared.**~~ **Settled: stdlib `lzma`, and `zstandard` was
   uninstalled from the venv.** Dedup is the primary win and compression is secondary, so a binary
   dependency bought little. The codec is recorded per blob in its filename suffix with a raw
   fallback when compression does not help, so switching later is a registry entry and needs no
   migration. Measured on a test payload: zstd-3 → 279 bytes, lzma → 380 bytes.
2. **~20 IDE diagnostics** (`PROBLEMS` panel) never examined. All 418 tests pass, so these are almost
   certainly lint or type-checker findings rather than defects. User deferred them; worth a pass
   before Phase 2 grows the codebase.
3. **One unexplained intermittent emulator failure.** `test_real_amigaos_boots_and_reports_back`
   once reported `emulator-exited` at 15.1 s with **exit code 0** and a complete clean shutdown log —
   FS-UAE quit itself mid-run. Seen once, not reproduced across four subsequent runs. The new outcome
   reporting identifies it correctly; the cause is unknown.
4. **`get --preserve-times` interpretation is untested against a real Amiga.** It maps the Amiga
   triple to host local time, which is self-consistent, but nothing has confirmed the round trip
   through a real AmigaOS.

### Phase 2 — what got built

```
amibuilder/layers/
  manifest.py   JSONL entries, canonical bytes, the comparison key
  blobs.py      SHA-256 content-addressed store, per-blob codec, raw fallback
  store.py      layers, refs, candidates, recipes, gc, integrity
  drive.py      RDB drive record: geometry, full DosEnvec, boot blocks, policies
  capture.py    volume -> entries + blobs; diff with per-entry reasons
amibuilder/commands/snap.py
  create diff review commit discard ls show verify gc rm   (all --json)
```

`--store PATH` or `$AMIBUILDER_STORE`, defaulting to `~/.amibuilder/store`.

**Exit codes**, consistent across commands: 2 usage · 3 not found · 4 unsupported · 5 plan not
writable · 6 the tool looked and found problems (`check` validation, and a failed
`compose --verify`).

### Phase 3 — what got built

```
amibuilder/layers/
  compose.py    Plan/VolumePlan/PlanEntry/Conflict/Problem, resolve_stack, flatten,
                preflight (names, capacity), build_plan; verify_written + VerifyResult
  targets.py    write_directory (.uaem sidecars), write_plain, write_rdb; fsuae_config_lines
amibuilder/commands/
  recipe.py     new ls show rm
  compose.py    --dry-run, --format dir|plain|rdb, --verify (default on)
```

All four composition targets from `KIP-FFS-LAYERS.md` §7 now write, except the PiStorm MBR `0x76`
device target, which is still deferred — it is the one that can destroy an Emu68 card, so it waits
until there is a real card to test against.

Three semantics worth not re-deriving:

- **Deletion is by omission during flatten, not a delete operation.** A whiteout on a directory
  removes its subtree, and `Old` does not take `Older`.
- **`destroys_existing_data = format_volume AND existed`.** Composing to a new target destroys
  nothing, and `preserve` on a missing volume is creation, not destruction. The plan reports three
  distinct outcomes: destroys / untouched / created_empty.
- **Preflight reports every offender, not the first.** Name limits come from the partition's
  DosType (30, or 110 for DOS6/7). Capacity is an explicit estimate: blocking on clear overflow,
  advisory above 90%.

**The RDB target reproduces the DosEnvec field by field**, which is the entire reason a base layer
captures it. Eleven fields are forced verbatim through amitools' `more_dos_env` escape hatch. The
four geometry-derived fields (`surfaces`, `blk_per_trk`, `block_size`, `sec_per_blk`) are
deliberately **verified rather than forced**: amitools computes them from the drive being added to,
and overriding them could produce a partition whose geometry disagrees with its drive's — which is
the arithmetic that decides where the partition starts. A mismatch warns instead of silently
relocating data.

**`compose --verify` is on by default** for the image formats, following the principle §5 already
states for `zerofree`: verification belongs in the command, not in the test suite. A passing test
proves the code works on a fixture and says nothing about the card written just now. It re-reads
the image and compares content hash, protection, comment and kind — the same key a diff uses —
through a `HashOnlyBlobStore` so a read-only check never grows the store. Skipped for `--format
dir`, which is plain host files that ordinary tools can inspect.

Still deferred: `snap create-from-adf`, `snap export/import`, and the MBR `0x76` device target.

### Phase 3 — the gap it exposed

**A single-volume (plain) drive record carries no volume name** (`partitions: []`,
`single_volume: True`), so the volume name reaches a plan only through the manifest entries.
Capture an *empty* formatted volume and there is nothing to name it with, so it cannot be composed
back. Harmless for any drive with files on it — which is every real backup — and pinned by
`test_an_empty_single_volume_capture_has_no_volume_to_compose` so it is deliberate rather than a
surprise. Fixing it means adding the volume name to the drive record, which **changes layer IDs**.

`dev_flags` is also not captured by the drive record, so the RDB target always writes 0.

### Phase 2 — the original plan, kept for reference

Design is settled in `KIP-FFS-LAYERS.md`; these are the decisions already taken, so they need no
re-litigation:

- **Store layout** (§3): `blobs/` content-addressed and compressed, `layers/<id>/{layer.json,
  manifest.jsonl}`, `refs/`, `recipes/`
- **`manifest.jsonl`** — one JSON object per line, sorted by path. Streams, diffs with ordinary tools,
  appends cheaply during capture
- **Volume-qualified paths** (`Workbench:S/Startup-Sequence`); physical placement lives only in the
  base layer's drive record, which makes multi-partition and multi-drive identical to a layer
- **Comparison key: content hash + protection + comment. NOT timestamps** — see item 1 above; a
  timestamp-sensitive key would report differences arising only from *which machine wrote the image*
- **Directories recorded explicitly** — empty ones are load-bearing on AmigaOS (`T/`, `WBStartup`)
- **Deletions recorded as whiteouts and applied by default** (user agreed)
- **Base layers capture the RDB drive record**, including `mask` / `max_transfer` / `boot_pri`, which
  converts the G5 footgun and the G17 bootability unknown into copied facts
- **Three per-volume policies**: `replace` / `merge` / `preserve`
- **Two-step capture with a review gate**: `snap diff` writes a candidate, `snap review --explain`
  inspects it, `snap commit` finalises

Intended module layout:

```
amibuilder/layers/
  manifest.py   Entry type, JSONL read/write, the comparison key
  blobs.py      content-addressed compressed blob store
  store.py      layers, refs, recipes, candidate layers
  capture.py    volume -> entries + blobs; diff against a parent
  drive.py      RDB drive record capture and replay
amibuilder/commands/
  snap.py       create, diff, create-from-adf, review, commit, ls, show, verify, gc, rm
  recipe.py     new, ls, show
```

**Exit criterion, and the number that validates the entire project:** capture a real AmigaOS 3.2.3
install as a base layer, install one piece of software, capture a diff layer, and compare that diff's
size against the 4 GB image it came from. Also confirm the diff contains roughly what was expected —
this is where the diff-noise risk in layers §5 either bites or does not.

---

## 1. Summary of the recommendation

**Build it in Python on amitools. Make the layered install model the centre of the tool. Sequence the
work so every phase is safer than the one after it.**

Four findings drive this, all measured or read from primary sources rather than assumed:

1. **Deleting files in FFS reclaims essentially nothing.** Removing 4/5 of the files from a 236 MB
   image cut its compressed size by **0.01%** and its on-disk size by **0%**. Free blocks keep their
   old contents forever (`KIP-FFS-NOTES.md` §1).
2. **Python is fast enough.** 5,950 files / 236 MB round-trips in 4.4 seconds total, byte-identical.
   The case for a compiled language was performance and the numbers do not support it (notes §4).
3. **An RDB whole-disk image is the universal format for this hardware**, not a plain HDF. ZuluSCSI
   needs an RDB for the Amiga to mount anything; PiStorm/Emu68 cannot mount an HDF at all and wants
   the bytes in an MBR `0x76` partition, which is `dd`-equivalent to an RDB image; both emulators take
   either (notes §9).
4. **The layered model dissolves the original problem rather than optimising it.** Layers decouple
   what is installed from what format the machine needs, which makes images disposable build
   artefacts and removes in-place mutation from the critical path (`KIP-FFS-LAYERS.md` §1).

The most important structural consequence is the risk gradient. Capture is **read-only**. Composition
writes only to a **fresh** volume, so a bug means discarding an artefact rather than losing data.
Additive writes come next, and are verified working. **Destructive** writes — deletion and rename
inside an existing volume — move to last and may never be needed.

Two further decisions settled in this revision:

**Multiple partitions on one drive, not one partition per drive.** This is the easier implementation,
not the harder one: amitools already derives partition offsets from the RDB and hands back an
independent block device per partition, whereas multiple drives means orchestrating multiple targets.
Manifest paths are volume-qualified so the two layouts are indistinguishable at the layer level, and
**partition-granular compose** (`--into card.hdf:0 --reformat`) gives the small blast radius that
motivated splitting across drives in the first place (`KIP-FFS-LAYERS.md` §3.1).

**ADF images are a first-class source**, both for browsing and for staging multi-disk downloads onto a
hard drive. Verified: three ADFs merged into one target path inside an RDB partition, validator clean,
byte-identical (notes §10). Routing ADFs through the layer store means multi-disk archives become
versioned, composable layers rather than one-off copies.

---

## 2. Architecture

```
                        ┌──────────────────────────────────────┐
     CLI / shell   ───▶ │ command layer (argparse, cmd/readline)│
                        └───────────────┬──────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │                               │                               │
┌───────▼─────────┐        ┌────────────▼────────────┐      ┌───────────▼──────────┐
│  CAPTURE        │        │  LAYER STORE            │      │  COMPOSE             │
│  read-only      │───────▶│  blobs + manifests      │─────▶│  writes fresh only   │
│                 │        │  refs + recipes         │      │                      │
│  snap create    │        │  content-addressed      │      │  rdb hdf │ plain hdf │
│  snap diff      │        │  dedup across layers    │      │  0x76 part │ dir     │
│  ls, get, check │        └─────────────────────────┘      │  init, format, RDB   │
└───────┬─────────┘                                          └───────────┬──────────┘
        │                                                                │
        │            ┌───────── zerofree / compact ─────────┐            │
        │            │  existing images only; composed      │            │
        │            │  images are clean by construction    │            │
        │            └──────────────────────────────────────┘            │
        │                                                                │
┌───────▼────────────────────────────────────────────────────────────────▼──────────┐
│  IMAGE ACCESS                                                                     │
│  plain HDF · RDB HDF · ADF/ADZ · raw device (/dev/rdiskN) · MBR 0x76 · directory  │
│  FFS layer behind a narrow interface  ◀── swappable, see §6                       │
└───────────────────────────────────────────────────────────────────────────────────┘
```

Three structural decisions to make on day one:

**Keep the FFS layer behind a narrow interface.** Everything amitools-specific lives in one module
with a small surface: open image, list partitions, walk tree, read file, write file, read bitmap,
create, format. If amitools ever needs replacing — for licensing, or because a bug proves
intractable — only that module changes. It also makes the differential test harness (§6) natural
rather than bolted on.

**Make the image-access layer format-agnostic from the start.** Plain HDF, RDB HDF, raw device and
MBR `0x76` slice all resolve to the same interface. This is verified feasible: a file-like slice
object passed as amitools' `fobj` reads an RDB image embedded in a `0x76` partition with no amitools
changes (notes §9.3). Retrofitting this later would touch every command.

**The layer store is the source of truth; images are artefacts.** No command should require an image
to exist in order to answer a question about what a stack contains.

---

## 3. Phased plan

Ordered by risk, ascending. Each phase is independently useful.

### Phase 0 — Decisions

Mostly done. Remaining questions are in §7 and none of them block starting Phase 1.

### Phase 1 — Read-only foundation ✅ DONE

**Risk: none.** Nothing writes. Immediate day-one utility, and it surveys what is actually on the
shelf.

**Delivered.** Ten commands in `amibuilder/`, 3,610 lines, installed as a console script.
262 new tests (403 total). Notes on what the build changed relative to this plan:

- **`test/helpers/blocks.py` was NOT graduated into the package**, contrary to the original
  intent. It is the independent oracle the structural tests assert against; importing it into
  the package would have destroyed that independence. The package carries its own block layer
  and `test_blocks.py` pins the two together instead. `mbr.py` *was* graduated, since it is
  plumbing rather than an oracle.
- **Volume-name partition selection had to be built**, not just wired up: amitools'
  `find_partition_by_string` resolves device names and indexes only, so `card.hdf:Workbench`
  needs each partition mounting on a miss.
- **Timestamps diverge from amitools deliberately** — see `amibuilder/timestamps.py`. This was
  not anticipated in the plan and is the single most subtle thing found.
- **Five further amitools defects** surfaced during the build and are pinned; the worst is
  `get_blocks(with_data=True)` silently omitting every data block on FFS volumes, which would
  have corrupted the byte-exact work planned for later phases.
- **`check` classifies rather than suppresses** the dircache false positives: each discrepant
  block is read independently and only reclassified if it is genuinely a checksum-valid
  dircache block. A test that corrupts a root block confirms real errors still fail, so the
  classification is not swallowing everything.

- Image access for all six shapes: plain HDF, RDB HDF, **ADF / ADZ / `.adf.gz`**, `/dev/rdiskN`,
  MBR `0x76` slice, directory
- MBR parser for `0x76` discovery (~60 lines, design verified)
- `info`, `partitions`, `ls` (`-l`, `-R`), `tree`, `cat`, `hexdump`, `find`, `du`
- `get` (extract file or subtree), `check` (full 5-step validation — notes §5.5)
- `--json` on everything
- Graceful, explicit refusal for PFS3/SFS, non-DOS ADFs and anything unrecognised; point at `xdms`
  for DMS images rather than reimplementing it (notes §10.3)

Guard rails for raw device access land here, not later: `--device` required, `diskutil list` shown,
typed confirmation, read-only default, hard refusal on the boot disk. Writing to the wrong MBR entry
on a PiStorm card destroys the Emu68 boot partition.

**Exit criterion:** correctly report geometry, partition layout, DosType and file listings for every
HDF and every card in the collection — and say clearly when it cannot parse something rather than
guessing. This will also answer the remaining survey questions in §7 empirically.

### Phase 2 — Layer capture

**Risk: none to any Amiga image.** Reads images, writes only to the layer store.

- Store layout: blobs, layer manifests, refs, recipes (`KIP-FFS-LAYERS.md` §3)
- **Volume-qualified manifest paths** (`Workbench:S/Startup-Sequence`), with physical placement held
  only in the drive record (layers §3.1)
- `snap create` — base layer: full capture plus the **recorded RDB drive layout** including per-volume
  policy (layers §4)
- `snap diff` — candidate diff layer against a parent
- **`snap create-from-adf`** — one or more ADFs become a layer rooted at a target path. Needs no write
  path at all, so it lands here rather than with the write work (layers §7.5)
- `snap review` (with `--explain`, `--drop`, `--keep`) and `snap commit` — the review gate
- `snap ls`, `snap show`, `snap verify`, `snap gc`, `snap rm`
- Comparison key defaults to content + protection + comment, **not** timestamps (layers §5)
- Whiteouts recorded for deletions (layers §6)

On diff noise: the review gate is worth building, but flexibility is acceptable here — a diff that
picks up some incidental changes still does the job. So keep the default comparison key
(content + protection + comment) and `--explain`, and treat elaborate exclusion tuning as optional
polish rather than a prerequisite.

**Exit criterion, and the number that validates the whole idea:** capture a real AmigaOS 3.2.3
install as a base layer, install one piece of software, capture a diff layer, and measure the diff
layer's size against the 4 GB image it came from. Also confirm the diff contains roughly what was
installed and not several hundred entries of timestamp noise. If the noise problem is worse than
expected, the exclusion defaults get tuned here, cheaply, before anything depends on them.

### Phase 3 — Compose ✅ DONE (branch `phase3`)

**Risk: moderate, but bounded.** Writes only to fresh targets. A bug means discarding an artefact.

This phase delivers the requested workflow end to end.

**What diverged from the list below, and why:**

- **`init` was deferred out of this phase and has since been built** (2026-08-20). `compose` creates
  and formats its own targets, so the restore workflow never needed a standalone `init`, but the
  original request asked for `--init` with `--size` to make a blank image, which is a genuinely
  different job from composing one — and it is what makes a *base* layer possible at all, since
  there has to be an empty drive to install AmigaOS onto before there is anything to capture.
  Built as `amibuilder init`, and deliberately **reusing `compose`'s RDB writer** rather than adding
  a second path to the same bytes; that was the whole reason it was deferred, and the reason stands.
  Verified on real AmigaOS 3.2 (stats §5): three partitions mounted with no HDToolBox step.
  **`format` is still owed** — `init --partition` formats what it creates, but there is no way to
  format a partition on a drive that already exists.
- **`merge` was built here, not deferred to Phase 4.** The plain and RDB targets can open an
  existing image and add to it, so the policy fell out of the write path rather than needing
  additive-write machinery. It cannot delete, and a stack whose whiteouts a merge would ignore is
  reported rather than silently applied.
- **The MBR `0x76` device target is still deferred.** It is the one target that can destroy an
  Emu68 card, so it waits for a real card to test against.
- **Verification was added, unplanned here.** `compose --verify` is on by default; see §0.

- ~~`init` — create images, **RDB by default**, `--plain` for emulator use, cylinder rounding
  reported~~ **Done 2026-08-20.** Blank by default (as the brief asked), `--partition` for RDB,
  `--plain` for a single-volume emulator image; rounding reported per partition. An existing file is
  never overwritten and there is no flag to make it happen.
- `format` — boot blocks, root block, bitmap, explicit DosType (DOS3 target). Still owed for
  *existing* drives; `init --partition` covers the create-and-format case.
- RDB construction from a base layer's recorded drive layout, including `de_Mask` and
  `de_MaxTransfer` copied from the working original rather than defaulted
- `compose` into: RDB HDF, plain HDF, MBR `0x76` partition on a device, or a directory with `.uaem`
- **Per-volume policy: `replace` and `preserve`** (layers §7). `merge` needs additive writes and lands
  in Phase 4
- **Partition-granular compose** — `--into card.hdf:0 --reformat` restores one volume to stock while
  leaving its neighbours alone. This is what makes multiple partitions on one drive viable, so it is
  core rather than optional (layers §3.1)
- Partition-bounds guards: refuse any write whose computed block range overlaps another partition or
  falls below the first partition's start; `check` the other partitions afterwards
- Conflict reporting, deletion application, parent-drift warnings (layers §6)
- `recipe` management
- `--dry-run` that exercises the real planning path
- **Refuse to overwrite an existing target without `--force`**, naming which volumes would be
  destroyed and which preserved (layers §7)

**Exit criterion:** compose `base + one software layer` into a fresh RDB HDF, and boot it. First in
FS-UAE, then on real hardware via ZuluSCSI. This is the first point at which anything has been
validated against a real Amiga filesystem implementation, which notes §7 flags as the outstanding gap
in everything verified so far.

**Status: MET under emulation on 2026-08-18. Real hardware still outstanding.**

A composed RDB HDF boots real AmigaOS 3.2.3 — **Kickstart 47.96, Workbench 47.2** — under FS-UAE,
and AmigaDOS cannot tell it from the drive it was captured from. Method and numbers in
`KIP-FFS-LAYERS.md` §13; the short version is that `Info` and `List SYS: ALL` return identical
output for source and composed (73 files, 797K, 15 directories, 1742 blocks, **0 errors**), with the
only difference being the `S/Startup-Sequence` the test harness deliberately injects into each copy.

This is the first result in the project that is **not** this program agreeing with itself: a real
Kickstart's `dosboot` read our RDB, a real FFS implementation mounted our partition, and real
AmigaDOS commands executed from it.

Still unvalidated, and worth keeping in view:

- **Real hardware.** ZuluSCSI and PiStorm/Emu68 have seen nothing.
- **Scale.** The test drive holds 797 KiB of install-floppy contents, not a 4 GB Workbench install.
- **Multiple partitions booting.** The round-trip tests cover two partitions; the boot test used
  one, so nothing has confirmed a real Amiga mounting several of our partitions at once.

### Phase 4 — Additive writes

**Risk: moderate.** Writes into existing volumes, but only ever adds or overwrites — never deletes or
renames, which is where the real danger sits.

This path is **already verified**: three ADFs staged into an existing `ffs+intl` partition produced a
volume the validator reported `ok`, with byte-identical contents (`KIP-FFS-NOTES.md` §10.1). It unlocks
two requested capabilities that share the same dependency:

- **`merge` volume policy** — add a games layer to an existing `Work:` without wiping it (layers §7)
- **ADF direct injection** — `amibuilder cp 'Disk1.adf:/' 'card.hdf:0:Install/App/' --recursive` for
  one-off staging (layers §7.5)
- `mkdir` (with a recursive `mkdir -p`, since amitools' `create_dir` is not recursive — notes G19)
- `put` / `cp` into an image, `protect`, `comment`, `touch`, `relabel`
- Pre-flight the whole tree before writing any of it: filename lengths, illegal characters, comment
  lengths, free space (notes G1, G20)
- Report collisions when merging multiple sources into one path (notes G21)

### Phase 5 — `zerofree` and `compact`

**Risk: writes to existing images**, but with an unusually strong self-check available.

Deprioritised relative to the original plan, because composed images have zero free space by
construction. Still needed for the existing image collection, and for byte-exact backups of cards
that come back from a machine.

- `zerofree` — zero unallocated blocks per the FFS bitmap
- `compact` — punch holes over zero runs via `F_PUNCHHOLE` (verified working from pure Python)
- `verify` — re-read and hash-compare, for post-write card checks

**`zerofree` must prove it changed no file content.** Extract every file and compare hashes before
and after, and run `check` both times. If anything differs, the bitmap was misread. This should be
`--verify` **on by default**, built into the command rather than left to a test suite. It turns "I
hope the bitmap parser is right" into "the tool demonstrated it did no harm." Plus: `check` gate,
`--dry-run` block counts, lock, and temp-copy-and-rename with `--in-place` opt-in.

### Phase 6 — Shell and quality of life

- `amibuilder shell` — REPL with readline, history, tab completion on in-image paths
- `diff` between any two sources (image, layer, ADF, directory)
- `doctor`, `completion`

### Phase 7 — Destructive writes (optional)

**Risk: highest.** Deliberately last, and quite possibly never needed.

- `rm`, `mv` / rename, `sync` with `--delete`

Everything additive already landed in Phase 4, so what remains here is specifically deletion and
renaming inside an existing volume. The argument for eventually building it is SD card wear: updating
changed files in place on a mounted card touches only dirty blocks instead of rewriting 4 GB
(notes G15). The argument against doing it early is that `replace`, `merge` and `preserve` policies
already cover the stated workflow — putting a card back to a known-good configuration — and this is
where corruption risk concentrates. Revisit once Phases 1–4 are in daily use.

---

## 4. CLI surface sketch

```
amibuilder <command> [options]

  Inspect    info · partitions · ls · tree · cat · hexdump · find · du · check
  Extract    get
  Layers     snap create|create-from-adf|diff|review|commit|ls|show|verify|gc|rm|export|import
  Recipes    recipe new|ls|show
  Build      compose · init · format
  Write      mkdir · put · cp · protect · comment · touch · relabel      (Phase 4, additive)
  Space      zerofree · compact · verify
  Compare    diff
  Session    shell
  Meta       doctor · completion · version
  Later      rm · mv · sync --delete                                     (Phase 7, destructive)

Global: -n/--dry-run  --json  --yes  -v/--verbose  -q/--quiet  --progress
        --partition <name|index>  --device  --target-partition 0x76:N
        --format rdb|plain|dir   --metadata {xdfmeta,uaem,both,none}
        --exclude PAT  --include PAT  --backup  --in-place  --lock

Sources and targets:
  image.hdf                  plain HDF, or RDB partition 0
  image.hdf:0 | image.hdf:DH0   RDB partition by index or name
  disk1.adf | disk1.adf.gz   floppy image (gzip read-only)
  /dev/rdisk4                raw device (requires --device + confirmation)
  /dev/rdisk4:0x76:1         MBR 0x76 partition on a PiStorm card
  ./somedir                  directory (with .uaem or .xdfmeta metadata)
  @base-os-3.2.3             a layer ref
```

Uniform addressing across sources is what makes `diff` able to compare a layer against a live card,
which is the operation that will get used most while iterating.

---

## 5. Design rules

1. **Read-only by default.** Write access is opt-in per command, never implicit.
2. **Capture never writes to an Amiga image.** Ever.
3. **Compose writes only to fresh targets**, and refuses to overwrite without `--force`.
4. **`--dry-run` on every mutating command**, exercising the real planning path.
5. **Validate before writing.** No mutation touches an image that fails `check`.
6. **Pre-flight the whole plan before writing any of it.** No half-applied bulk operations.
7. **`zerofree` proves it changed no file content.** On by default.
8. **Capture drive geometry, never synthesise it** when an original exists. Copying proven
   `de_Mask` / `de_MaxTransfer` values beats defaulting them.
9. **Refuse rather than guess.** Unknown DosType, ambiguous target, a partition over 4 GB, a `0x76`
   entry that does not contain an RDB — say so and stop.
10. **Never text-translate.** Byte-exact, always.
11. **Timestamps are stored raw** (days/mins/ticks), never converted through a host timezone.
12. **`--json` for every read command**, from the first release.
13. **Lock per image and per store.** No concurrent writers.

---

## 5.1 Deriving layers from install scripts

Two routes to turning a software install into a layer, analysed in
`KIP-FFS-IDEAS.md` §7:

- **Observe mode** — run the install in FS-UAE against the base image, then `snap diff`.
  Handles everything including games, archives and custom installers. Needs **no new
  machinery**: it is Phase 2 capture plus the emulator harness, both already planned. This
  is the route to build.
- **Simulate mode** — parse the Commodore `Installer` script (LISP-like S-expressions),
  prompt for volume, path and options, and emit a layer without booting anything.
  **Measured against the AmigaOS 3.2 base installer and found tractable**: 26 of its 30
  external-binary invocations are no-ops, trivially replaceable, or answerable from a target
  machine profile. See [`KIP-FFS-INSTALLERS.md`](KIP-FFS-INSTALLERS.md).

The four genuine gaps in the base OS case are `UpdateWBFiles` (an m68k binary whose effect
is not yet known), `IconPos` and `CopyToolTypes` (both write `.info` icon files, needing a
`DiskObject` parser — G24), and `Prefs/WBPattern` (cosmetic). None prevent a bootable
system.

Sequencing: observe mode falls out of Phases 2–3 at no extra cost and captures all four
gaps correctly, because it records outcomes. Simulate mode is a later accelerator; a hybrid
that simulates everything and hands only `UpdateWBFiles` to observe mode looks attractive.
Either way it must **refuse to emit a layer on any construct it cannot model** unless
`--allow-partial` is given with an explicit manifest of what was skipped.

A prototype tokeniser and analyser already exists at `test/helpers/installer.py`, with
tests that assert the AmigaOS 3.2 findings and skip when the CD image is absent.

---

## 6. Testing strategy

**Implemented.** A 104-test pytest suite exists in `test/` and runs in about 40 seconds with no
Amiga ROMs or images required — every fixture is built from scratch. It characterises the on-disk
formats and regression-tests amitools. See the README for the module breakdown. Key design
decision: `test/helpers/blocks.py` implements checksum and offset maths **independently of
amitools**, so a test cannot pass merely because amitools agrees with itself.

**Golden corpus (still to assemble).** Real images (reduced copies, never originals) covering: RDB
multi-partition, plain HDF, DOS1, DOS3, OFS, a PiStorm card image with MBR `0x76` partitions, a
PFS3 partition, an image with hard and soft links, one with 30-character and near-limit filenames,
a DD ADF, an HD ADF, a gzipped ADF, and a non-DOS ADF. Synthetic equivalents of most of these are
already generated by the suite; the value of real images is catching whatever a formatter other
than amitools produces.

**Partition isolation test.** After a partition-granular compose with `--reformat`, assert that every
*other* partition on the drive is byte-identical to before and still validates. This is the guard for
the one real risk introduced by preferring multiple partitions over multiple drives.

**ADF staging test.** Multi-ADF merge into one target path: all files present, byte-identical to
origin, volume validates, and collisions reported.

**Round-trip property tests.** `unpack` → `pack` → compare: content byte-identical, metadata
preserved. This is what validated amitools across 5,950 files and is the cheapest high-value test
available.

**Layer round-trip.** `snap create` → `compose` → `snap create` again → the two manifests must be
identical modulo recorded timestamps. This is the core correctness property of the layer model and it
is self-checking, needing no external oracle.

**Diff noise regression.** Capture a base, apply a synthetic "boot-like" mutation (touch datestamps,
write `env-archive`, add a Trashcan entry), and assert the diff is empty or near-empty. This locks in
the §5 comparison-key behaviour so it cannot silently regress.

**`zerofree` invariant.** All file hashes and metadata identical before and after; volume validates
both times; free-block count unchanged. Property-based over generated images.

**Differential oracle.** If the FFS layer is ever ported off amitools, apply identical operation
sequences with both implementations and assert byte-identical images. Worth designing the interface
for now even if never used.

**Emulator verification.** The gap notes §7 is explicit about: nothing has been tested against a real
Amiga filesystem implementation. Boot a composed image headless in FS-UAE, run `Info` and `Validate`,
assert clean output. The `.uaem` convention makes the directory-target variant of this cheap.

**Mutation-test the safety gates.** A guard that cannot fail is decoration. For each rule in §5
enforced in code, delete the guard and confirm the test goes red.

**Regression tests for the four amitools bugs** in notes §5.4, so a version bump that changes them is
noticed.

---

## 7. Remaining open questions

Answered: targets (ZuluSCSI + PiStorm + emulators), no dircache, no partitions over 4 GB,
private-but-possibly-published, compose-into-fresh rather than in-place, deletions recorded as
whiteouts and applied by default, ADF support in scope, multiple partitions preferred over multiple
drives, per-volume `replace`/`merge`/`preserve` policy, diff noise handled by default comparison key
with flexibility accepted.

Remaining L-series questions specific to the layer model are in `KIP-FFS-LAYERS.md` §10. **L3 is now
resolved** (volume-qualified paths, multiple partitions preferred, multi-drive still supported).

**Q1 — Is FS-UAE, vAmiga or WinUAE already set up for headless use?** The emulator verification loop
is the single best safety net available and Phase 3's exit criterion depends on it.

**Q2 — Which AmigaOS versions across the machines?** Affects whether one base layer serves several
machines or each needs its own, and whether any partition needs a filesystem binary embedded in the
RDB (notes G17).

**Q3 — Do any drives use PFS3 or SFS?** Those are invisible to file-level tooling, so they need the
byte-exact path instead (layers §10 L5). Phase 1 will answer this by inspection.

**Q4 — Are the existing images RDB or plain?** Phase 1 answers this too. It determines how much
migration work the existing collection needs.

**Q5 — Tool name?** `amibuilder` is a placeholder and ends up in the module name and entry point.

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Diff noise makes layers untrustworthy and the idea gets abandoned | **High** — most likely failure mode | Comparison key excludes timestamps by default; two-step capture with `snap review --explain`; diff-noise regression test; tune exclusions against real machines in Phase 2 |
| Writing to the wrong MBR entry destroys a PiStorm Emu68 boot partition | **Critical** | `--device` flag; `diskutil list` shown; typed confirmation; read-only default; refuse boot disk; refuse any `0x76` entry that does not contain a valid RDB; never touch unit 0 / the FAT32 partition |
| Composed image does not boot on real hardware | High | Capture drive layout from a working original rather than defaulting (layers §4); emulator verification before hardware; Phase 3 exit criterion is a real boot |
| Bitmap misparse causes `zerofree` to destroy live data | **Critical** | Default-on verify comparing all file hashes before/after; `check` gate; `--dry-run`; temp-copy-and-rename; property tests |
| Mutable state lost by recomposing (saves, configs, work) | Medium | Per-volume `replace`/`merge`/`preserve` policy; `compose` refuses to overwrite without `--force` and names which volumes would be destroyed (layers §7) |
| Partition-offset bug scribbles on a neighbouring partition during partition-granular compose | High | Validate the computed block range against the RDB and refuse on overlap or on falling below the first partition's start; `check` the other partitions afterwards; partition isolation test in the suite (layers §3.1) |
| ADF staging aborts partway, leaving a half-copied tree | Medium | Pre-flight the whole ADF tree for name lengths and free space before writing (notes G20); report collisions (G21) |
| Wrong `de_Mask` / `de_MaxTransfer` silently corrupts data on real hardware | High | Copy from a working drive, never default; the Emu68 PFS3 mask failure is a documented real-world instance (notes §9.2) |
| amitools bugs in untravelled paths (four found in one afternoon) | Medium | Regression tests per bug; pin the version; validate after every mutation |
| GPL becomes a problem if published permissively | Low | Decision taken; FFS layer behind a narrow interface preserves the option |
| Over-building — a snapshot engine, or in-place sync nobody needs | Medium | Phase 6 is explicitly optional; `KIP-FFS-IDEAS.md` §5 "not worth building" list |
| Hard/soft links mishandled — completely untested | Medium | Explicit policy; add linked images to the golden corpus in Phase 1 |

---

## 9. Immediate next steps

1. **Copy two or three representative images, one PiStorm card image, and a couple of ADFs somewhere
   safe** as the start of the golden corpus. Never test against originals.
2. **Build Phase 1** and run it across every image, card and ADF. Zero risk, and it answers Q2–Q4 by
   inspection.
3. **Build Phase 2 and run the measurement experiment**: real base layer, one software install, one
   diff layer. Its size versus the 4 GB image, and its noise level, decide how much of Phase 3
   onwards is worth building and whether the exclusion defaults need work.
4. **Pick a name** (Q5) before the first commit.
