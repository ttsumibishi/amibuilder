"""Drive FS-UAE non-interactively to validate an image against a real AmigaOS.

Everything else in this suite validates images against amitools plus its own validator.
That is circular: it cannot catch a case where amitools and this project agree on
something AmigaOS rejects. Booting the image is the only test that closes that loop.

## How validation works

1. Copy the image under test, so the original is never modified.
2. Inject an AmigaDOS script as `S/Startup-Sequence` in the copy.
3. Mount a host directory as a second drive, volume `RESULTS:`.
4. Boot FS-UAE. The script writes output to `RESULTS:` and finally touches
   `RESULTS:done`, which is the completion signal.
5. The host polls for `done`, then terminates the emulator and parses the output.

Result files rather than the serial port are the primary channel because they need no
AmigaOS device configuration -- `SER:` requires a DOSDrivers entry, whereas a mounted
directory just works. The serial port is available in parallel for a live log and is
enabled when `capture_serial=True`; FS-UAE's `tcp://host:port/wait` form makes it block
during boot until the host connects, so no early output is lost.

## Status

UNVERIFIED end to end. The configuration builder and script injection are covered by
tests that run without an emulator. The boot path itself has not been exercised, because
it needs a Kickstart ROM and a bootable AmigaOS install that cannot be bundled here.
Treat the timings and the exact FS-UAE option spellings as needing confirmation on
first real use.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SENTINEL = "done"
BEGIN_MARK = "AMIBUILDER-BEGIN"
END_MARK = "AMIBUILDER-END"
MISSING_MARK = "AMIBUILDER-MISSING-COMMAND"

#: FS-UAE's macOS bundle identifier, needed to clear its saved window state.
FSUAE_BUNDLE_ID = "no.fengestad.fs-uae"

#: Passed on FS-UAE's command line to stop macOS restoring windows -- and therefore to stop
#: it ever offering to. Cocoa parses `-Key Value` pairs out of argv into NSUserDefaults, so
#: this is scoped to the one launch and leaves the user's preferences untouched. FS-UAE's own
#: argument parser ignores them.
#:
#: This is the fix for the requester described in suppress_restore_dialog(). Two other
#: candidates were tried and measured as ineffective: `NSQuitAlwaysKeepsWindows false` and
#: deleting the saved-state directory. Only ApplePersistenceIgnoreState works, because the
#: requester is the "crashed *while restoring*" variant rather than plain window saving.
RESTORE_SUPPRESSION_ARGS = ("-ApplePersistenceIgnoreState", "YES")


def _stop_emulator(proc: subprocess.Popen) -> None:
    """Ask FS-UAE to quit, then insist.

    SIGTERM first, deliberately: FS-UAE handles it and shuts down cleanly, flushing its
    filesystem cache and removing its state directory, which the log records as far as
    "end of main function".

    A note against a wrong turn taken here. Ten FS-UAE crash reports were briefly blamed on
    this SIGTERM path, and it was switched to SIGKILL to avoid running FS-UAE's teardown at
    all. That was the wrong diagnosis: every one of those crashes was measured at 0.83-0.88
    seconds after *launch*, so none of them happened at shutdown. They came from
    `video_driver = none`, which segfaults instead of refusing the config (see
    test_emulator.py). SIGTERM is fine, and the clean shutdown is worth keeping.
    """
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def suppress_restore_dialog() -> bool:
    """Remove any saved macOS window state FS-UAE has left behind.

    Secondary housekeeping only. `RESTORE_SUPPRESSION_ARGS` is what actually prevents the
    requester; this just clears state written by launches that did not carry those
    arguments, such as the user starting FS-UAE from the Finder.

    **Measured, so as not to mislead: deleting this directory does not by itself prevent
    the requester.** It was tried first and the dialog still appeared -- captured on screen
    to confirm it. Kept because leaving stale state around has no upside, but it is not the
    fix.

    Returns True if anything was removed.
    """
    state = (
        Path.home() / "Library" / "Saved Application State"
        / f"{FSUAE_BUNDLE_ID}.savedState"
    )
    if not state.exists():
        return False
    try:
        shutil.rmtree(state)
        return True
    except OSError:
        # Not fatal: the user may have set NSQuitAlwaysKeepsWindows instead.
        return False


# Why a run stopped. Distinguishing these matters: every failure used to surface as
# `completed=False, stalled_at=None`, which reads the same whether the emulator died in a
# second or the Amiga sat on a requester for the full timeout.
OUTCOME_COMPLETED = "completed"
#: FS-UAE exited on its own before the sentinel appeared -- bad config, missing ROM, or a
#: host-side failure. Reproducible with `video_driver = none`.
OUTCOME_EMULATOR_EXITED = "emulator-exited"
#: Nothing was ever written to the progress file: the medium did not boot, or the injected
#: Startup-Sequence never ran. Waiting out the full timeout achieves nothing.
OUTCOME_NEVER_STARTED = "never-started"
#: The script began and then stopped advancing. Almost always an AmigaDOS requester.
OUTCOME_STALLED = "stalled"
#: Still making progress when the clock ran out.
OUTCOME_TIMEOUT = "timeout"


@dataclass
class AmigaRunResult:
    """Outcome of one emulator run."""

    completed: bool
    seconds: float
    results_dir: Path
    serial_log: str = ""
    files: dict[str, str] = field(default_factory=dict)
    emulator_log: str = ""
    #: The step the script stopped advancing on, if it stalled. Usually means an
    #: AmigaDOS requester is waiting for input.
    stalled_at: str | None = None
    #: One of the OUTCOME_* constants above. Left empty it is inferred from the other
    #: fields, so a result can never claim `completed=False` with `outcome=completed`.
    outcome: str = ""
    #: FS-UAE's exit status, set only when it terminated by itself.
    exit_code: int | None = None
    #: Where the config and emulator log were kept for inspection after a failure.
    artifacts_dir: Path | None = None
    #: The **copy** of the boot medium the Amiga actually ran from, and therefore wrote to. The
    #: original is never modified, so a test that wants the drive as the Amiga left it -- files it
    #: created, files it deleted -- has to use this rather than the image it passed in.
    boot_medium: Path | None = None

    def __post_init__(self) -> None:
        if self.outcome:
            return
        if self.completed:
            self.outcome = OUTCOME_COMPLETED
        elif self.stalled_at:
            self.outcome = OUTCOME_STALLED
        elif not self.progress:
            self.outcome = OUTCOME_NEVER_STARTED
        else:
            self.outcome = OUTCOME_TIMEOUT

    @property
    def progress(self) -> list[str]:
        """Steps the script reached, in order."""
        return [ln for ln in self.file(PROGRESS).splitlines() if ln.strip()]

    @property
    def missing_commands(self) -> list[str]:
        """Commands that were not present on the medium.

        A minimal boot disk carries very few: the AmigaOS 3.2 install floppy has about
        26 commands in C: and no `Type`, for instance. Without this, a missing command
        looks identical to a filesystem fault.
        """
        return [
            ln.split(MISSING_MARK, 1)[1].strip()
            for ln in self.file("log.txt").splitlines()
            if MISSING_MARK in ln
        ]

    def extracted(self, name: str) -> bytes:
        """Raw bytes of a file the Amiga copied into the results volume.

        Copying a file out and comparing on the host is a stronger check than `Type`:
        it verifies AmigaDOS can read every byte, and it does not depend on which
        commands happen to be installed.
        """
        p = self.results_dir / name
        return p.read_bytes() if p.exists() else b""

    def emulator_log_tail(self, lines: int = 12) -> str:
        """The end of FS-UAE's own output.

        Included in every failure message. When FS-UAE refuses a config or cannot open a
        display it says so here and nowhere else, and without it such a run is
        indistinguishable from the Amiga hanging.
        """
        kept = [ln.rstrip() for ln in self.emulator_log.splitlines() if ln.strip()]
        return "\n".join(f"    | {ln}" for ln in kept[-lines:])

    def diagnosis(self) -> str:
        """A full explanation suitable for an assertion message.

        Deliberately verbose: an emulator failure is expensive to reproduce, so the
        assertion output has to carry enough to diagnose it without another run.
        """
        if self.completed:
            return "completed"

        if self.outcome == OUTCOME_EMULATOR_EXITED:
            head = (
                f"FS-UAE EXITED BY ITSELF after {self.seconds:.1f}s "
                f"(exit code {self.exit_code}) before the Amiga signalled completion. "
                "This is a host-side failure -- a rejected config, a missing or "
                "unreadable Kickstart, or no available display -- not an Amiga hang."
            )
        elif self.outcome == OUTCOME_NEVER_STARTED:
            head = (
                f"NOTHING WAS EVER WRITTEN after {self.seconds:.1f}s. The medium did not "
                "boot, or the injected Startup-Sequence never ran. A requester shown "
                "before the script starts looks like this."
            )
        elif self.outcome == OUTCOME_STALLED:
            head = (
                f"STALLED at step {self.stalled_at!r} after {self.seconds:.1f}s -- "
                "most likely an AmigaDOS requester waiting for input, which cannot be "
                "dismissed from the host."
            )
        else:
            reached = self.progress[-1] if self.progress else "nothing"
            head = f"TIMED OUT after {self.seconds:.1f}s, reached {reached!r}."

        parts = [head, f"  outcome: {self.outcome}"]
        if self.progress:
            parts.append(f"  progress: {' -> '.join(self.progress)}")
        else:
            parts.append("  progress: (none)")
        if self.files:
            parts.append(f"  result files: {sorted(self.files)}")
        else:
            parts.append("  result files: (none -- the Amiga wrote nothing)")
        if self.artifacts_dir:
            parts.append(f"  artifacts kept in: {self.artifacts_dir}")
        tail = self.emulator_log_tail()
        if tail:
            parts.append("  FS-UAE log tail:")
            parts.append(tail)
        return "\n".join(parts)

    @property
    def output(self) -> str:
        """Concatenated result files, in sorted order."""
        return "\n".join(self.files[k] for k in sorted(self.files))

    def file(self, name: str) -> str:
        return self.files.get(name, "")


#: Environment variable that brings FS-UAE's window back on screen.
VISIBLE_ENV_VAR = "AMIBUILDER_FSUAE_VISIBLE"


def window_is_visible() -> bool:
    """Whether FS-UAE should show its window. False by default.

    FS-UAE has no headless mode -- `video_driver = none` segfaults -- but `window_hidden`
    creates the window without ever showing it. Measured: the frontmost application is
    unchanged across a launch, where a normal launch takes focus within three seconds.
    That matters a great deal in practice, because a test run otherwise seizes the
    keyboard repeatedly and makes the machine unusable while it works.

    Note `window_minimized` does *not* work -- it was measured as silently ignored, with
    the window appearing at full size and taking focus anyway.

    The cost is that a hidden window is absent from `CGWindowList`, so `window_capture`
    cannot see it. Set `AMIBUILDER_FSUAE_VISIBLE=1` to get the window back when you need
    to watch a run or capture it.
    """
    return os.environ.get(VISIBLE_ENV_VAR, "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


def build_config(
    *,
    results_dir: str,
    kickstart: str,
    image: str | None = None,
    floppy: str | None = None,
    serial_port: int | None = None,
    model: str = "A1200",
    chip_memory: int = 2048,
    fast_memory: int = 8192,
    extra: dict[str, str] | None = None,
) -> str:
    """Render an FS-UAE configuration file.

    Exactly one boot source is expected:

    * `image` becomes `hard_drive_0`. FS-UAE detects an RDB image by its RDSK magic and
      mounts its partitions; a plain HDF is mounted as a single volume.
    * `floppy` becomes `floppy_drive_0`. Useful for booting a real Amiga floppy such as
      an install disk, which needs no AmigaOS installed on a hard drive.

    `results_dir` is always mounted as a directory hard drive appearing as `RESULTS:`,
    and is how the Amiga hands data back.
    """
    if image is None and floppy is None:
        raise ValueError("need either an image or a floppy to boot from")

    lines = [
        "[config]",
        f"amiga_model = {model}",
        f"kickstart_file = {kickstart}",
        f"chip_memory = {chip_memory}",
        f"fast_memory = {fast_memory}",
    ]
    if image is not None:
        lines.append(f"hard_drive_0 = {image}")
    if floppy is not None:
        lines.append(f"floppy_drive_0 = {floppy}")

    # The results volume must never be bootable, or it could win the boot election
    # against the image actually under test.
    lines += [
        f"hard_drive_1 = {results_dir}",
        "hard_drive_1_label = RESULTS",
        "hard_drive_1_priority = -128",
        # Keep the window small, silent and unable to steal input. It is hidden outright
        # unless the caller asked to see it -- see window_is_visible().
        "fullscreen = 0",
        "window_width = 320",
        "window_height = 240",
        "floppy_drive_volume = 0",
        "volume = 0",
        "automatic_input_grab = 0",
        # Run flat out; nothing here is timing-sensitive.
        "video_sync = 0",
        "warp_mode = 1",
    ]
    if not window_is_visible():
        lines.append("window_hidden = 1")
    if serial_port is not None:
        # The /wait suffix makes FS-UAE block during boot until we connect, so no
        # early serial output is lost.
        lines.append(f"serial_port = tcp://127.0.0.1:{serial_port}/wait")
    for k, v in (extra or {}).items():
        lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


#: Minimal AmigaDOS environment, mirroring what a real Startup-Sequence sets up.
#:
#: This matters because injecting a Startup-Sequence *replaces* the original, including
#: the part that assigns ENV: and T:. Without them, any command touching ENV: raises a
#: "Please insert volume ENV" requester, and the run sits there until it times out --
#: there is no way to dismiss an AmigaDOS requester from the host.
#:
#: Copied from the AmigaOS 3.2 install disk's own Startup-sequence, minus the parts that
#: start Workbench. `NOREQ` on the Copy suppresses a requester when ENVARC: is absent.
ENV_SETUP = [
    "FAILAT 21",
    "MakeDir >NIL: RAM:T RAM:ENV",
    "Copy >NIL: ENVARC: RAM:ENV QUIET ALL NOREQ",
    "Assign >NIL: ENV: RAM:ENV",
    "Assign >NIL: T: RAM:T",
]

PROGRESS = "progress"

#: Commands built into the AmigaDOS Shell, so absent from C: and not worth probing for.
#: Anything else is an external command that may simply not exist on a minimal medium.
SHELL_INTERNAL = frozenset({
    "alias", "ask", "cd", "echo", "else", "endcli", "endif", "endskip", "failat",
    "fault", "if", "lab", "newcli", "path", "prompt", "quit", "resident", "return",
    "run", "set", "setenv", "skip", "stack", "unalias", "unset", "unsetenv",
})


def _external_command(cmd: str) -> str | None:
    """The C: command a line invokes, or None if it is Shell-internal or a path.

    Used to emit an existence check. This matters because **AmigaDOS has no stderr
    redirection**: a missing command prints "Unknown command" to the console, which is
    invisible in a redirected log, so the step looks like it ran and produced nothing.
    That is indistinguishable from a filesystem fault unless we check explicitly.
    """
    first = cmd.strip().split()[0] if cmd.strip() else ""
    if not first or first.lower() in SHELL_INTERNAL:
        return None
    if ":" in first or "/" in first:
        return None  # already an explicit path; let it fail on its own terms
    return first


def make_test_script(
    commands: list[str],
    results_volume: str = "RESULTS",
    setup_env: bool = True,
    check_commands: bool = True,
) -> str:
    r"""Build an AmigaDOS Startup-Sequence that runs `commands` and reports back.

    Structure, in order:

    1. `FAILAT 21` so a non-zero return code cannot abort the script before the
       sentinel is written.
    2. Environment setup (see ENV_SETUP) unless `setup_env` is False.
    3. A begin marker, then each command with its output appended to `RESULTS:log.txt`.
    4. A progress line per command, so a stall can be pinpointed to a specific step.
    5. The end marker, then `RESULTS:done` -- the completion signal, which must be last.
    """
    v = results_volume
    out: list[str] = []
    if setup_env:
        out += ENV_SETUP
    else:
        out.append("FAILAT 21")

    out.append(f'Echo >{v}:{PROGRESS} "setup"')
    out.append(f'Echo >{v}:log.txt "{BEGIN_MARK}"')

    for i, cmd in enumerate(commands):
        # Progress is written *before* the command, so the last line names the step that
        # stalled rather than the last one that finished.
        out.append(f'Echo >>{v}:{PROGRESS} "{i}: {cmd}"')
        out.append(f'Echo >>{v}:log.txt "--- {cmd}"')

        ext = _external_command(cmd) if check_commands else None
        if ext:
            out.append(f"IF NOT EXISTS C:{ext}")
            out.append(f'  Echo >>{v}:log.txt "{MISSING_MARK} {ext}"')
            out.append("ELSE")
            out.append(f"  {cmd} >>{v}:log.txt")
            out.append("ENDIF")
        else:
            out.append(f"{cmd} >>{v}:log.txt")

    out += [
        f'Echo >>{v}:{PROGRESS} "complete"',
        f'Echo >>{v}:log.txt "{END_MARK}"',
        f'Echo >{v}:{SENTINEL} "ok"',
    ]
    return "\n".join(out) + "\n"


DEFAULT_CHECKS = [
    "Version",
    "Info",
    "Assign",
    "List SYS: ALL",
]


def inject_startup_sequence(image: str, script: str, part: int | None = None) -> None:
    """Replace `S/Startup-Sequence` in an image.

    amitools refuses to overwrite an existing file, so the old one is deleted first.
    That behaviour is pinned in test_amitools_regressions.py.
    """
    import sys
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from helpers import images

    open_cmd = ["open"] + ([f"part={part}"] if part is not None else [])

    # Comparisons must be case-insensitive. FFS is case-insensitive but case-preserving,
    # and real media varies: the AmigaOS 3.2 install disk ships "S/Startup-sequence"
    # with a lowercase 's', which a case-sensitive check silently fails to find --
    # leaving the original in place and the injected script never running.
    listing = images.xdftool(image, *open_cmd, "+", "list").output
    lower = listing.lower()

    if "startup-sequence" in lower:
        images.xdftool(image, *open_cmd, "+", "delete", "S/Startup-Sequence",
                       check=False)
    if "\n  s " not in lower and not lower.lstrip().startswith("s "):
        images.xdftool(image, *open_cmd, "+", "makedir", "S", check=False)

    with tempfile.NamedTemporaryFile("w", delete=False, newline="\n") as tf:
        tf.write(script)
        tmp = tf.name
    try:
        images.xdftool(image, *open_cmd, "+", "write", tmp, "S/Startup-Sequence")
    finally:
        os.unlink(tmp)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _drain_serial(port: int, sink: list[str], stop: threading.Event) -> None:
    """Connect to FS-UAE's serial socket and accumulate output until told to stop."""
    deadline = time.time() + 30
    conn = None
    while time.time() < deadline and not stop.is_set():
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            time.sleep(0.25)
    if conn is None:
        return
    conn.settimeout(0.5)
    try:
        while not stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            sink.append(chunk.decode("latin-1", errors="replace"))
    finally:
        conn.close()


