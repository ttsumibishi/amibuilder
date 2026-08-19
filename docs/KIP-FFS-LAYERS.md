# KIP-FFS — Layered Snapshot Model

**Date:** 2026-08-17, implementation notes added 2026-08-18
**Status:** **Capture side built.** Base and diff capture, the store, the review gate and the
`snap` commands all exist and are tested (309 tests). Composition is not built — everything in
§7 is still design. Where implementation differed from this document, §11 says so.
Companion to `KIP-FFS-NOTES.md` (verified findings), `KIP-FFS-IDEAS.md` (options),
`KIP-FFS-PLAN.md` (build order).

This is the design for the Docker-style layered install model: a base snapshot plus a series of
diff layers that composite on top.

---

## 1. Why this is the right centre of the project

The layer model is not a feature bolted onto a backup tool. It reframes what the tool is, and it
does so in a way that fits the hardware situation better than the original plan did.

**Layers decouple "what software is installed" from "what disk format the machine needs."** That
separation matters because the three targets in play want three different things (see
`KIP-FFS-NOTES.md` §9):

- **ZuluSCSI** wants a raw whole-disk image with an RDB.
- **PiStorm / Emu68** cannot mount an HDF at all; it wants the bytes written into an MBR `0x76`
  partition on the microSD card.
- **WinUAE / FS-UAE** will take an RDB image, a plain HDF, or a plain directory.

A layer stack composes into any of them. The layer store becomes the source of truth and images
become **disposable build artefacts**. Three consequences follow, and they are the real argument for
this design:

1. **The 4 GB backup problem dissolves rather than being optimised.** There is no longer a reason to
   snapshot a 4 GB image at all in the normal workflow. Storage becomes the base layer plus small
   diffs, deduplicated by content.
2. **Free space stops mattering.** A composed image is built into a freshly formatted volume, so its
   unallocated blocks are already zeros. The `zerofree` finding
   (`KIP-FFS-NOTES.md` §1) is what makes *existing* images cheap to store; composition makes new
   images cheap by construction.
3. **The dangerous code becomes optional.** Capture is read-only. Composition writes only to a fresh
   target. Neither mutates an image in place, which is where the corruption risk lives. In-place
   editing drops from "core feature" to "convenience, build it last if at all."

The trade is one thing to stay aware of, covered in §7: composing into a fresh image discards
anything that was not captured into a layer.

---

## 2. Model

Deliberately close to OCI/Docker semantics, because they are well understood and the analogy holds.

| Concept | Meaning here |
|---|---|
| **Base layer** | A complete snapshot of a drive. No parent. Also records the drive's RDB layout (§4). Example label: `base-os-3.2.3`. |
| **Diff layer** | Additions, modifications and deletions relative to a parent. Example: `games-common`. |
| **Layer ID** | Content hash of the manifest plus metadata. Immutable. |
| **Label / ref** | Human-readable name pointing at a layer ID, like a git branch. |
| **Recipe** | An ordered list of layers to compose. Example: `base-os-3.2.3 + games-common + my-configs`. |
| **Blob** | One file's contents, stored once, addressed by hash. |
| **Store** | The blob pool plus layer manifests plus refs plus recipes. |

Composition is **last-wins per path**, which is what was asked for. Collisions are legal and
expected — several Amiga installers ship the same `reqtools.library`. The tool reports them rather
than complaining about them (§6).

---

## 3. Store layout

```
store/
  blobs/
    3f/3fa9c1e8....zst          # content-addressed, zstd-compressed
  layers/
    <layer-id>/
      layer.json                # metadata + drive geometry (base layers)
      manifest.jsonl            # one entry per path, sorted by path
  refs/
    base-os-3.2.3               # -> layer-id
    games-common                # -> layer-id
  recipes/
    a1200-games.json            # ordered layer list
```

**`manifest.jsonl`, one JSON object per line, sorted by path.** Not a single JSON document. Three
reasons: it streams without loading a whole install into memory; `diff` and `git diff` work on it
directly, so a layer's contents are reviewable with ordinary tools; and appending during capture is
trivial.

### Manifest entry

```jsonc
{"p":"Workbench:S/Startup-Sequence","t":"f","b":"3fa9c1e8…","sz":1234,
 "pr":"----rwed","ts":[17389,587,12],"c":"boot script"}
{"p":"Workbench:Libs","t":"d","pr":"----rwed","ts":[17389,587,0]}
{"p":"Workbench:Devs/old.device","t":"w"}
{"p":"Workbench:C/Copy","t":"h","lt":"Workbench:C/Move"}
{"p":"Work:Games","t":"d","pr":"----rwed","ts":[17389,600,0]}
```

| Field | Meaning |
|---|---|
| `p` | Path, **volume-qualified** — `Workbench:S/Startup-Sequence`. This is the decision that resolves the multi-partition / multi-drive question; see §4.1. |
| `t` | `f` file, `d` directory, `w` whiteout (deleted), `h` hard link, `s` soft link |
| `b` | Blob hash (files only) |
| `sz` | Size in bytes |
| `pr` | Protection bits as the canonical 8-character string |
| `ts` | Amiga datestamp as raw **[days, mins, ticks]** — see §5 |
| `c` | Comment (max 79 chars) |
| `lt` | Link target (`h` / `s` only) |

**Directories are recorded explicitly.** Empty directories matter on AmigaOS (`T/`,
`Prefs/Presets`, `WBStartup`) and are the classic thing a naive file-list-based tool loses.

**Timestamps are stored raw**, as days/minutes/ticks since 1978-01-01, not converted to a host
timezone. AmigaDOS datestamps carry no timezone, so any conversion is lossy and irreversible
(`KIP-FFS-NOTES.md` G7). Storing the raw triple makes a round-trip exact and sidesteps the whole
problem.

