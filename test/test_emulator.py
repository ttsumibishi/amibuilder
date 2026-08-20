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
import re
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
def test_an_emulator_that_exits_is_detected(boot_floppy, workdir):
    """Detection of `emulator-exited`, using a stub in place of FS-UAE.

    A stub is used rather than a real misconfiguration because **FS-UAE has no clean
    quick-refusal path**, measured:

    | trigger | result |
    |---|---|
    | `video_driver = none` | exit -11, SIGSEGV, files a crash report |
    | missing Kickstart | keeps running, no exit |
    | unreadable Kickstart | keeps running, no exit |
    | missing floppy | keeps running, no exit |

    The first version of this test used `video_driver = none`, which meant every suite run
    deliberately segfaulted FS-UAE, filed a crash report in
    `~/Library/Logs/DiagnosticReports` and could pop macOS's CrashReporter dialog. A stub
    exercises the same detection path with no crash, no dialog and no waiting.

    Everything up to the launch is still the real code path: config generation, floppy copy
    and Startup-Sequence injection.
    """
    stub = workdir / "fake-fs-uae"
    stub.write_text("#!/bin/sh\necho 'stub: refusing config' >&2\nexit 3\n")
    stub.chmod(0o755)

    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=str(stub),
        kickstart="/irrelevant/for/a/stub.rom",
        workdir=workdir,
        commands=["Info"],
        timeout=60,
    )

    assert not result.completed
    assert result.outcome == harness.OUTCOME_EMULATOR_EXITED, result.diagnosis()
    assert result.exit_code == 3

    # The point of the outcome: give up as soon as the process is gone, rather than
    # waiting out the full timeout for a process that will never write anything.
    assert result.seconds < 20, f"took {result.seconds:.1f}s to notice the exit"

    # And the failure must explain itself without another run.
    d = result.diagnosis()
    assert "host-side" in d
    assert "exit code 3" in d
    assert "stub: refusing config" in d, "the log tail must carry the reason"
    assert result.artifacts_dir is not None
    assert (result.artifacts_dir / "fs-uae-output.log").exists()
    assert (result.artifacts_dir / "outcome.txt").exists()


def test_video_driver_none_is_not_used_as_a_failure_trigger():
    """It segfaults rather than refusing, so it must not come back as a test fixture.

    Measured: exit -11 (SIGSEGV) about 0.85s after launch, one crash report per run, and a
    CrashReporter dialog for the user to dismiss.
    """
    import inspect

    def normalised(line: str) -> str:
        """Strip quotes and spaces so any spelling of the mapping form is comparable."""
        return line.replace('"', "").replace("'", "").replace(" ", "")

    # This guard has to exclude its own body. A source-scanning check that lives in the file
    # it scans will otherwise match itself -- which it did, twice: first on the loose
    # two-words-on-a-line form, then on the assertions written to prove the matcher works.
    own_source = inspect.getsource(test_video_driver_none_is_not_used_as_a_failure_trigger)
    src = Path(__file__).read_text().replace(own_source, "")

    live = [
        ln for ln in src.splitlines()
        if "video_driver:none" in normalised(ln) and not ln.strip().startswith("#")
    ]
    assert not live, f"video_driver=none used as a live trigger: {live}"

    # Prove the matcher can still fire, so it cannot rot into a no-op that always passes.
    for spelling in ('extra_config={"video_driver": "none"}',
                     "extra={'video_driver': 'none'}"):
        assert "video_driver:none" in normalised(spelling)
    assert "video_driver:none" not in normalised('extra={"video_driver": "opengl"}')


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


# ---------------------------------------------------------------------------
# The end-to-end check: capture a drive, compose it back, and boot both
#
# This is the only test in the project whose verdict comes from outside this codebase. Everything
# else compares amibuilder's output against amibuilder's reading of it, or against amitools --
# which shares a lineage with our writer. Here a real Kickstart's `dosboot` reads our RDB, a real
# FFS implementation mounts our partition, and real AmigaDOS commands report what they found.
#
# One session-scoped fixture does the expensive work (build ~6 s, two boots ~20 s each) and the
# tests below are cheap assertions over its output, so a failure names one property rather than
# "the end-to-end test broke".
# ---------------------------------------------------------------------------

#: Protection bits set on two files nothing reads while booting, so the source drive carries more
#: than one protection value. Without this every entry is an identical `----rwed` and the
#: comparison could not catch a bug that reset protection bits.
BOOT_PROTECT = {
    "Storage/DOSDrivers/CD0": "hsp-rwed",
    "Update/Release": "h-------",
}

#: `S/Startup-Sequence` is replaced by the harness in each copy to drive the test, and writing it
#: restamps its parent directory. Both differences are expected; nothing else is.
INJECTED_PATHS = ("SYS:S", "SYS:S/Startup-Sequence")

BOOT_COMMANDS = ["Version", "Info", "Assign", "List SYS: ALL"]


