# amibuilder: operational rules

Project-specific working rules for this repo. These are all things that have already cost
time at least once.

## Always clean up stray FS-UAE processes

A run that goes wrong can leave FS-UAE sitting there waiting for input, on a boot requester,
or mid-boot. The harness terminates the process it launched, but a killed pytest, a stalled
background shell or an early exception can orphan one.

**This is now more important, not less, because the window is hidden by default** (see the
next section). A stray FS-UAE no longer puts a window on the user's desktop where either of
you would notice it — it is an invisible process burning CPU. `ps` is the only way to find
it, so check even when the screen looks clean.

Any script that launches FS-UAE directly must kill it in a `finally`, and follow up with
`pkill -f 'FS-UAE.app/Contents/MacOS/fs-uae'`. Launch it with
`start_new_session=True` and never wait on it, so a hung emulator cannot hold the shell.

**Check for strays at these points, every time:**

- before starting any emulator run
- after any emulator run, especially a failed or interrupted one
- before reporting a task complete, if the task touched the emulator at all
- whenever the user reports the machine behaving oddly

```bash
ps -Ao pid,etime,state,command | grep -i 'fs-uae' | grep -v grep
```

If any are found, report them to the user with their ages **before** killing anything — one
of them may be a window the user opened deliberately. With their agreement:

```bash
pkill -f 'FS-UAE.app/Contents/MacOS/fs-uae'
```

Never blanket-kill on your own initiative during a session where the user might be running
the emulator themselves.

## FS-UAE's window is hidden by default — do not "fix" this

A visible FS-UAE window **takes keyboard focus within three seconds of launch**, and an
emulator suite launches it dozens of times, which makes the machine unusable while tests run.
`window_hidden = 1` creates the SDL window without ever showing it: measured with `lsappinfo`,
the frontmost application is unchanged across a launch, and a full 41-test emulator run left
the user's own app in front the whole time. Emulation is unaffected — the real AmigaOS 3.2.3
boot test passes hidden, in the same time as before.

`harness.build_config()` therefore emits `window_hidden = 1` unless
`AMIBUILDER_FSUAE_VISIBLE=1` is set. **Any new launch path should keep that default.**

Measured alternatives that do **not** work — do not retry them:

- **`window_minimized = 1`** is silently ignored. The window appears at full size and takes
  focus exactly as before.
- **`video_driver = none` / `dummy` / `null` and `SDL_VIDEODRIVER=dummy`** all segfault; see
  the section below.
- **The SDL hint `SDL_WINDOW_NO_ACTIVATION_WHEN_SHOWN`** would be the clean fix, but the SDL
  bundled in FS-UAE 3.2.35 predates it — the string is absent from `libSDL2-2.0.0.dylib`.

**The cost, and it is a real one:** a hidden window is absent from `CGWindowList`, so
`window_capture` cannot see it and `--list` will not show it. When you need to watch or capture
a run, ask for the window explicitly:

```bash
AMIBUILDER_FSUAE_VISIBLE=1 .venv/bin/python -m pytest -q test/test_emulator.py
```

Tell the user before doing that, because it will start grabbing their focus again.

## Run the test suite in the foreground

`control_bash_process` background runs of the emulator tests have repeatedly stalled or died
without flushing their output, and **stale background terminals accumulate across sessions
and interfere with later runs** — nine had built up at one point, four from a previous
session, and clearing them made runs reliable again.

- Use `execute_bash` with an explicit generous `timeout`, not a background process
- Split the suite rather than running one long job:
  `-m "not emulator"` (~3.5 min, 405 tests) then `test/test_emulator.py` (~43 s, 36 tests)
- Check `list_processes` occasionally and stop anything stale

## Terminal output is unreliable — redirect and read

Piping to `tail`/`cat` intermittently returns **empty** in this environment. Always redirect
to a file and read it with the file-reading tool. Related quirks:

