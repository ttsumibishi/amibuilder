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
90s overall with a 20s stall timeout. `warp_mode = 1` is what makes it fast.

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

`AmigaRunResult.diagnosis()` distinguishes the cases:

| Symptom | Meaning |
|---|---|
| `completed` | Sentinel written, all steps ran |
| `STALLED at step N` | Progress stopped advancing. Almost always a requester waiting for input |
| `script never started` | The injected Startup-Sequence did not run, or the medium did not boot |
| `timed out, reached N` | Still making progress when the clock ran out; raise `timeout` |

`result.progress` lists every step reached, `result.missing_commands` names commands absent
from the medium, and `result.emulator_log` holds FS-UAE's own output.

The stall timeout matters practically: without it a requester burns the full timeout per
test, so six tests took twelve minutes instead of failing in twenty seconds each.

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