def _boot(image: str, fsuae_config: dict[str, str], workdir: Path) -> harness.AmigaRunResult:
    result = harness.run_amiga(
        image=image,
        part=0,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=workdir,
        commands=BOOT_COMMANDS,
        timeout=180,
        boot_timeout=90,
    )
    assert result.completed, (
        f"{image} did not boot.\n{result.diagnosis()}\n"
        f"Emulator log tail:\n" + "\n".join(result.emulator_log.splitlines()[-25:])
    )
    return result


@pytest.fixture(scope="session")
def composed_boot(fsuae_config, boot_floppy, tmp_path_factory) -> dict:
    """Build a real-AmigaOS drive, capture it, compose it back, and boot both.

    Ordered so a failure is interpretable. The **source** drive is booted first: if a composed
    drive will not boot, that could equally mean install-floppy contents do not boot from a hard
    disk at all, and without the baseline there is no way to tell which. That ordering earned its
    keep the first time this was run by hand -- the baseline failed, and the cause turned out to be
    FS-UAE unable to launch at all.
    """
    from amibuilder.layers import compose as CP
    from amibuilder.layers import store as S
    from emulator import amigados

    root = tmp_path_factory.mktemp("composed-boot")
    source = images.make_bootable_hd_from_adf(
        boot_floppy, str(root / "source.hdf"), protect=BOOT_PROTECT
    )

    source_run = _boot(source, fsuae_config, root / "run-source")

    store = S.Store(str(root / "store"))
    store.init()
    code = _cli(["snap", "create", source, "--label", "os", "--store", store.root])
    assert code == 0, "capturing the source drive failed"

    composed = str(root / "composed.hdf")
    code = _cli(["compose", "--stack", "os", "--into", composed, "--format", "rdb",
                 "--store", store.root])
    assert code == 0, "composing the drive failed"

    composed_run = _boot(composed, fsuae_config, root / "run-composed")

    source_log = source_run.file("log.txt")
    composed_log = composed_run.file("log.txt")
    return {
        "source": source,
        "composed": composed,
        "source_log": source_log,
        "composed_log": composed_log,
        "source_info": amigados.parse_info(source_log),
        "composed_info": amigados.parse_info(composed_log),
        "source_entries": amigados.parse_list_all(source_log),
        "composed_entries": amigados.parse_list_all(composed_log),
        "source_totals": amigados.parse_grand_total(source_log),
        "composed_totals": amigados.parse_grand_total(composed_log),
        "store": store,
    }


def _cli(argv: list[str]) -> int:
    from amibuilder.cli import main

    return main(argv)


@pytest.mark.emulator
def test_composed_drive_boots_real_amigaos(composed_boot):
    """The headline: a drive built from a layer store runs a real AmigaOS to completion."""
    assert "Kickstart" in composed_boot["composed_log"]
    assert harness.END_MARK in composed_boot["composed_log"]


@pytest.mark.emulator
def test_composed_drive_is_mounted_read_write(composed_boot):
    dh0 = composed_boot["composed_info"]["DH0"]
    assert dh0.status == "Read/Write"
    assert dh0.name == "Workbench"


@pytest.mark.emulator
def test_composed_drive_reports_no_filesystem_errors(composed_boot):
    """`Errs` is maintained by FFS itself, so this is AmigaOS's own verdict on our image."""
    dh0 = composed_boot["composed_info"]["DH0"]
    assert dh0.is_healthy, f"AmigaDOS reported {dh0.errs} error(s) on the composed drive"


@pytest.mark.emulator
def test_composed_drive_uses_the_same_space_as_the_source(composed_boot):
    """Block-level agreement. A different used-block count would mean a different layout."""
    source, composed = composed_boot["source_info"]["DH0"], composed_boot["composed_info"]["DH0"]
    assert (composed.used, composed.free) == (source.used, source.free)


@pytest.mark.emulator
def test_amigados_counts_the_same_totals_on_both_drives(composed_boot):
    assert composed_boot["composed_totals"] == composed_boot["source_totals"]


@pytest.mark.emulator
def test_amigados_totals_agree_with_what_capture_recorded(composed_boot):
    """Two unrelated implementations counting the same drive.

    AmigaDOS walks FFS on a 68k CPU; `snap show` reads a manifest written by our capture. If they
    agree on file and directory counts, neither is inventing entries.
    """
    from amibuilder.layers import manifest as M

    store = composed_boot["store"]
    entries = store.read_manifest(store.resolve("os"))
    files = sum(1 for entry in entries if entry.kind == M.FILE)
    dirs = sum(1 for entry in entries if entry.kind == M.DIR)

    totals = composed_boot["source_totals"]
    assert totals is not None
    assert files == totals.files, (
        f"capture recorded {files} files, AmigaDOS counted {totals.files}"
    )
    assert dirs == totals.dirs, (
        f"capture recorded {dirs} directories, AmigaDOS counted {totals.dirs}"
    )


@pytest.mark.emulator
def test_every_path_survives_composition(composed_boot):
    from emulator import amigados

    result = amigados.compare_listings(
        composed_boot["source_entries"], composed_boot["composed_entries"]
    )
    assert not result.missing, f"absent from the composed drive: {list(result.missing)}"
    assert not result.extra, f"unexpectedly present: {list(result.extra)}"