def run_amiga(
    *,
    fsuae_binary: str,
    kickstart: str,
    workdir: str | Path,
    image: str | None = None,
    floppy: str | None = None,
    commands: list[str] | None = None,
    part: int | None = None,
    timeout: float = 90.0,
    stall_timeout: float = 20.0,
    boot_timeout: float = 45.0,
    capture_serial: bool = False,
    keep_artifacts: bool = True,
    model: str = "A1200",
    setup_env: bool = True,
    extra_config: dict[str, str] | None = None,
) -> AmigaRunResult:
    """Boot an image or floppy under FS-UAE, run `commands`, and collect the output.

    The boot medium is **copied first**, so the original is never modified -- injecting a
    Startup-Sequence is a destructive edit.

    Returns as soon as the outcome is known, which is one of five cases recorded in
    `AmigaRunResult.outcome`:

    * the Amiga writes the sentinel (`completed`)
    * FS-UAE exits by itself (`emulator-exited`) -- a host-side failure, typically in
      about a second
    * nothing is written within `boot_timeout` (`never-started`) -- the medium did not
      boot, or a requester appeared before the script ran
    * progress stops advancing for `stall_timeout` (`stalled`) -- an AmigaDOS requester,
      which cannot be dismissed from the host
    * `timeout` expires while still advancing (`timeout`)

    Bailing out early on the middle three matters because none of them improves by
    waiting, and because they used to be indistinguishable from each other: every
    failure surfaced as `completed=False, stalled_at=None` with no reason attached.

    A healthy run against the AmigaOS 3.2 install floppy takes about 7 seconds, so the
    defaults are generous.
    """
    if image is None and floppy is None:
        raise ValueError("need either an image or a floppy to boot from")

    workdir = Path(workdir)
    results = workdir / "results"
    results.mkdir(parents=True, exist_ok=True)

    script = make_test_script(commands or DEFAULT_CHECKS, setup_env=setup_env)

    test_image = test_floppy = None
    if image is not None:
        test_image = workdir / ("under-test" + Path(image).suffix)
        shutil.copy2(image, test_image)
        inject_startup_sequence(str(test_image), script, part=part)
    if floppy is not None:
        test_floppy = workdir / ("boot-" + Path(floppy).name)
        shutil.copy2(floppy, test_floppy)
        if image is None:
            # Booting the floppy, so the script has to live on it.
            inject_startup_sequence(str(test_floppy), script)

    port = _free_port() if capture_serial else None
    config = build_config(
        image=str(test_image) if test_image else None,
        floppy=str(test_floppy) if test_floppy else None,
        results_dir=str(results),
        kickstart=kickstart,
        serial_port=port,
        model=model,
        extra=extra_config,
    )
    config_path = workdir / "fs-uae.conf"
    config_path.write_text(config)

    serial_chunks: list[str] = []
    emu_out: list[str] = []
    stop = threading.Event()
    reader: threading.Thread | None = None

    # Housekeeping only; the real prevention is RESTORE_SUPPRESSION_ARGS on the launch
    # below. See suppress_restore_dialog().
    suppress_restore_dialog()

    proc = subprocess.Popen(
        [fsuae_binary, str(config_path), "--stdout", *RESTORE_SUPPRESSION_ARGS],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )

    # Drain the emulator's own output on a thread. Without this a full pipe buffer would
    # block FS-UAE, and its log is the first thing worth reading when a run fails.
    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            emu_out.append(line)

    out_reader = threading.Thread(target=_drain_stdout, daemon=True)
    out_reader.start()

    if port is not None:
        reader = threading.Thread(
            target=_drain_serial, args=(port, serial_chunks, stop), daemon=True
        )
        reader.start()

    sentinel = results / SENTINEL
    progress = results / PROGRESS
    started = time.time()
    completed = False
    stalled_at: str | None = None

    def progress_state() -> tuple[float, str]:
        """(mtime, last line) of the progress file, or (0, '') if absent."""
        try:
            text = progress.read_text(encoding="latin-1")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            return progress.stat().st_mtime, (lines[-1] if lines else "")
        except OSError:
            return 0.0, ""

    last_change = time.time()
    last_state = progress_state()
    outcome = OUTCOME_TIMEOUT
    exit_code: int | None = None

    try:
        while time.time() - started < timeout:
            if sentinel.exists():
                completed = True
                outcome = OUTCOME_COMPLETED
                # Give the Amiga a moment to flush any trailing writes.
                time.sleep(1.0)
                break

            if proc.poll() is not None:
                # FS-UAE gave up on its own. A host-side failure, and a fast one: a
                # rejected config exits in about a second. Reported distinctly because
                # nothing about the Amiga is at fault.
                outcome = OUTCOME_EMULATOR_EXITED
                exit_code = proc.returncode
                break

            state = progress_state()
            if state != last_state:
                last_state = state
                last_change = time.time()
            elif state[1] and time.time() - last_change > stall_timeout:
                # The script started and then stopped advancing. Almost always an
                # AmigaDOS requester waiting for input, which cannot be dismissed from
                # the host, so waiting out the full timeout achieves nothing.
                outcome = OUTCOME_STALLED
                stalled_at = state[1]
                break
            elif not state[1] and time.time() - started > boot_timeout:
                # Nothing has been written at all. The old code had no case for this, so
                # a medium that never booted burned the entire timeout and then reported
                # the same empty result as every other failure.
                outcome = OUTCOME_NEVER_STARTED
                break
            time.sleep(0.5)
    finally:
        stop.set()
        if proc.poll() is None:
            _stop_emulator(proc)
        if reader is not None:
            reader.join(timeout=5)
        out_reader.join(timeout=5)

    files = {}
    for p in sorted(results.iterdir()):
        if p.is_file():
            try:
                files[p.name] = p.read_text(encoding="latin-1")
            except OSError:
                files[p.name] = "<unreadable>"

    if not keep_artifacts:
        for p in (test_image, test_floppy):
            if p is not None:
                p.unlink(missing_ok=True)

    emulator_log = "".join(emu_out)

    # On failure, write the emulator log next to the config that produced it. pytest's
    # tmp_path is kept for the last few runs, so this survives long enough to read --
    # and an emulator failure is expensive enough to reproduce that losing the log to a
    # truncated assertion message is not acceptable.
    artifacts_dir: Path | None = None
    if not completed:
        try:
            (workdir / "fs-uae-output.log").write_text(emulator_log, encoding="utf-8")
            (workdir / "outcome.txt").write_text(
                f"outcome={outcome}\nexit_code={exit_code}\n"
                f"seconds={time.time() - started:.1f}\nstalled_at={stalled_at}\n",
                encoding="utf-8",
            )
            artifacts_dir = workdir
        except OSError:
            pass

    return AmigaRunResult(
        completed=completed,
        seconds=time.time() - started,
        results_dir=results,
        serial_log="".join(serial_chunks),
        files=files,
        emulator_log=emulator_log,
        stalled_at=stalled_at,
        outcome=outcome,
        exit_code=exit_code,
        artifacts_dir=artifacts_dir,
        boot_medium=test_image or test_floppy,
    )