---

## 3.1 Volume-qualified paths, and why multiple partitions beat multiple drives

Resolves what was question L3.

**Manifest paths are volume-qualified (`Workbench:S/Startup-Sequence`), and physical placement lives
only in the base layer's drive record.** That single decision makes multi-partition and multi-drive
layouts identical as far as layers are concerned — a layer never knows whether `Workbench:` is a
partition on a shared drive or a drive of its own. It is also exactly how AmigaOS addresses things, so
assigns and startup scripts line up with manifest keys naturally.

### Multiple partitions on one drive is *easier* to implement, not harder

The intuition that one-partition-per-drive simplifies the boundary arithmetic is understandable, but
it is the wrong way round here:

**The boundary arithmetic is already solved and already tested.** amitools' `PartBlockDevice` derives
`blk_off = heads × sectors × low_cyl` from the RDB's `DosEnvec` and hands back an independent block
device per partition. There is no arithmetic left for this tool to do — it asks for partition *N* and
gets something that behaves like a whole disk. A 4 GiB two-partition RDB was created, formatted and
validated per-partition during investigation (`KIP-FFS-NOTES.md` §4) with no special handling.

**Multiple drives is strictly more code.** Each drive means another target path or device, another
geometry decision, another RDB to construct, and another open/flush/close lifecycle for `compose` to
orchestrate — plus a drive→target mapping in every recipe. One drive means one target and one
lifecycle.

### The blast-radius concern is solved without multiple drives

The reason for splitting across drives was to avoid overwriting everything when restoring one thing.
**Partition-granular composition gives that directly:**

```bash
# Restore ONLY the OS partition to stock. Work: and Saves: untouched.
amibuilder compose --recipe stock-os --into card.hdf:0 --reformat
```

This reformats partition 0 and composes into it. It uses the same "format then write into a clean
volume" path as building a whole new image, so it inherits the same safety profile — no in-place file
editing, no partial-update states. Blast radius is one partition.

The one genuine risk is a partition-offset bug scribbling into a neighbour. Three cheap guards:

1. Validate the computed block range against the RDB before writing and **refuse if it overlaps any
   other partition** or falls below the first partition's start (where the RDB itself lives, in
   cylinder 0).
2. Run `check` on the *other* partitions after writing. At 0.19 s for a 4 GiB image this is free.
3. Never accept a partition index that the RDB does not actually define.

**Recommendation: one drive with multiple partitions as the default; multiple drives supported
because it costs little once targets are addressable, and ZuluSCSI exposes multiple SCSI IDs
naturally.** Either choice can be made per machine without the layer format changing, which is the
point of keeping placement in the drive record.

---

## 4. Base layers capture the drive layout — this solves two open problems

A base layer's `layer.json` records the **complete RDB layout**, not just the files:

```jsonc
{
  "label": "base-os-3.2.3",
  "kind": "base",
  "parent": null,
  "created": "2026-08-17T10:00:00Z",
  "source": {"kind": "rdb-hdf", "path": "wb.hdf", "size": 4294967296},
  "drive": {
    "block_size": 512, "cylinders": 32768, "heads": 8, "sectors": 32,
    "partitions": [
      {"name": "DH0", "volume": "Workbench", "low_cyl": 1, "high_cyl": 4000,
       "dos_type": "0x444f5303", "bootable": true, "boot_pri": 0,
       "mask": "0x7ffffffe", "max_transfer": "0xffffff",
       "num_buffers": 30, "reserved": 2, "pre_alloc": 0,
       "policy": "replace"},
      {"name": "DH1", "volume": "Work", "low_cyl": 4001, "high_cyl": 28000,
       "dos_type": "0x444f5303", "bootable": false, "boot_pri": 0,
       "mask": "0x7ffffffe", "max_transfer": "0xffffff",
       "policy": "merge"},
      {"name": "DH2", "volume": "Saves", "low_cyl": 28001, "high_cyl": 32767,
       "dos_type": "0x444f5303", "bootable": false, "boot_pri": 0,
       "mask": "0x7ffffffe", "max_transfer": "0xffffff",
       "policy": "preserve"}
    ]
  }
}
```

This is worth doing because it converts two of the riskiest unknowns into copied facts:

**It resolves the `de_Mask` / `de_MaxTransfer` footgun (`KIP-FFS-NOTES.md` G5) by construction.**
Rather than the tool guessing defaults that may silently corrupt data on a given controller, a base
layer captured from a drive that *already works* carries that drive's proven values forward. This is
not hypothetical: the Emu68 SD-card guide documents PFS3 failing on PiStorm specifically because
HDToolBox's suggested mask of `0xffffff` confines buffers to the first 16 MB while most Emu68 RAM
sits above it. Capturing beats guessing.

**It resolves the bootability unknown (G17) the same way.** Reproducing a known-good RDB — right
DosType, right bootable flag, right boot priority, right geometry — is far more likely to boot than
synthesising one from defaults.

So `compose` becomes: recreate the recorded drive layout, format the partitions, write the composited
files. `--layout-from <layer>` lets a different base's layout be reused, and `--layout` overrides
individual fields for the cases where the target genuinely differs (a bigger card, a different
adapter).

---

## 5. Diff semantics — the part most likely to disappoint

This is the biggest practical risk in the whole design, and it is worth being blunt about it: **a
naive diff will be full of noise and will erode trust in the tool fast.**

Between capturing the base and capturing the diff, the Amiga boots and runs. Booting and using
Workbench writes things that have nothing to do with the software just installed:

