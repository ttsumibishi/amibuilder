# KIP-FFS — Plan

**Date:** 2026-08-17 (revised after target hardware and the layered model were settled)
**Status:** Proposed. Nothing built.

Companion docs: `KIP-FFS-NOTES.md` (verified findings), `KIP-FFS-LAYERS.md` (layer model design),
`KIP-FFS-IDEAS.md` (option analysis).

---

## 0. Session state — read this first when resuming

**Last updated: 2026-08-21.** Written as a resume point, so a fresh session can pick up without
re-deriving anything. Where this section disagrees with the phase descriptions below, this section is
newer.

### Where the work stands

| | State |
|---|---|
| Phase 1 — read-only inspection | ✅ **Complete** |
| Phase 2 — layer capture | ✅ **Complete**, and now measured against a real AmigaOS 3.2 install |
| Phase 3 — composition | ✅ **Complete.** All four targets write; verification is on by default |
| `amibuilder init` | ✅ **Built 2026-08-20.** Verified on real AmigaOS; see stats §5 |
| Phase 4 — additive writes | ✅ **Complete.** `cp`, `mkdir`, `rm` (with an image-side wildcard) verified on real AmigaOS; `format` for existing drives; `touch`/`protect`/`comment`/`relabel` for metadata on a volume; and **Phase 4b `inject`** — copy an ADF's or a partition's contents into another volume. `format`/`inject`/metadata are test- and mutation-checked, not yet booted on real hardware |
| Phase 5 — space reclamation | ✅ **Complete 2026-08-24.** `zerofree` (`070a1cc`) zeros free FFS blocks — all RDB partitions by default, one via a selector — with content-preservation **verify on by default** (re-hash every file and re-count free blocks on the result, abort keeping the original untouched) and a temp-copy-and-rename default, `--in-place` opt-in; `compact` (`67e6dd8`) punches the zero runs into holes via `F_PUNCHHOLE` (APFS, file-only); `zerofree --compact` (`20c4a2d`) does both in one pass. Test- and mutation-checked; file-only in v1 |
| Session 2026-08-21 | ✅ **`snap create`/`diff` from a host directory · recorded per-volume policy in a `recipe` · image-side wildcards for `get` · `format` command.** Each committed and pushed separately |
| Session 2026-08-22 | ✅ **`diff` between any two sources** (`commands/compare.py`, commit `e57969f`) — read-only compare of two images / partitions / ADFs / host dirs; path-vs-volume alignment; an RDB selector narrows to one volume |
| Session 2026-08-24 | ✅ **Image-side wildcard for `rm` (`624b828`) · `touch`/`protect`/`comment` (`45c6b09`) · `relabel` (`8805e82`) · Phase 4b `inject` (`3eda047`).** Each committed and pushed separately. Two amitools quirks worked around at the block level: `change_meta_info` skips a zero protect mask, and `change_comment` crashes on any comment (`len()` on a `FileName`) |
| Tests | **1605 passing** (non-emulator), 63 deselected · 97 in `test_emulator.py`, 63 emulator-marked (emulator suite not re-run this session) · **1668 total** |
| Git | `main` pushed to `origin`. Phase 5 batch `070a1cc` feat(zerofree) · `67e6dd8` feat(compact) · `20c4a2d` feat(zerofree --compact), on top of `ea069f8` (prior docs). This docs refresh commits on top |

Run the suite in two halves — one long run has repeatedly hung:

```bash
.venv/bin/python -m pytest -q -m "not emulator"      # 1605 tests, ~16 min
.venv/bin/python -m pytest -q test/test_emulator.py   # 97 tests, ~1.6 min, no window appears
```

**Remote:** `origin` is `http://192.168.1.71:3069/lmdracos/amibuilder.git` (Gitea, HTTP on 3069).
SSH would be port **2222**, but `kiro_ide_id_ed25519` is not yet authorised there — add it under
Settings → SSH Keys if SSH is wanted. HTTP works today via the macOS keychain.

### The measurement that decided the project — done

**A real AmigaOS 3.2 install is captured, and the headline claim holds.** Full numbers in
`KIP-FFS-STATS.md` §2; the short version:

| | |
|---|---|
| Base layer `base-3.2` (`5c0b1e127911`) | 6,032,638 B of content → **3.1 MiB stored**, 812 files |
| Diff layer `sysinfo-4.4` (`131702ca2589`) | a real software install → **46.7 KiB**, 14 entries |
| Against the 4 GiB HDF it came from | **89,830× smaller** |

Round trip verified: composing `base-3.2,sysinfo-4.4` and re-capturing yields an identical layer ID.

