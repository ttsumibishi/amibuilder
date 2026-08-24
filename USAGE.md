# Using amibuilder

The how-to: setup, addressing, every command, the interactive shell, exit codes, and the
end-to-end workflow. For what the project is and why, start with the [README](README.md).
For measured numbers see [STATISTICS.md](STATISTICS.md), and for common questions see
[FAQ.md](FAQ.md).

## Requirements and setup

amibuilder needs Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
VIRTUAL_ENV=.venv uv pip install -e '.[dev]'
```

That installs the tool onto the venv's path:

```bash
.venv/bin/amibuilder --help
```

Nothing else is required for day-to-day use. The test suite's emulator tests need FS-UAE and
a Kickstart ROM, which cannot be bundled; see [Running the tests](#running-the-tests).

## Addressing

One argument names any of six things. This is the piece worth learning first, because every
command takes it:

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

A path *inside* an image is always a separate argument, never part of the spec, so a
partition named `Work` cannot be confused with a directory named `Work`. Selecting by volume
name is amibuilder's own addition — amitools resolves device names and indexes only, so
`Workbench` is matched by mounting each partition on a miss.

## Commands

Twenty-five commands are installed as `amibuilder`. Every command supports `--json`, and the
JSON shape is part of the interface rather than a pretty-printed afterthought.

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
| `get` | Extract a file or subtree to the host; the last path component may be a wildcard (`*`, `?`, `[…]`) to pull every match into a directory; `--dry-run`, `--force`, `--preserve-times` |
| `diff` | Compare two sources and report added / changed / removed; `--by path\|volume`, `--timestamps-significant`, `--no-deletions`, `--exclude` |

```bash
amibuilder info card.hdf
amibuilder ls card.hdf:Workbench S -l
amibuilder find card.hdf:0 --name '*.info' --type f
amibuilder get card.hdf:0 S ./backup/S
amibuilder get card.hdf:Work 'S/*.prefs' ./backup/S      # wildcard: every match into a dir
amibuilder hexdump card.hdf --block 0
amibuilder check card.hdf --json
```

An image-side wildcard in `get`'s last path component (`'S/*.prefs'`) matches entries in that
directory case-insensitively, as FFS is, and extracts each into `DEST`, which must be a directory
(it is created if missing, unless `--dry-run`). A match that already exists on the host is skipped
with a warning and a count in the summary — `--force` overwrites instead. No match at all is an
error (exit `3`). Only `get` expands wildcards; `cp` does not, because the host shell already
expands them on its side of the copy. Quote the pattern so your shell leaves it for amibuilder.

### Comparing two sources

`diff SOURCE_A SOURCE_B` reports what differs between any two sources — two images, two ADFs, a
partition and a host directory, whatever combination. It is read-only and stores nothing: each side
is captured and hashed in memory, never written to a layer store. `SOURCE_A` is the "before" and
`SOURCE_B` the "after", so *added* means present only in B, *removed* only in A, and *changed*
present in both but differing.

```console
$ amibuilder diff old.hdf new.hdf
A:                 old.hdf  (3 entries, Work)
B:                 new.hdf  (3 entries, Work)
by:                path

changes:           3
  added          1
  content        1
  removed        1