- Preferences saves under `ENV:` / `S/env-archive`
- `Devs/system-configuration`
- Temporary files in `T:`
- `Disk.info` and `.info` files when icons get moved or snapshotted
- Directory datestamps on every directory that gained or lost an entry
- Trashcan contents

If timestamp changes count as differences, a layer that should contain 40 files will contain 400.

### The comparison key

**Default: content hash + protection bits + comment. Timestamps are recorded but do not, by
themselves, make an entry a difference.**

| Change | In the diff by default? |
|---|---|
| New file | Yes |
| Content changed (hash differs) | Yes |
| Protection bits changed | Yes |
| Comment changed | Yes |
| **Timestamp only** | **No** — recorded, not diff-triggering |
| File deleted | Yes, as a whiteout (§6) |

`--timestamps-significant` opts into the strict behaviour for the rare case where it matters.

### Two-step capture, with a review gate

Trust comes from being able to look before committing:

```
amibuilder snap diff wb.hdf --parent base-os-3.2.3 --label games-common
    → writes a candidate manifest, prints a summary, commits nothing

amibuilder snap review games-common          # inspect, with --explain per entry
amibuilder snap review games-common --drop 'Workbench:T/**' --drop '**/Trashcan/**'
amibuilder snap commit games-common          # finalise, store blobs, create the ref
```

`--explain` gives the reason each entry is present: new / content-changed / protection-changed /
comment-changed / deleted. That is what turns a surprising diff into a diagnosable one.

### Exclusions

A starting exclusion set is needed, but **the right list has to be tuned against real machines and I
should not pretend to know it in advance.** A sensible opening position, all overridable:

- `T:` and any `T/` directory — temporary by definition
- `Trashcan/` and its `.info`
- `*.info` for Trashcan only, never for real files (icons are load-bearing,
  `KIP-FFS-NOTES.md` G11)
- macOS pollution in the other direction: `.DS_Store`, `._*` (G10)

Deliberately **not** excluded by default, because they are plausibly the whole point of a "my custom
configuration" layer: `ENV:` / `S/env-archive`, `Devs/system-configuration`, `S/Startup-Sequence`,
`S/User-Startup`, `WBStartup/`.

The honest expectation is that the first two or three diffs will reveal noise nobody predicted, and
the exclusion list will get edited. `snap review` is what makes that a five-minute job instead of a
reason to abandon the approach.

---

## 6. Deletions, conflicts, and provenance

### Deletions get recorded, and applied by default

The brief said overwrites are fine and did not mention deletions. They need a decision, because
**AmigaOS installers do delete files** — patch installers replace and remove obsolete libraries, and
`Installer` scripts clean up previous versions.

Recommendation: **always record deletions as whiteouts, apply them by default, and report them
prominently.** If a patch layer removes an obsolete library and composition silently skips that
removal, the result is an install with two conflicting versions of a library — exactly the kind of
fault that is miserable to diagnose on real hardware. `--no-deletions` is available for the case
where a layer should be treated as purely additive.

Recording costs nothing and cannot be recovered later, so record unconditionally.

### Conflicts are reported, not prevented

Last-wins is the requested behaviour and the right one. But composition should print a summary:

```
composed 3 layers, 4,182 paths
  conflicts: 7 paths written by more than one layer
    Workbench:Libs/reqtools.library    games-common -> utils-common   (utils-common won)
    Workbench:C/Assign                 base-os-3.2.3 -> my-configs    (my-configs won)
  ...
```

Not an error. Just the information needed to understand what was built. `--conflicts=error` for
anyone who wants strictness later.

### Provenance is recorded and checked loosely

Each diff layer records its parent layer ID. On composition, if a diff layer's recorded parent is not
in the stack, **warn but proceed**:

```
warning: layer 'games-common' was captured against 'base-os-3.2.3' (a1b2c3),
         but the stack contains 'base-os-3.2.4' (d4e5f6). Proceeding.
```

For additive software layers this is almost always fine and blocking it would be obstructive. The
warning is enough. `--strict-parents` for anyone who disagrees.

---

## 7. Composition targets, and the mutable-state trap

```bash
# ZuluSCSI: RDB whole-disk image, layout reproduced from the base layer
amibuilder compose --recipe a1200-games --into /Volumes/ZULU/HD10_512.hda --format rdb

# PiStorm: straight into an MBR 0x76 partition on the microSD card
amibuilder compose --recipe a1200-games --into /dev/rdisk4 --target-partition 0x76:1

# WinUAE / FS-UAE: plain HDF
amibuilder compose --recipe a1200-games --into test.hdf --format plain

# FS-UAE directory hard drive, for fast iteration with no image at all
amibuilder compose --recipe a1200-games --into ./wb/ --format dir --metadata uaem
```

The PiStorm path is **verified feasible**: parsing the MBR for `0x76` entries and presenting the
byte range to amitools as a file-like object works with no amitools modifications
(`KIP-FFS-NOTES.md` §9.3). About 60 lines of glue.

The `dir` target is worth calling out because it removes images from the loop entirely — compose a
stack straight to a directory, mount it in FS-UAE as a directory hard drive, and test in seconds.

### Per-volume policy — the mutable-state answer

This is the Docker "image versus container" distinction. **Composing into a fresh volume discards
anything not captured in a layer** — save games, high scores, downloads, work in progress. Rather
than a single managed/unmanaged flag, each volume in the drive record carries one of three policies:

| Policy | Format on compose? | Layers written? | Existing content | Typical use |
|---|---|---|---|---|
| **`replace`** | Yes, always | Yes | **Destroyed** | `Workbench:` — restore the OS to stock |
| **`merge`** | No | Yes, overwriting matching paths | **Kept**; nothing deleted | `Work:` — add a games layer without wiping what is there |
| **`preserve`** | Only if the volume does not exist | **No** | **Untouched** | `Saves:` — persistent, non-volatile storage |

