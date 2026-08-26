# amibuilder — FAQ

Short answers to the questions this project tends to raise. For the how-to see
[USAGE.md](USAGE.md); for the numbers see [STATISTICS.md](STATISTICS.md).

### Why is my 4 GiB image only ~1 MB on disk?

Because it is sparse. A freshly composed image declares its full capacity but only occupies the
blocks actually written — a 40 MiB image measured 928 KiB on APFS. `ls -l` shows the declared
size; `du` (or `amibuilder du`) shows what it really costs. This holds as long as the image stays
on a filesystem that supports sparse files, and it is lost once an image has been *used*, because
deleted FFS blocks stay allocated (see the next answer but one). `zerofree --compact` puts it back:
it zeroes the freed blocks and punches them into holes, so a used image goes sparse again on APFS —
see [Reclaiming space](USAGE.md#reclaiming-space).

### Why does `amibuilder ls -l` disagree with `xdftool` by an hour?

amibuilder reads timestamps straight from the on-disk `(days, mins, ticks)` triple and never
converts through Unix time, so it matches the bytes exactly. amitools builds its epoch with
`time.mktime`, which treats 1978 as *local* time, so a value written in summer lands an hour out —
and it then displays it by re-applying the same error in reverse, so it looks self-consistent while
disagreeing with the disk. amibuilder is the side that matches the bytes. Full detail in
[USAGE.md](USAGE.md#timestamps).

### How do I delete files?

`amibuilder rm card.hdf:Work path/to/file`, and `-r` for a directory. It mirrors AmigaDOS
`Delete` — the entry is unlinked and its blocks freed, but the bytes are not wiped — and it
validates the whole path list before removing anything, so a typo removes nothing. The last path
component may be a wildcard (`rm card.hdf:Work 'T/*'`, files-only unless `-r`). Inside
`amibuilder shell` there is an `rm` too — file-only by design, and it takes a bounded wildcard
(`rm *.info` deletes matching files, never a directory). See
[USAGE.md](USAGE.md#creating-and-writing).

### How do I change a file's date, protection bits or comment, or rename a volume?

`touch`, `protect`, `comment` and `relabel` set metadata on things already on the volume, without
rewriting file data. `amibuilder touch card.hdf:Work file` sets the modification time to now
(creating an empty file if it is missing, like the host `touch`); `amibuilder protect card.hdf:Work
file --bits rwed` sets the AmigaDOS protection bits (resetting to `----rwed` really clears them —
amitools' own path skips a zero mask, so amibuilder writes the block directly); `amibuilder comment
card.hdf:Work file --text 'note'` sets the file note (`--text ''` clears it); and `amibuilder
relabel card.hdf:Work NewName` renames the volume. `relabel` changes only the FFS volume name, not
an RDB partition's device name (`DH0`) — the two are independent. See
[USAGE.md](USAGE.md#creating-and-writing).

### How do I wipe or reformat a whole partition?

`amibuilder format card.hdf:Work --force` lays a fresh empty filesystem over one volume. On an RDB
drive it reuses the name and DosType the partition table records; on a plain HDF pass
`--volume NAME`. It refuses a `--dos-type` that would disagree with what the RDB records — that
mounts under emulation but fails on a real Amiga, so repartition with HDToolBox to change a
partition's type. `--force` is required for a file target, `--device` plus a typed confirmation for
a real device, and `--dry-run` shows what it would do without writing. That is the reformat step;
`init` makes a whole new drive. See [USAGE.md](USAGE.md#creating-and-writing).

### What happens to the space when I delete files — does it come back?

At the FFS level, almost none of it. Deleting clears bitmap bits and unlinks the header but never
touches the data blocks: on a measured 1000 MiB image, deleting four fifths of the files reclaimed
**0.01%** of the image size. Every byte ever written is still there in blocks marked free. This is
the finding that shaped the whole design — see [STATISTICS.md](STATISTICS.md#the-finding-that-shaped-the-design).
The layer model sidesteps it: composed images are built into freshly formatted volumes, so there is
nothing stale to carry over. And for an existing image you would rather not recompose, `zerofree`
overwrites those free blocks with zeros so they compress away, and `compact` (or `zerofree
--compact` in one pass) punches them into holes to reclaim the disk space directly — see
[Reclaiming space](USAGE.md#reclaiming-space).

### How do I make a backup of a card small?

Reclaim the dead space first. FFS leaves every deleted file's bytes lying in blocks marked free
(previous answer), so a used image compresses and copies at its full declared size. `zerofree`
overwrites those free blocks with zeros — in a demo, an image holding one 4 MiB deleted file
compressed from **4.03 MiB down to 10 KiB** afterwards, because the dead bytes stop defeating the
compressor — and `compact` punches the zero runs into filesystem holes so the image shrinks on disk
straight away (a 10 MiB scratch image dropped from **10240 KiB to 12 KiB** on APFS). `zerofree
--compact` does both in one pass. That is the original point of the project: back a card up at the
size of its live data, not its capacity. Full walk-through in
[Reclaiming space](USAGE.md#reclaiming-space); the APFS-only caveat for `compact` is there too.

### Will that small backup stay small when I copy it to a NAS or another disk?

It depends where it lands, and it is worth knowing why. "Small on disk" comes from two different
mechanisms: a **sparse** file (the zero runs are holes that were never allocated) and **transparent
filesystem compression** (the filesystem squeezes the blocks as it writes them). Sparseness is a
property of how the file is *stored*, not of its bytes — read it back and the holes hand you real
zeros — so whether it survives a copy depends on both the copy tool and the destination filesystem.
Compression happens at the destination regardless of the tool.

So copied onto **btrfs or ZFS** — a typical NAS — a zeroed image stays tiny on disk: ZFS with
compression on (the usual default) stores all-zero blocks as holes and compresses the rest, and
btrfs with `compress=zstd` does the same in spirit. `ls -l` still reports the full declared size,
but `du`/`df` show the real, small cost. Copied onto **FAT32 or exFAT** — most SD cards and cheap
USB sticks — it balloons to the full declared size, because that family supports neither holes nor
compression. A plain **ext4/XFS/APFS** destination keeps it small only if the copy tool preserved
the holes (`rsync --sparse`, GNU `cp --sparse=always`, `tar --sparse`); a naive copy writes the
zeros out and there is no compression to save you. (Exact hole-preservation varies by tool and
version, so verify your copy path rather than trust it — unless the destination is btrfs/ZFS, where
compression makes it moot.)

The move that sidesteps all of it is to **store backups compressed** — `card.hdf.zst` or `.gz`. A
`zerofree`'d image compresses to a tiny fraction (the 4 MiB → 10 KiB demo above), and the compressed
file is an ordinary small file that copies faithfully to *any* filesystem including exFAT, transfers
small over any protocol, and only needs decompressing when you write it back to a card. That is also
why `zerofree` zeroes the free space rather than only punching holes: zeros are what both the
hole-puncher and every compressor want.

In practice you rarely store whole images at all. The layer model means one **base** OS image plus a
stack of small **diffs**, composed into a ready-to-use HDF on demand (`compose`) — so what you keep
and copy around is a base plus a handful of megabyte-scale layers, not a shelf of near-identical
multi-gigabyte images. See [Reclaiming space](USAGE.md#reclaiming-space) and
[snapshots and composition](USAGE.md#snapshots-and-composition).

### How do I back up a card to a folder, or restore a folder onto a card?

`amibuilder sync card.hdf:Work ./backup` mirrors the partition into a host folder; `amibuilder
sync ./backup card.hdf:Work` copies it back. Direction is the argument order — SOURCE → DEST —
so it is never ambiguous. Only files whose **content** changed are copied, so the first run
copies everything and a restore afterwards touches just the handful of files that differ, not the
whole multi-gigabyte image (the SD-card-wear win). Add `--delete` to make the destination an exact
mirror, removing what the source dropped; without it nothing is ever removed. Exactly one side is a
host folder and the other an image, and no raw devices. v1 syncs content and modification time, not
protection bits or comments — that arrives with image-to-image sync. See
[Syncing a folder and an image](USAGE.md#syncing-a-folder-and-an-image). This is file-level and
incremental; for a whole-image snapshot at the size of the live data, see the previous answer, and
for versioned layers see `snap`.

### What happens to files I delete on a drive — do they leave a layer?

A `snap diff` records a delete as a *whiteout*: an absence, not data
(`{"p":"...","t":"w"}`). When the stack is composed, that path is simply not written, so the file
is gone from the result while still present in the base layer. This is verified on real content
(the AmigaOS 3.2.3 patch removes a file from the 3.2 base) — see
[STATISTICS.md](STATISTICS.md#whiteouts-and-three-layer-stacking-measured-2026-08-21).

### What decides what ends up in a layer?

You do, by shaping the drive or the capture before the diff — the tool does not second-guess it. A
`snap diff` records the drive as it is, so if you do not want a staged installer in the layer,
either `--exclude 'Work:Installers/**'` at capture time (non-destructive) or delete the files on the
disk first. A real `patch-3.2.3` layer measured 17.34 MiB, 92% of it a staged installer; excluding
it gave the same OS files at 1.40 MiB.

### What is the difference between `snap create` and `snap diff`?

`snap create` captures a whole drive as a self-contained **base layer**. `snap diff` captures a
drive, compares it against a parent layer, and records only what changed — and it produces a
**candidate** rather than a layer directly, so there is a `snap review` step between "here is what
changed" and "keep this forever". Use `create` for the OS at a known patch level, `diff` for the
games, utilities and configuration on top.

### Can I build a layer straight from a folder on my Mac?

Yes. `snap create ./folder --label mylayer` (and `snap diff`) take a host directory as their
source and layer its tree directly, with no intermediate image — handy for an unpacked archive or a
folder of configs. `--volume NAME` sets the recorded volume name (default: the folder's own name).
Such a layer carries no RDB geometry, so composing it uses `--format plain` with a `--size`, and
`.uaem` sidecars beside the files supply protection bits, timestamps and comments. See
[USAGE.md](USAGE.md#snapshots-and-composition).

### How do I compare two images, or an image against a folder?

`amibuilder diff SOURCE_A SOURCE_B` reports what was added, changed or removed between any two
sources — two images, two ADFs, a partition and a host directory, and so on. It is read-only and
stores nothing. `SOURCE_A` is the "before", so *added* means present only in B and *removed* only
in A. Two single-volume sources are compared by path within the volume, so a folder and a `Work:`
partition line up; multi-volume drives are matched by volume name (`--by` forces either). It exits
`0` whether or not it finds differences, so a script reads the `identical` field of `--json`. This
is not `snap diff`: that compares a drive against a stored *layer* and writes a reviewable
candidate, whereas `diff` compares two live sources and just reports. See
[USAGE.md](USAGE.md#comparing-two-sources).

### How do I copy files from an ADF (or another image) straight into a card?

`amibuilder inject game.adf card.hdf:Work --to Games -r -p` folds the ADF's contents into
`card.hdf:Work/Games` without the files ever touching the host — the tool for assembling a card
from ADFs and other images. Source first, destination last, like `cp`; `--from` picks a sub-path of
the source (default: the whole volume), `--to` the directory to land in. Because both sides are
Amiga volumes, it carries protection bits, comments and timestamps across unchanged. One v1
limitation: the two sides must be different image files — move files *within* one image by exporting
with `get` and re-importing with `cp`. See [USAGE.md](USAGE.md#injecting-one-image-into-another).

### Can it write to my real SD card, ZuluSCSI or PiStorm?

ZuluSCSI, yes in principle: `compose --format rdb` produces the raw whole-disk image it wants, and
you copy that onto the card. PiStorm/Emu68 is **deliberately not wired up yet** — reading an RDB out
of an MBR `0x76` slot is verified, but the *write* path is the one that can destroy an Emu68 install
on a card where `rdisk2` versus `rdisk3` is one keystroke, so it waits for a real card to test
against. Nothing else is blocked on it; the bytes are identical to the RDB image the other targets
already produce. Raw-device access is gated behind `--device` and a set of guard rails either way
(see [USAGE.md](USAGE.md#safety)).

### Why does `cp` take the image last, when every other command takes it first?

Because Unix `cp` put the destination last decades ago and fingers already know it —
`amibuilder cp ./file card.hdf:Work` reads naturally. Every read command, and `rm`/`mkdir`/`shell`,
take the image first. In both orders the in-image path stays a separate argument, so `card.hdf:Work`
can only ever mean a volume, never a volume-or-directory ambiguity.

### Is there an interactive mode?

Yes: `amibuilder shell card.hdf:Work` opens an AmigaDOS-style (coloured) prompt with
`cd`/`ls`/`put`/`get`/`cp`/`mv`/`rm` and tab completion, keeping separate image and local working
directories FTP-style. `drives` lists the volumes and typing `Work:` switches between an RDB's
partitions; `put`/`get`/`rm` take wildcards (`put *.lha`, `rm *.info`); and `!cmd` runs a command
in the local shell without leaving the prompt. Nothing is ever overwritten, and every change is flushed to
disk immediately. See [USAGE.md](USAGE.md#the-interactive-shell).

### Why FFS only — not PFS3 or SFS?

amibuilder reads and writes AmigaDOS FFS/OFS (DOS0–DOS7), which is what the overwhelming majority of
Amiga installs use. PFS3 and SFS are different filesystems with their own on-disk formats;
amibuilder recognises them and refuses with a clear message (exit code 4) rather than guessing at a
layout and corrupting the disk. Writing a correct PFS3 implementation is a large project with all
the risk in the least-reviewed code.

### Do I need Amiga ROMs to run the tests?

No, for almost all of them. 1656 of the 1719 tests build every fixture from scratch and need
nothing external. The 63 that boot a real AmigaOS under FS-UAE need a Kickstart ROM and FS-UAE,
which cannot be bundled, so they are opt-in and skip cleanly when absent. See
[USAGE.md](USAGE.md#enabling-the-emulator-tests).

### Will my Workbench install, ROMs or ADFs get committed to git?

No. Licensed source material — Kickstart ROMs, the AmigaOS CD, ADFs, real disk images — is never
committed. Drop it in `source-files-do-not-add-to-git/`, which is gitignored, and the relevant tests
pick it up. The layer store (which *is* what you back up) lives outside the repo too.