change   path         size
-------  -----------  ----
removed  notes.txt
content  scsi.device    13
added    tool.run       10
unchanged:         1
```

**Volume alignment.** Entries are matched by their volume-qualified path (`Work:S/foo`), which
lines up two backups of the same drive. But a folder and a partition hold the same files under
different volume names, so when each side is a single volume they are compared by their path
*within* the volume (`--by path`); when either side has several volumes they are matched
volume-qualified (`--by volume`). The default picks `path` for two single-volume sources and
`volume` otherwise; `--by` forces either, and `--by path` on a multi-volume source is refused
rather than silently merging its volumes. An explicit partition selector narrows a source to one
volume, so `diff card.hdf:Work ./exported` compares just that partition against a folder.

The remaining options mirror `snap diff`: `--timestamps-significant` counts a timestamp-only change
(off by default, because booting an Amiga restamps files that have nothing to do with what was
installed), `--no-deletions` suppresses the *removed* side, and `--exclude GLOB` /
`--no-default-excludes` shape what is compared (`T/` and `Trashcan` are skipped by default). Exit
status is `0` whether or not there are differences — a difference is a finding, not an error — so a
script branches on the `identical` field of `--json` rather than on the exit code.

### Creating and writing

| Command | Does |
|---|---|
| `init` | Create a new image: `--size 1-1000M` / `1-16G`, repeatable `--partition NAME=SIZE[,bootable][,dostype=…]`, or `--plain` for a single-volume HDF |
| `format` | Lay a fresh filesystem onto a partition of an existing drive; `--volume`, `--dos-type`, `-f`, `-n` |
| `cp` | Copy host files or directories in; `-r`, `-f`, `--to`, `-p`, `-n`, `--preserve-times`, `--protect`, `--comment` |
| `mkdir` | Create directories; `-p` for parents, `-n` for a dry run |
| `rm` | Delete files, or directories with `-r`; the last path component may be a wildcard (`*`, `?`, `[…]`); `-n` for a dry run |
| `touch` | Set an entry's modification time to now, creating empty files for missing paths (`-c`/`--no-create` skips them); `-n` |
| `protect` | Set the AmigaDOS protection bits of existing entries; `--bits rwed` or `--bits=----rwed`; `-n` |
| `comment` | Set the file comment (FileNote) of existing entries; `--text ''` clears it; `-n` |
| `relabel` | Rename a volume (its AmigaDOS volume name, not an RDB device name); `-n` |
| `inject` | Copy files from one image or ADF into another; `--from`, `--to`, `-r`, `-f`, `-p`, `-n` |

```bash
amibuilder init card.hdf --size 4G --partition Workbench=1G,bootable --partition Work=rest
amibuilder format card.hdf:Work --force               # wipe and reformat one partition
amibuilder cp ./lha ./patch.lha card.hdf:Work --to Utils -p
amibuilder mkdir card.hdf:Work Utils/Patches -p
amibuilder rm card.hdf:Work Installers/AmigaOS-3.2.3.lha
amibuilder rm card.hdf:Work Installers -r
amibuilder rm card.hdf:Work 'T/*'                     # wildcard: every file in T/
amibuilder touch card.hdf:Work Notes.txt Ideas.txt    # create empty files, or restamp
amibuilder protect card.hdf:Work Startup --bits rwe   # set AmigaDOS protection bits
amibuilder comment card.hdf:Work README --text 'read me first'
amibuilder relabel card.hdf:Work Games                # rename the volume itself
```

`init` produces a drive real AmigaOS mounts with **no HDToolBox step**, verified under
emulation. `cp` takes the image **last**, matching Unix `cp`; every other command takes it
first. The in-image path stays a separate argument (`--to` for `cp`) in both cases, for the
reason under [Addressing](#addressing).

`format` wipes one existing volume and lays down a fresh empty filesystem — the same root-block
creation `init` uses, so a formatted partition mounts on a real Amiga. It defaults to what the
target already records: on an **RDB** partition the volume name and DosType come from the
partition table, so `format card.hdf:Work --force` reuses both. It **refuses** a `--dos-type` that
differs from the one the RDB records, because the RDB and the filesystem would then disagree —
harmless under emulation but a failed mount on real hardware; repartition with HDToolBox to change
a partition's type. On a **plain** HDF there is no partition table to read a name from, so
`--volume NAME` is required (the DosType is reused if it can be read, otherwise `ffs+intl`).
Because it destroys data, a file target needs `-f`/`--force` and a device needs `--device` plus
the typed-identifier confirmation; `--dry-run` reports what it would format and writes nothing.

`rm` mirrors AmigaDOS `Delete`: it unlinks the entry and frees its blocks but does not wipe
the data, so it behaves exactly as it would on the real machine. It refuses a directory
unless `-r`, refuses the volume root, and validates the whole path list before removing
anything — a typo in a batch removes nothing. A wildcard in the last path component (`'T/*'`)
matches entries in that directory case-insensitively, as FFS is; it stays bounded like the
shell's `rm`, matching files only unless `-r` is given (a matched directory is skipped with a
warning), and no match at all is an error (exit `3`). Quote the pattern so your shell leaves it
for amibuilder.

`touch`, `protect`, `comment` and `relabel` change metadata on things already on the volume,
without rewriting file data — the AmigaDOS `SetDate`, `Protect`, `FileNote` and `Relabel` verbs.
The first three take the image first and one or more paths, and validate the whole list before
touching anything, the same no-half-applied rule `cp` and `rm` follow:

- `touch` sets an entry's modification time to now. A missing path is *created* as an empty file
  (like the host `touch`), unless `-c`/`--no-create`; it does not create parent directories, and
  refuses the whole batch if one is missing.
- `protect` sets the AmigaDOS protection bits, taking the same `--bits` spec as `cp --protect`:
  name the permitted bits (`--bits rwed`), or give the eight-character form (`--bits=----rwed`,
  which the `=` keeps argparse from reading as another option). Resetting to `----rwed` really does
  clear the bits — amitools' own path skips a zero mask, so amibuilder drives the block directly.
- `comment` sets the file note; `--text ''` clears it.
- `relabel IMAGE NEWNAME` renames the volume in its FFS root block — the `Work` in `Work:S/…` and
  the label under the disk icon. On an RDB drive it does **not** change the partition's device name
  (`DH0`); the two are independent, so the drive letter is unchanged. The name is validated
  (non-empty, no `:` or `/`, at most 30 bytes).

### Injecting one image into another

`inject SOURCE DEST` copies files straight from one Amiga volume into another — an ADF or a
partition into a partition — so the files never round-trip through the host on the way. It is the
third edge of the `cp`/`get` triangle, and the tool for assembling a card from ADFs and other
images.

```bash
amibuilder inject game.adf card.hdf:Work --to Games -r -p          # fold an ADF's contents in
amibuilder inject other.hdf:Work card.hdf:Work --from Tools --to Apps -r -p
amibuilder inject disk.adf card.hdf:Work --from S/Startup-Sequence --to S -f
amibuilder inject old.hdf:Work new.hdf:Work -r --dry-run
```

The argument order and options match `cp` (source first, destination last; `--to`, `-r`, `-f`,
`-p`). `--from PATH` is the one addition — the sub-path within the *source* to inject, defaulting
to the whole volume — and its semantics mirror `cp`'s: `--from` omitted merges the source volume's
**contents** into `--to`, a named directory nests as `--to/<name>/…`, and a single file lands as
`--to/<name>`. A directory needs `-r`, and `--to` is created only with `-p`.

Because both sides are real Amiga volumes, inject **always** carries the source's protection bits,
comment and modification time across, rather than resetting them the way a host copy must — a
faithful copy is the only sensible one, so there is nothing to opt into. As everywhere, the whole
tree is resolved and checked before anything is written: a directory without `-r`, a collision
without `-f`, or a tree too big to fit is refused while the destination is untouched.

One v1 limitation: source and destination must be **different image files**. Two open handles on
one file, one of them writing, could let a destination write clobber a block the source read has
not reached yet, so the same-file case is refused rather than risked; export with `get` and
re-import with `cp` to move files within a single image.

### Snapshots and composition

| Command | Does |
|---|---|
| `snap create` | Capture a whole drive as a base layer, RDB layout and boot blocks included — or a host directory, with `--volume` naming the recorded volume |
| `snap diff` | Capture and compare against a parent, recording only what changed; the source may be a host directory too |
| `snap review` | Inspect a candidate before committing it; `--drop` / `--keep` by glob |
| `snap commit` / `snap discard` | Turn a candidate into a layer, or throw it away |
| `snap ls` / `snap show` | List layers, or show one's metadata, drive record and contents |
| `snap verify` / `snap gc` / `snap rm` | Integrity check, unreferenced-blob collection, removal |
| `recipe new` / `recipe ls` / `recipe show` / `recipe rm` | Name an ordered stack of layers; `recipe new --policy VOLUME=POLICY` records a volume's compose policy |
| `compose` | Build a drive from a stack: `--format rdb\|plain\|dir`, `--dry-run`, per-volume `--policy`, verification on by default |

A capture never writes to the image it reads. Composition writes into freshly formatted
volumes, which is what makes deletion-by-omission safe: there is no delete operation to get
wrong. `--exclude 'Work:Installers/**'` shapes a capture without touching the disk, so a
layer can hold exactly what you want it to.

**Capturing a host directory.** `snap create` and `snap diff` also accept a directory on the Mac
as their source, layering its tree directly with no intermediate image — useful for turning a
folder of files, or an unpacked archive, straight into a layer. `--volume NAME` sets the volume
name recorded on the layer (it defaults to the directory's own name). A directory has no RDB, so
the layer carries no drive geometry; composing it needs `--format plain` (and a `--size`, since
there is no recorded partition size to inherit), and `compose` will note the missing DosType and
default it to `ffs+intl`. `.uaem` sidecars beside the files are read for protection bits,
timestamps and comments, matching what `compose --format dir` writes.

**Compose policies, and where they come from.** Each volume composes under a policy: `replace`
(format the volume and write the stack's files — the default for a fresh drive), `merge` (write
into whatever is already there), or `preserve` (leave an existing volume's contents untouched and
only create it if absent). `recipe new --policy VOLUME=POLICY` records the intended policy on the
recipe, `recipe show` prints it, and `compose --recipe` applies it. A `compose --policy` on the
command line overrides the recipe, which overrides the per-volume default the base layer recorded
in its drive record. A dry run shows exactly what will happen and which policies were set
explicitly:

```console
$ amibuilder recipe new mystack --layers base --policy Work=preserve
$ amibuilder compose --recipe mystack --into fresh.hdf --dry-run
...
volumes
  volume      policy    action          files  content
  ----------  --------  --------------  -----  -------
  Workbench:  replace   format + write      1        3
  Work:       preserve  create empty
  policy set explicitly: Work:=preserve