`replace` is the "put the card back to a stock intended configuration" operation and is the
straightforward, safest one: format, then write into a clean volume.

`preserve` is trivially safe — compose creates and formats the partition on a genuinely new drive, and
thereafter never writes to it at all.

`merge` is the interesting one. It writes files into an existing formatted volume, which is the
additive write path — and that path is **verified working**: three ADFs staged into an existing
`ffs+intl` partition produced a volume the validator reported as `ok`, with byte-identical contents
(`KIP-FFS-NOTES.md` §10.1). It needs no deletion or rename support, which is where the real risk
lives. So `merge` is available as soon as additive writes land, and does not have to wait for the
destructive write path.

Two further habits worth keeping regardless of policy:

- **Capture before rebuilding.** Run `snap diff` against the current stack before recomposing so
  anything unexpected becomes a layer rather than a loss.
- **`compose` refuses to overwrite an existing target without `--force`**, and when it refuses it
  should name which volumes would be destroyed and which preserved, so the consequence is explicit
  rather than inferred.

---

## 7.5 ADF images as a layer source

Staging ADF contents onto a hard drive fits the layer model rather than sitting beside it, and that
turns out to be the better of the two available routes.

**Route A — ADF becomes a layer (preferred for anything repeatable).**

```bash
amibuilder snap create-from-adf Disk1.adf Disk2.adf Disk3.adf Disk4.adf \
       --at 'Work:Install/SomeBigApp' --label somebigapp-1.2
amibuilder compose --stack base-os-3.2.3,somebigapp-1.2 --into card.hdf
```

The ADF contents are read, hashed into blobs, and recorded in a manifest rooted at the chosen target
path. From then on it is an ordinary layer: versioned, deduplicated, part of a recipe, composable onto
any target. Multi-disk sets collapse into one layer — **verified**: three ADFs merged into a single
tree under one target path, validator clean, contents byte-identical
(`KIP-FFS-NOTES.md` §10.1).

Notably this route needs **no write path at all** beyond normal composition, because the ADF is only
ever read.

**Route B — direct injection (for one-offs).**

```bash
amibuilder cp 'Disk1.adf:/' 'card.hdf:0:Install/SomeBigApp/' --recursive
```

Writes straight into an existing volume. Convenient when the goal is "get this onto the drive now"
rather than "record this as part of a reproducible setup". Requires the additive write path, same
dependency as `merge` policy.

Both routes should exist; Route A is what makes an ADF part of a reproducible card setup.

Practical notes carried over from the verification (notes §10):

- Gzipped `.adf.gz` images are readable directly, read-only. No need to decompress first.
- Non-DOS disks — most game floppies — have no filesystem and cannot be browsed by anything. Report
  that plainly rather than appearing broken.
- **DMS images need external conversion** with `xdms` first; amitools has no DMS support and
  reimplementing it is not worth it.
- ADF filenames are subject to the same 30-character FFS limit (notes G1, G20), so pre-flight the whole
  ADF tree before writing anything.
- Merging several ADFs into one path is last-wins on collision. Multi-part archives normally have
  distinct names per disk, so a collision usually means the wrong disks were combined — **report
  collisions** (notes G21).

---

## 8. Command surface

```
amibuilder snap create <source> --label <name>            # base layer: full capture + drive layout
amibuilder snap diff   <source> --parent <ref> --label <name>   # candidate diff layer
amibuilder snap create-from-adf <adf>... --at <Volume:path> --label <name>   # ADF(s) -> layer
amibuilder snap review <ref> [--explain] [--drop GLOB] [--keep GLOB]
amibuilder snap commit <ref>
amibuilder snap ls                                        # layers with size, entry count, parent
amibuilder snap show  <ref> [--files] [--json]
amibuilder snap rm    <ref>
amibuilder snap verify [<ref>]                            # all blobs present and hash-correct
amibuilder snap gc                                        # drop blobs no layer references
amibuilder snap export <ref> --out <file.tar.zst>         # share a layer
amibuilder snap import <file.tar.zst>

amibuilder recipe new  <name> --layers a,b,c
amibuilder recipe ls | show <name>

amibuilder compose --recipe <name> | --stack a,b,c
               --into <target> [--format rdb|plain|dir]
               [--target-partition 0x76:N] [--layout-from <ref>]
               [--dry-run] [--no-deletions] [--force]

amibuilder diff <ref-or-source> <ref-or-source>           # compare any two things, file level
```

`<source>` accepts an HDF, a raw device, a `0x76` partition on a device, or a directory. Every
command supports `--json`.

---

## 9. Storage cost

The current cost of testing N install permutations is N full images. Under this model it is the base
layer's unique blobs plus each diff layer's unique blobs, compressed, with content-addressing
collapsing anything shared.

I have **not** measured this on a real AmigaOS install, so I am not going to invent a compression
ratio or a base-layer size. What is measured (`KIP-FFS-NOTES.md` §1, §4) and does bound the answer:

- The tool can pack and unpack 5,950 files in a few seconds, so capture cost is not a concern.
- A freshly formatted image's free space compresses to essentially nothing (133 KB for 4 GB), so a
  composed image stores cheaply even as a byte-exact artefact.
- Content-addressed dedup means a library shipped by five different installers is stored once.

The number worth measuring early, because it decides how much of this is worth building: **capture a
real AmigaOS 3.2.3 install as a base layer, then a single software install as a diff, and compare the
diff layer's size against the 4 GB image it came from.** That is a Phase 2 experiment, not a
guess.

---

## 10. Open questions specific to layers

