# Emulator validation

Everything else in this suite validates images against amitools plus amitools' own
validator. That is circular: it cannot catch a case where amitools and amibuilder agree on
something a real AmigaOS rejects. The classic example is international hashing — write a
file with the wrong hash variant and it occupies space while being **invisible to
AmigaDOS**, yet amitools reads it back perfectly.

Booting the image is the only test that closes that loop.

## How it works

```
   host                                          emulated Amiga
   ────                                          ──────────────
1. copy the image under test  ──────────────────▶ hard_drive_0
2. inject S/Startup-Sequence into the copy
3. mount a host directory     ──────────────────▶ hard_drive_1 as RESULTS:
4. launch fs-uae
                                                  script runs Info, Version, ...
                              ◀────────────────── writes RESULTS:log.txt
                              ◀────────────────── writes RESULTS:done
5. poll for `done`, terminate fs-uae
6. parse RESULTS:log.txt
```

**The image under test is copied first.** The original is never modified, which matters
because injecting a Startup-Sequence is a destructive edit.

**Result files, not the serial port, are the primary channel.** A mounted host directory
needs no AmigaOS configuration, whereas `SER:` requires a `Devs/DOSDrivers` entry that a
freshly composed image may not have. The serial port is available in parallel for a live
log by passing `capture_serial=True`.

**`RESULTS:done` is the completion signal** and must be the last thing the script writes.
The host polls for it rather than guessing at a boot duration.

**`FAILAT 21`** is the first line of every generated script. Without it a non-zero return
code from any command aborts the script before the sentinel is written, and the run looks
like a hang rather than a failure.

## Serial port details

FS-UAE exposes the emulated serial port over TCP:

```
serial_port = tcp://127.0.0.1:1234/wait
```

The `/wait` suffix makes FS-UAE **block during boot until the host connects**, so no early
output is lost. Without it there is a race between the emulator starting and the host
attaching, and the first seconds of output vanish.

To drive an interactive Amiga shell over the same channel, `socat` can present a PTY and
`newshell aux:` on the Amiga side attaches to it. That needs the `AUX` handler moved from
`Storage/DOSDrivers` to `Devs/DOSDrivers` first.

## Configuration

```bash
export AMIBUILDER_FSUAE=/Applications/FS-UAE.app/Contents/MacOS/fs-uae
export AMIBUILDER_KICKSTART=$HOME/Amiga/roms/kick31.rom
export AMIBUILDER_BOOT_IMAGE=$HOME/Amiga/images/workbench-3.2.hdf
```

`AMIBUILDER_BOOT_IMAGE` must be a bootable AmigaOS install. Kickstart ROMs and AmigaOS are
licensed software and cannot be bundled, so all three are supplied by whoever runs the
tests. If any is unset the boot tests skip and the other 100+ tests still run.

## Status: VERIFIED end to end

Confirmed working against **FS-UAE 3.2.35** on macOS with **Kickstart 3.2**
(`kicka1200.rom`, from the AmigaOS 3.2 CD) booting **Install3.2.adf**:

```
completed=True in 6.1s
AMIBUILDER-BEGIN
--- Version
Kickstart 47.96, Workbench 47.2
--- Info
Unit      Size       Used       Free Full Errs   Status   Name
DF0       879K       1742         16  99%   0  Read/Write Install3.2
DH1      4095M    4194304    4194303  50%   0  Read/Write RESULTS
RAM      9902K         11       9891   0%   0  Read/Write Ram Disk
...
AMIBUILDER-END
```

**A healthy run takes about 6 seconds**, not the 180 originally guessed. Defaults are now
90s overall, with a 20s stall timeout and a 45s boot timeout. `warp_mode = 1` is what makes
it fast. The whole emulator file runs in about 43 seconds.

One practical note: running these under a *backgrounded* shell has proved unreliable here —
runs stall or die without flushing, and stale background terminals appear to interfere with
later ones. Run them in the foreground.

Also verified: a file and a three-level-deep directory tree written by amitools are fully
visible to AmigaDOS (`List` shows correct names, sizes and protection bits) and read back
**byte-identical** when AmigaDOS copies them out. That is the check no amount of
amitools-versus-amitools testing can provide.