- **No heredocs** — they hang. Write a script with the file tool, then run it.
- **One logical command per line.** Multi-line commands with trailing `\` collapse and
  swallow the next line as an argument.
- **Nothing chained after `screencapture` on the same line will run.** The capture succeeds
  and writes its file, but every following command in that line silently does not execute.
  Give `screencapture` its own invocation.

## Screen capture — use it when the emulator misbehaves

Screen Recording permission has been granted to Kiro, Terminal and iTerm, so `screencapture`
works. **Reach for this early on any emulator problem.** It found a modal requester that had
been blocking runs and was invisible in every log, after code reading had failed to.

**Capture needs a visible window.** The default is hidden, so launch with
`AMIBUILDER_FSUAE_VISIBLE=1` first or there will be nothing to find.

Prefer window-scoped capture over whole-screen: it works while the window is occluded, does
not steal focus, and captures only the emulator rather than the user's desktop, email and
Slack.

```bash
cd test && ../.venv/bin/python -m emulator.window_capture --list
cd test && ../.venv/bin/python -m emulator.window_capture fs-uae --repeat 6 --interval 3
```

- The real FS-UAE window is titled `FS-UAE · Amiga <model>`. An **untitled 260×337** FS-UAE
  window is the macOS restore requester; an untitled 500×500 one is a blank placeholder that
  captures as a white rectangle.
- A raw capture of the full 6016×3384 display is too large to read — downscale first:
  `sips -s format jpeg -s formatOptions 70 --resampleWidth 1500 in.png --out small.jpg`
- Whole-screen capture inevitably includes whatever else the user has open. Prefer
  window-scoped, and do not dwell on unrelated content.

## Never let FS-UAE's window-restore requester come back

The harness always kills FS-UAE, so macOS records an abnormal quit and the *next* launch
shows a modal "unexpectedly quit while reopening windows" requester. FS-UAE then never
starts, produces an **empty log**, and the run reports `never-started`. It is
self-perpetuating and explains "passes alone, fails in the suite".

The fix is `-ApplePersistenceIgnoreState YES` on FS-UAE's command line
(`harness.RESTORE_SUPPRESSION_ARGS`). **Any new code path that launches FS-UAE must pass
it.** Two alternatives were measured and do NOT work — do not retry them:
`NSQuitAlwaysKeepsWindows false`, and deleting
`~/Library/Saved Application State/no.fengestad.fs-uae.savedState`.

Prefer the argv form: it is scoped to one launch and leaves the user's settings alone.
**Fallback if the requester ever appears from a launch outside our control** (the user
starting FS-UAE from the Finder, or a future code path that forgets the flag) — a
system-wide preference is still available, and is reversible:

```bash
defaults write no.fengestad.fs-uae ApplePersistenceIgnoreState -bool YES   # set
defaults delete no.fengestad.fs-uae ApplePersistenceIgnoreState            # undo
```

Ask before writing to the user's defaults; it is their machine, not ours.

## Never use `video_driver = none` as a test trigger

It does not refuse the config, it **segfaults**: exit -11, `EXC_BAD_ACCESS` at `0x0`, about
0.85 s after launch. Each occurrence files a report in `~/Library/Logs/DiagnosticReports`
and can pop macOS's *"FS-UAE quit unexpectedly"* CrashReporter dialog — a different dialog
from the window-restore requester above, owned by `ReportCrash` rather than FS-UAE, and
therefore **not** a blocker, just noise for the user to dismiss.

An early version of the `emulator-exited` test used it and so crashed FS-UAE once per suite
run. Use a **stub binary** that exits non-zero instead; it exercises the same detection path
with no crash. There is no clean quick-refusal path in FS-UAE — a missing or unreadable
Kickstart, and a missing floppy, all leave it running rather than exiting.

Check for accumulating crash reports after emulator work:

```bash
ls ~/Library/Logs/DiagnosticReports/ | grep -c '^fs-uae'
```

If genuine FS-UAE crashes ever recur from a cause outside our control, the dialog itself can
be silenced — but it is user-wide across **all** applications, so ask first:

```bash
defaults write com.apple.CrashReporter DialogType none          # silence
defaults write com.apple.CrashReporter DialogType crashreport   # restore default
```

**Diagnosing a crash:** compare `procLaunch` against `captureTime` in the `.ips` report. A
sub-second gap means a *startup* crash, which points at the configuration; a gap matching the
run length means a shutdown crash, which would point at how the harness stops it. Ten reports
were briefly misattributed to the SIGTERM teardown before this check showed all ten were
0.83–0.88 s after launch.

## Never test against the user's real images

Everything in `source-files-do-not-add-to-git/` is licensed source material or a real disk
image. Build fixtures from scratch; treat that directory as read-only.

## Raw devices

Never write to `/dev/*` during testing, and never widen amibuilder's device guard rails to
make a test pass. A wrong device write destroys a PiStorm card's Emu68 partition and is not
recoverable.