**L1 — Should a base layer be a full file-level capture, or a byte-exact image?**
File-level composes flexibly across targets, which is the whole point. Byte-exact reproduces the
original perfectly, including boot blocks and anything the FFS parser does not understand. They are
not mutually exclusive: a base layer could store both a file manifest and an optional byte-exact
image blob, at the cost of roughly doubling base storage. **Recommendation:** file-level plus the
recorded drive layout, with a byte-exact capture available as `snap create --exact` for the "this
install works, freeze it perfectly" case.

**L2 — How should the boot block be handled?**
A composed RDB drive gets its DosType signature from `format`, and booting proceeds via the RDB. But
if a base drive has a custom or patched boot block, that is not a file and would be lost. Capturing
blocks 0–1 of each partition into `layer.json` is cheap insurance. **Unverified:** whether any of
these drives actually has a non-standard boot block.

**L3 — RESOLVED: multiple partitions on one drive, with multi-drive also supported.**
Manifest paths are volume-qualified and physical placement lives only in the base layer's drive
record, so the two layouts are indistinguishable at the layer level. Multiple partitions is also the
easier implementation. See §3.1.

**L4 — Layer size limits and splitting.**
A "common games" layer could be large. No technical limit, but `snap ls` should make size obvious so
layers do not quietly become unwieldy.

**L5 — Is a byte-exact backup path still wanted alongside layers?**
For a PFS3 or SFS drive, or a card whose exact state matters, block-level snapshot is the only
option that works (the file-level path cannot read those filesystems). Recommendation: keep it, but
as a small separate command rather than a co-equal tier.

---

## 11. What the implementation changed, and what it measured

Written 2026-08-18, after building the capture side. Recorded here rather than left as a
difference between document and code, because the reasons matter more than the changes.

### Deliberate departures from this design

**Blobs are written during capture, not at `commit`.** §5 put them at commit so an abandoned
candidate left nothing behind. But computing a diff already requires reading and hashing every
file, so the only extra cost of storing early is compressing the entries that actually changed
— unchanged ones deduplicate against the parent's blobs instantly. Deferring would mean a
second full read of a multi-gigabyte image to save disk that `snap gc` reclaims anyway.
Content addressing is what makes this safe: an early write is idempotent and cannot corrupt
anything already present. `snap gc` treats candidates as roots, so unreviewed work is never
reclaimed underneath the user.

**A layer ID hashes the manifest, kind, parent and drive record — not the label, creation time
or source path.** Those describe the act of capturing rather than the layer. Two consequences,
both wanted: re-capturing unchanged content is idempotent instead of forking the store, and a
diff captured against a different parent is correctly a different layer even with an identical
file list, because composing it means something different.

**The DosEnvec is captured in full — all 20 fields — not the curated subset shown in §4.**
Anything omitted becomes a default at compose time, and a guessed `de_Mask` is precisely the
failure this record exists to prevent. The field list is written out explicitly in
`image.py:DOS_ENV_FIELDS` rather than harvested from the object, so an amitools release that
adds a field cannot silently change every layer ID it touches.

**Links are not captured.** Not a design choice — an amitools limitation, measured and now
pinned by a regression test. Its `fs` package contains no link node type and defines only
`ST_ROOT`, `ST_USERDIR` and `ST_FILE`, so a link's target is unreachable. Capture warns and
skips rather than inventing a target that composition would act on. The manifest format
already carries the `h` and `s` kinds and a `link_target` field, so only the reading side is
missing. **This is the one open risk for capturing a real AmigaOS install** — see below.

### Answers to the open questions

**L2 — boot blocks: partially answered.** Boot blocks are now captured for every partition,
stored compactly: DosType and checksum always, and the boot code itself only when there is
any. Measured on amitools-formatted partitions: **no boot code**, so an ordinary base layer
costs a few dozen bytes per partition rather than a kilobyte of base64 zeros. Whether the real
ZuluSCSI and PiStorm drives carry custom boot blocks is now one `snap create` away, and
`snap show` flags it as `custom boot block`.

**§5's diff-noise risk: the mechanism works, on synthetic evidence.** Two separately built
images with identical contents — every datestamp differing — produce a diff with **zero**
entries. Under `--timestamps-significant` the same pair reports every entry as changed. So the
comparison key is doing real work rather than passing by luck. What this does *not* yet prove
is the interesting case: whether a real AmigaOS boot produces noise beyond the default
exclusion list. That needs the real install, and the expectation in §5 stands — the first few
diffs will reveal something unpredicted, and the exclusion list will get edited.

### Still unmeasured, and it is the number that matters

**The storage-cost question in §9 is still open.** Nothing here has been run against a real
AmigaOS 3.2.3 install, so there is no measured base-layer size, no diff-layer size, and no
compression ratio for real Amiga content. Everything above is fixture-scale. The experiment
remains: capture the real install as a base layer, install one piece of software, capture the
diff, and compare that diff against the 4 GB image it came from.

Two risks specific to doing that, both worth knowing before starting:

1. **A real install may contain links**, which capture will warn about and skip. If AmigaOS
   3.2.3 ships any, the base layer is incomplete in a way that only matters at compose time.
   The warning names each one, so the cost is known rather than silent.
2. **A real install exercises paths no fixture does** — large files, deep trees, unusual
   protection bits, comments, and whatever the installer left in `T:`. The capture is
   read-only, so the downside is a failed capture rather than a damaged image.

---

## 12. What composition changed, and what it proved

Written 2026-08-18, after building the compose side (branch `phase3`). Same intent as §11: record
the reasons, not just the diffs.

### Deliberate departures from §7

