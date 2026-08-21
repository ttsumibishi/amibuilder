# amibuilder

File-level access, layered snapshots and image composition for Amiga hard disk images.

**Status: inspection, snapshots, composition and image creation all work.** Sixteen commands
are installed as `amibuilder`, backed by a 1298-test suite — of which **63 boot a real
AmigaOS 3.2** under FS-UAE and check the result with AmigaDOS's own tools. Writing host files
into an image works (`cp`, `mkdir`); rename and delete do not exist yet, by design.
See [Roadmap](#roadmap).

**The design claim is measured, not projected.** A real SysInfo 4.4 install onto a 4 GiB
AmigaOS 3.2 drive captures as a **46.7 KiB** diff layer — 89,830× smaller than the image it
came from — and composing that layer back produces a drive real AmigaOS boots. Numbers and
method in [`docs/KIP-FFS-STATS.md`](docs/KIP-FFS-STATS.md).

```console
$ amibuilder info card.hdf
Image:             card.hdf
Kind:              RDB whole-disk image (RigidDiskBlock + partitions)
Size:              32Mi  (33554432 bytes)
Geometry:          2048 cyl x 1 head x 32 sec, 65536 blocks of 512
Partitions:        2

Partitions:
  #  device  volume     dostype               cyls    size  flags
  -  ------  ---------  ----------------  --------  ------  -------
  0  DH0     Workbench  DOS\3 (FFS+intl)     1-640    10Mi  boot(0)
  1  DH1     Work       DOS\3 (FFS+intl)  641-2047  22.0Mi  -
```

---

## What this is for

Restoring an Amiga SD card to a known-good configuration currently means either copying
multi-gigabyte images to slow media, or reinstalling everything from scratch. Both are
slow, and repeated whole-image writes wear the card.

amibuilder treats an Amiga install the way Docker treats a container image: a **base layer**
holding AmigaOS at a known patch level, plus **diff layers** for games, utilities and
personal configuration, composited on demand into whatever format the target machine
wants.

```
base-3.2  +  patch-3.2.x  +  games-common  +  my-configs
      │
      ├── compose --into card.hda --format rdb       # ZuluSCSI          ✅
      ├── compose --into test.hdf --format plain     # WinUAE / FS-UAE   ✅
      ├── compose --into ./wb/    --format dir       # FS-UAE dir drive  ✅
      └── compose --into /dev/rdisk4:0x76:1 --device # PiStorm / Emu68   not yet
```

Images become disposable build artefacts. The layer store is what gets backed up.

The PiStorm target is **deliberately last**. Reading an RDB out of a `0x76` slot is verified
working, and the write path is the one that can destroy an Emu68 install on a card that is one
keystroke away from `rdisk2` — so it waits until there is a real card to test against. The
bytes are identical to the RDB image the other targets already produce, so nothing else is
blocked on it.

## The finding that shaped the design

Deleting files in FFS reclaims almost nothing at the image level. Measured on a 1000 MiB
image holding 236 MiB of data, after deleting four fifths of the files:

| | Before | After |
|---|---|---|
| FFS reports used | 236 MiB | 46 MiB |
| Compressed image size | 235,302,718 B | 235,279,723 B |
| On-disk (APFS sparse) | 230 MB | 230 MB |

**0.01% reclaimed.** FFS clears bitmap bits and unlinks the header; it never touches the
data blocks. Every byte ever written to a partition is still there, in blocks marked
free, defeating both compression and deduplication.

Two consequences drive the design: a `zerofree` pass makes existing images cheap to
store, and composed images avoid the problem entirely because they are built into freshly
formatted volumes.

Full detail in [`docs/KIP-FFS-NOTES.md`](docs/KIP-FFS-NOTES.md) section 1.

## Target hardware

| Target | Format it needs |
|---|---|
| **ZuluSCSI** | Raw whole-disk image with an RDB, on a FAT32/exFAT card |
| **PiStorm / Emu68** | Cannot mount an HDF at all. Wants the bytes in an MBR partition of type `0x76` on the microSD card, each holding its own RDB |
| **WinUAE / FS-UAE** | RDB image, plain HDF, or a host directory |

An **RDB whole-disk image is the universal interchange format**: it works on ZuluSCSI,
works in both emulators, and is byte-compatible with the contents of a PiStorm `0x76`
partition. A plain HDF only works under emulation.

Reading an RDB embedded in a `0x76` partition is verified working via a byte-range slice
view, with no changes needed to amitools.

## Addressing

One argument names any of six things. This is the piece worth learning:

| Spec | Means |
|---|---|
| `card.hdf` | Whole image — an RDB's first partition, or a plain HDF's volume |
| `card.hdf:0` | RDB partition by index |
| `card.hdf:DH0` | By AmigaDOS device name |
| `card.hdf:Workbench` | By volume name |
| `disk.adf` | Floppy image (`.adf`, `.adz`, `.adf.gz`) |
| `/dev/rdisk4` | Raw device — requires `--device` |
| `/dev/rdisk4:0x76:1` | MBR primary slot 1, holding its own RDB (PiStorm / Emu68) |
| `/dev/rdisk4:0x76:1:2` | Partition 2 inside that slot |

Paths *inside* an image are always a separate argument, never part of the spec, so a
partition named `Work` cannot be confused with a directory named `Work`.

Selecting by volume name is amibuilder's own addition — amitools resolves device names and
indexes only, so `Workbench` is matched by mounting each partition on a miss.

## The workflow, end to end

Every command below was run in this order to check this section; nothing here is aspirational.

```bash
# 1. Make a drive. Mounts on a real Amiga with no HDToolBox step.
amibuilder init card.hdf --size 4G \
  --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest

# 2. Install AmigaOS onto it (once, interactively, under an emulator),
#    or put files on directly:
amibuilder cp -r ./stuff card.hdf:Work --to Utils -p

# 3. Capture it as a base layer. Read-only: the image is not touched.
amibuilder snap create card.hdf --label base-3.2

# 4. Install something, then capture only what changed.
amibuilder snap diff card.hdf --parent base-3.2 --label sysinfo-4.4
amibuilder snap review sysinfo-4.4 --explain      # see it before keeping it
amibuilder snap commit sysinfo-4.4

# 5. Name a stack, and build a drive from it.
amibuilder recipe new a1200 --layers base-3.2,sysinfo-4.4
amibuilder compose --recipe a1200 --into fresh.hdf
```

`snap create` produces a layer directly. `snap diff` produces a **candidate** instead, so
there is a review step between "here is what changed" and "keep this forever" — which is where
you drop the noise a boot left behind.

Composition verifies itself by default, by re-reading the image it just wrote:

```console
verifying
  Workbench: 3 entr(ies) match
  Work: 1 entr(ies) match

verified: the image holds exactly what was composed (4 entr(ies))
```

That is deliberately in the command rather than only in the test suite. A passing test proves
the code worked on a fixture; it says nothing about the card written thirty seconds ago.

## Commands

Every command supports `--json`, and the JSON shape is part of the interface rather than a
pretty-printed afterthought.

### Inspect and extract

| Command | Does |
|---|---|
| `info` | Image kind, geometry, partition table, volume usage |
| `partitions` | RDB or MBR partition table; `-v` adds mask, max-transfer, buffers |
| `check` | Full 5-step structural validation, per partition |
| `ls` | Directory listing; `-l` long form, `-R` recursive |
| `tree` | Indented hierarchy; `--depth N` |
| `find` | By `--name`, `--path`, `--type`, `--min-size`, `--max-size`, `--comment` |
| `du` | Apparent *and* on-disk size, exposing block-rounding overhead |
| `cat` | File contents to stdout; `--text` normalises Amiga CR line endings |
| `hexdump` | A file, or `--block N` raw with block identification — works on volumes that will not mount |
| `get` | Extract a file or subtree; `--dry-run`, `--force`, `--preserve-times` |

### Snapshots and composition

| Command | Does |
|---|---|
| `snap create` | Capture a whole drive as a base layer, RDB layout and boot blocks included |
| `snap diff` | Capture and compare against a parent, recording only what changed |
| `snap review` | Inspect a candidate before committing it; `--drop` / `--keep` by glob |
| `snap commit` `snap discard` | Turn a candidate into a layer, or throw it away |
| `snap ls` `snap show` | List layers, or show one's metadata, drive record and contents |
| `snap verify` `snap gc` `snap rm` | Integrity check, unreferenced-blob collection, removal |
| `recipe new` `recipe ls` `recipe show` `recipe rm` | Name an ordered stack of layers |
| `compose` | Build a drive from a stack: `--format rdb\|plain\|dir`, `--dry-run`, per-volume `--policy`, verification on by default |

A capture never writes to the image it reads. Composition writes into freshly formatted
volumes, which is what makes deletion-by-omission safe: there is no delete operation to get
wrong.

### Creating and writing

| Command | Does |
|---|---|
| `init` | Create a new image: `--size 1-1000M` / `1-16G`, repeatable `--partition NAME=SIZE[,bootable][,dostype=…]`, or `--plain` for a single-volume HDF |
| `cp` | Copy host files or directories in; `-r`, `-f`, `--to`, `-p`, `-n`, `--preserve-times`, `--protect`, `--comment` |
| `mkdir` | Create directories; `-p` for parents, `-n` for a dry run |

```bash
amibuilder init card.hdf --size 4G --partition Workbench=1G,bootable --partition Work=rest
amibuilder cp ./lha ./patch.lha card.hdf:Work --to Utils -p
amibuilder mkdir card.hdf:Work Utils/Patches -p
```

`init` produces a drive real AmigaOS mounts with **no HDToolBox step**, verified under
emulation. `cp` takes the image **last**, matching Unix `cp`; every read command takes it
first. The in-image path stays a separate argument (`--to`) in both cases, for the reason
under [Addressing](#addressing).

Exit codes are stable and distinct so scripts can branch on them: `2` usage/addressing,
`3` path not found, `4` unsupported filesystem, `5` malformed image or refused write,
`6` validation failure, `7` device refused.

### Still to come

| Group | Commands | Phase |
|---|---|---|
| Write | `protect` `comment` `touch` `relabel`, ADF injection, `merge` policy, `snap create` from a host directory | 4 |
| Space | `zerofree` `compact` | 5 |
| Compare | `diff` | 6 |
| Session | `shell` | 6 |
| Destructive | `rm` `mv` `sync --delete` | 7, optional |

Ordered by risk: deletion and rename come last and may never be needed. Two known gaps are
deliberate — the PiStorm MBR `0x76` **device** target waits until there is a real card to
test against, since it is the one that can destroy an Emu68 install, and `format` for an
existing drive is not written.

## Safety

Raw device access is gated, because the failure mode is unrecoverable — on a PiStorm card
the non-`0x76` MBR slots hold Emu68 itself, and `rdisk2` versus `rdisk3` is one keystroke.

- Touching any device requires `--device`; refusals print `diskutil list` so the right
  identifier is visible
- The Mac's boot disk is refused outright, read or write, regardless of flags
- Addressing a slot whose MBR type is not `0x76` is refused by name
- Writes require typing the device identifier, not `y`; without a TTY they require `--yes`
- Byte ranges are served through a slice view that clamps writes to the partition, so an
  offset bug cannot reach past it
- `--dry-run` opens read-only, so a dry run against a device never asks to confirm a write
  and cannot perform one

Every mutating command pre-flights the **whole** operation before writing any of it —
filename lengths, illegal characters, comment lengths, free space, collisions. A refusal
happens while the target is untouched, rather than aborting halfway and leaving a
partially-populated volume.

## Documentation

| Document | Contents |
|---|---|
| [`docs/KIP-FFS-NOTES.md`](docs/KIP-FFS-NOTES.md) | Verified findings: FFS on-disk format reference, measured performance, amitools assessment, target hardware, 30 numbered gotchas |
| [`docs/KIP-FFS-STATS.md`](docs/KIP-FFS-STATS.md) | Every measurement, with method: storage ratios, speed, fidelity against real AmigaOS, and what is still a sample of one |
| [`docs/KIP-FFS-LAYERS.md`](docs/KIP-FFS-LAYERS.md) | Layered snapshot design: store layout, manifests, diff semantics, per-volume policy, composition |
| [`docs/KIP-FFS-PLAN.md`](docs/KIP-FFS-PLAN.md) | Build order, design rules, testing strategy, open questions, risks. Section 0 is the live resume point |
| [`docs/KIP-FFS-IDEAS.md`](docs/KIP-FFS-IDEAS.md) | Option analysis: language choice, command surface, ideas considered and rejected |
| [`docs/KIP-FFS-INSTALLERS.md`](docs/KIP-FFS-INSTALLERS.md) | Feasibility of deriving layers from Amiga `Installer` scripts, measured against the AmigaOS 3.2 base installer |

Notes are labelled **VERIFIED** or **UNVERIFIED** throughout, and the stats document keeps a
list of what is a repeatable measurement versus a sample of one. Two of its own predictions
have been falsified by later measurement and are **left visible rather than edited out**,
because the pattern is worth remembering: a plausible mechanism is not a measurement.

## Built on amitools

amibuilder uses [amitools](https://github.com/cnvogelg/amitools) for the FFS/OFS/RDB/ADF
implementation rather than reimplementing it. It handles DOS0–DOS7 including
international hashing and directory caches, bitmap extension blocks at multi-GB scale,
RDB partitioning, raw device access, and metadata round-trips — and it has been used by
the Amiga community for over a decade. Writing an FFS *writer* from scratch would put
all the risk in the least-reviewed code, against disks that cannot be replaced.

**amitools is GPL-2.0-or-later, so amibuilder is too.** This is a private utility that may be
published later; the FFS layer sits behind a narrow interface so a permissive rewrite
stays possible against an existing test corpus.

Twenty regression tests (`pytest -m regression`) pin the amitools bugs, limitations and traps
found so far, so a version bump cannot change behaviour silently. Each is absorbed inside
`amibuilder/volume.py` or `amibuilder/image.py` and documented where the workaround lives.
The ones that would have caused silent data errors:

- **`FileName.__str__` and `__repr__` both raise `TypeError`** — they return an `FSString`,
  so `str(node.get_file_name())` crashes. The working accessor is `get_unicode_name()`.
- **`get_blocks(with_data=True)` returns no data blocks on FFS volumes** — only the OFS
  branch of `ADFSFile.read()` populates `data_blks`, so a 200 KB file is under-reported by
  391 blocks. `data_blk_nums` is correct and is used instead.
- **`amiga_epoch` is derived with `time.mktime`**, making it depend on the host's January
  UTC offset. See [Timestamps](#timestamps).
- **`BlkDevFactory.open()` on an RDB image silently returns partition 0**, not the disk, so
  its block count is the partition's. Enumeration drives `RawBlockDevice` + `RDisk` directly.
- **`dos_env.block_size` counts longwords, not bytes** — a 512-byte block reports 128.
- **`ADFSDir.create_dir` is not recursive**, and creating a child before its parent raises
  `Invalid Parent Directory` rather than creating the chain.
- **Writing to an existing path raises rather than replacing**, so an overwrite means
  deleting first — and every create must pass `update_ts=False`, or amitools restamps the
  parent directory through the broken epoch above.
- **`read_only` lives in a different place on every block-device class** — on `ImageFile` for
  HDF and raw, on the device itself for ADF, and nowhere at all on the partition wrapper an
  RDB partition is mounted through. So the obvious `getattr(blkdev, "read_only", True)`
  reports every RDB partition as read-only.
- **`BlkDevTools` is in `amitools.util`, not `amitools.fs.blkdev`** — where it looks like it
  belongs, and where amitools' own blkdev modules import it *from elsewhere*. Getting it
  wrong kills raw-device support entirely, at construction.

## Timestamps

amibuilder renders Amiga timestamps from the on-disk `(days, mins, ticks)` triple and never
converts through Unix time. **This means `amibuilder ls -l` can disagree with
`xdftool list` by an hour or more, and amibuilder is the side that matches the bytes.**

AmigaDOS stores naive local wall clock and does no timezone arithmetic. amitools builds its
epoch constant with `time.mktime`, which interprets 1978-01-01 as *local* time — 252489600
in UTC-8 against 252460800 for true UTC. Measured on 2026-08-17 in US/Pacific: the wall
clock read 16:48:59, and the bytes amitools wrote decode to **15:48:59**, an hour adrift
because January's -08:00 was applied during August's -07:00. amitools then displays
16:48:59 by re-applying the same error in reverse, so it is self-consistent while
disagreeing with the disk.

An image written under one timezone also shifts when read under another. This is why
timestamps are excluded from the default layer-diff comparison key — including them would
report differences that depend only on where an image was written.

`--json` emits the naive ISO form with no timezone designator, plus the raw
`modified_amiga_secs` and `modified_ticks`, which are the portable ground truth.

## Development

Requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
VIRTUAL_ENV=.venv uv pip install -e '.[dev]'
```

That puts `amibuilder` on the venv's path:

```bash
.venv/bin/amibuilder --help
```

### Running the tests

**Run it in two halves.** A single combined run has repeatedly hung:

```bash
.venv/bin/python -m pytest -q -m "not emulator"      # 1235 tests, ~10.5 min
.venv/bin/python -m pytest -q test/test_emulator.py   # 96 tests, ~1.6 min
```

Useful subsets when iterating on one area:

```bash
.venv/bin/python -m pytest -m "not slow"      # skip multi-GB images
.venv/bin/python -m pytest -m regression      # just the amitools pins
.venv/bin/python -m pytest test/test_write_cli.py     # cp and mkdir
```

1298 tests in total. 63 carry the `emulator` mark and need FS-UAE plus a Kickstart ROM; the
other 1235 need neither, because every fixture is built from scratch. `test_emulator.py` holds
97 tests — the 63 marked ones plus 34 harness-logic tests that run in the first half — which is
why the two halves do not add up to the total.

Tests needing licensed source material (the AmigaOS 3.2 CD, a Kickstart ROM) skip cleanly when
it is absent, and the emulator tests run with the window hidden so they do not steal keyboard
focus.

Source material for investigation goes in `source-files-do-not-add-to-git/`, which is
gitignored. Drop an ISO, ADF or disk image there and the relevant tests pick it up.

| Module | Covers |
|---|---|
| `test_cli.py` | Read commands end to end: output, `--json` shape, and exit codes |
| `test_write_cli.py` | `cp` and `mkdir`: preflight refusals leaving the volume untouched, timestamp bytes, overwrite rules, and a read-back through `xdftool` rather than our own reader |
| `test_image.py` | Container detection, partition enumeration and selection, MBR slicing, volume access, raw-device size |
| `test_addressing.py` | Source-spec parsing, including filenames containing `:` and devices with selectors |
| `test_blocks.py` | The package's block layer, pinned against the independent oracle |
| `test_timestamps.py` | The Amiga-epoch handling and its deliberate divergence from amitools |
| `test_ffs_structure.py` | Block offsets, four checksum conventions, hash function, bitmap layout, all asserted against raw bytes |
| `test_rdb.py` | RDB and partition blocks, geometry, cylinder-0 reservation, partition isolation, sparse creation |
| `test_init_parsing.py` `test_init_layout.py` `test_init_cli.py` | `init`: size and partition grammar, cylinder arithmetic, and the command surface |
| `test_layers_manifest.py` `test_layers_blobs.py` `test_layers_store.py` `test_layers_drive.py` | The layer store: canonical manifest bytes, content addressing, refs and integrity, the RDB drive record |
| `test_layers_capture.py` `test_layers_cli.py` | Capture and diff, and the `snap` command surface |
| `test_layers_compose_plan.py` `test_layers_compose_cli.py` `test_layers_targets.py` | Composition: planning and preflight, the CLI, and all three write targets |
| `test_layers_roundtrip.py` | Capture → compose → re-capture producing an identical layer ID |
| `test_dead_space.py` | The deletion finding, and a `zerofree` prototype proving it reclaims space without altering files |
| `test_sparse.py` | `F_PUNCHHOLE`, and why zeroing alone does not free disk space |
| `test_metadata.py` | `.xdfmeta` and `.uaem` round-trips, protection bits, full byte-identical tree round-trip |
| `test_adf.py` | ADF browsing, gzip, non-DOS refusal, multi-ADF staging into an RDB partition |
| `test_mbr_slice.py` | The PiStorm `0x76` path, including that writes cannot escape the partition |
| `test_amitools_regressions.py` | Known amitools bugs and quirks, pinned |
| `test_amigados_parsing.py` | Parsers for `Info` and `List` output, so a real Amiga's view can be compared to ours |
| `test_installer_scripts.py` | Installer-script tokeniser and feasibility analyser; real AmigaOS 3.2 analysis skipped unless the CD image is present |
| `test_render.py` | Output formatting, size parsing and the `--json` envelope |
| `test_emulator.py` | FS-UAE harness logic and its failure outcomes, plus the boot tests: composed drives, `init`-created drives, `cp`-written drives and partition-granular restore |

Structural tests deliberately implement checksum and offset maths **independently** of
amitools, in `test/helpers/blocks.py`. A test that compared amitools to its own constants
would still pass if amitools changed its layout. `amibuilder/blocks.py` is therefore a
second implementation rather than an import of the helper, and `test_blocks.py` pins the two
together so the package cannot drift from the oracle.

The same principle applies to writes, one level up: validating our output with our own reader
cannot catch a wrong assumption the two share. A hash bucket computed consistently but
incorrectly would round-trip perfectly and be unreadable to anything else. So written volumes
are also read back by `xdftool` and validated by `xdfscan` as separate processes — and, for
the cases that matter most, by a real AmigaOS.

### Mutation testing

A test that cannot fail is worse than no test, because it reads as coverage. Guards on the
riskier paths are therefore checked by mutating the code they protect and requiring them to go
red:

```bash
.venv/bin/python utils/scripts/mutate-write-guards.py
```

21 mutations over the `cp`/`mkdir` guards. Its first run found **five** that survived, each a
different way to write a convincing non-test: an assertion measuring the wrong quantity
entirely, two that were unobservable rather than uncaught, one whose branch the fixture could
never reach, and one matching an error message loosely enough that a *later* failure satisfied
it. All five are written up in the stats document rather than quietly fixed.

### Enabling the emulator tests

Booting an image under a real AmigaOS is the only validation that is not ultimately
circular — everything else checks amitools against amitools. It needs software that
cannot be bundled, so it is opt-in:

```bash
export AMIBUILDER_FSUAE=/Applications/FS-UAE.app/Contents/MacOS/fs-uae
export AMIBUILDER_KICKSTART=$HOME/Amiga/roms/kick31.rom
export AMIBUILDER_BOOT_IMAGE=$HOME/Amiga/images/workbench-3.2.hdf
```

Without these the boot tests skip and the rest of the suite still runs. See
[`test/emulator/README.md`](test/emulator/README.md) for how validation works.

## Roadmap

- [x] Verify the FFS on-disk format and correct a widely-circulated wrong offset table
- [x] Measure amitools performance at realistic scale
- [x] Establish target image formats for ZuluSCSI, PiStorm and the emulators
- [x] Prove the PiStorm MBR `0x76` slice path
- [x] Verify ADF staging into an RDB partition
- [x] Design the layered snapshot model
- [x] Regression test suite for the dependency
- [x] Assess installer-script automation against the AmigaOS 3.2 base installer
- [x] FS-UAE harness verified end to end
- [x] Phase 1: read-only inspection
- [x] Phase 2: layer capture
- [x] Phase 3: composition — all three image/directory targets, verification on by default
- [x] `init`: create a drive real AmigaOS mounts with no HDToolBox step
- [x] Measure the design claim against a real AmigaOS 3.2 install
- [x] Phase 4a: `cp` and `mkdir`, verified against real AmigaOS
- [ ] Phase 4b: ADF injection, `merge` policy, `snap create` from a host directory
- [ ] A modifying/deleting diff layer — every diff measured so far is purely additive, so
      whiteouts have never been exercised on real content
- [ ] Phase 5: `zerofree` and `compact`
- [ ] Real hardware: ZuluSCSI and PiStorm/Emu68 have still seen nothing

## Licence

GPL-2.0-or-later, inherited from amitools.