@pytest.mark.emulator
def test_the_only_differences_are_the_harness_injected_script(composed_boot):
    """Asserted as an exact set rather than filtered out.

    Excluding the injected paths before comparing would let a genuine regression hide behind the
    exclusion. Requiring the difference set to be *exactly* the injected paths means a third
    difference fails the test.
    """
    from emulator import amigados

    result = amigados.compare_listings(
        composed_boot["source_entries"], composed_boot["composed_entries"]
    )
    assert set(result.paths_differing) <= set(INJECTED_PATHS), (
        "unexpected differences beyond the injected Startup-Sequence:\n" + result.describe()
    )


@pytest.mark.emulator
def test_protection_bits_survive_to_a_real_amiga(composed_boot):
    """Requires the source to carry more than one protection value, or this proves nothing.

    A comparison over uniformly-protected entries would pass even if composition reset every bit,
    so the variety is asserted first.
    """
    source = composed_boot["source_entries"]
    composed = composed_boot["composed_entries"]

    values = {entry.protect for entry in source.values()}
    assert len(values) > 1, (
        f"the source drive has only one protection value ({values}), so this check is vacuous"
    )

    for path, entry in source.items():
        if path in INJECTED_PATHS:
            continue
        assert composed[path].protect == entry.protect, (
            f"{path}: protection {composed[path].protect} != {entry.protect}"
        )


@pytest.mark.emulator
def test_timestamps_survive_to_a_real_amiga(composed_boot):
    """AmigaDOS renders dates from the on-disk triple, so this checks our timestamps end to end."""
    source = composed_boot["source_entries"]
    composed = composed_boot["composed_entries"]
    for path, entry in source.items():
        if path in INJECTED_PATHS:
            continue
        assert composed[path].when == entry.when, (
            f"{path}: timestamp {composed[path].when} != {entry.when}"
        )


@pytest.mark.emulator
def test_assigns_resolve_to_the_composed_volume(composed_boot):
    """SYS:, C:, S:, LIBS:, DEVS: and L: must all land on our volume, or the OS is only half up."""
    log = composed_boot["composed_log"]
    for name in ("SYS", "C", "S", "LIBS", "DEVS", "L"):
        assert f"{name} " in log, f"{name}: assign missing from the composed drive"
    assert "Workbench:" in log


@pytest.mark.emulator
def test_every_command_ran_on_the_composed_drive(composed_boot):
    """A command absent from C: would be reported rather than failing, so check none were."""
    for command in BOOT_COMMANDS:
        assert f"--- {command}" in composed_boot["composed_log"]
    assert harness.MISSING_MARK not in composed_boot["composed_log"]


# ---------------------------------------------------------------------------
# Multiple partitions on one drive
#
# This is the arrangement the project actually exists for: Workbench to restore to stock, Work and
# Saves to leave alone. The single-partition test above cannot reach any of it -- the RDB partition
# chain, cylinder ranges that must not overlap, the boot election choosing among candidates, or a
# per-partition DosType.
#
# The middle volume deliberately uses a different DosType (DOS\1, plain FFS) from the other two
# (DOS\3, FFS+intl), so a compose that defaulted the DosType instead of reproducing it would
# produce a drive a real Amiga mounts differently.
# ---------------------------------------------------------------------------

MULTI_WORK_FILES = {
    "Games/Readme": b"work partition, first file\n",
    "Games/Lemmings/data.bin": bytes(range(256)) * 12,
    "Docs/notes.txt": b"a" * 3000,
    "Docs/Deep/Deeper/buried": b"still here\n",
    # A zero-length file, which AmigaDOS renders as `empty` rather than `0`. Present on purpose:
    # the first version of the parser dropped it silently, which would have let a composition bug
    # that lost every empty file pass unnoticed.
    "empty-file": b"",
}

MULTI_SAVES_FILES = {
    "Slot1/save.dat": bytes(1500),
    "Slot2/save.dat": bytes(2500),
    "index": b"two slots\n",
}

MULTI_COMMANDS = ["Version", "Info", "Assign", "List SYS: ALL", "List Work: ALL",
                  "List Saves: ALL"]


def _multi_specs(adf: str) -> list[images.VolumeSpec]:
    return [
        images.VolumeSpec(
            partition=images.Partition(size="30MiB", dos_type="ffs+intl", bootable=True,
                                       volume="Workbench"),
            from_adf=adf,
            protect=dict(BOOT_PROTECT),
        ),
        images.VolumeSpec(
            partition=images.Partition(size="15MiB", dos_type="ffs+intl", volume="Work"),
            files=MULTI_WORK_FILES,
            protect={"Games/Readme": "h-------"},
        ),
        images.VolumeSpec(
            partition=images.Partition(dos_type="ffs", volume="Saves"),
            files=MULTI_SAVES_FILES,
            protect={"index": "hs------"},
        ),
    ]