**Deletion is by omission during flatten, not a delete operation.** §6 describes whiteouts as
recorded deletions, which they are — but composition never *deletes* anything. It computes the
final file set and writes that. A whiteout simply causes a path to be absent from the result, and a
whiteout on a directory removes its whole subtree. This makes prefix matching the only subtlety
worth testing: `Old` must not take `Older`, and there is a test for exactly that.

**`destroys_existing_data` is `format_volume AND existed`, not `format_volume`.** The first version
shouted DESTROYS at a brand-new target and at `preserve`, which is the fastest way to teach someone
to ignore a warning. Composing to a target that does not exist destroys nothing, and `preserve` on
a missing volume is creation. The plan now reports three distinct outcomes — destroys, untouched,
created_empty — because a warning that fires when nothing is at risk is worse than no warning.

**`merge` landed here rather than in Phase 4.** §7 assumed it needed additive-write machinery. It
did not: both image targets can open an existing image and add to it, so `merge` fell out of the
write path. What it cannot do is delete, so a stack whose whiteouts a merge would silently ignore
is reported as `ineffective_whiteouts` with a warning. Silence there would look exactly like the
deletion had been applied.

**Verification is part of the command, not the test suite.** Unplanned in §7. `compose --verify` is
on by default for the image formats: it re-reads what it just wrote and compares content hash,
protection, comment and kind. The argument is the one `KIP-FFS-PLAN.md` §5 already makes for
`zerofree` — a green test proves the code works on a fixture and says nothing about the card
written thirty seconds ago. Reads go through a `HashOnlyBlobStore`, so a read-only check never
grows the store.

That store's lookups deliberately answer "absent" rather than consulting anything. If `has()` could
return `True`, a caller that skips work for content it already holds would skip the very read the
verification depends on — and a verify that quietly reads nothing is worse than no verify at all.

**The four geometry-derived DosEnvec fields are verified, not forced.** §4's whole argument is that
the DosEnvec must be reproduced rather than defaulted, and eleven fields are, verbatim. But
`surfaces`, `blk_per_trk`, `block_size` and `sec_per_blk` are computed by amitools from the drive a
partition is being added to. Forcing them could produce a partition whose own geometry disagrees
with its drive's, which is the arithmetic that decides *where the partition starts* — a way to move
data while appearing to preserve it. They are checked against the record and a mismatch warns.

**Partition-to-volume matching never falls back to ordinal position.** Matching is by recorded
partition index, then device name. An unmatched partition is created and left unformatted with a
warning, which a person can recover from; guessing would write a volume's contents into the wrong
partition, which they cannot.

### What the round trip proved

A two-partition RDB captured, composed straight back, and compared: geometry, every partition, the
DosEnvec field for field, the bootable flag, every path, every file byte for byte, protection bits,
comments, and timestamps to the tick. Then re-captured — the manifest bytes and the **layer ID** are
identical, and `snap diff` against the original layer finds nothing to record.

The layer-ID equality is the strongest of those. Since a layer ID hashes the manifest, kind, parent
and drive record (§11), it can only match if nothing capture is capable of seeing has changed.

### What it did not prove, and why §13 exists

**Every one of those checks is this program agreeing with itself.** The same code reads and writes,
so a shared misunderstanding of FFS would pass all of them. `check` is amibuilder's own validator;
even the amitools-based fixtures share a lineage with the writer. Nothing above has been read by an
implementation with no connection to this repository.

So the outstanding question was never "is the layer model sound" but **"does a real Amiga boot what
this produces."** §13 answers it for the emulated case.

### The gap composition exposed in capture

**A single-volume (plain) drive record carries no volume name** — `partitions: []`,
`single_volume: True` — so the volume name reaches a plan only through the manifest entries.
Capture an *empty* formatted volume and there is nothing to name it with, so it cannot be composed
back. Harmless for any drive with files on it, which is every real backup, and it is pinned by a
test so it stays deliberate. Fixing it means adding the volume name to the drive record, which
changes every layer ID, so it waits for a moment when that is acceptable.

`dev_flags` is likewise not captured, so the RDB target always writes 0.

---

## 13. A composed drive booting real AmigaOS

Run 2026-08-18. The first check in the project whose verdict does not come from this codebase.

### Method, and why it is ordered this way

1. Extract the real AmigaOS 3.2 install floppy (`Install3.2.adf`, licensed material, read only) with
   amibuilder's own `get` — **73 files, 797 KiB**.
2. Build a 40 MiB RDB HDF with one bootable DOS\3 partition and populate it. This is the *source*
   drive: real AmigaOS files on a real hard-drive layout.
3. **Boot the source drive first.** This baseline is the whole reason the result means anything: a
   composed drive that fails to boot could mean composition is broken, or could mean install-floppy
   contents simply do not boot from a hard disk. Without establishing which, a failure says nothing.
4. `snap create` the source → base layer.
5. `compose --format rdb` → a fresh image.
6. Boot the composed drive with the identical command set.
7. Compare what **AmigaDOS** reported, not what amibuilder reports.

Step 3 justified itself immediately: it failed, and the cause turned out to be FS-UAE unable to
launch at all (a deleted `Info.plist` invalidating its code signature, so AMFI killed it — nothing
to do with the images). Had that first run been the composed drive, the obvious conclusion would
have been "compose produces unbootable images", which was false.

### Result

Both drives boot **Kickstart 47.96, Workbench 47.2**. Every command ran; none was missing.

| As reported by AmigaDOS | Source | Composed |
|---|---|---|
| `Info` DH0 size / used / free / full / **errs** | 39M / 1762 / 80124 / 2% / **0** | 39M / 1762 / 80124 / 2% / **0** |
| `List SYS: ALL` totals | 73 files, 797K, 15 dirs, 1742 blocks | 73 files, 797K, 15 dirs, 1742 blocks |
| Entries listed | 85 | 85 |
| Entries missing / extra / differing | — | 0 / 0 / **2, both explained** |

