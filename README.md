<div align="center">

# amibuilder

**Treat an Amiga install like a container image: a base layer, a few small diff layers,
and a fresh bootable drive whenever you want one.**

[![version](https://img.shields.io/badge/version-0.1.0-orange?style=flat-square)](pyproject.toml)
[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](USAGE.md#requirements-and-setup)
[![built on amitools](https://img.shields.io/badge/built%20on-amitools-8A2BE2?style=flat-square)](https://github.com/cnvogelg/amitools)
[![tests](https://img.shields.io/badge/tests-1776%20%C2%B7%2063%20boot%20AmigaOS-success?style=flat-square)](USAGE.md#running-the-tests)
[![licence](https://img.shields.io/badge/licence-GPL--2.0--or--later-blue?style=flat-square)](#licence)

[Usage](USAGE.md) ·
[Measurements](STATISTICS.md) ·
[FAQ](FAQ.md) ·
[Layer design](docs/KIP-FFS-LAYERS.md) ·
[Roadmap](#roadmap)

</div>

---

> [!NOTE]
> **Status: works on image files, not yet on real hardware.** Everything below runs against
> HDF, RDB and ADF images today, and the test suite boots real AmigaOS 3.2 under FS-UAE to
> check the results. Writing straight to a PiStorm/Emu68 card is the last piece, and it waits
> until there is a real card to test it on.

amibuilder reads and writes Amiga disk images at the file level, without mounting them, and
puts a layer store on top. Capture a stock AmigaOS install once as a base layer. After that,
each change you make (a game, a utility, a tweaked `Startup-Sequence`) becomes a diff layer
holding only the files that changed. Stack the layers you want and compose them into whatever
the target machine reads.

**Snapshots cost kilobytes.** Installing SysInfo 4.4 onto a 4 GiB AmigaOS 3.2 drive captures as
a 46.7 KiB layer, 89,830 times smaller than the image it came from. Compose it back and real
AmigaOS boots the result, checked under FS-UAE with AmigaDOS's own tools.
[How that was measured](STATISTICS.md).

**The card stops being precious.** Images become build artefacts you can throw away and rebuild.
The layer store is the thing worth backing up, and it is small.

## Why

<details open>
<summary><b>Restoring an SD card means rewriting gigabytes</b></summary>

Getting an Amiga card back to a known-good state usually means copying a multi-gigabyte image
onto slow media, or reinstalling from scratch. Both take ages, and writing the whole card every
time wears it out. Most of those gigabytes did not change.

`sync` copies only the files whose content changed, so a restore touches a few hundred KiB.
Layers go further: build a fresh drive from a known stack instead of repairing the old one.
</details>

<details open>
<summary><b>Deleting files on FFS frees almost nothing</b></summary>

Delete four fifths of a 236 MiB dataset on FFS and the compressed image shrinks by **0.01%**.
FFS clears the bitmap bits and unlinks the header, but it never touches the data blocks. Every
byte ever written is still in the image, sitting in blocks marked free, and it defeats both
compression and deduplication.

Composed drives avoid this by being built into freshly formatted volumes. For drives you keep,
`zerofree` and `compact` clean up what FFS leaves behind. Numbers in
[STATISTICS.md](STATISTICS.md#the-finding-that-shaped-the-design), mechanism in
[`docs/KIP-FFS-NOTES.md`](docs/KIP-FFS-NOTES.md) §1.
</details>

## How it works

```
  stock 3.2 drive ── snap create ──► base-3.2
  + SysInfo 4.4   ── snap diff ────► sysinfo-4.4     46.7 KiB
  + some games    ── snap diff ────► games
                                         │
                  recipe a1200 = base-3.2 + sysinfo-4.4 + games
                                         │
                                      compose
                                         │
         ┌─────────────────┬─────────────┴─────┬───────────────────┐
         ▼                 ▼                   ▼                   ▼
     card.hda          test.hdf            ./wb/               /dev/rdisk4:0x76:1
     rdb               plain               dir                 device
     ZuluSCSI          WinUAE, FS-UAE      FS-UAE dir drive    PiStorm / Emu68
     works             works               works               not yet
```

A recipe also records a policy for each volume (replace, merge or preserve), so you can put
Workbench back to stock and keep your `Work:` drive byte-for-byte. That case is checked by
booting the composed drive afterwards.

## Features

- **No mounting.** `info`, `partitions`, `ls`, `tree`, `find`, `du` and `cat` read an image
  directly. `check` validates the boot block, root, tree, files and bitmap. `hexdump` names raw
  blocks even on a volume that won't mount.
- **File-level writes.** `get` copies out, `cp` and `mkdir` copy in, `mv` and `rm` work inside.
  `get` and `rm` take image-side wildcards, like `rm card.hdf:Work 'T/*'`. Writes check the
  whole operation first, so a refusal leaves the volume as it was.
- **Metadata.** `touch` a timestamp, `protect` the AmigaDOS bits, `comment` the file note,
  `relabel` the volume.
- **Image to image.** `inject` copies an ADF's or a partition's contents into another volume,
  with protection bits, comments and dates intact. Building a card from ADFs never round-trips
  through the Mac.
- **New drives, no HDToolBox.** `init` builds a partitioned drive that real AmigaOS mounts as it
  is. `format` lays a fresh filesystem on one partition of an existing drive, and refuses a
  `--dos-type` that would leave the RDB and the filesystem disagreeing.
- **Get the space back.** `zerofree` zeroes free blocks and, by default, proves it touched no
  live file. `compact` punches the zero runs into APFS holes so the image shrinks on disk
  straight away. `zerofree --compact` does both. A 4 GiB image holding 200 MiB of files ends up
  as a ~200 MiB backup.
- **rsync, but for cards.** `sync SOURCE DEST` mirrors a folder and an image, or two images, and
  copies only what changed. Protection bits and comments come along, in `.uaem` sidecars on the
  host side. `--delete` prunes; without it nothing is removed.
- **A shell.** `amibuilder shell card.hdf:Work` opens a coloured AmigaDOS-style prompt with
  `cd`, `ls`, `put`, `get`, `cp`, `mv`, `rm`, tab completion, `Work:`-style volume switching and
  a `!` escape to the host. It never overwrites anything.
- **Layered snapshots.** `snap create` a base from an image or a host folder, `snap diff` what
  changed, `review` it, `commit` it, name a stack with `recipe`, then `compose`. Deletions travel
  as whiteouts. Three-layer stacks are tested on real AmigaOS content.
- **Diff anything.** `diff old.hdf new.hdf` lists what was added, changed and removed between
  images, partitions, ADFs or host folders. Read-only.
- **JSON everywhere.** Every command takes `--json`, and the shape is part of the interface.
- **Housekeeping.** `doctor` checks Python, amitools, hole punching and sparse-store support.
  `version` reports what you're running. `completion` emits zsh completion generated from the
  parser, so it can't drift.

## Quick start

**Install**

```bash
uv venv
VIRTUAL_ENV=.venv uv pip install -e '.[dev]'
.venv/bin/amibuilder doctor
```

**Look inside an image**

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

```bash
amibuilder ls card.hdf:Workbench S -l
amibuilder find card.hdf:0 --name '*.info' --type f
amibuilder check card.hdf
```

**Make a drive and fill it**

```bash
amibuilder init card.hdf --size 4G --partition Workbench=1G,bootable --partition Work=rest
amibuilder cp -r ./SysInfo card.hdf:Work --to Tools -p
amibuilder inject game.adf card.hdf:Work --to Games -r
amibuilder shell card.hdf:Work
```

**Back up and restore**

```bash
amibuilder sync card.hdf:Work ./backup       # image to folder, changed files only
amibuilder sync ./backup card.hdf:Work       # and back again
amibuilder zerofree card.hdf --compact       # reclaim what FFS left behind
```

**Layers**

```bash
amibuilder snap create card.hdf --label base-os-3.2.3
amibuilder snap diff card.hdf --parent base-os-3.2.3 --label games
amibuilder snap review games --explain
amibuilder snap commit games
amibuilder recipe new a1200 --layers base-os-3.2.3,games
amibuilder compose --recipe a1200 --into fresh.hdf
```

Every command and option, plus the end-to-end workflow, is in [USAGE.md](USAGE.md).

## Addressing

One argument names any of these:

| Spec | Means |
|:--|:--|
| `card.hdf` | Whole image (an RDB's first partition, or a plain HDF's volume) |
| `card.hdf:0` | RDB partition by index |
| `card.hdf:DH0` | By AmigaDOS device name |
| `card.hdf:Workbench` | By volume name |
| `disk.adf` | Floppy image (`.adf`, `.adz`, `.adf.gz`) |
| `/dev/rdisk4` | Raw device, needs `--device` |
| `/dev/rdisk4:0x76:1` | MBR slot 1 holding its own RDB (PiStorm / Emu68) |
| `/dev/rdisk4:0x76:1:2` | Partition 2 inside that slot |

A path inside an image is always its own argument, never part of the spec, so a partition
named `Work` can't be confused with a directory named `Work`.

> [!WARNING]
> Raw devices are refused unless you pass `--device`. On a PiStorm card, `rdisk2` and `rdisk3`
> are one keystroke apart, and a wrong write takes out the Emu68 install with no way back.

## Target hardware

| Target | Format it needs |
|:--|:--|
| **ZuluSCSI** | Raw whole-disk image with an RDB, on a FAT32/exFAT card |
| **PiStorm / Emu68** | The bytes in an MBR partition of type `0x76`, each holding its own RDB |
| **WinUAE / FS-UAE** | RDB image, plain HDF, or a host directory |

An RDB whole-disk image works everywhere: ZuluSCSI, both emulators, and it is byte-compatible
with the contents of a PiStorm `0x76` partition. Writing to the PiStorm card itself comes last,
because it is the target where a mistake costs the most.

## Documentation

| | |
|:--|:--|
| [USAGE.md](USAGE.md) | Setup, addressing, every command and option, the shell, exit codes, the workflow, development |
| [STATISTICS.md](STATISTICS.md) | Every measured number, how it was measured, and which ones rest on a single sample |
| [FAQ.md](FAQ.md) | Short answers to the common questions |
| [CLI surface](docs/KIP-FFS-CLI-SURFACE.md) | Why addressing and argument order are shaped the way they are |
| [Layers](docs/KIP-FFS-LAYERS.md) | The snapshot design |
| [FFS notes](docs/KIP-FFS-NOTES.md) | FFS format reference, the amitools assessment, 30 numbered gotchas |
| [Plan](docs/KIP-FFS-PLAN.md) | Build order and open questions |
| [Stats record](docs/KIP-FFS-STATS.md) | The full dated measurement record |
| [Ideas](docs/KIP-FFS-IDEAS.md) | The early brainstorm: language choice, candidate commands, what might be missing |
| [Installers](docs/KIP-FFS-INSTALLERS.md) | Whether the AmigaOS 3.2 installer can be driven at the file level |

The `docs/KIP-FFS-*.md` files are working notes, kept as they were written. Some describe
features that were planned and never built, so treat USAGE.md as the word on what exists.

## What it is not

- **Not an installer.** Installing AmigaOS still happens once, by hand, under an emulator.
  amibuilder takes over from there.
- **Not a source of ROMs or Workbench.** Bring your own Kickstart and AmigaOS media. Nothing
  licensed lives in this repository, and `.gitignore` keeps it that way.
- **Not a whole-disk imager.** If you want a byte-for-byte copy of a card, `dd` already does
  that. amibuilder works on files, which is why a snapshot costs kilobytes.
- **Not proven on real hardware yet.** Every boot test runs real AmigaOS under FS-UAE.
  ZuluSCSI and PiStorm are next on the [roadmap](#roadmap).

## Requirements

| | |
|:--|:--|
| **Host** | Python 3.10+ and [`uv`](https://docs.astral.sh/uv/). amitools is pulled in as a dependency. Developed on macOS; `compact` needs a filesystem that can punch holes (APFS). |
| **Emulator tests** | FS-UAE and a Kickstart ROM. Without them those 63 tests skip and the other 1713 still run. |
| **You** | Your own Kickstart ROMs and AmigaOS media. |

## Built on amitools

amibuilder uses [amitools](https://github.com/cnvogelg/amitools) for FFS, OFS, RDB and ADF
rather than writing its own. amitools has handled DOS0 to DOS7, RDB partitioning and raw device
access for over a decade. The bugs and quirks found so far are pinned by a regression suite
(`pytest -m regression`), so a version bump can't change behaviour quietly. They're listed in
[USAGE.md](USAGE.md#built-on-amitools).

## Roadmap

**Next:** real hardware. ZuluSCSI first, then the PiStorm/Emu68 `0x76` device write target.

<details>
<summary><b>Done so far</b></summary>

- [x] Verify the FFS on-disk format and correct a widely-circulated wrong offset table
- [x] Establish target image formats for ZuluSCSI, PiStorm and the emulators
- [x] Prove the PiStorm MBR `0x76` slice read path, and ADF staging into an RDB partition
- [x] Design the layered snapshot model, with a regression suite for the dependency
- [x] FS-UAE harness verified end to end
- [x] Phase 1: read-only inspection
- [x] Phase 2: layer capture
- [x] Phase 3: composition to all three targets, verification on by default
- [x] `init`: create a drive real AmigaOS mounts with no HDToolBox step
- [x] Measure the design claim against a real AmigaOS 3.2 install
- [x] Phase 4: `cp` and `mkdir`, verified against real AmigaOS
- [x] `rm`, and a modifying/deleting diff layer, with whiteouts exercised on real 3.2.3 content
- [x] Phase 6: the interactive `shell`, with tab completion
- [x] `replace`/`merge`/`preserve` compose policies, recorded per volume in a `recipe`
- [x] `snap create` and `snap diff` straight from a host directory
- [x] `format` an existing drive's partition, and image-side wildcards for `get`
- [x] `diff` between any two sources: images, partitions, ADFs or host directories
- [x] `touch`/`protect`/`comment`/`relabel` for metadata already on a volume, and an image-side wildcard for `rm`
- [x] Phase 4b: `inject`, copying an ADF's or a partition's contents into another volume with metadata preserved
- [x] Phase 5: `zerofree` and `compact`, reclaiming the space FFS leaves behind, verified by default
- [x] `sync`: mirror a host directory and an image, one direction, with opt-in `--delete`
- [x] `doctor`: a self-check of Python, amitools, hole punching and sparse-store support
- [x] `version`, and zsh `completion` generated from the parser so it cannot drift, completing Phase 6
- [x] Image-to-image `sync`, carrying protection bits and comments, with metadata-only differences fixed in place
- [x] `sync` carries protection bits and comments to and from a host folder too, in `.uaem` sidecars, so a backup is faithful and a restore complete
- [x] `mv`: rename or move a file inside a volume, metadata carried across

</details>

## Contributing

Run the tests in two halves (a single combined run has hung before), and check the docs against
the real CLI after touching them:

```bash
.venv/bin/python -m pytest -q -m "not emulator"
.venv/bin/python -m pytest -q test/test_emulator.py
.venv/bin/python utils/scripts/audit-docs.py
```

`audit-docs.py` pulls every `amibuilder` command line out of the docs and validates it against
the live argument parser, so an example can't quietly rot when a flag changes. Test layout,
mutation testing and enabling the emulator tests are covered in [USAGE.md](USAGE.md#development).

<sub>Badges above are static. This repository lives on a LAN Gitea that shields.io can't
reach, so the test count is updated by hand alongside USAGE.md.</sub>

## Licence

GPL-2.0-or-later, inherited from amitools. The FFS layer sits behind a narrow interface, so a
permissively licensed rewrite stays possible against the existing test corpus.