@pytest.fixture(scope="session")
def multi_source_drive(boot_floppy, tmp_path_factory) -> str:
    """A three-partition drive with real AmigaOS on the boot volume, built once per session.

    Shared by the tests below. Nothing modifies it: `run_amiga` copies its boot medium before
    injecting a script, so each run gets its own copy to write to.
    """
    root = tmp_path_factory.mktemp("multi-source")
    return images.make_multi_volume_hd(
        str(root / "multi-source.hdf"), _multi_specs(boot_floppy), size="60Mi"
    )


@pytest.fixture(scope="session")
def multi_volume_boot(fsuae_config, multi_source_drive, tmp_path_factory) -> dict:
    """Boot a three-partition drive, compose it back, and boot that too.

    Same ordering discipline as the single-partition fixture: the source is booted first, so a
    composed failure can be told apart from "this drive layout never booted in the first place".
    """
    from emulator import amigados
    from amibuilder.layers import store as S

    root = tmp_path_factory.mktemp("multi-boot")
    source = multi_source_drive

    def boot(image: str, tag: str) -> dict:
        result = harness.run_amiga(
            image=image, part=0,
            fsuae_binary=fsuae_config["binary"], kickstart=fsuae_config["rom"],
            workdir=root / f"run-{tag}", commands=MULTI_COMMANDS,
            timeout=240, boot_timeout=90,
        )
        assert result.completed, (
            f"{tag} drive did not boot.\n{result.diagnosis()}\n"
            + "\n".join(result.emulator_log.splitlines()[-25:])
        )
        log = result.file("log.txt")
        sections = amigados.split_list_sections(log)
        return {
            "log": log,
            "info": amigados.parse_info(log),
            "listings": {vol: amigados.parse_list_all(text) for vol, text in sections.items()},
            "totals": {vol: amigados.parse_grand_total(text) for vol, text in sections.items()},
        }

    source_run = boot(source, "source")

    store = S.Store(str(root / "store"))
    store.init()
    assert _cli(["snap", "create", source, "--label", "multi", "--store", store.root]) == 0
    composed = str(root / "multi-composed.hdf")
    assert _cli(["compose", "--stack", "multi", "--into", composed, "--format", "rdb",
                 "--store", store.root]) == 0

    return {
        "source": source,
        "composed": composed,
        "source_run": source_run,
        "composed_run": boot(composed, "composed"),
        "store": store,
    }


@pytest.mark.emulator
def test_all_three_composed_volumes_mount(multi_volume_boot):
    """The claim in one line: a real Amiga mounts every partition we composed."""
    names = {row.name for row in multi_volume_boot["composed_run"]["info"].values()}
    assert {"Workbench", "Work", "Saves"} <= names, f"only mounted: {sorted(names)}"


@pytest.mark.emulator
def test_the_same_volumes_mount_on_both_drives(multi_volume_boot):
    source = {r.name for r in multi_volume_boot["source_run"]["info"].values()}
    composed = {r.name for r in multi_volume_boot["composed_run"]["info"].values()}
    assert composed == source


@pytest.mark.emulator
def test_no_composed_volume_reports_filesystem_errors(multi_volume_boot):
    """Per-volume `Errs`, so a partition whose bounds were miscomputed shows up here."""
    for unit, row in multi_volume_boot["composed_run"]["info"].items():
        if row.name == "RESULTS":
            continue  # the harness's host directory, not part of the drive
        assert row.is_healthy, f"{unit} ({row.name}) reported {row.errs} error(s)"


@pytest.mark.emulator
def test_every_volume_uses_the_same_space_as_its_source(multi_volume_boot):
    """Block-level agreement per volume.

    The check that would catch overlapping partitions: if a composed partition started at a
    different block, its neighbour's contents would be corrupted and the counts would move.
    """
    source = multi_volume_boot["source_run"]["info"]
    composed = multi_volume_boot["composed_run"]["info"]
    by_name = {row.name: row for row in composed.values()}
    for row in source.values():
        if row.name == "RESULTS":
            continue
        other = by_name[row.name]
        assert (other.used, other.free) == (row.used, row.free), (
            f"{row.name}: used/free {other.used}/{other.free} != {row.used}/{row.free}"
        )


@pytest.mark.emulator
def test_the_bootable_partition_wins_the_boot_election(multi_volume_boot):
    """Only DH0 is flagged bootable, so SYS: must be Workbench and not a data volume.

    Reproducing the flag on the wrong partition, or on all of them, would boot the wrong volume --
    which on a real machine looks like the restore having silently gone to the wrong place.
    """
    log = multi_volume_boot["composed_run"]["log"]
    assert re.search(r"^SYS\s+Workbench:", log, re.M), (
        "SYS: did not resolve to Workbench: on the composed drive"
    )
    for volume in ("Work", "Saves"):
        assert not re.search(rf"^SYS\s+{volume}:", log, re.M)


