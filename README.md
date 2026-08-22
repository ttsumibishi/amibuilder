# amibuilder

File-level access, layered snapshots and image composition for Amiga hard disk images.

Restoring an Amiga SD card to a known-good state today means either copying multi-gigabyte
images to slow media, or reinstalling from scratch. Both are slow, and repeated whole-image
writes wear the card. amibuilder treats an Amiga install the way Docker treats a container
image: a **base layer** holding AmigaOS at a known patch level, plus **diff layers** for games,
utilities and configuration, composited on demand into whatever format the target machine wants.

**The design claim is measured, not projected.** A real SysInfo 4.4 install onto a 4 GiB AmigaOS
3.2 drive captures as a **46.7 KiB** diff layer — 89,830× smaller than the image it came from —
and composing that layer back produces a drive real AmigaOS boots, verified under FS-UAE with
AmigaDOS's own tools. Numbers and method in [STATISTICS.md](STATISTICS.md).

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

## Features

- **Inspect** an image without mounting it: `info`, `partitions`, `check` (5-step structural
  validation), `ls`, `tree`, `find`, `du`, `cat`, and a `hexdump` that identifies raw blocks
  even on a volume that will not mount.
- **Read and write files** at the file level, no loopback mount: `get` out to the host, `cp`
  and `mkdir` in, and `rm` (which mirrors AmigaDOS `Delete`). Every write pre-flights the whole
  operation, so a refusal leaves the volume untouched.
- **Create bootable drives** with `init` — a drive real AmigaOS mounts with **no HDToolBox
  step**, verified on real AmigaOS.
- **An interactive shell** (`amibuilder shell`): a coloured AmigaDOS-style prompt with `cd`/`ls`/
  `put`/`get`/`cp`/`mv`/`rm`, tab completion, `drives` and `Work:`-style volume switching,
  wildcards (`put *.lha`), a `!` escape to run a local command, and separate image and host
  working directories. Nothing is ever overwritten.
- **Docker-style layered snapshots**: `snap create` a base, `snap diff` only what changed,
  `review` it, `commit` it, name a stack with `recipe`, and `compose` a drive from it. Whiteouts
  (deletions) and three-layer stacks are verified on real AmigaOS content.
- **Six-way addressing** from one argument — image, partition index, device name, volume name,
  ADF, or a raw device including PiStorm/Emu68 `0x76` slices.
- **Guard rails on raw devices**, because the failure mode is unrecoverable.

Every command supports `--json`, and the JSON shape is part of the interface.

## What it looks like

Compose a drive from a named stack of layers, into whatever the target wants:

```
base-3.2  +  patch-3.2.3  +  games-common  +  my-configs
      │
      ├── compose --into card.hda --format rdb       # ZuluSCSI          ✅
      ├── compose --into test.hdf --format plain     # WinUAE / FS-UAE   ✅
      ├── compose --into ./wb/    --format dir       # FS-UAE dir drive  ✅
      └── compose --into /dev/rdisk4:0x76:1 --device # PiStorm / Emu68   not yet
```

Images become disposable build artefacts; the layer store is what gets backed up. A few things
it makes easy:

- **Snapshot an install to kilobytes, and restore it on demand** rather than copying gigabytes to
  a wearing SD card.
- **Restore one volume to stock while the others survive** byte-for-byte — the "I broke my OS but
  want to keep my Work: drive" case, verified by booting the drive afterwards.
- **Stand up a fresh bootable drive** and install onto it under emulation, with no HDToolBox step.
- **Clean up a drive interactively** in the shell — delete staged installers, move files into
  place — without retyping the source spec on every command.

## Addressing, in one line

One argument names any of six things:

| Spec | Means |
|---|---|
| `card.hdf` | Whole image — an RDB's first partition, or a plain HDF's volume |
| `card.hdf:0` | RDB partition by index |
| `card.hdf:DH0` | By AmigaDOS device name |
| `card.hdf:Workbench` | By volume name |
| `disk.adf` | Floppy image (`.adf`, `.adz`, `.adf.gz`) |
| `/dev/rdisk4` | Raw device — requires `--device` |
| `/dev/rdisk4:0x76:1` | MBR slot 1 holding its own RDB (PiStorm / Emu68) |

A path *inside* an image is always a separate argument, never part of the spec, so a partition
named `Work` cannot be confused with a directory named `Work`. Full command reference, options and
the end-to-end workflow are in [USAGE.md](USAGE.md).

## Target hardware

| Target | Format it needs |
|---|---|
| **ZuluSCSI** | Raw whole-disk image with an RDB, on a FAT32/exFAT card |
| **PiStorm / Emu68** | The bytes in an MBR partition of type `0x76`, each holding its own RDB |
| **WinUAE / FS-UAE** | RDB image, plain HDF, or a host directory |

