# KIP-FFS — Plan

**Date:** 2026-08-17 (revised after target hardware and the layered model were settled)
**Status:** Proposed. Nothing built.

Companion docs: `KIP-FFS-NOTES.md` (verified findings), `KIP-FFS-LAYERS.md` (layer model design),
`KIP-FFS-IDEAS.md` (option analysis).

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

### Phase 3 — Compose

**Risk: moderate, but bounded.** Writes only to fresh targets. A bug means discarding an artefact.

This phase delivers the requested workflow end to end.

- `init` — create images, **RDB by default**, `--plain` for emulator use, cylinder rounding reported
- `format` — boot blocks, root block, bitmap, explicit DosType (DOS3 target)
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
