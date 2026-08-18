# KIP-FFS — Ideas, Options and Brainstorm

**Date:** 2026-08-17
Companion to `KIP-FFS-NOTES.md` (verified findings) and `KIP-FFS-PLAN.md` (what to actually build).

This document answers the four direct questions: what to write it in, what other commands are
necessary or nice to have, what has been missed, and what entirely new ideas are worth considering.

---

## 1. What to write it in

**Recommendation: Python, using amitools as a library.** Then reassess. The reasoning, in order of
weight:

**Correctness dominates.** The failure mode here is corrupting an image of a machine that cannot be
re-bought, on hardware that is 30+ years old. A hand-rolled FFS *writer* has to get bitmap
allocation, bitmap extension blocks, hash chain insertion, `own_key` self-pointers, seven checksum
conventions across four algorithms, reverse-order data block tables, international hashing and
dircache maintenance all correct — and get them correct on the first real write, because the
feedback loop is "does my Amiga still boot". amitools has been doing this for the Amiga community
for over a decade. Starting from a working implementation is worth more than any language feature.

**The performance argument does not survive measurement.** I benchmarked it rather than guessing:
236 MB across 5,950 files packs in 2.61 s and unpacks in 1.74 s, all files byte-identical, and a
4 GiB image validates in 0.19 s. See `KIP-FFS-NOTES.md` §4. A realistic Workbench partition
round-trips in a few seconds. The heavy lifting (`hashlib`, `struct`, file I/O) is already C inside
CPython. Rust would make a 2.6-second operation faster, which nobody will notice.

**amitools has zero runtime dependencies** and needs only Python ≥3.8. `uv tool install` gives a
clean isolated install. Distribution is a solved problem for a personal utility.

**The one real cost is licensing, and it has been settled enough to proceed.** amitools is
**GPL-2.0-or-later**, so linking it makes the tool GPL too. This is a private utility that may be
published later if it proves useful, which makes GPL acceptable now: the GPL constrains
*distribution*, not private use, and publishing under GPL-2.0-or-later remains perfectly possible.

