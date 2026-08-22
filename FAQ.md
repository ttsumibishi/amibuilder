# amibuilder — FAQ

Short answers to the questions this project tends to raise. For the how-to see
[USAGE.md](USAGE.md); for the numbers see [STATISTICS.md](STATISTICS.md).

### Why is my 4 GiB image only ~1 MB on disk?

Because it is sparse. A freshly composed image declares its full capacity but only occupies the
blocks actually written — a 40 MiB image measured 928 KiB on APFS. `ls -l` shows the declared
size; `du` (or `amibuilder du`) shows what it really costs. This holds as long as the image stays
on a filesystem that supports sparse files, and it is lost once an image has been *used*, because
deleted FFS blocks stay allocated (see the next answer but one).

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
validates the whole path list before removing anything, so a typo removes nothing. Inside
`amibuilder shell` there is an `rm` too — file-only by design, and it takes a bounded wildcard
(`rm *.info` deletes matching files, never a directory). See
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
nothing stale to carry over.

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

No, for almost all of them. 1509 of the 1572 tests build every fixture from scratch and need
nothing external. The 63 that boot a real AmigaOS under FS-UAE need a Kickstart ROM and FS-UAE,
which cannot be bundled, so they are opt-in and skip cleanly when absent. See
[USAGE.md](USAGE.md#enabling-the-emulator-tests).

### Will my Workbench install, ROMs or ADFs get committed to git?

No. Licensed source material — Kickstart ROMs, the AmigaOS CD, ADFs, real disk images — is never
committed. Drop it in `source-files-do-not-add-to-git/`, which is gitignored, and the relevant tests
pick it up. The layer store (which *is* what you back up) lives outside the repo too.