```

Overriding on the command line (`compose --recipe mystack --policy Work=replace`) flips `Work:` to
`format + write` and reports `policy set explicitly: Work:=replace` instead. A policy named for a
volume no layer defines is reported as a warning, so a stale recipe entry cannot silently do
nothing.

### The interactive shell

`amibuilder shell IMAGE` opens the image (writable) and gives an AmigaDOS-style prompt, so a
session of walking a drive and moving files stops meaning the twenty-character source spec
retyped on every command.

```console
$ amibuilder shell card.hdf:Workbench
amibuilder shell -- Workbench. 'help' for commands, 'quit' to leave.
Workbench:> drives
volumes in this image (type a name with a colon to switch, e.g. Work:):
* Workbench:  DH0      10Mi   current, bootable
  Work:       DH1    22.0Mi
Workbench:> Work:
now on Work:
Work:> lcd ~/Downloads
Work:> put *.lha
put SnoopDos.lha (2Ki)
put SysInfo.lha (4Ki)
put 2 file(s)
Work:> quit
bye
```

It keeps two current directories, FTP-style: an image one for `cd`/`ls`/`rm`/`cp`/`mv`, and a
local (host) one for `lcd`/`lls`/`lpwd` and the local side of `put`/`get`.

| Command | Acts on |
|---|---|
| `pwd` `cd [PATH]` `ls [PATH]` | the image directory |
| `drives` | lists the volumes; type a name with a colon (`Work:`) to switch |
| `cp SRC DST` `mv SRC DST` | the image directory |
| `rm NAME`\|`GLOB` | deletes file(s) in the image directory |
| `put NAME`\|`GLOB` | copies host file(s) into the image directory, by name |
| `get NAME`\|`GLOB` | copies file(s) or a directory out to the local directory |
| `lpwd` `lcd [DIR]` `lls [DIR]` | the local (host) directory |
| `!COMMAND` | runs COMMAND in the local shell, in the local directory |
| `help` (`?`), `quit` (`exit`, `q`) | — |

In-shell paths are AmigaDOS-flavoured: `cd name` descends, `cd /` goes up one level (`//` two),
`cd :` returns to the volume root, and a leading `:` is volume-absolute. `cd ..` also goes up one,
for Unix muscle memory. Aliases `dir`, `copy`, `delete` and `rename` work too.