The only scenario that hurts is deciding later to publish under MIT or Apache. Keeping the FFS layer
behind a narrow interface (see the plan's architecture section) preserves that option cheaply — a
future permissive rewrite would replace one module against an existing test corpus rather than
starting from nothing.

### The alternatives, honestly

| Language | Case for | Case against |
|---|---|---|
| **Python + amitools** | Working FFS/OFS/RDB read *and* write today, dircache handled, bitmap extension blocks proven at 3.5 GiB, metadata round-trip already implemented, validator included, raw device support included. Fast enough by measurement. | GPL-2.0-or-later. Not a single binary. Four bugs found in one afternoon (all small, see notes §5.4). |
| **Rust** | Already installed (1.93). Single static binary, trivial cross-compile. `binrw`/`zerocopy` are genuinely good at big-endian struct parsing. Strong typing catches the block-offset class of bug at compile time. Permissive licence possible. | Must write the FFS writer from scratch — weeks of work and the entire risk sits in the least-reviewed code. No mature Amiga filesystem crate exists as far as I know (**unverified**). |
| **Go** | Best-in-class single-binary distribution and cross-compilation. | **Not installed on this machine.** Same from-scratch writer risk as Rust, with a weaker type system for this problem. |
| **C++** | — | Ruled out, correctly. |

**If a permissive licence or a single binary turns out to be a hard requirement**, the sane path is
still not "write Rust from scratch and hope". It is: build the Rust FFS layer against a
**differential test oracle** — generate random operation sequences, apply them with both the Rust
implementation and amitools, and assert the resulting images are byte-identical. amitools becomes
the reference implementation instead of the dependency, which sidesteps the GPL entirely while
keeping its decade of correctness. That harness is worth building regardless, because it is also the
regression suite.

A pragmatic middle road: prototype in Python to nail down behaviour and build the test corpus, then
port to Rust only if a concrete need appears. The test corpus transfers; the prototype is disposable.

---

## 2. Feedback on the proposed CLI design

Taking the design as described, with specific suggestions.

### 2.1 Prefer subcommands over top-level flags

`--init` and `--automated-sync` as top-level mode flags will fight you as the tool grows: modes
become mutually exclusive flag soup, help text degrades, and shell completion gets awkward.
Git-style subcommands (`amibuilder init`, `amibuilder sync`, `amibuilder ls`) cost nothing now and scale.
The old flags can stay as hidden aliases if muscle memory matters.

### 2.2 Inferring sync direction from argument types is a footgun

`--automated-sync --source X --target Y`, with direction inferred from which side is an HDF, is
clever and I would not ship it as the only interface. A single mistyped path silently reverses the
direction of a destructive operation, and `--delete` makes that unrecoverable.

Safer, and barely more typing:

- **`amibuilder backup <image>[:partition] <dir>`** — always image → host.
- **`amibuilder restore <dir> <image>[:partition]`** — always host → image.
- Keep `sync` as the symmetric form for scripting, but have it **print the resolved direction and
  the operation counts, then require confirmation** unless `--yes` or `--dry-run` is given.

Two named verbs that cannot be confused beat one symmetric verb that can.

Additionally: **`--delete` should refuse to run without a prior `--dry-run` in the same invocation,
or an explicit `--yes`.** It is the only flag here that destroys data that exists nowhere else.

### 2.3 Sync needs a pre-flight pass, not just a copy loop

Because filenames over 30 characters simply cannot exist on classic FFS
(`KIP-FFS-NOTES.md` G1), a naive copy loop will abort partway and leave a half-updated image. Sync
should run in two phases: **validate the entire plan** (name lengths, illegal characters, comment
lengths, free space, DosType compatibility, link handling), report every problem at once, and only
then write. This also gives `--dry-run` for free, since the plan is already computed.

### 2.4 The `--size` grammar has a gap and a trap

`1–1000M` or `1–16G` leaves 1001M–1023M unexpressible, and 16G cannot be a single FFS partition
(notes G6). Suggestions: accept any size with a suffix (`M`/`Mi`/`G`/`Gi`), be explicit that these
mean MiB/GiB, cap at something sane like 128G, and **warn or refuse above 4G for a single
partition** while allowing it for a multi-partition RDB disk. Report cylinder rounding rather than
hiding it (notes G18).

### 2.5 `--init` should ask which flavour of HDF

There are two common and incompatible shapes: a **plain** single-filesystem image (what WinUAE calls
a bare HDF), and a **whole-disk RDB** image with a partition table. Which one is correct depends
entirely on the SD adapter. `init` needs `--rdb` / `--plain`, and should probably refuse to guess.

### 2.6 `cd` implies an interactive shell

`ls`, `cd`, `cp`, `mv` as one-shot subcommands cannot support `cd` — there is no session to hold the
working directory. Two complementary interfaces:

- **One-shot** for scripting: `amibuilder ls image.hdf:0 /S`
- **`amibuilder shell image.hdf`** — a REPL with `readline`, history, tab completion on in-image paths,
  and `ls / cd / pwd / get / put / rm / mv / mkdir / cat / hexdump / info / df / protect / comment`.
  Python's `cmd` plus `readline` gets this working in an afternoon.

The shell is also the best possible manual test harness for the FFS layer.

---

## 3. Commands worth adding

### Necessary

| Command | Why |
|---|---|
| **`zerofree`** | Zero unallocated blocks. **The single highest-value feature** — see notes §1. Without it, every backup carries every file ever deleted. |
| **`compact`** | Punch holes over zero runs via `F_PUNCHHOLE` to reclaim local disk. Verified working from pure Python. |
| **`info`** | Volume and partition summary: DosType, block size, geometry, free/used, bitmap health. |
| **`check` / `fsck`** | Validate before every write. Must run amitools' full 5-step sequence (notes §5.5) and must special-case dircache volumes, where the bundled validator emits false positives. |
| **`partitions`** | List, add, delete, set bootable, set boot priority, set DosType. Works for *any* filesystem including PFS3/SFS, since it only reads the RDB. |
| **`format`** | Write boot blocks, root block and bitmap for a partition, with explicit DosType. |
| **`diff`** | Compare image vs directory, or image vs image, at file level. This is what makes "which of my install permutations changed what" answerable. |
| **`mv` / `cp`** | In-image rename and copy. Absent from amitools. |
| **`verify`** | Re-read and hash-compare an image against a reference. SD cards fail silently; after writing a card, verify it. |

### Nice to have

| Command | Why |
|---|---|
| `tree` | Visual hierarchy; pairs well with `--json`. |
| `find` | Locate files by name or glob inside an image without extracting. |
| `cat` / `hexdump` | Inspect `Startup-Sequence` or a `.info` file without a round trip through the host filesystem. |
| `du` | Per-directory space usage inside the image, including slack from the 512-byte block size. |
| `touch` / `setdate` | Fix up Amiga datestamps. Also needed for deterministic output (§4.6). |
| `relabel` | Change volume name. |
| `boot install` / `boot read` | Read and write the boot block. Cannot ship Commodore's boot code, but preserving and restoring an existing one is legitimate and useful. |
| `snap` | Snapshot subcommands — see §4.2. |
| `mount` | Expose an image in Finder without a kernel extension — see §4.5. |
| `completion` | Shell completions for zsh. |
| `doctor` | Self-check: Python version, amitools version and known-bug status, `F_PUNCHHOLE` availability, filesystem of the backup store. |

### Global options worth having from the start

`--dry-run` / `-n`, `--json` (machine-readable output for every read command), `--yes`,
`--verbose` / `--quiet`, `--progress`, `--partition <name|index>`, `--exclude` / `--include` globs,
`--checksum` vs `--size-and-date` comparison mode (rsync-style), `--block-size` and `--geometry`
overrides for headerless images, `--charset`, `--metadata {xdfmeta,uaem,both,none}`,
`--backup` (auto-snapshot before any mutation), and `--lock` (refuse concurrent writers to the same
image).

`--json` on every read command is worth insisting on early. It turns the tool into something
scriptable rather than something to be screen-scraped, and it costs almost nothing if designed in
from the start.

---

## 4. Things not yet considered

### 4.1 Two tiers, not one — and the block tier works on filesystems we cannot parse

There are really two separate problems wearing one hat:

**Tier 1 — block-level, filesystem-agnostic, byte-exact.** Snapshot the raw image. Works on FFS,
OFS, PFS3, SFS, an unformatted disk, or something unrecognisable. Restores byte-identical. Cannot
answer "which file changed".

**Tier 2 — file-level, FFS/OFS only, flexible.** Browse, extract, inject, sync, diff. Answers
questions about files. Cannot reproduce an image byte-exactly.

These are complementary, and the ordering matters: **Tier 1 is simpler, safer and delivers most of
the value immediately**, because combined with `zerofree` it turns a 4 GB backup into roughly the
size of the live data. Tier 2 is where the interesting work is, and where the corruption risk lives.
Build Tier 1 first.

The tier split also solves a problem that would otherwise be awkward: any drive here that uses PFS3
or SFS is completely opaque to file-level tooling, but Tier 1 handles it without knowing or caring.

### 4.2 Snapshots — superseded by the layered model

**This section is largely obsolete. See `KIP-FFS-LAYERS.md` for the design that replaced it.**

The original reasoning was: a content-addressed *block-level* snapshot store duplicates what
`restic`, `kopia` and `borg` already do well, so after `zerofree` the sensible move is to point one
of those at the images and spend the effort on FFS-aware features instead.

That reasoning still holds **for byte-exact image backup**, and is the right answer for any PFS3 or
SFS drive that the file-level path cannot read at all.

It has been overtaken for the primary workflow by the **layered install model**: a base snapshot plus
diff layers that composite on top, Docker-style. That model is *file-level* and content-addressed, so
it deduplicates at the granularity that actually matters (the same library shipped by five
installers is stored once), and — the part a generic block-level backup tool fundamentally cannot do —
it composes into whatever image format the target hardware needs.

What remains true from the original analysis:

1. **Do not build a block-level chunking engine.** For the occasional byte-exact backup, use restic
   or kopia after a `zerofree` pass.
2. **FFS-aware free-space zeroing** is still the thing that makes existing images cheap to store.
3. **File-level diff** — "these 6 files changed", not "these byte ranges" — is still unique value,
   and is now the foundation of layer capture rather than a standalone feature.

### 4.3 The backup directory as a git repository — a direct fit for the stated goal

The goal is to "test many different permutations of software installs". That is *branching*.

An unpacked image plus `.xdfmeta` is a directory of mostly-small files with a single flat text
metadata file. That is close to ideal for git:

- **A branch per permutation.** Install something, commit. Try a different combination on a new
  branch off the same base. Compare with `git diff`. Bisect a broken Workbench with `git bisect`.
- **Free history and dedup.** Git stores each blob once across all branches and commits.
- **`.xdfmeta` diffs cleanly**, so protection-bit and timestamp changes are visible in review.
- **Restore = `git checkout <branch>` then `amibuilder restore`** into a freshly formatted image.

Caveats worth being honest about: git does not track empty directories, does not preserve
permissions (which is why the metadata file matters), and is not happy about large binary blobs
(fine for a few hundred MB of mostly-small files, less fine for a partition full of ADFs). `jj` is
also an option if the branching gets heavy.

This is the idea I would most want to try, because it maps the actual workflow onto a tool that
already does branching, diffing and history properly.

### 4.4 The `.uaem` output is directly mountable in an emulator

Because amitools' `unpack ... fsuae` mode writes exactly the WinUAE/FS-UAE directory-hard-drive
metadata convention, a backup directory can be mounted as a hard drive in an emulator with no
conversion. That means: **test a restore in FS-UAE or vAmiga before writing anything to a real
card.** Fast, free, and it removes the SD-card write from the experimentation loop entirely — which
is also the stated motivation for the whole project.

Taken further, this becomes an automated verification step: boot a minimal Workbench headless in
FS-UAE against the restored image, run `Info` and `Validate`, capture the output, and fail the test
if anything complains. That closes the one gap the notes flag as untested (nothing has been verified
against a real Amiga filesystem implementation).

### 4.5 Mounting without a kernel extension

macFUSE needs a kext and Apple is increasingly hostile to those. The modern approach — used by
`rclone` and `fuse-t` — is to run a userspace **NFSv3 server on localhost** and mount it with the
built-in NFS client. No kext, no system extension, works in Finder.

Genuinely nice for browsing an image, but firmly a later nice-to-have. `shell` plus `get`/`put`
covers the real need at a fraction of the complexity.

### 4.6 Deterministic, reproducible images

If `init` + `format` + `restore` were deterministic — fixed timestamps, fixed allocation order — then
the same file content would always produce a byte-identical image. Two consequences:

- A snapshot could be stored as *manifest plus format parameters* and the image regenerated on
  demand, rather than stored at all.
- Verification becomes trivial: rebuild and compare hashes.

Needs a `--timestamp` / `SOURCE_DATE_EPOCH` option and care about allocation order. Not required for
v1, but cheap to preserve as an option and painful to retrofit.

### 4.7 Direct SD card access — now a core requirement, not a nice-to-have

Originally filed as optional. The hardware findings promoted it: **PiStorm / Emu68 cannot mount an
HDF at all**, so the only way to reach its Amiga partitions is by addressing MBR `0x76` partitions on
the card itself. See `KIP-FFS-NOTES.md` §9.2.

amitools' `BlkDevFactory` explicitly handles `S_ISBLK` and `S_ISCHR`, so `/dev/rdiskN` opens as an
image with no additional code. And reading an RDB image *inside* a `0x76` partition is
**verified working** via a file-like slice object passed as `fobj` — no amitools changes needed
(`KIP-FFS-NOTES.md` §9.3). The MBR parser plus slice wrapper is around 60 lines.

This obviously needs guard rails: `diskutil unmountDisk` first, explicit `--device` flag,
confirmation showing `diskutil list` output and the target's size and identifier, read-only by
default, and a hard refusal if the device is the boot disk. Getting this wrong overwrites a Mac
volume.

### 4.8 In-place partial writes are much kinder to the SD card

Syncing changed files directly into an image on a mounted card writes only the dirty blocks instead
of rewriting 4 GB. Given the stated concern about card wear, this is one of the better arguments for
supporting a target under `/Volumes/`. It is also faster in wall-clock terms by a wide margin.

### 4.9 Adapter conventions worth auto-detecting

BlueSCSI and ZuluSCSI encode SCSI ID, LUN and block size in the image filename (for example
`HD10_512.hda`). Parsing that would let the tool infer geometry and warn about mismatches instead of
asking. **Unverified** — I have not confirmed the exact grammar for the adapters in use, and it
varies by firmware generation. Worth checking against the actual cards.

### 4.10 Concurrency and crash safety

FFS has no journal. An interrupted write leaves an inconsistent volume. Two mitigations:

- **Lock file** per image, so two invocations cannot write the same image concurrently.
- **Write to a temp copy and atomically rename**, with `--in-place` as an explicit opt-in. For a
  4 GB sparse file the copy is cheaper than it sounds, and `--backup` (auto-snapshot first) is the
  belt-and-braces version.

### 4.11 Small things that will bite

- **Free space check before writing**, and a warning above ~90% full, where FFS fragmentation gets
  ugly.
- **A `Trashcan`, `T/`, and `.DS_Store` exclusion set** by default, in both directions.
- **Report slack space.** With 512-byte blocks, a partition full of tiny `.info` files wastes a
  surprising amount. `du` should show it.
- **Never text-translate anything** (notes G11).
- **Hard and soft link policy** must be explicit (notes G12) — and links are completely untested so
  far.

---

## 5. Explicitly not worth building

Recording these so they do not get reinvented:

- **A `defrag` command.** FFS fragmentation barely matters at these sizes, and the risk-to-benefit
  ratio is terrible. `unpack` then `pack` into a fresh image achieves the same thing safely.
- **`resize` of an existing FFS filesystem.** Growing a partition means relocating the root block
  (it sits at the midpoint) and rebuilding the bitmap. High risk, low reward — restore into a new,
  correctly-sized image instead.
- **A repair mode for `fsck`.** Detecting problems is valuable. Repairing them automatically on a
  filesystem this easy to corrupt is asking for trouble. Report, and let a human decide.
- **A from-scratch snapshot engine**, unless §4.2's reasoning is rejected.
- **Support for PFS3 or SFS at file level.** Enormous effort. Tier 1 block snapshots cover those
  drives adequately.

---

## 6. Naming

`amibuilder` is used as a working name in these documents. Other candidates: `hdf`, `amitool` (taken),
`workbench`, `adfs`, `guru` (as in Guru Meditation — good for the error path, poor for the happy
path), `hdfs` (badly overloaded), `ffstool`. Worth choosing before the first commit, since it ends up
in the module name and the entry point.

---

## 7. Deriving layers from Amiga install scripts

The idea: read the `Install` script that ships with software, prompt for the choices a
human would make (destination volume, path, options), and emit a layer — so "installing"
never requires booting an Amiga at all.

**Feasible, and measurement made the case stronger than this section originally guessed.**
The analysis below was written before examining a real installer. It has since been tested
against the AmigaOS 3.2 base installer — the hardest case available — and the verdict is in
[`KIP-FFS-INSTALLERS.md`](KIP-FFS-INSTALLERS.md): of 30 external-binary invocations, only
four need real work, and none of them prevent a bootable system. Read that document for the
measured position; this section remains as the option analysis behind it.

### 7.1 What Amiga installers are actually written in

| Mechanism | Prevalence | Parseable? |
|---|---|---|
| **Commodore `Installer`** — LISP-like S-expressions | The standard for applications and OS components since AmigaOS 2.1 (1992) | **Yes**, readily |
| **AmigaDOS shell script** — `Copy`, `MakeDir`, `Assign` | Common for smaller tools | Partly; it is a real language with pattern matching |
| **Just a drawer to drag to the hard drive** | Most games | Nothing to parse — it is already a file tree |
| **LhA / LZX archive** | Very common for downloads | Nothing to parse; extract and place |
| **Custom compiled installer** | Rare | No |

The Commodore `Installer` language was developed by Sylvan Technical Arts and its syntax is
based on LISP. S-expressions are trivial to parse, and **`InstallerLG` is an existing open
reimplementation of the language**, which is strong evidence the job is tractable rather
than speculative.

### 7.2 What maps cleanly, and what does not

Cleanly:

- **The prompts** — `askdir`, `askoptions`, `askchoice`, `askbool`, `askstring`,
  `asknumber`. These are precisely the questions worth surfacing ("which volume?", "which
  path?", "which optional components?"), and they map to CLI prompts or a saved answer
  file without difficulty.
- **The file operations** — `copyfiles`, `makedir`, `copylib`, `delete`, `rename`,
  `protect`. These are exactly what a layer manifest records.
- **Control flow** — `if`, `while`, `foreach`, `procedure`, `set`, arithmetic. Ordinary
  interpreter work.
- **`startup`** — appends an assign or command to `S:User-Startup`. A text edit on a file
  the layer already owns.

Awkward or blocking:

- **`(run ...)` / `(execute ...)`** runs an arbitrary m68k executable. Used for
  decompressing archives, applying patches, and running helper tools. This is the main
  blocker — and the plausible escape hatch is **vamos**, amitools' m68k AmigaOS emulator,
  which installs cleanly (`amitools[vamos]`). **Unverified** that it actually runs a real
  installer helper; see `KIP-FFS-NOTES.md` §11.6.
- **`(database ...)`** queries the machine: CPU, chipset, OS version, available RAM. This
  is arguably a *feature* rather than a problem — define a target machine profile once and
  installers pick the right binaries for it. It does mean the profile has to be modelled.
- **`(tooltype ...)`** edits `.info` icon files. amitools has no icon parser, so this is a
  genuine gap. Icons are binary and load-bearing on Workbench.
- **Anything that is not an Installer script** — games, archives, drag-to-drawer. Not
  covered at all by this route.

### 7.3 The cheaper route: observe instead of simulate

There are two ways to turn an install into a layer, and the obvious one is not the parser.

**Observe mode.** Run the install in FS-UAE against the base image, then `snap diff`.

- Handles **everything**: Installer scripts, shell scripts, custom binaries, archives,
  drag-to-drawer.
- Needs **no new machinery**. FS-UAE is already wanted for validation, and `snap diff` is
  already Phase 2. Observe mode is Phase 2 plus Phase 3 with nothing added.
- Installing in an emulator is dramatically faster than on real hardware, which was the
  original motivation.
- Captures **outcome**, not intent. Re-running with different answers means doing the
  install again.

**Simulate mode.** Parse the script, prompt, compute the operations, emit a layer.

- Fully offline, fast, and reproducible.
- Captures **intent**: the answers become layer metadata, so "install to `Work:Apps` with
  options X and Y" is recorded and can be replayed against a different base or with
  different answers.
- Partial coverage, and the gaps above need handling.

**Recommendation: observe mode first, because it is nearly free and complete. Then
simulate mode as an accelerator for the well-behaved majority.**

### 7.4 The rule that makes simulate mode safe

If simulate mode gets built, one rule matters more than the rest:

> **On encountering any construct it cannot faithfully model, it must refuse to emit a
> layer and say exactly what it hit.**

A silently incomplete install layer is worse than no layer. It composes into a Workbench
that boots but misbehaves in some way that is miserable to trace back to a missing
`tooltype` call or a skipped `(run)`. Loud partial failure is cheap; silent partial
success is expensive.

A reasonable middle path: simulate what it can, and where it hits `(run ...)`, fall back to
observe mode for just that step rather than abandoning the whole script.

### 7.5 Sketch

```bash
# Simulate: parse, prompt, emit a layer
amibuilder install-from-adf SomeApp.adf --parent base-os-3.2.3 --label someapp-2.1
    → parsed Install script (Commodore Installer, 143 forms)
    → Which volume to install on? [Work:]
    → Install directory? [Work:Apps/SomeApp]
    → Optional components: [x] docs  [ ] source  [x] examples
    → 3 constructs could not be modelled:
        line 88:  (run "lha x data.lha")     -- needs vamos or observe mode
        line 102: (tooltype ...)             -- icon editing unsupported
      refusing to emit a partial layer; re-run with --observe or --allow-partial

# Observe: run it for real, diff the result
amibuilder install-observe SomeApp.adf --parent base-os-3.2.3 --label someapp-2.1
    → boots base-os-3.2.3 in FS-UAE with SomeApp.adf in DF0:
    → (you perform the install interactively)
    → snapshot diff: 47 files added, 2 modified, 0 deleted
    → review with: amibuilder snap review someapp-2.1
```

Answers are saved either way, so a layer records how it was produced.

### 7.6 Recording the answers is the real prize

Whichever mode produces a layer, storing the prompt answers in `layer.json` turns a
one-off install into a reproducible recipe. That is what makes "Standard-Games-Installs"
a maintainable artefact rather than a snapshot nobody can regenerate: six months later the
layer says which volume, which path, and which options produced it.
