"""Validation against a real AmigaOS via FS-UAE.

Split into two groups:

* Tests that exercise the harness itself -- configuration rendering, AmigaDOS script
  generation, Startup-Sequence injection. These always run, because they need no
  emulator, and they cover most of the harness's logic.

* Tests that actually boot an image. Marked `emulator` and skipped unless the machine
  is configured for it.

To enable the boot tests:

    export AMIBUILDER_FSUAE=/Applications/FS-UAE.app/Contents/MacOS/fs-uae
    export AMIBUILDER_KICKSTART=$HOME/Amiga/roms/kick31.rom
    export AMIBUILDER_BOOT_IMAGE=$HOME/Amiga/images/workbench-3.2.hdf

`AMIBUILDER_BOOT_IMAGE` must be a bootable AmigaOS install. Nothing here can supply one:
Kickstart ROMs and AmigaOS are licensed software.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from emulator import harness
from helpers import images


# ---------------------------------------------------------------------------
# Harness logic, no emulator required
# ---------------------------------------------------------------------------


def test_config_mounts_image_and_results_directory(tmp_path):
    cfg = _config_keys(
        harness.build_config(
            image="/img/test.hdf",
            results_dir=str(tmp_path),
            kickstart="/roms/kick31.rom",
        )
    )
    assert cfg["hard_drive_0"] == "/img/test.hdf"
    assert cfg["hard_drive_1"] == str(tmp_path)
    assert cfg["hard_drive_1_label"] == "RESULTS"
    assert cfg["kickstart_file"] == "/roms/kick31.rom"


def _config_keys(cfg: str) -> dict[str, str]:
    """Parse a rendered config into key/value pairs.

    Deliberately not a substring search: pytest names temp directories after the test,
    so a path can contain an option name and make a naive `in` check lie.
    """
    out = {}
    for line in cfg.splitlines():
        if "=" in line and not line.startswith("["):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_config_omits_serial_port_unless_requested(tmp_path):
    without = _config_keys(
        harness.build_config(image="/i.hdf", results_dir=str(tmp_path),
                             kickstart="/k.rom")
    )
    assert "serial_port" not in without

    with_serial = _config_keys(
        harness.build_config(image="/i.hdf", results_dir=str(tmp_path),
                             kickstart="/k.rom", serial_port=1234)
    )
    assert with_serial["serial_port"] == "tcp://127.0.0.1:1234/wait"


def test_serial_uses_the_wait_form_so_no_output_is_lost(tmp_path):
    """The /wait suffix makes FS-UAE block on boot until the host connects."""
    cfg = _config_keys(
        harness.build_config(
            image="/i.hdf", results_dir=str(tmp_path), kickstart="/k.rom",
            serial_port=9999,
        )
    )
    assert cfg["serial_port"].endswith("/wait"), (
        f"expected the waiting form, got {cfg['serial_port']!r}"
    )


def test_script_writes_sentinel_last():
    """The sentinel is the completion signal, so nothing may follow it."""
    script = harness.make_test_script(["Info", "Version"])
    lines = [ln for ln in script.splitlines() if ln.strip()]
    assert harness.SENTINEL in lines[-1]
    assert "Info" in script and "Version" in script


def test_script_tolerates_failing_commands():
    """FAILAT 21 stops a non-zero return code aborting before the sentinel is written."""
    script = harness.make_test_script(["ThisWillFail"])
    assert script.splitlines()[0] == "FAILAT 21"
    assert harness.SENTINEL in script


def test_script_sets_up_env_and_t_assigns():
    """Injecting a Startup-Sequence replaces the one that normally assigns ENV: and T:.

    Without these, any command touching ENV: raises a "Please insert volume ENV"
    requester, which cannot be dismissed from the host, so the run hangs until timeout.
    Copied from the AmigaOS 3.2 install disk's own startup.
    """
    script = harness.make_test_script(["Info"])
    assert "Assign >NIL: ENV: RAM:ENV" in script
    assert "Assign >NIL: T: RAM:T" in script
    assert "MakeDir >NIL: RAM:T RAM:ENV" in script
    assert "NOREQ" in script, "the ENVARC: copy must not raise its own requester"


def test_env_setup_can_be_disabled():
    script = harness.make_test_script(["Info"], setup_env=False)
    assert "RAM:ENV" not in script
    assert script.splitlines()[0] == "FAILAT 21"


def test_external_commands_are_guarded_by_an_existence_check():
    """AmigaDOS has no stderr redirection, so a missing command produces silence.

    Guarding with IF NOT EXISTS turns an invisible failure into a log line.
    """
    script = harness.make_test_script(["Type SYS:foo"])
    assert "IF NOT EXISTS C:Type" in script
    assert harness.MISSING_MARK in script


def test_shell_internal_commands_are_not_guarded():
    """Echo and friends live in the Shell, not C:, so probing for them would always fail."""
    script = harness.make_test_script(['Echo "hi"'])
    assert "IF NOT EXISTS" not in script


def test_explicit_paths_are_not_guarded():
    script = harness.make_test_script(["SYS:System/Format DRIVE DH0:"])
    assert "IF NOT EXISTS" not in script


def test_progress_is_written_before_each_command():
    """So the last progress line names the step that stalled, not the last one to finish."""
    script = harness.make_test_script(["Info", "Assign"])
    lines = [ln.strip() for ln in script.splitlines()]
    prog_info = next(i for i, ln in enumerate(lines) if '"0: Info"' in ln)
    ran_info = next(i for i, ln in enumerate(lines) if ln.startswith("Info >>"))
    assert prog_info < ran_info


def test_result_diagnosis_distinguishes_failure_modes(tmp_path):
    """A stall, a non-start and a timeout need different explanations."""
    results = tmp_path / "r"
    results.mkdir()

    never_started = harness.AmigaRunResult(
        completed=False, seconds=30.0, results_dir=results
    )
    assert never_started.outcome == harness.OUTCOME_NEVER_STARTED
    assert "NOTHING WAS EVER WRITTEN" in never_started.diagnosis()

    (results / harness.PROGRESS).write_text("setup\n0: Info\n")
    stalled = harness.AmigaRunResult(
        completed=False, seconds=30.0, results_dir=results,
        files={harness.PROGRESS: "setup\n0: Info\n"}, stalled_at="0: Info",
    )
    assert stalled.outcome == harness.OUTCOME_STALLED
    assert "STALLED" in stalled.diagnosis() and "0: Info" in stalled.diagnosis()
    assert "requester" in stalled.diagnosis()

    timed_out = harness.AmigaRunResult(
        completed=False, seconds=90.0, results_dir=results,
        files={harness.PROGRESS: "setup\n0: Info\n"},
    )
    assert timed_out.outcome == harness.OUTCOME_TIMEOUT
    assert "TIMED OUT" in timed_out.diagnosis()

    done = harness.AmigaRunResult(completed=True, seconds=6.0, results_dir=results)
    assert done.outcome == harness.OUTCOME_COMPLETED
    assert done.diagnosis() == "completed"


def test_outcome_is_never_inconsistent_with_completed(tmp_path):
    """A result must not be able to claim `completed=False, outcome=completed`.

    The first cut of the outcome field defaulted to `completed`, so any caller building a
    failure result by hand got a contradictory object whose diagnosis then fell through
    to the wrong branch.
    """
    failed = harness.AmigaRunResult(
        completed=False, seconds=1.0, results_dir=tmp_path
    )
    assert failed.outcome != harness.OUTCOME_COMPLETED

    succeeded = harness.AmigaRunResult(
        completed=True, seconds=1.0, results_dir=tmp_path
    )
    assert succeeded.outcome == harness.OUTCOME_COMPLETED


def test_missing_commands_are_extracted_from_the_log(tmp_path):
    res = harness.AmigaRunResult(
        completed=True, seconds=1.0, results_dir=tmp_path,
        files={
            "log.txt": f"{harness.BEGIN_MARK}\n{harness.MISSING_MARK} Type\n"
                       f"{harness.END_MARK}\n"
        },
    )
    assert res.missing_commands == ["Type"]


def test_results_volume_cannot_win_the_boot_election(tmp_path):
    """A bootable results volume could shadow the image actually under test."""
    cfg = _config_keys(
        harness.build_config(image="/i.hdf", results_dir=str(tmp_path),
                             kickstart="/k.rom")
    )
    assert int(cfg["hard_drive_1_priority"]) < 0


def test_config_requires_a_boot_source(tmp_path):
    with pytest.raises(ValueError, match="image or a floppy"):
        harness.build_config(results_dir=str(tmp_path), kickstart="/k.rom")


def test_config_supports_floppy_boot(tmp_path):
    cfg = _config_keys(
        harness.build_config(floppy="/d.adf", results_dir=str(tmp_path),
                             kickstart="/k.rom")
    )
    assert cfg["floppy_drive_0"] == "/d.adf"
    assert "hard_drive_0" not in cfg


def test_script_brackets_output_with_markers():
    script = harness.make_test_script(["Info"])
    assert harness.BEGIN_MARK in script
    assert harness.END_MARK in script
    # First redirection creates the log, subsequent ones append.
    assert ">RESULTS:log.txt" in script
    assert ">>RESULTS:log.txt" in script


def test_script_honours_a_custom_results_volume():
    script = harness.make_test_script(["Info"], results_volume="OUT")
    assert ">OUT:log.txt" in script
    assert "RESULTS:" not in script


def test_inject_startup_sequence_replaces_an_existing_one(workdir):
    """Injection must delete first, since amitools refuses to overwrite."""
    path = images.make_plain_hdf(str(workdir / "boot.hdf"), size="10Mi", volume="Sys")
    images.write_files(path, {"S/Startup-Sequence": b"Echo original\n"})

    script = harness.make_test_script(["Info"])
    harness.inject_startup_sequence(path, script)

    out = str(workdir / "ss.txt")
    images.xdftool(path, "open", "+", "read", "S/Startup-Sequence", out)
    text = Path(out).read_text()

    assert "original" not in text, "the previous Startup-Sequence should be gone"
    assert harness.SENTINEL in text
    assert images.scan_is_ok(path)


def test_inject_startup_sequence_creates_s_directory_if_absent(workdir):
    path = images.make_plain_hdf(str(workdir / "bare.hdf"), size="10Mi", volume="Bare")

    harness.inject_startup_sequence(path, harness.make_test_script(["Info"]))

    listing = images.xdftool(path, "open", "+", "list").output
    assert "Startup-Sequence" in listing
    assert images.scan_is_ok(path)


def test_injection_works_on_an_rdb_partition(rdb_hdf):
    harness.inject_startup_sequence(rdb_hdf, harness.make_test_script(["Info"]), part=0)
    listing = images.xdftool(rdb_hdf, "open", "part=0", "+", "list").output
    assert "Startup-Sequence" in listing
    assert images.scan_is_ok(rdb_hdf)


# ---------------------------------------------------------------------------
# Real boot, requires configuration
# ---------------------------------------------------------------------------


REPO = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO / "source-files-do-not-add-to-git"


@pytest.fixture(scope="session")
def boot_floppy() -> str:
    """A bootable Amiga floppy, which needs no AmigaOS installed on a hard drive.

    The AmigaOS 3.2 CD's Install3.2.adf serves nicely: it is a real, bootable AmigaDOS
    environment. `AMIBUILDER_BOOT_FLOPPY` overrides.
    """
    env = os.environ.get("AMIBUILDER_BOOT_FLOPPY")
    if env:
        if not Path(env).exists():
            pytest.skip(f"boot floppy not found at {env}")
        return env
    for name in ("Install3.2.adf", "Install.adf"):
        p = SOURCE_DIR / name
        if p.exists():
            return str(p)
    pytest.skip(
        "no bootable ADF found in source-files-do-not-add-to-git/ "
        "(set AMIBUILDER_BOOT_FLOPPY)"
    )


@pytest.fixture(scope="session")
def boot_image() -> str:
    """A bootable AmigaOS hard drive image, if one has been supplied."""
    path = os.environ.get("AMIBUILDER_BOOT_IMAGE")
    if not path:
        pytest.skip("AMIBUILDER_BOOT_IMAGE is not set (needs a bootable AmigaOS image)")
    if not Path(path).exists():
        pytest.skip(f"boot image not found at {path}")
    return path


@pytest.mark.emulator
def test_real_amigaos_boots_and_reports_back(fsuae_config, boot_floppy, workdir):
    """The decisive test: a real AmigaOS runs our script and hands data back.

    This is the only check in the suite that is not ultimately circular -- everything
    else validates amitools against amitools.
    """
    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=["Version", "Info", "Assign"],
        timeout=120,
    )

    assert result.completed, (
        f"{result.diagnosis()}\nFiles produced: {sorted(result.files)}\n"
        f"Progress: {result.progress}\n"
        f"Emulator log tail:\n" + "\n".join(result.emulator_log.splitlines()[-25:])
    )

    log = result.file("log.txt")
    assert not result.missing_commands, (
        f"commands absent from this medium: {result.missing_commands}"
    )
    assert harness.BEGIN_MARK in log, f"missing begin marker:\n{log}"
    assert harness.END_MARK in log, f"script did not run to completion:\n{log}"
    assert "Kickstart" in log, f"Version produced no Kickstart line:\n{log}"
    assert "RESULTS" in log, "the results volume should be visible to AmigaDOS"


@pytest.mark.emulator
def test_injected_startup_sequence_actually_replaces_the_original(
    fsuae_config, boot_floppy, workdir
):
    """Proof the injection took effect rather than the original script running.

    The AmigaOS 3.2 install disk ships `S/Startup-sequence` with a lowercase 's'. A
    case-sensitive check silently fails to find it, leaves it in place, and the installer
    runs instead of our script -- which looks like a hang. This pins the fix.
    """
    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=["Echo \"INJECTED-SCRIPT-RAN\""],
        timeout=120,
    )
    assert result.completed
    assert "INJECTED-SCRIPT-RAN" in result.file("log.txt")


@pytest.mark.emulator
def test_amigados_info_reports_no_errors(fsuae_config, boot_floppy, workdir):
    """AmigaDOS `Info` surfaces problems amitools' own validator cannot.

    `Errs` is a per-volume error count and must be zero.
    """
    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=["Info"],
        timeout=120,
    )
    assert result.completed
    log = result.file("log.txt")

    for bad in ("not validated", "Not a DOS disk", "read error"):
        assert bad.lower() not in log.lower(), f"Info reported a problem:\n{log}"

    # Every mounted unit line should report 0 errors.
    for line in log.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0].startswith(("DF", "DH")):
            errs = parts[5]
            assert errs == "0", f"volume {parts[0]} reports {errs} errors:\n{line}"


@pytest.mark.emulator
def test_amigados_can_read_a_file_written_by_amitools(fsuae_config, boot_floppy,
                                                     workdir):
    """The check that catches a wrong-hash write: visible to amitools, invisible to AmigaDOS.

    A file written with the wrong international-hashing variant occupies space and reads
    back perfectly under amitools, while AmigaDOS cannot find it at all. Nothing except a
    real AmigaOS can detect that.
    """
    import shutil

    probe = str(workdir / "probe.adf")
    shutil.copy2(boot_floppy, probe)

    # Deliberately not round bytes, and containing a NUL, so a truncating or
    # text-translating read shows up.
    marker = b"AMIBUILDER\x00WROTE\xa0THIS\n" + bytes(range(256)) * 3
    images.write_files(probe, {"amibuilder-probe.bin": marker})

    result = harness.run_amiga(
        floppy=probe,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=[
            "List SYS:amibuilder-probe.bin",
            "Copy SYS:amibuilder-probe.bin TO RESULTS:extracted.bin",
        ],
        timeout=120,
    )
    assert result.completed, result.diagnosis()
    assert not result.missing_commands, (
        f"commands absent from this medium: {result.missing_commands}"
    )

    log = result.file("log.txt")
    assert "amibuilder-probe.bin" in log, f"AmigaDOS could not see the file:\n{log}"

    # The decisive check: AmigaDOS read every byte and handed them back unchanged.
    assert result.extracted("extracted.bin") == marker, (
        "bytes differ after a round trip through AmigaDOS -- possible hashing, size or "
        f"translation fault. Got {len(result.extracted('extracted.bin'))} bytes, "
        f"expected {len(marker)}"
    )


@pytest.mark.emulator
def test_a_directory_written_by_amitools_is_traversable(fsuae_config, boot_floppy,
                                                       workdir):
    """Directories need their hash chains right too, not just files."""
    import shutil

    probe = str(workdir / "dirprobe.adf")
    shutil.copy2(boot_floppy, probe)

    deep = b"DEEP-FILE-CONTENT\n" * 40
    images.write_files(probe, {
        "KipTest/nested/deeper/deep.bin": deep,
        "KipTest/top.txt": b"TOP-FILE\n",
    })

    result = harness.run_amiga(
        floppy=probe,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=[
            "List SYS:KipTest ALL",
            "Copy SYS:KipTest/nested/deeper/deep.bin TO RESULTS:deep.bin",
        ],
        timeout=120,
    )
    assert result.completed, result.diagnosis()
    assert not result.missing_commands, (
        f"commands absent from this medium: {result.missing_commands}"
    )

    log = result.file("log.txt")
    assert "deep.bin" in log, f"nested file not listed by AmigaDOS:\n{log}"
    assert "top.txt" in log.lower()

    # Three levels deep, so every intermediate directory's hash chain had to be right.
    assert result.extracted("deep.bin") == deep, (
        "nested file unreadable from AmigaDOS -- directory hash chains may be wrong"
    )


@pytest.mark.emulator
@pytest.mark.slow
def test_hard_drive_image_boots_if_one_is_supplied(fsuae_config, boot_image, workdir):
    """Same checks against a real installed AmigaOS hard drive, when available."""
    result = harness.run_amiga(
        image=boot_image,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=["Version", "Info", "Assign"],
        timeout=240,
    )
    assert result.completed, (
        f"image did not boot within {result.seconds:.0f}s.\n"
        + "\n".join(result.emulator_log.splitlines()[-25:])
    )
    assert harness.END_MARK in result.file("log.txt")


# ---------------------------------------------------------------------------
# Failure reporting
#
# Every emulator failure used to surface as `completed=False, stalled_at=None` with no
# reason attached, and the "nothing ever ran" case had no detection at all -- it burned
# the full timeout and then reported the same empty result as everything else. These
# tests pin the five outcomes apart.
# ---------------------------------------------------------------------------


def test_outcome_constants_are_distinct():
    outcomes = {
        harness.OUTCOME_COMPLETED,
        harness.OUTCOME_EMULATOR_EXITED,
        harness.OUTCOME_NEVER_STARTED,
        harness.OUTCOME_STALLED,
        harness.OUTCOME_TIMEOUT,
    }
    assert len(outcomes) == 5


def _result(**kw):
    """An AmigaRunResult with only the fields a diagnosis needs."""
    base = dict(completed=False, seconds=12.3, results_dir=Path("/tmp/none"))
    base.update(kw)
    return harness.AmigaRunResult(**base)


def test_diagnosis_names_an_emulator_exit_as_host_side():
    d = _result(outcome=harness.OUTCOME_EMULATOR_EXITED, exit_code=1,
                emulator_log="config rejected\nend of main function\n").diagnosis()
    assert "FS-UAE EXITED BY ITSELF" in d
    assert "exit code 1" in d
    assert "host-side" in d, "must not be mistaken for an Amiga hang"
    assert "end of main function" in d, "the log tail carries the actual reason"


def test_diagnosis_distinguishes_never_started_from_stalled():
    never = _result(outcome=harness.OUTCOME_NEVER_STARTED).diagnosis()
    stalled = _result(outcome=harness.OUTCOME_STALLED, stalled_at="step-3").diagnosis()

    assert "NOTHING WAS EVER WRITTEN" in never
    assert "requester shown before the script starts" in never

    assert "STALLED" in stalled and "step-3" in stalled
    assert "NOTHING WAS EVER WRITTEN" not in stalled


def test_diagnosis_reports_absent_results_explicitly():
    """"(none)" beats an empty list: it says the Amiga wrote nothing, rather than
    leaving the reader to infer it."""
    d = _result(outcome=harness.OUTCOME_NEVER_STARTED).diagnosis()
    assert "the Amiga wrote nothing" in d
    assert "progress: (none)" in d


def test_diagnosis_includes_the_artifacts_path(tmp_path):
    d = _result(outcome=harness.OUTCOME_TIMEOUT, artifacts_dir=tmp_path).diagnosis()
    assert str(tmp_path) in d


def test_emulator_log_tail_keeps_the_end_not_the_start():
    """A rejected config says why in its last lines; the first 40 are boilerplate."""
    log = "\n".join(f"line{i}" for i in range(40))
    tail = _result(emulator_log=log).emulator_log_tail(lines=5)
    assert "line39" in tail
    assert "line0" not in tail


def test_completed_diagnosis_stays_terse():
    assert harness.AmigaRunResult(
        completed=True, seconds=5.0, results_dir=Path("/tmp/none"),
        outcome=harness.OUTCOME_COMPLETED,
    ).diagnosis() == "completed"


@pytest.mark.emulator
def test_a_rejected_config_reports_emulator_exited(fsuae_config, boot_floppy, workdir):
    """The load-bearing test for the fix, against a real FS-UAE.

    `video_driver = none` makes FS-UAE refuse the config and exit in about a second --
    the same signature (no results, no progress, no stall) that previously took the full
    timeout and reported nothing useful. Verified empirically: none of `none`, `dummy`,
    `null` or `SDL_VIDEODRIVER=dummy` will run, because FS-UAE requires a real window.
    """
    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=["Info"],
        timeout=60,
        extra_config={"video_driver": "none"},
    )

    assert not result.completed
    assert result.outcome == harness.OUTCOME_EMULATOR_EXITED, result.diagnosis()
    assert result.exit_code is not None

    # The whole point: it must give up quickly rather than waiting out the timeout.
    assert result.seconds < 20, (
        f"took {result.seconds:.1f}s to notice FS-UAE had exited"
    )

    # And the failure must explain itself without another run.
    d = result.diagnosis()
    assert "host-side" in d
    assert result.artifacts_dir is not None
    assert (result.artifacts_dir / "fs-uae-output.log").exists()
    assert (result.artifacts_dir / "outcome.txt").exists()


@pytest.mark.emulator
def test_a_successful_run_reports_completed(fsuae_config, boot_floppy, workdir):
    """The control for the test above: the same call without the bad option succeeds.

    Without this, a change that broke every run would still satisfy the failure tests.
    """
    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=["Info"],
        timeout=90,
    )
    assert result.completed, result.diagnosis()
    assert result.outcome == harness.OUTCOME_COMPLETED
    assert result.exit_code is None
    assert result.artifacts_dir is None, "artifacts are only kept for failures"


# ---------------------------------------------------------------------------
# macOS window-restore requester
# ---------------------------------------------------------------------------


def test_launch_passes_the_persistence_override():
    """The actual fix for the window-restore requester, and the reason it is on argv.

    Killing FS-UAE -- which this harness always does -- makes macOS record an abnormal quit,
    and the next launch then shows a modal "unexpectedly quit while reopening windows"
    requester that nothing on the host can dismiss. FS-UAE never starts, so it produces no
    log at all, which is why the failure was invisible until the window was photographed.

    Measured: `NSQuitAlwaysKeepsWindows false` and deleting the saved-state directory both
    failed to prevent it. `-ApplePersistenceIgnoreState YES` on argv works, takes effect for
    that launch only, and leaves the user's preferences alone.
    """
    import inspect

    assert harness.RESTORE_SUPPRESSION_ARGS == ("-ApplePersistenceIgnoreState", "YES")

    src = inspect.getsource(harness.run_amiga)
    assert "RESTORE_SUPPRESSION_ARGS" in src, (
        "the override must be on the launch command line, not merely defined"
    )


def test_suppress_restore_dialog_removes_saved_state(monkeypatch, tmp_path):
    """Secondary housekeeping: clear state left by launches without the override."""
    fake_home = tmp_path
    state = fake_home / "Library" / "Saved Application State" / \
        f"{harness.FSUAE_BUNDLE_ID}.savedState"
    state.mkdir(parents=True)
    (state / "windows.plist").write_bytes(b"junk")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    assert harness.suppress_restore_dialog() is True
    assert not state.exists()

    # Idempotent: nothing to remove the second time, and no error.
    assert harness.suppress_restore_dialog() is False


def test_suppress_restore_dialog_targets_the_right_bundle():
    """A wrong bundle id would silently delete nothing, or something else's state."""
    assert harness.FSUAE_BUNDLE_ID == "no.fengestad.fs-uae"


def test_run_amiga_clears_saved_state_before_launching():
    """The suppression must be on the launch path, and before FS-UAE starts.

    Asserted structurally rather than by spying on a call, because reaching the launch
    point needs a real bootable floppy to copy and inject into -- licensed material that
    may be absent. Brittle to reformatting, deliberately: clearing the state *after* Popen
    would be useless, so the ordering is the property worth pinning.
    """
    import inspect

    src = inspect.getsource(harness.run_amiga)
    suppress_at = src.find("suppress_restore_dialog()")
    popen_at = src.find("subprocess.Popen(")

    assert suppress_at != -1, "run_amiga must clear FS-UAE's saved window state"
    assert popen_at != -1
    assert suppress_at < popen_at, (
        "the saved state must be cleared before FS-UAE is launched; clearing it "
        "afterwards would not prevent the requester"
    )
