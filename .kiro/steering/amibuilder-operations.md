# amibuilder: operational rules

Project-specific working rules for this repo. These are all things that have already cost
time at least once.

## Always clean up stray FS-UAE processes

FS-UAE runs **windowed** — it cannot run headless (verified: `video_driver = none`, `dummy`,
`null` and `SDL_VIDEODRIVER=dummy` all exit in about a second). So a run that goes wrong can
leave a window sitting on the user's desktop waiting for input, on a boot requester, or mid-
boot. The harness terminates the process it launched, but a killed pytest, a stalled
background shell or an early exception can orphan one.

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

Avoid `defaults write` on the user's system for this; the argv form is scoped to one launch.

## Never test against the user's real images

Everything in `source-files-do-not-add-to-git/` is licensed source material or a real disk
image. Build fixtures from scratch; treat that directory as read-only.

## Raw devices

Never write to `/dev/*` during testing, and never widen amibuilder's device guard rails to
make a test pass. A wrong device write destroys a PiStorm card's Emu68 partition and is not
recoverable.