@pytest.mark.emulator
def test_the_system_assigns_follow_the_boot_volume(multi_volume_boot):
    """C:, S:, LIBS:, DEVS: and L: must all land on Workbench, not scatter across partitions."""
    log = multi_volume_boot["composed_run"]["log"]
    for name in ("C", "S", "LIBS", "DEVS", "L"):
        assert re.search(rf"^{name}\s+Workbench:", log, re.M), f"{name}: is not on Workbench"


@pytest.mark.emulator
def test_every_volume_listing_is_reproduced(multi_volume_boot):
    """Each volume compared on its own, so a swap between two would be caught."""
    from emulator import amigados

    source = multi_volume_boot["source_run"]["listings"]
    composed = multi_volume_boot["composed_run"]["listings"]
    assert set(composed) == set(source) == {"SYS", "Work", "Saves"}

    for volume in sorted(source):
        result = amigados.compare_listings(source[volume], composed[volume])
        unexpected = set(result.paths_differing) - set(INJECTED_PATHS)
        assert not result.missing, f"{volume}: absent after composition {list(result.missing)}"
        assert not result.extra, f"{volume}: unexpected {list(result.extra)}"
        assert not unexpected, f"{volume}: unexpected differences\n{result.describe()}"


@pytest.mark.emulator
def test_amigados_totals_match_per_volume(multi_volume_boot):
    assert multi_volume_boot["composed_run"]["totals"] == multi_volume_boot["source_run"]["totals"]


@pytest.mark.emulator
def test_an_empty_file_survives_to_a_real_amiga(multi_volume_boot):
    """A zero-length file is a distinct FFS case: a header block and no data blocks."""
    work = multi_volume_boot["composed_run"]["listings"]["Work"]
    assert "Work:empty-file" in work, sorted(work)
    assert work["Work:empty-file"].is_empty_file


@pytest.mark.emulator
def test_a_deeply_nested_path_survives_to_a_real_amiga(multi_volume_boot):
    work = multi_volume_boot["composed_run"]["listings"]["Work"]
    assert "Work:Docs/Deep/Deeper/buried" in work


@pytest.mark.emulator
def test_protection_bits_survive_on_a_data_volume(multi_volume_boot):
    """Set on `Work:Games/Readme`, which nothing touches while booting."""
    source = multi_volume_boot["source_run"]["listings"]["Work"]
    composed = multi_volume_boot["composed_run"]["listings"]["Work"]
    assert len({e.protect for e in source.values()}) > 1, "no protection variety to compare"
    for path, entry in source.items():
        assert composed[path].protect == entry.protect, f"{path}: protection changed"


@pytest.mark.emulator
def test_the_second_dostype_is_reproduced(multi_volume_boot):
    """`Saves` is DOS\\1 while the others are DOS\\3.

    Checked through amibuilder rather than AmigaDOS because `Info` does not print a DosType --
    but the volume mounting cleanly on a real Amiga is what proves the reproduced value is
    actually valid, rather than merely equal to the recorded one.
    """
    from amibuilder.addressing import parse
    from amibuilder.image import open_container

    with open_container(parse(multi_volume_boot["composed"])) as container:
        by_name = {p.volume_name: p for p in container.partitions(probe_volumes=True)}
    assert by_name["Saves"].dos_type.raw != by_name["Workbench"].dos_type.raw
    assert by_name["Saves"].dos_type.label == "DOS\\1"
    assert by_name["Workbench"].dos_type.label == "DOS\\3"


@pytest.mark.emulator
def test_partitions_do_not_overlap_on_the_composed_drive(multi_volume_boot):
    """Cylinder ranges must be contiguous and disjoint, or one volume corrupts another.

    Verified structurally as well as by the block counts above, because an overlap that happened to
    fall in unused space would leave the counts intact while remaining a latent data loss.
    """
    from amibuilder.addressing import parse
    from amibuilder.image import open_container

    with open_container(parse(multi_volume_boot["composed"])) as container:
        parts = sorted(container.partitions(), key=lambda p: p.low_cyl)
    assert len(parts) == 3
    for earlier, later in zip(parts, parts[1:]):
        assert earlier.high_cyl < later.low_cyl, (
            f"{earlier.device_name} ends at {earlier.high_cyl} but "
            f"{later.device_name} starts at {later.low_cyl}"
        )


# ---------------------------------------------------------------------------
# Partition-granular restore onto a drive already in use
#
# The workflow the project exists for, and the one a user reaches for after breaking their OS:
# put Workbench: back to stock and leave everything else exactly as the Amiga left it.
#
# What makes this different from every test above is that the drive is modified **by the Amiga
# itself** first. Files written by AmigaDOS, in FFS's own layout, at block positions nothing here
# chose -- and then a restore has to leave them alone while rebuilding the volume next door.
#
# Before this existed, `--volume Workbench` rebuilt the whole drive from the record and left the
# other two partitions unformatted. It reported success, with the destruction mentioned only as a
# warning. That is the bug these tests exist to keep fixed.
# ---------------------------------------------------------------------------