**One prediction of mine was falsified along the way, and it is left visible in the stats doc rather
than edited out:** I claimed AmigaOS restamps files on boot, so a bare boot would generate diff
noise. It does not — the image is byte-identical after booting (`cmp` rc=0, mtime untouched). That is
the second wrong prediction in that document (the first was about compression), and the pattern has a
name: *a plausible mechanism is not a measurement.*

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
   through a real AmigaOS. **The opposite direction now is:** `cp --preserve-times` writes a host
   mtime of `1999-07-14 15:09:26` and AmigaDOS renders `14-Jul-99 15:09:26` (stats §5). Since both
   directions use the same interpretation — Amiga time is naive local wall clock — that is strong
   circumstantial evidence for `get` as well, but the host-side leg is still unverified.

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
- **A recorded per-volume policy cannot be changed after capture, and this is structural.** The
  drive record is the fourth input to `compute_layer_id`, so editing a policy string changes the
  layer ID; `verify_layer` therefore reports a hand-edited policy as corruption, and re-capturing to
  obtain a new ID orphans every diff layer that recorded the old base as its `parent`. `set_policy()`
  exists and has no caller for exactly this reason. The consequence is that `preserve` — the one
  policy that cannot be inferred — has no persistent home, so it must be passed on every compose.
  Designed out under "Recorded policy intent" in Phase 4; **do not** start by wiring up
  `set_policy()`, which is the obvious move and the wrong one.

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

### Phase 4 — what got built (`cp`, `mkdir`)

```
amibuilder/volume.py       write side: mkdir, write_file, set_times, parse_protect,
                           check_name, check_comment, blocks_for, writable
amibuilder/timestamps.py   now(), from_unix(), from_datetime() -- writing, without mktime
amibuilder/commands/write.py   cmd_cp, cmd_mkdir
utils/scripts/mutate-write-guards.py   21 mutations over the new guards
```

`Volume` is no longer read-only, and everything amitools-specific about writing is inside it — the
rule that nothing above `volume.py` imports amitools still holds. Four things worth not re-deriving:

- **`update_ts=False` on every create, then stamp explicitly.** amitools' own update runs through
  the January-`mktime` epoch. Note the subtlety mutation testing exposed: because `_stamp` runs
  immediately afterwards with correct arithmetic, `update_ts=False` is *belt-and-braces here* and
  genuinely load-bearing only in `layers/targets.py`, which reproduces a recorded tree and must not
  restamp anything.
- **Writing into a directory stamps that directory**, which is correct AmigaDOS behaviour. So
  `cp --preserve-times` re-applies directory mtimes in a final pass, or every directory in a `-r`
  copy ends up carrying the copy time instead of the host's.
- **`--protect` accepts two spellings**, because argparse reads a bare `----rwed` as another option.
  `--protect rwed` names only the permitted bits and needs no escaping; `--protect=----rwed` is the
  canonical form. amitools' `ProtectFlags.parse` already supported the short form.
- **Symlinks are reported and skipped, never followed.** Following one turns a link into a duplicate
  copy, or — pointing outside the tree — copies something that was not asked for.

The naming and comment limits moved from `layers/compose.py` to `volume.py`, where the code that
writes through them lives; `compose` imports them, so there is one copy rather than two that can
disagree about what FFS accepts.