`Assign` resolved `SYS:`, `C:`, `S:`, `LIBS:`, `DEVS:`, `L:` and `ENVARC:` to the composed volume
exactly as on the source, so Kickstart's `dosboot` accepted our RDB, a real FFS implementation
mounted our partition, and real AmigaDOS commands executed from it.

**The two differing entries are `S` and `S/Startup-Sequence`** — the file the harness injects into
each copy to drive the test, written at different wall-clock moments. Verified rather than assumed:
reading both images *before* injection shows identical `S/` contents (`Startup-sequence`, 351 bytes,
`----rwed`, same timestamp). The injection also restamps its parent directory, which accounts for
the second.

Corroboration worth noting: AmigaDOS independently counted **73 files, 797K, 15 directories** —
the same figures `snap create` reported for the capture. Two unrelated implementations agreeing on
the inventory.

### The first real compression measurement

797 KiB of genuine AmigaOS content stored as **352 KiB of blobs, 44%** — and 2 files deduplicated
*within a single capture*, before any second layer existed. Small sample, and one that skews toward
already-compressed `.info` icons and executables, so treat it as a floor rather than a forecast.
`KIP-FFS-PLAN.md` §0 still wants the number for a full install.

### What remains unvalidated

- **Real hardware.** ZuluSCSI and PiStorm/Emu68 have seen nothing. The MBR `0x76` device target is
  not even written yet.
- **Scale.** 797 KiB, not a 4 GB Workbench install with Datatypes, WHDLoad and a full `Devs/`.
- ~~**Multiple partitions booting.**~~ Done 2026-08-19; see §14.
### Codified 2026-08-19

The run above was by hand. It is now twelve tests behind the `emulator` marker in
`test/test_emulator.py`, driven by one session-scoped fixture that builds a real-AmigaOS drive from
the install floppy, boots it, captures it, composes it back, and boots that. The whole emulator
suite went from 41 tests in ~40 s to **53 in ~61 s**.

Three things were fixed or strengthened in the process, each of which had made the hand-run version
weaker than it looked:

**The hand comparison silently merged three entries.** It keyed on bare filenames, and this drive
has `CLI` in both `SYS:` and `SYS:System`, so 88 real entries appeared as 85. The parsers in
`test/emulator/amigados.py` are path-aware, and a comparison that quietly loses entries can report
a match it never checked.

**Every file had identical `----rwed` protection**, so the comparison could not have caught a bug
that reset protection bits. The fixture now sets distinctive bits on two files nothing reads while
booting, and the test asserts the source carries more than one protection value *before* comparing —
otherwise the check is vacuous by construction.

**The known difference is asserted as an exact set, not filtered out.** The harness injects
`S/Startup-Sequence` into each copy, which also restamps its parent. Excluding those two paths
before comparing would let a genuine third difference hide behind the exclusion; requiring the
difference set to be a subset of exactly those two paths means a third one fails.

Both properties were then mutation-tested: resetting protection to a constant, and shifting
timestamps by a day, each turned the suite red and named the test that caught it. So the chain from
compose through a real Kickstart boot back to the comparison is demonstrably live, rather than
merely green.

The parsers themselves are unit-tested against captured real output in
`test/test_amigados_parsing.py` — 23 tests, 0.05 s, no emulator — because they are what the boot
test's verdict rests on, and a parser bug should not need a 20-second boot to find.

---

## 14. Multiple partitions on one drive

Run and codified 2026-08-19. This is the arrangement the project actually exists for -- restore
`Workbench:` to stock while `Work:` and `Saves:` are left alone -- and none of it is reachable by a
single-partition test.

### The drive

| # | Device | Volume | DosType | Size | Flags | Content |
|---|---|---|---|---|---|---|
| 0 | DH0 | Workbench | DOS\3 (FFS+intl) | 30 MiB | **bootable** | real AmigaOS 3.2, 73 files |
| 1 | DH1 | Work | DOS\3 (FFS+intl) | 15 MiB | — | nested dirs, a 3 KB file, an **empty file** |
| 2 | DH2 | Saves | **DOS\1 (FFS)** | 15 MiB | — | two save slots, an index |

`Saves` uses a different DosType on purpose. A compose that defaulted the DosType instead of
reproducing it would still produce a mountable drive, so the difference is what makes the check
meaningful.

### Result

Both the source and the composed drive boot, and AmigaDOS reports them identically:

| Volume | used / free (source) | used / free (composed) | Errs | Listing |
|---|---|---|---|---|
| Workbench | 1758 / 59680 | 1758 / 59680 | 0 | 89 entries, injected script only |
| Work | 33 / 30685 | 33 / 30685 | 0 | 10 entries, identical |
| Saves | 23 / 30663 | 23 / 30663 | 0 | 5 entries, identical |

`SYS:` resolved to `Workbench:` on both, with `C:`, `S:`, `LIBS:`, `DEVS:` and `L:` following it --
so the **boot election picked the flagged partition** rather than a data volume, and the assigns did
not scatter across partitions. Per-volume `TOTAL:` lines match exactly.

What this adds over the single-partition case: the RDB partition chain is walked correctly by a
third party, three cylinder ranges are honoured without overlapping, the bootable flag decides the
election among candidates, and two different DosTypes coexist on one drive.

### The bug it found, which is the interesting part

The `Work:` volume holds a zero-length file, and **AmigaDOS prints `empty` in the size column
rather than `0`**. The listing parser matched digits only, so it silently dropped the entry: 10
real entries were compared as 9, and the totals line disagreed with the entry count without
anything failing.