#: Commands run to make the drive "already in use". No inner redirection: the harness appends
#: `>>RESULTS:log.txt` to each command, and a second redirection on the same line confuses
#: AmigaDOS. `Copy` from a known file gives content whose size can be asserted afterwards.
GRANULAR_WRITE_PHASE = [
    "MakeDir Work:AmigaMade",
    "Copy SYS:C/List TO Work:AmigaMade/List-copy",
    "Copy SYS:C/Info TO Saves:Info-copy",
    # Two ways of breaking the OS, so the restore has to both put something back and take
    # something away. A restore that only added files would pass on the first alone.
    "Delete SYS:Installer",
    "MakeDir SYS:JunkDir",
    # `Info` after the writes, so the per-volume block counts describe the drive as the Amiga
    # left it -- which is what the restore has to leave alone.
    "Info",
    "List SYS: ALL", "List Work: ALL", "List Saves: ALL",
]

GRANULAR_CHECK_PHASE = ["Info", "Assign", "List SYS: ALL", "List Work: ALL", "List Saves: ALL"]


@pytest.fixture(scope="session")
def granular_restore(fsuae_config, multi_source_drive, tmp_path_factory) -> dict:
    """Boot and let the Amiga write, restore only Workbench:, then boot again and look."""
    from emulator import amigados
    from amibuilder.layers import store as S

    root = tmp_path_factory.mktemp("granular")

    def boot(image: str, tag: str, commands: list[str]) -> dict:
        result = harness.run_amiga(
            image=image, part=0,
            fsuae_binary=fsuae_config["binary"], kickstart=fsuae_config["rom"],
            workdir=root / f"run-{tag}", commands=commands, timeout=240, boot_timeout=90,
        )
        assert result.completed, f"{tag} boot failed.\n{result.diagnosis()}"
        log = result.file("log.txt")
        return {
            "log": log,
            "info": amigados.parse_info(log),
            "listings": {v: amigados.parse_list_all(t)
                         for v, t in amigados.split_list_sections(log).items()},
            "medium": str(result.boot_medium),
        }

    used = boot(multi_source_drive, "write", GRANULAR_WRITE_PHASE)

    store = S.Store(str(root / "store"))
    store.init()
    assert _cli(["snap", "create", multi_source_drive, "--label", "multi",
                 "--store", store.root]) == 0

    # Restore into the drive the Amiga just wrote to, naming one volume.
    code = _cli(["compose", "--stack", "multi", "--volume", "Workbench",
                 "--into", used["medium"], "--format", "rdb", "--force",
                 "--store", store.root])
    assert code == 0, "the partition-granular restore failed"

    return {"before": used, "after": boot(used["medium"], "check", GRANULAR_CHECK_PHASE),
            "store": store}


@pytest.mark.emulator
def test_the_amiga_really_modified_the_drive_first(granular_restore):
    """Guards the whole fixture: without these, every check below is vacuous.

    If the write phase silently did nothing, a restore that changed nothing would also pass.
    """
    before = granular_restore["before"]["listings"]
    assert "Work:AmigaMade/List-copy" in before["Work"], "the Amiga did not write to Work:"
    assert "Saves:Info-copy" in before["Saves"], "the Amiga did not write to Saves:"
    assert "SYS:Installer" not in before["SYS"], "the Amiga did not delete from Workbench:"
    assert "SYS:JunkDir" in before["SYS"], "the Amiga did not add to Workbench:"


@pytest.mark.emulator
def test_restore_puts_back_a_file_the_amiga_deleted(granular_restore):
    after = granular_restore["after"]["listings"]
    assert "SYS:Installer" in after["SYS"], "the restore did not bring Installer back"


@pytest.mark.emulator
def test_restore_removes_something_the_amiga_added(granular_restore):
    """`replace` means the volume ends up matching the layer, not merely containing it."""
    after = granular_restore["after"]["listings"]
    assert "SYS:JunkDir" not in after["SYS"], "the restore left junk behind on Workbench:"


@pytest.mark.emulator
def test_restore_keeps_what_the_amiga_wrote_to_other_volumes(granular_restore):
    """The promise of a partition-granular restore, and previously the thing it destroyed."""
    after = granular_restore["after"]["listings"]
    assert "Work:AmigaMade/List-copy" in after["Work"], "Work: lost the Amiga's file"
    assert "Work:AmigaMade" in after["Work"], "Work: lost the Amiga's directory"
    assert "Saves:Info-copy" in after["Saves"], "Saves: lost the Amiga's file"


@pytest.mark.emulator
def test_the_kept_file_is_unchanged_not_merely_present(granular_restore):
    """Same size, protection and timestamp -- present-but-corrupted would otherwise pass."""
    before = granular_restore["before"]["listings"]["Work"]["Work:AmigaMade/List-copy"]
    after = granular_restore["after"]["listings"]["Work"]["Work:AmigaMade/List-copy"]
    assert after == before, f"{after} != {before}"