**Switching volumes.** `drives` lists the volumes in the image, marking the one you are on and
noting which is bootable. Type a volume name with a colon to switch to it, the way `Work:` does
at an AmigaShell prompt: `Work:` lands at that volume's root, and `Work:Utils` switches and then
`cd`s into `Utils`. A device name (`DH1:`) works too. Only a multi-partition RDB has more than
one volume; a plain HDF or ADF holds a single one.

**Wildcards.** `put`, `get` and `rm` expand an argument that contains `*`, `?` or `[`. `put s*`
copies every matching host file into the current image directory, `put *` copies them all
(skipping hidden files, and directories, which `put` does not take), `get S/*.prefs` pulls
matching entries out, and `rm *.info` deletes matching files — all case-insensitively, as FFS is.
A leading dot opts hidden files back in (`put .*`). A `put`/`get` batch skips any destination that
already exists — with a warning naming each one and a count in the summary — and carries on
rather than aborting. `rm` stays bounded: files only (a matched directory is skipped with a
warning, never removed), no recursion, and it names every deletion. A name with no wildcard keeps
the single-item behaviour and its hard errors.

**`!` runs a local command.** `!unzip archive.zip` hands the rest of the line to the host shell,
run in the local working directory, so its output lands where `put` will look for it — no need
to leave the prompt or open another terminal.

Three rules keep a session safe:

- **Nothing is ever overwritten.** `put`, `get`, `cp` and `mv` refuse and do nothing if the
  destination already exists — a wildcard batch skips it with a warning and moves on. There is
  no `--force` in the shell.
- **`rm`, `cp`, `mv` and `put` are file-only.** `get` is the one verb that takes a directory,
  recursively.
- **Every mutating command flushes to disk immediately**, so an unclean exit cannot leave the
  allocation bitmap stale. Switching volumes flushes the one you are leaving. Quit, Ctrl-D and
  Ctrl-C all close cleanly.

The prompt and directory listings are coloured at a terminal — the volume name green, the path
yellow, directories green and files white in `ls`. Colour turns itself off when output is piped
or redirected; `--no-color` (or setting `NO_COLOR`) disables it outright.

Tab completion completes the command word, in-image paths (against the image directory, with
`:` absolute and `/`-nested fragments) and host paths (against the local directory), chosen by
which argument is under the cursor. Matching is case-insensitive, as FFS is, and directories
carry a trailing slash.

## Exit codes

Exit codes are stable and distinct so scripts can branch on them without parsing messages:

| Code | Meaning |
|---|---|
| `0` | Success (or "worked, but found problems", as `check` reports) |
| `2` | Usage or addressing error |
| `3` | Path not found inside the image |
| `4` | Unsupported filesystem (PFS3/SFS, non-DOS ADF) |
| `5` | Malformed image, or a refused write (read-only, or a directory without `-r`) |
| `6` | Validation failure (`check`) |
| `7` | Device operation refused by a guard rail |

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

`snap create` produces a layer directly. `snap diff` produces a **candidate** instead, so there
is a review step between "here is what changed" and "keep this forever" — which is where you drop
the noise a boot left behind.

Composition verifies itself by default, by re-reading the image it just wrote:

```console
verifying
  Workbench: 3 entr(ies) match
  Work: 1 entr(ies) match

verified: the image holds exactly what was composed (4 entr(ies))
```

That is deliberately in the command rather than only in the test suite. A passing test proves the
code worked on a fixture; it says nothing about the card written thirty seconds ago.

## Safety

Raw device access is gated, because the failure mode is unrecoverable — on a PiStorm card the
non-`0x76` MBR slots hold Emu68 itself, and `rdisk2` versus `rdisk3` is one keystroke.

- Touching any device requires `--device`; refusals print `diskutil list` so the right
  identifier is visible.
- The Mac's boot disk is refused outright, read or write, regardless of flags.
- Addressing a slot whose MBR type is not `0x76` is refused by name.
- Writes require typing the device identifier, not `y`; without a TTY they require `--yes`.
- Byte ranges are served through a slice view that clamps writes to the partition, so an offset
  bug cannot reach past it.
- `--dry-run` opens read-only, so a dry run against a device never asks to confirm a write and
  cannot perform one.

Every mutating command pre-flights the **whole** operation before writing any of it — filename
lengths, illegal characters, comment lengths, free space, collisions. A refusal happens while the
target is untouched, rather than aborting halfway and leaving a partially-populated volume.

## Timestamps

amibuilder renders Amiga timestamps from the on-disk `(days, mins, ticks)` triple and never
converts through Unix time. **This means `amibuilder ls -l` can disagree with `xdftool list` by
an hour or more, and amibuilder is the side that matches the bytes.**

AmigaDOS stores naive local wall clock and does no timezone arithmetic. amitools builds its epoch
constant with `time.mktime`, which interprets 1978-01-01 as *local* time, so a value written in
summer lands an hour out and is then displayed by re-applying the same error in reverse — self-
consistent while disagreeing with the disk. An image written under one timezone therefore shifts
when read under another, which is why timestamps are excluded from the default layer-diff key:
including them would report differences that depend only on where an image was written.

`--json` emits the naive ISO form with no timezone designator, plus the raw `modified_amiga_secs`
and `modified_ticks`, which are the portable ground truth. Full detail in
[`docs/KIP-FFS-NOTES.md`](docs/KIP-FFS-NOTES.md) §5.7.

## Development

### Running the tests

**Run it in two halves.** A single combined run has repeatedly hung:

```bash
.venv/bin/python -m pytest -q -m "not emulator"      # 1583 tests, ~16 min
.venv/bin/python -m pytest -q test/test_emulator.py   # 97 tests, ~1.6 min
```

1646 tests in total. 63 carry the `emulator` mark and need FS-UAE plus a Kickstart ROM; the other
1583 need neither, because every fixture is built from scratch. `test_emulator.py` holds 97 — the
63 marked ones plus 34 harness-logic tests that run in the first half — which is why the two halves
do not add up to the total.