That is a comparison quietly not checking something. **A composition bug that lost every empty
file would have passed.** It was only visible because the fixture deliberately contains an empty
file and because the per-volume totals were compared as well as the entries -- two independent
figures disagreeing is what surfaced it.

Same pass also found that a directory containing only an empty file gets a totals line with **no
`bytes` clause** (`1 file - 2 directories - 6 blocks used`), which the totals-detection pattern did
not match. It happened not to be misread as a file entry, but only because the line had no
double-space run in it, which is luck rather than design. Both are now pinned by tests using the
captured real output.

### Codified

Thirteen tests behind the `emulator` marker, sharing one session-scoped fixture. The emulator suite
is now **66 tests in ~70 s**. Three mutations confirm the new checks can fail:

| Mutation to compose | Caught by |
|---|---|
| Every partition gets the default DosType | `test_the_second_dostype_is_reproduced` |
| The last partition is never populated | `test_all_three_composed_volumes_mount` |
| The bootable flag is dropped | the drive does not boot at all |

The partition-overlap check is asserted structurally as well as through the block counts, because
an overlap that happened to fall in unused space would leave every count intact while remaining
latent data loss.

---

## 15. Partition-granular restore, and the data-loss bug it found

Run and codified 2026-08-19. This is the workflow the project was described by in the first place:
*"oops I screwed up my OS"* — put `Workbench:` back to stock, leave everything else alone.

### The bug

`write_rdb` built the whole drive from the drive record every time. So restoring a single volume
with `--volume Workbench` recreated the partition table and then formatted only the partition the
plan covered. **The other two partitions were left unformatted.**

The output said this, in a way nobody would act on:

```
wrote 73 file(s), 16 director(ies), 797Ki
warnings (2)
  partition DH1: no volume in the plan corresponds to it, so it was created but left unformatted
  partition DH2: no volume in the plan corresponds to it, so it was created but left unformatted
verified: the image holds exactly what was composed (89 entr(ies))
```

Exit code **0**, a "verified" line at the bottom, and the destruction of `Work:` and `Saves:`
reported as a warning between them. Afterwards `partitions` showed both as
`<not a mountable AmigaDOS volume>`. This is the operation a user would reach for most often, and
it destroyed the data they were most trying to protect.

Two lessons worth keeping. **A warning is not a refusal**, and burying an irreversible consequence
in one while reporting success is worse than saying nothing. And the reason it survived this long is
that every test to date composed to a *fresh* target, where creating empty partitions is the correct
behaviour — the bug lived entirely in the case no test covered.

### The fix

`write_rdb` now branches on what the target actually is:

| Target | Behaviour |
|---|---|
| Absent | Build the whole drive from the record (unchanged) |
| A readable RDB whose layout matches the record | **Restore in place.** Only planned partitions are opened; the partition table is not rewritten |
| A readable RDB whose layout differs | **Refuse**, listing every difference, and say to delete it to rebuild instead |
| Not a readable RDB | Rebuild, still behind `--force` |

Restoring into an existing drive requires `--force`, and the refusal names which volumes would be
replaced so the confirmation is informed rather than reflexive. Two policies now behave differently
on an existing drive than on a fresh one: `merge` **opens** the volume instead of creating it, and
`preserve` leaves the partition entirely unopened. Untouched partitions are reported positively —
`left untouched: DH1, DH2` — because on a granular restore, what you did *not* touch is the
reassurance the user is looking for.

### The verified result

A three-partition drive was booted, and **the Amiga itself** modified it: created
`Work:AmigaMade/List-copy` and `Saves:Info-copy`, deleted `SYS:Installer`, added `SYS:JunkDir`.
Then `--volume Workbench` was restored into that drive, and it was booted again.

| Check, as AmigaDOS reports it | Result |
|---|---|
| `SYS:Installer`, deleted by the Amiga | **restored** |
| `SYS:JunkDir`, added by the Amiga | **removed** |
| `Work:AmigaMade/List-copy` | **kept**, identical size, protection and timestamp |
| `Saves:Info-copy` | **kept** |
| Original `Work:` and `Saves:` content | **kept** |
| `Work:` / `Saves:` used blocks, before → after restore | **unchanged** |
| All three volumes mount, `Errs` | **0** |
| `SYS:` after the restore | `Workbench:` |

The restored volume is also compared as a whole rather than by spot checks: it must differ from the
Amiga-modified drive in **exactly** the two places the Amiga changed, and nowhere else.

### Mutation-verified

| Mutation | Caught by |
|---|---|
| Always rebuild, never restore in place (the original bug) | two emulator tests |
| `merge` recreates the volume instead of opening it | `test_merge_into_an_existing_volume_adds_without_clearing` |
| `preserve` stops being honoured | `test_preserve_leaves_an_existing_volume_completely_alone` |
| Untouched partitions get formatted anyway | `test_restoring_one_volume_leaves_the_others_alone` |

Worth noting how the second one was nearly missed: the granular emulator restore uses `replace` on
its only planned volume, so it never reaches the merge path at all. Running that mutation against
the emulator suite showed it passing, which looked like a decoration guard — it was actually the
mutation being scoped to the wrong suite. **A mutation has to be run against the tests that cover
the property, not the tests that look related.**

### A bug in the fix, found the same way

`_existing_rdb_layout` originally ended in `except Exception: return None`. It contained an
attribute typo — `geometry.cylinders`, where the attribute is `cyls` — and the blanket catch turned
that `AttributeError` into "there is no existing drive". So the first version of the fix silently
took the rebuild path and destroyed the partitions it had just been written to preserve: the
original bug, reintroduced through the way its replacement reported failure.

It now catches only `ImageError`, `OSError` and `ValueError`, the errors that genuinely mean "not a
readable RDB". A programming error propagates, loudly.