@pytest.mark.emulator
def test_untouched_volumes_keep_their_original_content_too(granular_restore):
    """Everything that was on Work: and Saves: before the Amiga ran must still be there."""
    after = granular_restore["after"]["listings"]
    for path in ("Work:empty-file", "Work:Docs/Deep/Deeper/buried", "Work:Games/Readme",
                 "Saves:index", "Saves:Slot1/save.dat"):
        volume = path.split(":", 1)[0]
        assert path in after[volume], f"{path} was lost by the restore"


@pytest.mark.emulator
def test_every_volume_still_mounts_without_errors_after_a_restore(granular_restore):
    """The failure mode being guarded: the other partitions left unformatted and unmountable."""
    info = granular_restore["after"]["info"]
    names = {row.name for row in info.values()}
    assert {"Workbench", "Work", "Saves"} <= names, f"only mounted: {sorted(names)}"
    for unit, row in info.items():
        if row.name == "RESULTS":
            continue
        assert row.is_healthy, f"{unit} ({row.name}) reported {row.errs} error(s)"


@pytest.mark.emulator
def test_the_drive_still_boots_from_the_right_volume_after_a_restore(granular_restore):
    log = granular_restore["after"]["log"]
    assert re.search(r"^SYS\s+Workbench:", log, re.M)


@pytest.mark.emulator
def test_the_restored_volume_matches_the_layer_exactly(granular_restore):
    """Not just "the deleted file came back" but "the volume is the layer".

    Compared against the entry count the same drive reported before the Amiga touched it, so an
    extra or missing file anywhere on Workbench: fails rather than only the two paths checked
    above.
    """
    from emulator import amigados

    before = granular_restore["before"]["listings"]["SYS"]
    after = granular_restore["after"]["listings"]["SYS"]

    # The Amiga deleted one file and added one directory, so the restored volume should differ
    # from the modified one in exactly those two places -- and in nothing else.
    result = amigados.compare_listings(before, after)
    assert set(result.missing) == {"SYS:JunkDir"}, f"missing: {list(result.missing)}"
    assert set(result.extra) == {"SYS:Installer"}, f"extra: {list(result.extra)}"
    assert set(result.paths_differing) <= set(INJECTED_PATHS), result.describe()


@pytest.mark.emulator
def test_block_usage_on_the_kept_volumes_is_unchanged_by_the_restore(granular_restore):
    """Block-level proof the kept data is really on the disk, not just in a directory entry.

    A volume that was reformatted and then happened to look plausible would still show a different
    used-block count, so this catches damage the listings could miss.
    """
    def used_by_name(info: dict) -> dict[str, int]:
        return {row.name: row.used for row in info.values()
                if row.name in {"Work", "Saves"}}

    before = used_by_name(granular_restore["before"]["info"])
    after = used_by_name(granular_restore["after"]["info"])
    assert before, "the write phase recorded no block counts to compare against"
    assert after == before, f"block usage changed: {before} -> {after}"

# ---------------------------------------------------------------------------
# `amibuilder init` against a real Amiga
#
# The claim init makes is a strong one: that a drive it creates is usable on real hardware
# with no HDToolBox step. Nothing host-side can verify that -- our own reader could agree
# with our own writer about a layout that AmigaOS rejects. So the drive is attached to a
# real AmigaOS 3.2 booted from the install floppy, which is also precisely Dave's install
# workflow: fresh drive, boot the installer, install onto it.
#
# The drive goes in `hard_drive_2` because slot 1 is the harness's RESULTS volume. Note the
# consequence, asserted below: RESULTS claims the `DH1` device name first, so AmigaDOS
# renames our second partition's device to `DH1_0`.
# ---------------------------------------------------------------------------

INIT_PARTITIONS = ["Boot=60M,bootable", "Games=80M", "Keep=rest"]
INIT_SIZE = "200M"


@pytest.fixture(scope="session")
def initialised_drive_boot(fsuae_config, boot_floppy, tmp_path_factory) -> dict:
    """Create a drive with `init`, then mount it on a real Amiga booted from the floppy.

    The floppy carries the OS and the injected script -- the drive under test is empty by
    definition, so it cannot boot itself and is only here to be mounted. `run_amiga` injects
    into the floppy exactly when no `image` is passed, which is why the drive arrives through
    `extra_config` rather than as the image.
    """
    from emulator import amigados

    root = tmp_path_factory.mktemp("init-boot")
    drive = str(root / "initialised.hdf")

    argv = ["init", drive, "--size", INIT_SIZE]
    for spec in INIT_PARTITIONS:
        argv += ["--partition", spec]
    assert _cli(argv) == 0, "init failed to create the drive"

    result = harness.run_amiga(
        floppy=boot_floppy,
        fsuae_binary=fsuae_config["binary"],
        kickstart=fsuae_config["rom"],
        workdir=root / "run",
        extra_config={"hard_drive_2": drive},
        commands=["Info", "Assign", "List Boot: ALL", "List Games: ALL", "List Keep: ALL"],
        timeout=240,
        boot_timeout=90,
    )
    assert result.completed, (
        "the install floppy did not boot with an initialised drive attached.\n"
        f"{result.diagnosis()}\nEmulator log tail:\n"
        + "\n".join(result.emulator_log.splitlines()[-25:])
    )

    log = result.file("log.txt")
    info = amigados.parse_info(log)
    return {
        "drive": drive,
        "log": log,
        "info": info,
        "by_name": {row.name: row for row in info.values()},
        "listings": amigados.split_list_sections(log),
    }