Useful subsets when iterating on one area:

```bash
.venv/bin/python -m pytest -m "not slow"      # skip multi-GB images
.venv/bin/python -m pytest -m regression      # just the amitools pins
.venv/bin/python -m pytest test/test_write_cli.py     # cp, mkdir, rm
.venv/bin/python -m pytest test/test_meta_cli.py      # touch, protect, comment, relabel
.venv/bin/python -m pytest test/test_inject_cli.py    # inject
.venv/bin/python -m pytest test/test_shell.py         # the interactive shell
```

Tests needing licensed source material (the AmigaOS 3.2 CD, a Kickstart ROM) skip cleanly when it
is absent, and the emulator tests run with the window hidden so they do not steal keyboard focus.
Source material for investigation goes in `source-files-do-not-add-to-git/`, which is gitignored.

### Mutation testing

A test that cannot fail is worse than no test, because it reads as coverage. Guards on the riskier
paths are checked by mutating the code they protect and requiring them to go red:

```bash
.venv/bin/python utils/scripts/mutate-write-guards.py   # cp / mkdir guards
.venv/bin/python utils/scripts/mutate-rm-guards.py      # rm guards
.venv/bin/python utils/scripts/mutate-shell-guards.py   # shell + completion guards (34)
```

Each harness patches a source file, runs the tests that claim to cover the property, and requires
them to fail. Real runs have repeatedly found vacuous guards — assertions that read as coverage but
cannot fail; several are written up in [STATISTICS.md](STATISTICS.md).

### Enabling the emulator tests

Booting an image under a real AmigaOS is the only validation that is not ultimately circular —
everything else checks amitools against amitools. It needs software that cannot be bundled, so it
is opt-in:

```bash
export AMIBUILDER_FSUAE=/Applications/FS-UAE.app/Contents/MacOS/fs-uae
export AMIBUILDER_KICKSTART=$HOME/Amiga/roms/kick31.rom
export AMIBUILDER_BOOT_IMAGE=$HOME/Amiga/images/workbench-3.2.hdf
```

Without these the boot tests skip and the rest of the suite still runs. See
[`test/emulator/README.md`](test/emulator/README.md) for how validation works.

### Built on amitools

amibuilder uses [amitools](https://github.com/cnvogelg/amitools) for the FFS/OFS/RDB/ADF
implementation rather than reimplementing it, and pins the bugs and traps found so far with
`pytest -m regression` so a version bump cannot change behaviour silently. Each is absorbed inside
`amibuilder/volume.py` or `amibuilder/image.py` and documented where the workaround lives. The ones
that would otherwise have caused silent data errors:

- **`FileName.__str__` and `__repr__` both raise `TypeError`** — they return an `FSString`, so
  `str(node.get_file_name())` crashes. The working accessor is `get_unicode_name()`.
- **`get_blocks(with_data=True)` returns no data blocks on FFS volumes** — only the OFS branch of
  `ADFSFile.read()` populates `data_blks`, so a 200 KB file is under-reported by 391 blocks.
  `data_blk_nums` is correct and is used instead.
- **`amiga_epoch` is derived with `time.mktime`**, making it depend on the host's January UTC
  offset. See [Timestamps](#timestamps).
- **`BlkDevFactory.open()` on an RDB image silently returns partition 0**, not the disk, so its
  block count is the partition's. Enumeration drives `RawBlockDevice` + `RDisk` directly.
- **`dos_env.block_size` counts longwords, not bytes** — a 512-byte block reports 128.
- **`ADFSDir.create_dir` is not recursive**, and creating a child before its parent raises
  `Invalid Parent Directory` rather than creating the chain.
- **Writing to an existing path raises rather than replacing**, so an overwrite means deleting
  first — and every create must pass `update_ts=False`, or amitools restamps the parent directory
  through the broken epoch above.
- **`read_only` lives in a different place on every block-device class** — on `ImageFile` for HDF
  and raw, on the device itself for ADF, and nowhere on the partition wrapper an RDB partition is
  mounted through.
- **`BlkDevTools` is in `amitools.util`, not `amitools.fs.blkdev`** — where it looks like it
  belongs. Getting it wrong kills raw-device support entirely, at construction.

amitools is GPL-2.0-or-later, so amibuilder is too. The FFS layer sits behind a narrow interface so
a permissive rewrite stays possible against an existing test corpus.