FS-UAE does open a window on macOS; there is no true headless mode. It is configured
small, silent and unable to grab input. On Linux, `xvfb-run` works.

## Three failure modes found on first real use

Each is now handled, and each was invisible in the design.

**1. A requester the host cannot dismiss.** Injecting a Startup-Sequence *replaces* the
original — including the part that assigns `ENV:` and `T:`. The first real run raised
**"Please insert volume ENV in any drive"** with Retry/Cancel buttons, and simply sat
there. Nothing on the host can click Cancel.

Fixed by having generated scripts reproduce the environment setup from the install disk's
own startup:

```
MakeDir >NIL: RAM:T RAM:ENV
Copy >NIL: ENVARC: RAM:ENV QUIET ALL NOREQ
Assign >NIL: ENV: RAM:ENV
Assign >NIL: T: RAM:T
```

`NOREQ` matters: without it the `Copy` raises its own requester when `ENVARC:` is absent.

**2. Case-sensitive matching against real media.** The install disk ships
`S/Startup-sequence` with a **lowercase 's'**. The injection check looked for
`Startup-Sequence`, silently found nothing, left the original in place, and ran the actual
AmigaOS installer instead of the test script. FFS is case-insensitive but case-preserving,
so all comparisons against real media must be too.

**3. Missing commands look exactly like filesystem faults.** `Type` is **not on the
install disk** — a minimal boot floppy carries about 26 commands in `C:`. And **AmigaDOS
has no stderr redirection**, so "Unknown command" goes to the console, not to a redirected
log. The step appeared to run and produce nothing, which is indistinguishable from an
unreadable file.

Fixed two ways: external commands are wrapped in `IF NOT EXISTS C:<cmd>` so absence
becomes a log line, and content checks now use `Copy <file> TO RESULTS:<name>` and compare
bytes on the host rather than relying on `Type`. That is a stronger check anyway — it
proves AmigaDOS read every byte, and it works on any medium regardless of which commands
are installed.

## Diagnosing a failed run

`AmigaRunResult.outcome` is one of five values, and `diagnosis()` renders it with enough
context to avoid a second run:

| `outcome` | Meaning | Detected by |
|---|---|---|
| `completed` | Sentinel written, all steps ran | sentinel file appears |
| `emulator-exited` | **Host-side failure** — FS-UAE quit by itself. Rejected config, unreadable Kickstart, no display. Nothing about the Amiga is at fault | `proc.poll()`, typically within a second |
| `never-started` | Nothing was ever written. The medium did not boot, or a requester appeared *before* the script ran | `boot_timeout`, default 45s |
| `stalled` | The script began and stopped advancing. Almost always a requester waiting for input | `stall_timeout`, default 20s |
| `timeout` | Still advancing when the clock ran out; raise `timeout` | `timeout`, default 90s |

`result.progress` lists every step reached, `result.missing_commands` names commands absent
from the medium, `result.exit_code` is set only for `emulator-exited`, and
`result.emulator_log` holds FS-UAE's own output — `emulator_log_tail()` is included in every
failure message, because a rejected config explains itself there and nowhere else.

On any failure the FS-UAE log and an `outcome.txt` are written into the run's workdir and
`result.artifacts_dir` points at it. An emulator failure is expensive to reproduce, so the
evidence must not be lost to a truncated assertion message.

**Why the middle three exist.** Every failure used to surface as
`completed=False, stalled_at=None` with no reason attached, and two of these cases had no
detection at all — they burned the entire timeout and then produced the same empty result.
A one-second config rejection was indistinguishable from an Amiga sitting on a requester for
90 seconds.

`emulator-exited` is tested with a **stub binary** that exits non-zero, standing in for
FS-UAE. Everything up to the launch is the real path: config generation, floppy copy and
Startup-Sequence injection.

A stub is used because **FS-UAE has no clean quick-refusal path.** Measured:

| Trigger | Result |
|---|---|
| `video_driver = none` | exit **-11, SIGSEGV** — crashes, files a crash report |
| `video_driver = dummy` / `null` | same |
| `SDL_VIDEODRIVER=dummy` | same |
| missing Kickstart | keeps running, never exits |
| unreadable Kickstart | keeps running, never exits |
| missing floppy | keeps running, never exits |

Two things follow. **FS-UAE cannot run headless** — it requires a real window, and
`fs_emu_video_dummy_init` appearing in successful windowed runs is a red herring. And
`video_driver = none` must not be used as a test fixture: the first version of that test
segfaulted FS-UAE once per suite run, filing crash reports and popping macOS's *"FS-UAE quit
unexpectedly"* dialog. A guard test now fails if it comes back.

## The macOS window-restore requester

**This blocked runs for a long time and was invisible in every log.** Recorded in full
because the symptom points nowhere near the cause.

The harness always ends a run by killing FS-UAE, so macOS records an abnormal quit. On the
*next* launch it shows a modal requester:

> The last time you opened FS-UAE, it unexpectedly quit while reopening windows. Do you want
> to try to reopen its windows again?  \[Reopen] \[Don't Reopen]

FS-UAE does not start until someone clicks. Nothing on the host can. So the run boots
nothing, writes nothing, and — because FS-UAE never reached its own logging — produces a
**completely empty emulator log**. It is self-perpetuating: every killed run arms the
requester for the one after it, which is why it appeared intermittently and why a run in
isolation often passed while the same test failed in a full suite.

It was found by capturing the FS-UAE window during a failing run. The tell in the window
list is a **260×337 untitled** window owned by FS-UAE; the real emulator window is titled
`FS-UAE · Amiga <model>` and is 320×268 for an A1200.

**The fix is `-ApplePersistenceIgnoreState YES` on FS-UAE's command line**
(`RESTORE_SUPPRESSION_ARGS`). Cocoa parses `-Key Value` out of argv into `NSUserDefaults`,
so it applies to that launch only and leaves the user's preferences untouched. FS-UAE's own
argument parser ignores the pair. Confirmed by its first log line:

```
ApplePersistenceIgnoreState: Existing state will not be touched.
New state will be written to /var/folders/.../no.fengestad.fs-uae.savedState
```

Two other candidates were tried and **measured as ineffective** — recorded so nobody retries
them:

| Attempt | Result |
|---|---|
| `defaults write no.fengestad.fs-uae NSQuitAlwaysKeepsWindows -bool false` | requester still appeared |
| deleting `~/Library/Saved Application State/no.fengestad.fs-uae.savedState` | requester still appeared |
| `-ApplePersistenceIgnoreState YES` on argv | **works** — run completes in 5.6 s |

The requester is the "crashed *while restoring*" variant, which is governed by persistence
rather than by window saving, which is why the first two miss it.

## Watching a run: window capture

`window_capture.py` finds a window by title substring and captures it with
`screencapture -l <windowID>`. That reads the window's own buffer, so it works while the
window is occluded, does not steal focus, and captures **only the emulator** rather than the
user's whole desktop.

```bash
.venv/bin/python -m emulator.window_capture --list              # what windows exist
.venv/bin/python -m emulator.window_capture fs-uae -o /tmp/shots
.venv/bin/python -m emulator.window_capture fs-uae --repeat 6 --interval 3
```

Verified working: the Amiga display is captured, not just window chrome — a run booting
`Install3.2.adf` photographs Workbench with `Install3.2`, `Ram Disk` and `RESULTS` mounted.

Practical notes:

- Needs `pyobjc-framework-Quartz` (in the `dev` extra) because macOS ships no CLI that lists
  window IDs. Absent it, every entry point raises `WindowCaptureUnavailable` with
  instructions.
- Needs **Screen Recording** permission for whatever runs the tests, or `screencapture`
  fails with `could not create image from display`.
- Match on titled windows: FS-UAE owns blank 500×500 placeholder surfaces, and picking
  merely the largest match returns one of those, which captures as a white rectangle and
  looks like a broken capture. `find_window` prefers titled windows for this reason.
- A capture at 6016×3384 is far too large to read back; `downscale()` handles it.
- **Nothing chained after `screencapture` in a shell line will run.** The capture succeeds
  and writes its file, but the rest of the line silently does not execute. Give it its own
  invocation.
- Give the emulator time. With `warp_mode = 0` a boot has not finished at 14 s and captures
  as a black screen; the harness default `warp_mode = 1` reaches Workbench in a few seconds.

### FS-UAE's own screenshots, and why they are not used

FS-UAE can save the Amiga display to `screenshots_output_dir`, but only in response to the
`action_screenshot` **input event** — there is no timer or automatic option, checked against
all 1,923 config option names in the binary. Firing an input event needs host-level input
injection. Whole-window capture needs no cooperation from FS-UAE and additionally catches
host-level requesters like the one above, which FS-UAE's own screenshots never could.

## Booting an image by hand, with a visible window

The harness is built for unattended runs: hidden window, warp speed, silent, killed as soon as it
has its answer. None of that is what you want when you actually want to *look* at a drive. These
commands bypass the harness entirely — no copy, no injected script, no timeout — so what boots is
the image exactly as it sits on disk.

Set the ROM once:

```bash
ROM=~/cooooode/amiga/source-files-do-not-add-to-git/roms/kicka1200.rom
FSUAE=/Applications/FS-UAE.app/Contents/MacOS/fs-uae
```

Then boot any RDB or plain HDF:

```bash
"$FSUAE" --amiga_model=A1200 --kickstart_file="$ROM" --hard_drive_0=/path/to/image.hdf --chip_memory=2048 --fast_memory=8192 --window_width=960 --window_height=720 -ApplePersistenceIgnoreState YES
```

Quit with the FS-UAE menu, or `pkill -f 'FS-UAE.app/Contents/MacOS/fs-uae'`.

Notes, each of which has cost time at least once:

- **`-ApplePersistenceIgnoreState YES` is not optional if the app was ever killed.** macOS then
  records an abnormal quit and the next launch shows a modal "unexpectedly quit while reopening
  windows" requester, behind which FS-UAE never starts and writes an **empty log**. See the
  window-restore section above.
- **Omit `--window_hidden`.** The harness sets it because a visible window takes keyboard focus
  within about three seconds of launch; for hand use that is exactly what you want.
- **Omit `--warp_mode`.** The harness runs at warp so tests finish quickly, which makes the boot
  unwatchable.
- **Sound is on by default** here. The harness sets `volume = 0`; if a hand-run boot is silent,
  check you have not copied that in.
- `--hard_drive_1=/some/dir` mounts a host directory as a second drive, which is a quick way to get
  files in and out without touching the image.
- FS-UAE's `--stdout` prints its own log, including the RDB it parsed — `RDSK at 0, C=… S=… H=…`
  and a `Partition '…' Dostype=…` line per partition. That is a genuinely useful independent check
  that an image's partition table is what you meant it to be.

## Remaining unverified

- Booting a real installed AmigaOS **hard drive** image. Set `AMIBUILDER_BOOT_IMAGE` to enable
  that test; only floppy boot has been exercised.
- Serial capture over TCP. The plumbing is written and the `/wait` form is documented, but
  results files have proven sufficient so nothing has exercised it.
- Whether a composed multi-partition RDB image boots. That is the Phase 3 exit criterion.

## Extending the checks

`harness.run_amiga(commands=[...])` takes any list of AmigaDOS commands. Useful ones:

| Command | Checks |
|---|---|
| `Info` | Volume state, free space, whether AmigaDOS considers the disk validated |
| `Version` | Kickstart and Workbench versions actually running |
| `Assign` | That startup assigns resolved, which catches a broken `Startup-Sequence` |
| `List SYS: ALL` | Full tree as AmigaDOS sees it — the check for invisible files |
| `Type <file>` | That specific content is readable |
| `C:Version SYS:C/List FULL` | Version of an individual installed command |

For composition testing the valuable pair is `List SYS: ALL` against the layer manifest,
and `Type` on a file written by amibuilder. A file present in the manifest but absent from
`List` output is the hashing bug this harness exists to catch.