@pytest.mark.emulator
def test_an_initialised_drive_mounts_on_a_real_amiga(initialised_drive_boot):
    """Every partition init wrote is mounted by AmigaOS, with no HDToolBox step."""
    names = set(initialised_drive_boot["by_name"])
    assert {"Boot", "Games", "Keep"} <= names, (
        f"AmigaOS did not mount all three initialised partitions; saw {sorted(names)}"
    )


@pytest.mark.emulator
def test_initialised_partitions_report_no_filesystem_errors(initialised_drive_boot):
    """A partition can mount and still be structurally wrong; Errs would show it."""
    by_name = initialised_drive_boot["by_name"]
    bad = {n: by_name[n].errs for n in ("Boot", "Games", "Keep") if by_name[n].errs}
    assert not bad, f"AmigaOS reported filesystem errors on initialised partitions: {bad}"


@pytest.mark.emulator
def test_initialised_partitions_are_mounted_read_write(initialised_drive_boot):
    """An installer needs to write to them, so read-only would defeat the purpose."""
    log = initialised_drive_boot["log"]
    for name in ("Boot", "Games", "Keep"):
        assert re.search(rf"Read/Write\s+{name}\b", log), (
            f"{name} was not mounted Read/Write"
        )


@pytest.mark.emulator
def test_initialised_partitions_are_empty(initialised_drive_boot):
    """`init` formats, it does not populate. Anything present would be a writer bug."""
    from emulator import amigados

    listings = initialised_drive_boot["listings"]
    for name in ("Boot", "Games", "Keep"):
        assert name in listings, f"{name}: could not be listed at all"
        entries = amigados.parse_list_all(listings[name])
        assert not entries, f"a freshly initialised {name}: is not empty: {entries}"


@pytest.mark.emulator
def test_an_installer_floppy_outboots_an_initialised_bootable_partition(
    initialised_drive_boot,
):
    """The whole install workflow depends on this election going to the floppy.

    `init` marks the first partition bootable, so a fresh drive presents a bootable-but-empty
    volume. If that won the election the machine would not boot at all -- there is no
    Startup-Sequence on it -- and Dave could never install onto a drive init had made. Verified
    two ways: SYS: must resolve to the floppy, and the floppy must be the mounted DF0.
    """
    log = initialised_drive_boot["log"]
    by_name = initialised_drive_boot["by_name"]

    assert "Install3.2" in by_name, "the install floppy is not mounted"
    assert not re.search(r"^SYS\b.*\bBoot:", log, re.MULTILINE), (
        "SYS: resolved to the initialised drive, so the empty bootable partition won the "
        "boot election -- an installer could never run against a fresh drive"
    )


@pytest.mark.emulator
def test_initialised_partitions_are_the_size_that_was_asked_for(initialised_drive_boot):
    """Mounting proves the layout parses; free space proves the sizes are real.

    A layout bug could easily mount three partitions of the wrong sizes. Free blocks are
    compared rather than the reported size because AmigaDOS truncates that to whole
    megabytes. Filesystem overhead (root block, bitmap) is well under the 2% tolerance.
    """
    by_name = initialised_drive_boot["by_name"]
    expected = {"Boot": 60 * 1024 * 1024, "Games": 80 * 1024 * 1024, "Keep": 60 * 1024 * 1024}
    for name, want in expected.items():
        free_bytes = by_name[name].free * 512
        ratio = free_bytes / want
        assert 0.98 <= ratio <= 1.0, (
            f"{name}: has {free_bytes} bytes free, expected about {want} "
            f"(ratio {ratio:.4f})"
        )


@pytest.mark.emulator
def test_amigados_renames_a_device_that_collides_with_an_existing_one(
    initialised_drive_boot,
):
    """Recorded behaviour, not a defect -- but it is a real constraint on `init`.

    `init` hands out `DH0..DHn` unconditionally. Here the harness's RESULTS volume takes
    `DH1` first, so AmigaOS renames our second partition's device to `DH1_0` while leaving its
    volume name alone. The same thing would happen on a real machine with a second drive using
    the same prefix. Volume names are what the tests key on for exactly this reason; if `init`
    ever gains a configurable device prefix, this test documents why.
    """
    info = initialised_drive_boot["info"]
    units = {unit: row.name for unit, row in info.items()}

    assert units.get("DH1") == "RESULTS", (
        f"expected RESULTS to hold DH1, saw {units.get('DH1')!r}; the collision this test "
        "documents may no longer occur"
    )
    games_unit = next(unit for unit, name in units.items() if name == "Games")
    assert games_unit != "DH1", "Games kept DH1, which RESULTS already claimed"
    assert games_unit.startswith("DH1"), (
        f"Games landed on {games_unit!r}; expected a DH1-derived name from collision renaming"
    )
