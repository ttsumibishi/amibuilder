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
    assert "never started" in never_started.diagnosis()

    (results / harness.PROGRESS).write_text("setup\n0: Info\n")
    stalled = harness.AmigaRunResult(
        completed=False, seconds=30.0, results_dir=results,
        files={harness.PROGRESS: "setup\n0: Info\n"}, stalled_at="0: Info",
    )
    assert "STALLED" in stalled.diagnosis() and "0: Info" in stalled.diagnosis()
    assert "requester" in stalled.diagnosis()

    done = harness.AmigaRunResult(completed=True, seconds=6.0, results_dir=results)
    assert done.diagnosis() == "completed"


def test_missing_commands_are_extracted_from_the_log(tmp_path):
    res = harness.AmigaRunResult(
        completed=True, seconds=1.0, results_dir=tmp_path,
        files={"log.txt": f"AMIBUILDER-BEGIN\n{harness.MISSING_MARK} Type\nKIPFFS-END\n"},
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
    marker = b"KIPFFS\x00WROTE\xa0THIS\n" + bytes(range(256)) * 3
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