**Done 2026-08-21:** `snap create` and `snap diff` now accept a host directory as their source — the
*proper* fix for staging that `cp` only worked around. A `DirectoryVolume` adapter
(`layers/hostdir.py`) walks the tree and reads `.uaem` sidecars through the shared `layers/uaem.py`
parser, feeding `capture_container` directly; `--volume NAME` sets the recorded volume name (default:
the directory's own name). `cp` still just gets host files onto a drive; this turns a folder into a
layer.

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
- `snap create` — base layer: full capture plus the **recorded RDB drive layout** including a
  per-volume policy (layers §4). Note what that policy is and is not: a *default* inferred from the
  bootable flag, not a statement of intent, and it cannot be edited afterwards because the drive
  record is inside the layer's identity hash. See "Recorded policy intent" under Phase 4
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
  **`format` for existing drives shipped 2026-08-21** — `init --partition` formats what it creates,
  and `format card.hdf:Work` now lays a fresh filesystem onto a partition of a drive that already
  exists, reusing the RDB-recorded name and DosType and refusing a `--dos-type` change that would
  desync the RDB from the filesystem.
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
- `format` — boot blocks, root block, bitmap, explicit DosType (DOS3 target). **Shipped 2026-08-21
  for existing drives** (`format card.hdf:Work`), reusing `init`'s root-block writer; `init
  --partition` covers the create-and-format case.
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

- **Real hardware.** ZuluSCSI and PiStorm/Emu68 have seen nothing. Worth knowing that until
  2026-08-20 they *could* not have: `_device_size` imported `BlkDevTools` from the wrong module, so
  every raw-device operation died at construction. Fixed and pinned (notes G30). Nothing had caught
  it because every card fixture is a file, so the device path was never exercised.
- **Scale.** The test drive holds 797 KiB of install-floppy contents, not a 4 GB Workbench install.
- **Multiple partitions booting.** The round-trip tests cover two partitions; the boot test used
  one, so nothing has confirmed a real Amiga mounting several of our partitions at once.

### Phase 4 — Additive writes ✅ DONE

**Risk: moderate.** Writes into existing volumes, but only ever adds or overwrites — never deletes or
renames, which is where the real danger sits.

This path is **already verified**: three ADFs staged into an existing `ffs+intl` partition produced a
volume the validator reported `ok`, with byte-identical contents (`KIP-FFS-NOTES.md` §10.1). It unlocks
two requested capabilities that share the same dependency:

- ✅ **`merge` volume policy** — add a games layer to an existing `Work:` without wiping it (layers §7)
- ✅ **ADF direct injection** — shipped 2026-08-24 as its own `inject SOURCE DEST [--from PATH]
  [--to DIR]` command (`commands/inject.py`, commit `3eda047`) rather than overloading `cp`, since
  a source that is an image reads nothing like a host file. Metadata (protect/comment/timestamps)
  is carried across, and source and dest must be different files in v1 (layers §7.5)
- ✅ `mkdir` (with a recursive `mkdir -p`, since amitools' `create_dir` is not recursive — notes G19)
- ✅ `cp` into an image, with `--protect` and `--comment`; and `touch`/`protect`/`comment`/`relabel`
  for entries and volumes already on a disk (commits `45c6b09`, `8805e82`, 2026-08-24)
- ✅ Pre-flight the whole tree before writing any of it: filename lengths, illegal characters, comment
  lengths, free space (notes G1, G20)
- ✅ Report collisions when merging multiple sources into one path (notes G21) — refused, because FFS
  ignores case, so which source won would depend on argument order

**Done 2026-08-20: `cp` and `mkdir`.** Verified on real AmigaOS 3.2 (`KIP-FFS-STATS.md` §5). Four
decisions in there worth not re-litigating:

1. **Write methods live on `Volume`, not in the command.** Keeps the rule that nothing above
   `volume.py` imports amitools, and keeps the `update_ts=False` discipline in one place. Rejected:
   duplicating `create_file` logic in the command (would drift), and refactoring `layers/targets.py`
   to use the new methods (risks the well-tested compose path for no gain).
2. **`cp` takes the image last** (`cp FILE... IMAGE`), unlike every read command, because that is
   what Unix `cp` established. `mkdir` keeps the image first. The in-image path stays a **separate
   argument** (`--to`) in both cases, preserving the rule from `cli.py`: glue a path onto the spec and
   `card.hdf:Work` becomes ambiguous between a volume and a directory.
3. **Timestamps default to "now", `--preserve-times` opts into the host mtime** — symmetric with
   `get`, which defaults the same way in the opposite direction. `timestamps.now()` and
   `timestamps.from_unix()` were added rather than reusing amitools' conversion, for the reason the
   whole module exists.
4. **Directories are re-stamped after their contents.** Writing into a directory stamps it, which is
   correct AmigaDOS behaviour and exactly wrong when reproducing a host tree — so `--preserve-times`
   applies directory mtimes in a final pass. Without it, every directory in a `cp -r` ends up carrying
   the copy time.

**Phase closed 2026-08-24.** ADF-to-HDF injection (`inject`), `touch`/`protect`/`comment`/`relabel`
and the `rm` image-side wildcard all shipped this session; `snap create`/`diff` from a host directory
and recorded policy intent shipped 2026-08-21. The one check still worth doing is a booted AmigaOS
*writing* to an `inject`-written volume as a round-trip — everything so far is validated by amitools'
own validator plus mutation-tested guards, not by a real machine reading the metadata back.

#### Recorded policy intent — a volume that should never be overwritten · ✅ Implemented 2026-08-21

**Shipped as designed below** (commit `7353c30`): the recipe stores a `policies` map — both snags
were handled, so `write_recipe` always writes the key and `_stack_specs` forwards it — the precedence
is *CLI `--policy` → recipe → recorded default → `merge`*, and `compose` prints which volumes had a
policy **set explicitly** and warns when a recipe names a volume no layer defines. That closes the
one failure mode this feature must not have: a `preserve` that silently did not apply. The rationale
is kept below as the design record.

**The problem, concretely.** Dave's layout is `Workbench:` (the OS, disposable), `Work:` (games and
utilities, "effectively lost" and fine to lose) and `Persist:` (anything worth keeping, which must
survive an OS reinstall). `Persist:` therefore wants the `preserve` policy permanently. Today it
gets `merge`, and the only way to change that is `--policy Persist=preserve` on **every single**
`compose` invocation. Forget it once during a restore and the volume whose entire purpose is
surviving reinstalls gets written into.

**Why it is not simply "store the policy" — policy is already stored.** `drive.capture()` writes
`"policy"` into every partition entry (`drive.py:174`), and `build_plan` reads it back
(`compose.py:541-545`) with the precedence *CLI override → recorded policy → `merge`*. What is
recorded is an **inference**, not intent: `default_policy()` returns `replace` for the bootable
partition and `merge` for everything else, and it deliberately never infers `preserve` — the comment
explains why, and it is correct. *"A save-games volume looks exactly like a work volume from
outside."* Nothing on the drive distinguishes `Work:` from `Persist:`. Only Dave knows, and there is
nowhere for him to say it once.

**Why the obvious fix is the wrong one.** `drive.set_policy()` already exists, is tested, and has no
caller — so "wire it up to a `snap set-policy` command" looks like an afternoon. It is a trap, for
three compounding reasons:

1. **The drive record is inside the layer's identity hash.** `compute_layer_id` hashes
   `canonical_json(drive)` as its fourth input (`store.py:99-126`). Changing one policy string mints
   a different layer ID.
2. **So it cannot be edited in place.** `verify_layer` recomputes the ID from the stored record and
   reports a mismatch as *"the manifest has been modified since it was written"* — i.e. a
   hand-edited policy is indistinguishable from corruption, which is exactly the property that check
   exists to have. Nothing in the store ever rewrites a `layer.json`, by design.
3. **And re-capturing to get a new ID orphans the children.** Every diff layer records its
   `parent` by ID. A new base ID leaves each of them pointing at a base that is no longer in the
   stack, so `check_provenance` warns on every compose and `--strict-parents` refuses outright. One
   policy edit would invalidate the provenance of every delta stacked on top of it.

**So the policy must live outside the hash.** The store has exactly two durable mutable artifacts:
`refs/<label>` (a single layer ID) and `recipes/<name>.json` (rewritable, and already the per-stack
object). The recipe is the natural home, and it needs no change to identity, no new directory and no
new file format — `build_plan` already takes `policies: dict[str, str]` keyed by volume name, and
already casefolds and strips a trailing colon so `Persist`, `Persist:` and `persist` all work.

```
amibuilder recipe new a1200 --layers base,patches,drivers --policy Persist=preserve
amibuilder compose --recipe a1200 --into card.hdf        # preserve applies, unprompted
```

Precedence becomes *CLI `--policy` → recipe → recorded default → `merge`*, so an explicit flag still
wins and nothing existing changes behaviour.

**Two known snags, both small and both easy to miss:**

- `write_recipe` builds its dict from a **fixed literal** (`store.py:~610`), so a `policies` key
  added by hand survives `read_recipe` and is then silently dropped by the next `recipe new`. The
  literal has to be extended, not just written through.
- `_stack_specs` in `commands/compose.py` reads only `recipe["layers"]`, so it must be taught to
  forward the policy map as well — otherwise the recipe stores a policy that compose never reads,
  which is worse than not storing it.

**The cost of the recipe approach, stated plainly:** the intent becomes per-stack rather than
travelling with the layer, so composing the same base by ID without the recipe silently loses it.
That is the right trade — the alternative sacrifices content-addressed identity, which is what makes
re-capture idempotent and dedup possible — but it means `compose --stack` should say when a volume's
policy came from a bare default rather than a stated intent. **A `preserve` that silently did not
apply is the one failure mode this feature must not have.**

An alternative worth a moment's thought if the recipe proves too narrow: a per-store
`policies.json` keyed by volume name, applying to every compose in that store. Simpler to reason
about and harder to forget, but volume names are not unique across drives — two different cards can
both have a `Work:` — so it would need care. Not recommended; recorded so it is not rediscovered
from scratch.

### Phase 5 — `zerofree` and `compact` ✅ **DONE 2026-08-24**

**Risk: writes to existing images** — met with an unusually strong self-check, now shipped and on
by default.

This is for everything that was *not* freshly composed: the existing image collection, and
byte-exact backups of cards that come back from a machine (composed images have zero free space by
construction). It ships the project's original motivation — back a card up at the size of its live
data, not its declared capacity.

- `zerofree` (`070a1cc`) — zeros unallocated blocks per the FFS bitmap. All RDB partitions by
  default, one via a selector. **Verify on by default** (below). Temp-copy-and-rename default,
  `--in-place` opt-in; `--dry-run` free-block counts; `--json`.
- `compact` (`67e6dd8`) — punches holes over zero runs via `F_PUNCHHOLE`. APFS-only and file-only
  (refuses a device with a clear message); reports bytes reclaimed via `du` before/after; safe by
  construction (only punches confirmed-zero pages), so no temp/verify needed.
- `zerofree --compact` (`20c4a2d`) — zero then punch in one pass, reusing the `compact` core.
- `verify` — **folded into the command, not a separate verb.** `zerofree` re-reads and hashes every
  file and re-counts the free blocks on the result (via `capture_container` + `diff`), and aborts
  keeping the original untouched if a single file changed or the free count moved. `--no-verify`
  opts out.

**Delivered against the original acceptance bar.** "`zerofree` must prove it changed no file
content" is met by the default-on verify above — it turns "I hope the bitmap parser is right" into
"the tool re-read the whole volume and proved it did no harm," and a mutation test (`_misparse`)
confirms a deliberately broken bitmap parse is caught and the original preserved. The `check` gate,
`--dry-run` and temp-copy-and-rename all shipped. **Lock was deferred** (agreed) — a single-user CLI
on a scratch image does not need it yet. Measured win: an image holding one 4 MiB deleted file
compressed 4.03 MiB → 10 KiB after `zerofree`, and `du` dropped 10240 KiB → 12 KiB after `compact`.

### Phase 6 — Shell and quality of life

- `amibuilder shell` — REPL with readline, history, tab completion on in-image paths
- `diff` between any two sources (image, layer, ADF, directory)
- `doctor`, `completion`

#### `amibuilder shell` — design note (agreed 2026-08-21; first cut shipped 2026-08-22)

> **Status:** built and merged — `commands/shell.py` + the shared `commands/transfer.py`, with
> `cp` and `mv` included (file-only, metadata-preserving). **Tab completion landed 2026-08-22**
> (pure `complete(state, line, text)` + a libedit-aware readline adapter; 34/34 guards
> mutation-proved), so the shell feature is now complete. The design below is what shipped.

**Requested and scoped with Dave 2026-08-21.** An interactive REPL pointed at one image, so
walking a drive and moving files around stops being "retype the 20-character spec on every
command." It is the proper fix for the retyping friction listed below, and it earns its keep on
`patch-work` clean-up work (deleting staged installers, shuffling files into place) that is
currently a chore.

**Build order, decided:** the shell ships **without tab completion first** — get the REPL, the
commands and the tests solid, *then* add completion as an orthogonal follow-up (it plugs into
`Volume.listdir`/`os.scandir` and touches no command logic, so it cannot destabilise the core).
And **`rm` lands first, as a standalone CLI command**, because it is needed regardless and the
shell simply reuses it — see the separate `rm` item; it is not "shell work."

**Invocation.** A subcommand, not a separate binary: `amibuilder shell card.hdf` (or
`card.hdf:Work`). Lives in `commands/shell.py`, wired into the same dispatch table as every other
command, opening the volume **writable** for the session.

**Where the shared core is — the part worth getting right.** The reusable core is `Volume` (+
`Container`), which is *already* what the `cmd_*` functions wrap; they are a presentation layer, and
the shell is a second one over the same `Volume`. The shell does **not** call `cmd_*(args, out)`
directly: those are argparse-shaped and open/close the image per call, whereas the shell holds one
volume open all session. One modest refactor makes the reuse real rather than aspirational — the
recursive get/put orchestration currently in `extract.py`/`write.py` (`_get_tree`, `_get_file`,
`_safe_name`, the cp plan/preflight) moves down into a shared `commands/transfer.py` that both the
CLI commands and the shell call. Resulting layers:

- `Volume` / `Container` — in-image operations only, never touches the host filesystem.
- `commands/transfer.py` — host↔image get/put orchestration and the "destination must not exist"
  rule, in one place.
- `cli.py` commands — argparse presentation.
- `commands/shell.py` — REPL presentation.

**Two current directories, FTP-style.** Shell state is `{volume, image_cwd, local_cwd}`.

| command | acts on | backed by |
|---|---|---|
| `cd` `ls` `rm` `mv` `cp` | `image_cwd` | `Volume` |
| `lcd` `lls` `lpwd` | `local_cwd` | `os` / `pathlib`, no `Volume` at all |
| `put LOCAL` | reads `local_cwd/LOCAL` → writes `image_cwd/LOCAL` | transfer helper |
| `get DISK` | reads `image_cwd/DISK` → writes `local_cwd/DISK` | transfer helper |
| `pwd` | prints `image_cwd` | — |

The two-cwd model is not cosmetic: it gives `put`/`get` a well-defined local side and removes the
"where does this come from / go to" ambiguity. `get` is the **only** verb that accepts a directory
(recursive), matching the CLI `get`; everything else is file-only in shell mode.

**Amiga-flavoured paths**, since the point is "feels like the CLI": `cd name` descends, `cd /` goes
up one level, `cd :` returns to the volume root, a leading `:` means volume-absolute. These are
in-volume paths only — no `card.hdf:` specs inside the shell, because the volume is already open.

**The non-negotiable safety rule: no overwrites, anywhere.** Writing a file — `put`, `get`, in-image
`cp`, or `mv` — errors and does nothing if the destination already exists. Reading or transferring a
source that does not exist errors too. One uniform "destination must not exist / source must exist"
check across every verb, no `--force` exposed in the shell. Overwrite is too dangerous here for now.
`rm` is file-only in the shell (no recursive directory delete), even though the standalone `rm`
command may later grow an opt-in for trees.

**The one correctness gotcha, designed in from the start:** the allocation bitmap is flushed to disk
only on `ADFSVolume.close()` (verified this session, notes G29). A CLI command opens/closes per call
so is always safe; the shell holds the volume open, so an unclean exit after a `put`/`rm`/`mv` would
leave file blocks written but the on-disk bitmap stale — latent corruption. Mitigation: **flush after
every mutating command** (and guarantee a clean close on `quit`, EOF and Ctrl-C). Cheap, but it must
be built in, not bolted on.

**The only genuinely new primitives** are `rm` and `mv`; everything else orchestrates existing code:

- `rm` — wraps amitools `node.delete(wipe=False, all=False, update_ts=False)`. Easy. Standalone
  first (below).
- `mv` — **copy-then-delete**, because amitools has *no* node-level rename (only volume `relabel`;
  `node.name` is read-only, set from the block on load — verified 2026-08-21). So `mv` rewrites the
  file at the new path and deletes the old. Consequences to handle: it needs transient free space for
  two copies, and it is the long pole — reasonable to make it a fast-follow after the first shell cut
  rather than block on it.

**Testability — the reason this is not a scary ask.** Structure it as a pure-ish
`dispatch(state, line) -> (output, new_state)` plus a dumb readline wrapper that only reads a line
and calls dispatch. Then test exactly like `test_write_cli.py`: build a scratch RDB fixture, feed a
list of command-line strings, assert on output and read-back. No TTY, no emulator, fully hermetic.
Only the readline loop needs a terminal, it is ~20 lines, and it carries no logic. Command-line
parsing must handle quoted paths (Amiga names contain spaces).

**Completion (done 2026-08-22, exactly as designed here):** stdlib `readline` `complete(text, state)` callback —
command names from a static list, in-image paths via `Volume.listdir`, local paths via `os.scandir`,
chosen by parsing the line buffer to know which argument is being completed. The completer function
is pure and unit-testable; the binding is not but is trivial. **macOS trap to remember:** Python's
`readline` is usually libedit there, so `parse_and_bind("tab: complete")` silently does nothing —
need `parse_and_bind("bind ^I rl_complete")`, gated on detecting libedit via `readline.__doc__`.

#### Everyday file handling is janky — make `cp`, `get` and `ls` pleasant

**Backlog item, requested 2026-08-20:** *"we need to make copies, gets, ls, etc. a little easier
ultimately. It works for now, but it's a little janky."*

Nothing here is broken; it is friction, and friction in the commands used most often costs more than
a missing feature. Recorded now while the specifics are fresh, deliberately **not** fixed in the same
pass that added `cp` — changing the shape of a command a day after shipping it is how a CLI ends up
with three ways to do everything.

What actually grates, roughly in order of how often it bites:

1. **The image spec has to be retyped on every command.** `amibuilder ls card.hdf:Work Utils`,
   then `amibuilder cp ./x card.hdf:Work --to Utils`, then `amibuilder get card.hdf:Work Utils ./out`
   — the same 20 characters, three times, and a typo in the volume name is a `NotFound` rather than
   an obvious mistake. Candidate fixes, in increasing order of ambition: an `AMIBUILDER_IMAGE`
   environment default; a `--image` flag that every command accepts; `amibuilder shell` (already
   planned above), which solves it properly by holding the image open and giving tab completion.
2. **`cp` and the read commands disagree about argument order**, which is defensible (Unix `cp` puts
   the destination last) and still means stopping to think each time. A `put` alias with the read
   commands' order — `put IMAGE PATH FILE...` — would let each person pick one and stay consistent.
3. **`--to` is a second place a path can live.** `cp ./x card.hdf:Work --to Utils/Patches` reads worse
   than a single destination would. The reason it exists is real (see the addressing rule), but a
   trailing-path form that is unambiguous *because the sources came first* may be possible:
   `cp ./x card.hdf:Work/Utils/Patches` cannot be confused with a volume selector, since anything
   after the first `/` is necessarily a path. Worth checking whether that holds for every spec shape,
   including `:0` and the MBR `0x76:1:2` forms.
4. **Wildcards** — ✅ **`get` done 2026-08-21.** `get card.hdf:Work 'S/*.lha'` now expands an
   image-side glob in the last path component (`transfer.is_glob` + `fnmatch`, case-insensitive as
   FFS is; no match is exit 3, an existing host file is skipped with a warning), and the interactive
   `shell` already globs `put`/`get`/`rm`. `cp` stays single on the image side — the host shell
   expands its sources — and a wildcard for the top-level `rm` is still open.
5. **Repeated `-p`.** `cp --to A/B -p` then `mkdir A/C -p`; creating parents is almost always what is
   wanted when a path is given explicitly. Consider making it the default and adding
   `--no-parents`, which inverts the current safety bias — worth doing deliberately rather than
   drifting into it.
6. **`ls` output is not pipeline-friendly** without `--json`. A `-1`/`--names-only` mode printing bare
   paths would make `for f in $(amibuilder ls ... -1)` work the way fingers expect.

Constraint on all of it: **the addressing rule stays.** A path inside an image is a separate
argument, or `card.hdf:Work` becomes ambiguous between a volume and a directory. Any ergonomic fix
has to survive that, which is what rules out the most obvious "just concatenate it" answers.

#### `utils/scripts/install-wb.sh` — a reproducible OS install

**Backlog item, requested 2026-08-20.** Generalises `scripts/install-base-3.2.sh`, which was written
for one install and hardcodes 3.2 throughout. Installing an OS is the one step of the pipeline that
cannot be automated, so the least it can do is not require rediscovering the emulator configuration
each time.

Layout:

```
images/floppy/workbench/3.2/*.adf     user-supplied, never committed
utils/scripts/install-wb.sh           the launcher
```

Behaviour: check the ADFs are present, and only then configure the machine. Takes the path to an
existing RDB/HDF, or `--new-disk` to create one first via `amibuilder init`.

**The media is never committed, and that is a licensing line, not a size one.** Workbench ADFs are
licensed software. `.gitignore` already excludes `*.adf` / `*.adz` at any depth, so `images/**` is
covered — with one portability trap noted below. The script's job is to *report clearly* which disks
are missing so a user can supply their own, never to fetch anything.

Decisions to make when building it, none of them settled:

- **Which disks are actually required.** A 3.2 install needs a specific subset (Install, Workbench,
  Locale, Extras, Fonts, Storage, Classes, and the right `ModulesA<model>`), and the model-specific
  one depends on the machine being emulated. So the check cannot be "are all 35 present" — it needs a
  small manifest per version, and `ModulesA1200` vs `ModulesA4000` selected from the target model.
  Refusing an install for a missing `Locale-GR.adf` would be obstructive; refusing for a missing
  `Workbench3.2.adf` is correct.
- **Version generality.** The `3.2` in the path implies `3.1` and `3.2.1` can sit alongside, so the
  script wants `--version` (defaulting to the newest directory present) rather than a constant. Disk
  names differ across versions, which is another reason the manifest is per-version data rather than
  logic.
- **What `--new-disk` defaults to.** Dave's layout is 1 Gi Workbench (bootable) / 2 Gi Work /
  1 Gi Persist, which is a sensible default but should stay overridable, and it must refuse to touch
  an existing file exactly as `init` does.
- **Kickstart selection** currently points at one ROM in `source-files-do-not-add-to-git/roms/`. It
  should follow the emulated model, and say which ROM it chose.

Carry forward from the 3.2 script, both learned the hard way:

- **`floppy_drive_speed = 0`.** Cycle-accurate floppy timing makes a dozen 880 KB disks into most of
  an hour. Turbo makes floppy operations immediate and is safe for an installer, which reads disks
  normally — it is copy-protected *games* that turbo breaks, by timing the drive. Confirmed to make a
  large difference in practice.
- **Tell the user about warp mode, and get the key right.** `Mod+W` toggles it, and on macOS `Mod`
  is **Cmd**, not F12; F12 alone merely opens the menu, which contains no warp entry. F11/F12 do work
  as alternative modifiers in a held chord. FS-UAE prints `Warp mode enabled` on screen, so the
  script should name that as the confirmation to look for.
- **`-ApplePersistenceIgnoreState YES`**, as every launch path in this project must.

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
  Write      cp · mkdir ✅ · protect · comment · touch · relabel          (Phase 4, additive)
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

**Superseded by §0, which is the live resume point.** Kept because it records what the original
ordering was and how it turned out.

1. ~~**Copy two or three representative images, one PiStorm card image, and a couple of ADFs somewhere
   safe** as the start of the golden corpus. Never test against originals.~~ Done differently, and
   better: fixtures are built from scratch by `test/helpers/images.py`, so no test depends on a real
   image at all. `source-files-do-not-add-to-git/` is treated as read-only.
2. ~~**Build Phase 1** and run it across every image, card and ADF.~~ Done.
3. ~~**Build Phase 2 and run the measurement experiment**: real base layer, one software install, one
   diff layer.~~ Done 2026-08-20 — and the answer was decisive: a real software install snapshots to
   **46.7 KiB**, against the 4 GiB HDF it came from. See §0.
4. ~~**Pick a name** (Q5) before the first commit.~~ `amibuilder`.

**What actually comes next**, in the order it matters:

1. ~~**A modifying/deleting diff layer.**~~ **Done 2026-08-21.** AmigaOS 3.2.3 captured as
   `patch-3.2.3`: 126 content changes, 36 new, **1 whiteout**, 1 case-only rename, 1 protection
   change, 686 files deduplicated. Composes cleanly on its own and stacked with `sysinfo-4.4`.
   Whiteouts and case folding both verified against real content — stats §5.
   **Decision resolved 2026-08-21 (Dave):** the tool does not decide what belongs in a layer — the
   caller does, by shaping the disk (or the capture) before the diff. So `patch-3.2.3` stays exactly
   as captured, installer included, because that is what the drive held. There is nothing to fix in
   the tool: `--exclude 'Work:Installers/**'` already produces the installer-free layer
   non-destructively (measured: identical layer, **1.40 MiB stored vs 17.34**, whiteout intact), and
   deleting the files on the disk first is equally valid for anyone who prefers the disk to be the
   single source of truth. The one practical wrinkle worth remembering: amibuilder has **no `rm`
   yet** (Phase 7), so "delete first" today means booting AmigaOS or using xdftool, whereas
   `--exclude` needs neither.

2. **Documentation restructure — README as the entry point, four linked docs. ✅ DONE 2026-08-22.**
   Shipped as a slim `README.md` (entry point + Features + use cases + links) plus `USAGE.md`,
   `STATISTICS.md` and `FAQ.md` at the repo root. The `docs/KIP-FFS-*.md` files are working notes,
   not user-facing docs, and were left exactly as they are — the reasoning trail. Two adjustments
   the plan below did not foresee: the FAQ's "why is there no `rm`" became "how do I delete files"
   (rm exists), and README Features + USAGE now cover `rm` and the `shell`. Every code example was
   run; links and anchors verified. The original design intent is preserved below for the record.

   - **`README.md`** — the top-level entry point. High-level: what the project is, the problem it
     solves, its intention. A **Features** section near the top listing the major capabilities.
     A few common use cases chosen to show *merit and functionality*, not exhaustive. Links out to
     each doc below. Keep it skimmable — anything that is setup, options or reference moves out.
   - **`USAGE.md`** — the how-to. venv setup, installing/invoking the tool, every command, options,
     modes, addressing, exit codes, the end-to-end workflow. Linked from the README. This is where
     the current README's "Commands"/"Development"/"Addressing" depth belongs.
   - **`STATISTICS.md`** — every measured number, moved out of the README and consolidated from
     `KIP-FFS-STATS.md`'s user-relevant parts (storage ratios, speed, fidelity, the whiteout and
     three-layer results). Linked from the README. Keep the "sample of one vs repeatable" honesty.
   - **`FAQ.md`** — pre-answered questions to cut confusion up front. Seed it with the real ones
     this project raises, e.g.: *Why is a 4 GiB image only ~1 MB on disk? Why does `ls -l` disagree
     with `xdftool` by an hour? Can it write to my real SD card / PiStorm? Why is there no `rm`?
     What happens to files I delete — do they leave the layer? Why does `cp` take the image last?
     Do I need Amiga ROMs to run the tests? Is my Workbench install going to be committed to git?
     What's the difference between `snap create` and `snap diff`? Why FFS-only, not PFS3/SFS?*
     Link from the README.

   Constraints: the addressing rule and the licensing lines (ROMs, ADFs, real images are never
   committed) must survive the move, stated wherever a user first meets them. Every code example
   should be one that has actually been run, as the current README's were.

3. **`rm` — standalone CLI command, ✅ DONE 2026-08-21.** `Volume.remove()` wrapping amitools
   `node.delete(wipe=False)`, plus `cmd_rm` (validates the whole path list before deleting any, so a
   typo removes nothing; `-r` for directories; `-n`; `--json`). Guards mutation-proved in
   `utils/scripts/mutate-rm-guards.py`.
4. **`amibuilder shell` — first cut, ✅ DONE 2026-08-22 (no tab completion yet).** Interactive REPL in
   `commands/shell.py` over one open `Volume`, FTP-style two-cwd model. The get/put orchestration was
   refactored down into `commands/transfer.py` (the shared host↔image core: `extract_path`,
   `put_file`, `copy_in_image`), which both the CLI `get`/`cp` and the shell call — the reuse is real,
   not aspirational. Commands: `pwd cd ls` (+`dir`), `lpwd lcd lls`, `put get`, `cp mv rm` (+ aliases),
   `help quit`. `cp` and `mv` shipped in this cut (not deferred) — both file-only, metadata-preserving
   (protection/comment/mtime carried across), `mv` = copy-then-delete. No overwrites anywhere, `rm`
   file-only, bitmap flushed after every mutating command. **Tab completion landed 2026-08-22** --
   a pure `complete(state, line, text)` (command names, in-image paths via `Volume.listdir`, host
   paths via `os.scandir`, routed by which argument is under the cursor) plus a libedit-aware
   readline adapter (`bind ^I rl_complete` on macOS). Guards mutation-proved in
   `utils/scripts/mutate-shell-guards.py` (34/34 killed). The shell feature is now complete.

   Two things learned building it, worth keeping: (a) **"flush" means the bitmap, not the tree.**
   amitools writes tree/data/header blocks straight through (it seeks constantly, and Python's
   `BufferedRandom` flushes its write buffer on seek), so a mid-session read sees a change with or
   without `flush()`; the *only* thing left stale without a flush is the allocation bitmap
   (`ADFSBitmap.write()` runs on close/flush), which `check` catches (exit 6) — so the flush tests
   assert `check` passes mid-session, not that the tree changed. (b) **short substring assertions can
   match the pytest tmp path**, which is named after the test; `Volume` errors echo the source label
   (the host path), so `"same" in output` for `test_..._the_same_path...` matched the *path*, not the
   guard — the mutation harness caught it as a vacuous guard. Assert the full message phrase.
5. **`snap create`/`diff` from a host directory — ✅ DONE 2026-08-21.** The proper fix for staging
   that `cp` only worked around: `layers/hostdir.py` (`DirectoryVolume`) plus the shared
   `layers/uaem.py` sidecar parser feed `capture_container`; `--volume` names the recorded volume.
   Commit `3d388fa`.
6. **Recorded policy intent — ✅ DONE 2026-08-21.** `recipe new --policy VOLUME=POLICY` stores it in
   the recipe, outside the identity hash (`set_policy()` was correctly *not* used); precedence is CLI
   → recipe → recorded default → `merge`, and `compose` reports which policies were set explicitly and
   warns on an unmatched override. Commit `7353c30`.
7. **Image-side wildcards for `get` — ✅ DONE 2026-08-21.** `get card.hdf:Work 'S/*.prefs'` expands
   the last path component (`transfer.is_glob` + `fnmatch`); no match is exit 3, an existing host file
   is skipped with a warning. `cp` stays single; a top-level `rm` glob is still open. Commit
   `87fefdc`.
8. **`format` an existing drive's partition — ✅ DONE 2026-08-21.** `format card.hdf:Work` reuses
   `init`'s root-block writer, defaults name and DosType from the RDB, and refuses a `--dos-type`
   change that would desync the RDB. Test- and mutation-checked; not yet booted on real hardware.
   Commit `6800df4`.
9. **`diff` between any two sources — ✅ DONE 2026-08-22.** A read-only top-level `diff` that
   captures two sources through a `HashOnlyBlobStore` and runs `capture.diff` — `snap diff`'s engine
   pointed at two arbitrary sources instead of a drive against a stored layer. `commands/compare.py`;
   path/volume alignment (auto, `--by` to force, `--by path` refused on a multi-volume source); an
   RDB partition selector narrows to one volume. Commit `e57969f`.
10. **Real hardware.** ZuluSCSI and PiStorm/Emu68 have still seen nothing, and the MBR `0x76` device
    target waits on a card to test against.
