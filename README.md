# amibuilder

File-level access, layered snapshots and image composition for Amiga hard disk images.

**Status: Phase 1 complete — read-only inspection works.** Ten commands are implemented
and installed as `amibuilder`, backed by a 403-test suite. Nothing writes to an image yet.
See [Roadmap](#roadmap).

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
base-os-3.2.3  +  games-common  +  my-configs
        │
        ├── compose --into card.hda --format rdb          # ZuluSCSI
        ├── compose --into /dev/rdisk4 --target-partition 0x76:1   # PiStorm
        ├── compose --into test.hdf --format plain        # WinUAE / FS-UAE
        └── compose --into ./wb/ --format dir             # FS-UAE directory drive
```

Images become disposable build artefacts. The layer store is what gets backed up.

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

## Commands

Phase 1 is implemented. Every command supports `--json`, and the JSON shape is part of the
interface rather than a pretty-printed afterthought.

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

Exit codes are stable and distinct so scripts can branch on them: `2` usage/addressing,
`3` path not found, `4` unsupported filesystem, `5` malformed image, `6` validation
failure, `7` device refused.

### Still to come

| Group | Commands | Phase |
|---|---|---|
| Layers | `snap create` `snap create-from-adf` `snap diff` `snap review` `snap commit` `snap ls` `snap show` `snap verify` `snap gc` | 2 |
| Recipes | `recipe new` `recipe ls` `recipe show` | 2 |
| Build | `compose` `init` `format` | 3 |
| Write | `mkdir` `put` `cp` `protect` `comment` `touch` `relabel` | 4 |
| Space | `zerofree` `compact` `verify` | 5 |
| Compare | `diff` | 6 |
| Session | `shell` | 6 |
| Destructive | `rm` `mv` `sync --delete` | 7, optional |

Ordered by risk. Capture is read-only; composition writes only to fresh volumes;
deletion and rename come last and may never be needed.

## Safety

Raw device access is gated, because the failure mode is unrecoverable — on a PiStorm card
the non-`0x76` MBR slots hold Emu68 itself, and `rdisk2` versus `rdisk3` is one keystroke.

- Touching any device requires `--device`; refusals print `diskutil list` so the right
  identifier is visible
- The Mac's boot disk is refused outright, read or write, regardless of flags
- Addressing a slot whose MBR type is not `0x76` is refused by name
- Writes (Phase 4+) require typing the device identifier, not `y`; without a TTY they
  require `--yes`
- Byte ranges are served through a slice view that clamps writes to the partition, so an
  offset bug cannot reach past it

## Documentation

| Document | Contents |
|---|---|
| [`docs/KIP-FFS-NOTES.md`](docs/KIP-FFS-NOTES.md) | Verified findings: FFS on-disk format reference, measured performance, amitools assessment, target hardware, 22 numbered gotchas |
| [`docs/KIP-FFS-LAYERS.md`](docs/KIP-FFS-LAYERS.md) | Layered snapshot design: store layout, manifests, diff semantics, per-volume policy, composition |
| [`docs/KIP-FFS-PLAN.md`](docs/KIP-FFS-PLAN.md) | Build order, design rules, testing strategy, open questions, risks |
| [`docs/KIP-FFS-IDEAS.md`](docs/KIP-FFS-IDEAS.md) | Option analysis: language choice, command surface, ideas considered and rejected |
| [`docs/KIP-FFS-INSTALLERS.md`](docs/KIP-FFS-INSTALLERS.md) | Feasibility of deriving layers from Amiga `Installer` scripts, measured against the AmigaOS 3.2 base installer |

Notes are labelled **VERIFIED** or **UNVERIFIED** throughout. The FS-UAE harness now boots
a real AmigaOS 3.2 install disk, so the claims that depend on a genuine Amiga can be
checked rather than inferred from amitools alone.

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

Nine amitools bugs, limitations and traps found during investigation are pinned by
regression tests (`pytest -m regression`) so a version bump cannot change behaviour
silently. Each is absorbed inside `amibuilder/volume.py` or `amibuilder/image.py` and
documented where the workaround lives. The ones that would have caused silent data errors:

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

```bash
.venv/bin/python -m pytest                    # everything
.venv/bin/python -m pytest -m "not slow"      # skip multi-GB images
.venv/bin/python -m pytest -m regression      # just the amitools pins
```

413 tests: 405 in about 3.5 minutes with `-m "not emulator"`, plus the emulator file in
about 43 seconds. They need no
Amiga ROMs or images — every fixture is built from scratch. Tests that need licensed source
material (the AmigaOS 3.2 CD, a Kickstart ROM) skip cleanly when it is absent.

Source material for investigation goes in `source-files-do-not-add-to-git/`, which is
gitignored. Drop an ISO, ADF or disk image there and the relevant tests pick it up.

| Module | Covers |
|---|---|
| `test_cli.py` | Every command end to end: output, `--json` shape, and exit codes |
| `test_image.py` | Container detection, partition enumeration and selection, MBR slicing, volume access |
| `test_addressing.py` | Source-spec parsing, including filenames containing `:` and devices with selectors |
| `test_blocks.py` | The package's block layer, pinned against the independent oracle |
| `test_timestamps.py` | The Amiga-epoch handling and its deliberate divergence from amitools |
| `test_ffs_structure.py` | Block offsets, four checksum conventions, hash function, bitmap layout, all asserted against raw bytes |
| `test_rdb.py` | RDB and partition blocks, geometry, cylinder-0 reservation, partition isolation, sparse creation |
| `test_dead_space.py` | The deletion finding, and a `zerofree` prototype proving it reclaims space without altering files |
| `test_sparse.py` | `F_PUNCHHOLE`, and why zeroing alone does not free disk space |
| `test_metadata.py` | `.xdfmeta` and `.uaem` round-trips, protection bits, full byte-identical tree round-trip |
| `test_adf.py` | ADF browsing, gzip, non-DOS refusal, multi-ADF staging into an RDB partition |
| `test_mbr_slice.py` | The PiStorm `0x76` path, including that writes cannot escape the partition |
| `test_amitools_regressions.py` | Known amitools bugs and quirks, pinned |
| `test_installer_scripts.py` | Installer-script tokeniser and feasibility analyser; real AmigaOS 3.2 analysis skipped unless the CD image is present |
| `test_emulator.py` | FS-UAE harness logic and its five failure outcomes; boot tests skipped unless configured |

Structural tests deliberately implement checksum and offset maths **independently** of
amitools, in `test/helpers/blocks.py`. A test that compared amitools to its own constants
would still pass if amitools changed its layout. `amibuilder/blocks.py` is therefore a
second implementation rather than an import of the helper, and `test_blocks.py` pins the two
together so the package cannot drift from the oracle.

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
- [ ] Phase 2: layer capture
- [ ] Phase 3: composition
- [ ] Phase 4: additive writes, ADF injection, `merge` policy
- [ ] Phase 5: `zerofree` and `compact`

## Licence

GPL-2.0-or-later, inherited from amitools.