An RDB whole-disk image is the universal interchange format — it works on ZuluSCSI, in both
emulators, and is byte-compatible with a PiStorm `0x76` partition's contents. The PiStorm *write*
target is deliberately last: it is the one that can destroy an Emu68 install on a card where
`rdisk2` versus `rdisk3` is one keystroke, so it waits for a real card to test against.

## The finding that shaped the design

Deleting files in FFS reclaims almost nothing at the image level — measured at **0.01%** after
deleting four fifths of a 236 MiB dataset. FFS clears bitmap bits and unlinks the header but never
touches the data blocks, so every byte ever written is still there, defeating compression and
deduplication. The layer model sidesteps it entirely: composed images are built into freshly
formatted volumes. Detail in [STATISTICS.md](STATISTICS.md#the-finding-that-shaped-the-design) and
[`docs/KIP-FFS-NOTES.md`](docs/KIP-FFS-NOTES.md) §1.

## Status

Inspection, file read/write (`cp`, `mkdir`, `rm`), the interactive shell, layered snapshots,
composition to all three image/directory targets, and image creation all work, backed by a
**1466-test suite** — of which **63 boot a real AmigaOS 3.2** under FS-UAE and check the result
with AmigaDOS's own tools. The remaining known gaps are deliberate: the PiStorm MBR `0x76`
**device** write target (waiting on a real card), and `zerofree`/`compact` for reclaiming space in
existing images. See [Roadmap](#roadmap).

## Documentation

| Document | Contents |
|---|---|
| [USAGE.md](USAGE.md) | Setup, addressing, every command and option, the shell, exit codes, the workflow, and development |
| [STATISTICS.md](STATISTICS.md) | Every measured number, with method and the sample-of-one honesty |
| [FAQ.md](FAQ.md) | Short answers to the common questions |

The `docs/KIP-FFS-*.md` files are the working notes behind those — the reasoning trail, kept as-is:
[NOTES](docs/KIP-FFS-NOTES.md) (FFS format reference, amitools assessment, 30 numbered gotchas),
[LAYERS](docs/KIP-FFS-LAYERS.md) (snapshot design), [PLAN](docs/KIP-FFS-PLAN.md) (build order and
open questions), [IDEAS](docs/KIP-FFS-IDEAS.md), [INSTALLERS](docs/KIP-FFS-INSTALLERS.md) and
[STATS](docs/KIP-FFS-STATS.md) (the full dated measurement record).

## Built on amitools

amibuilder uses [amitools](https://github.com/cnvogelg/amitools) for the FFS/OFS/RDB/ADF
implementation rather than reimplementing it — it has handled DOS0–DOS7, RDB partitioning and raw
device access for the Amiga community for over a decade. A regression suite pins the bugs and traps
found so far so a version bump cannot change behaviour silently; they are catalogued in
[USAGE.md](USAGE.md#built-on-amitools). Licensed source material (Kickstart ROMs, the AmigaOS CD,
ADFs, real images) is never committed — it goes in a gitignored directory.

**amitools is GPL-2.0-or-later, so amibuilder is too.** The FFS layer sits behind a narrow interface
so a permissive rewrite stays possible against an existing test corpus.

## Roadmap

- [x] Verify the FFS on-disk format and correct a widely-circulated wrong offset table
- [x] Establish target image formats for ZuluSCSI, PiStorm and the emulators
- [x] Prove the PiStorm MBR `0x76` slice read path, and ADF staging into an RDB partition
- [x] Design the layered snapshot model, with a regression suite for the dependency
- [x] FS-UAE harness verified end to end
- [x] Phase 1: read-only inspection
- [x] Phase 2: layer capture
- [x] Phase 3: composition — all three targets, verification on by default
- [x] `init`: create a drive real AmigaOS mounts with no HDToolBox step
- [x] Measure the design claim against a real AmigaOS 3.2 install
- [x] Phase 4: `cp` and `mkdir`, verified against real AmigaOS
- [x] `rm`, and a modifying/deleting diff layer — whiteouts exercised on real 3.2.3 content
- [x] Phase 6: the interactive `shell`, with tab completion
- [ ] Phase 4b: ADF injection, `merge` policy, `snap create` from a host directory
- [ ] Phase 5: `zerofree` and `compact`
- [ ] `diff` between any two sources
- [ ] Real hardware: ZuluSCSI, then the PiStorm/Emu68 `0x76` device write target

## Licence

GPL-2.0-or-later, inherited from amitools.
