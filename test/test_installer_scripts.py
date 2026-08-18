"""Parsing and feasibility analysis of Commodore `Installer` scripts.

Two groups:

* Parser tests on synthetic scripts. These always run.
* Analysis of the real AmigaOS 3.2 installer, which needs the CD image in
  `source-files-do-not-add-to-git/`. Skipped when it is absent, so a checkout without
  licensed material runs everything else.

The real-script assertions are deliberately loose bounds rather than exact figures: they
exist to catch "this script is fundamentally different from what we analysed", not to
break when a point release shifts a count by one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import images, installer

REPO = Path(__file__).resolve().parents[1]
ISO = REPO / "source-files-do-not-add-to-git" / "amigaos32.iso"


# ---------------------------------------------------------------------------
# Parser, no source material needed
# ---------------------------------------------------------------------------


def test_tokenizer_handles_comments_strings_and_atoms():
    src = '''
    ; a comment with (parens) and "quotes"
    (set x "hello ; not a comment")
    (copyfiles (source "A:") (dest "B:"))
    '''
    toks = list(installer.tokenize(src))
    strings = [v for k, v in toks if k == "str"]
    assert "hello ; not a comment" in strings, "a ; inside a string is not a comment"
    atoms = [v for k, v in toks if k == "atom"]
    assert "set" in atoms and "copyfiles" in atoms


def test_tokenizer_honours_backslash_escapes():
    r"""Escaped quotes must not terminate the string. These scripts embed quoted
    AmigaDOS command lines, so this is not a corner case."""
    src = r'(run (cat "DAControl LOAD \"" adf ".adf\""))'
    strings = [v for k, v in installer.tokenize(src) if k == "str"]
    assert strings[0] == 'DAControl LOAD "'
    assert strings[1] == '.adf"'


def test_parse_builds_nested_structure():
    tree = installer.parse('(a (b "s") c)')
    assert tree == [["a", ["b", ("str", "s")], "c"]]


def test_parse_rejects_unbalanced_parens():
    with pytest.raises(ValueError, match="unclosed"):
        installer.parse("(a (b")
    with pytest.raises(ValueError, match="closing"):
        installer.parse("(a))")


def test_analyze_counts_head_symbols_not_all_atoms():
    a = installer.analyze('(makedir "A") (makedir "B") (set x makedir)')
    assert a.heads["makedir"] == 2, "the bare atom in (set x makedir) is not a call"
    assert a.heads["set"] == 1


def test_analyze_flags_hard_constructs():
    a = installer.analyze('(run "Foo") (database "cpu") (tooltype (dest "x"))')
    assert a.hard_calls == {"run": 1, "database": 1, "tooltype": 1}


def test_analyze_separates_benign_run_targets_from_real_ones():
    """`run wait` and `run Delete` need no emulation; an unknown binary does."""
    a = installer.analyze(
        '(run "wait 1") (run "Delete QUIET x") (run "SomeMysteryTool ARG")'
    )
    assert a.run_targets["wait"] == 1
    assert a.run_targets["Delete"] == 1
    unmodellable = a.unmodellable_run_targets
    assert "SomeMysteryTool" in unmodellable
    assert "wait" not in unmodellable and "Delete" not in unmodellable


def test_analyze_measures_string_fraction():
    a = installer.analyze('(set #msg "' + "x" * 900 + '")')
    assert a.string_fraction > 0.9
    assert a.code_bytes < 60


def test_analyze_finds_procedures_and_languages():
    src = '''
    (procedure MOUNTADF (run "x"))
    (procedure UNMOUNTADF (run "y"))
    (if (= @language "deutsch") (set a 1))
    (set #greeting "hi")
    '''
    a = installer.analyze(src)
    assert a.procedures == ["MOUNTADF", "UNMOUNTADF"]
    assert a.languages == ["deutsch"]
    assert a.message_symbols == 1


def test_report_is_renderable():
    a = installer.analyze('(run "wait 1") (makedir "x") (askdir (prompt "where?"))')
    text = installer.report(a)
    assert "file operations" in text and "run targets" in text


# ---------------------------------------------------------------------------
# Real AmigaOS 3.2 installer
# ---------------------------------------------------------------------------

requires_iso = pytest.mark.skipif(
    not ISO.exists(),
    reason=f"needs {ISO.name} in source-files-do-not-add-to-git/",
)
requires_macos = pytest.mark.skipif(
    sys.platform != "darwin", reason="ISO mounting here uses hdiutil"
)


def _existing_mount(iso: Path) -> Path | None:
    """Find an already-attached mountpoint for `iso`, if any.

    macOS refuses to attach the same image twice, so a stray mount left over from manual
    poking would otherwise make every test here skip -- which looks like the ISO is
    missing rather than already in use.
    """
    proc = subprocess.run(["hdiutil", "info"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    current_image = None
    for line in proc.stdout.splitlines():
        if line.startswith("image-path"):
            current_image = line.split(":", 1)[1].strip()
        elif "/Volumes/" in line or "/private/tmp/" in line or "/tmp/" in line:
            if current_image and Path(current_image).resolve() == iso.resolve():
                mp = line.split("\t")[-1].strip()
                if mp.startswith("/") and Path(mp).is_dir():
                    return Path(mp)
    return None


@pytest.fixture(scope="module")
def aos32_cd(tmp_path_factory):
    """Mount the AmigaOS 3.2 CD read-only, reusing an existing mount if there is one.

    Only detaches what it attached itself, so a mount someone else set up survives.
    """
    if not ISO.exists():
        pytest.skip("AmigaOS 3.2 ISO not present")
    if sys.platform != "darwin" or shutil.which("hdiutil") is None:
        pytest.skip("hdiutil not available")

    existing = _existing_mount(ISO)
    if existing is not None:
        yield existing
        return

    mount = tmp_path_factory.mktemp("aos32cd") / "mnt"
    proc = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount),
         str(ISO)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not mount ISO: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        yield mount
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount)],
                       capture_output=True)


@pytest.fixture(scope="module")
def install_script(aos32_cd, tmp_path_factory) -> installer.Analysis:
    """Extract Install/Install from Install3.2.adf and analyse it."""
    adf = aos32_cd / "ADF" / "Install3.2.adf"
    if not adf.exists():
        pytest.skip("Install3.2.adf not found on the CD")

    out = tmp_path_factory.mktemp("script") / "Install"
    images.xdftool(str(adf), "open", "+", "read", "Install/Install", str(out))
    text = out.read_bytes().decode("latin-1")
    return installer.analyze(text, path="AmigaOS3.2 Install")


@requires_iso
@requires_macos
def test_real_install_adf_is_readable_and_bootable(aos32_cd):
    """Our read path against a genuine Amiga floppy, not a synthetic fixture."""
    adf = aos32_cd / "ADF" / "Install3.2.adf"
    listing = images.xdftool(str(adf), "open", "+", "list").output

    assert "Install3.2" in listing
    assert "DOS1:ffs" in listing, "the 3.2 install disk is plain FFS, not international"
    assert "Installer" in listing, "the Installer interpreter should be present"

    scan = images.xdfscan(str(adf)).stdout
    assert "boot" in scan and "ok" in scan, f"validator did not report boot ok:\n{scan}"


@requires_iso
@requires_macos
def test_cd_ships_the_install_floppies_as_adfs(aos32_cd):
    """Every `askdisk` prompt can be satisfied from the CD's own ADF directory."""
    adfs = {p.stem for p in (aos32_cd / "ADF").glob("*.adf")}
    assert len(adfs) >= 30, f"expected the full floppy set, found {len(adfs)}"

    for expected in ("Workbench3.2", "Extras3.2", "Classes3.2", "Fonts",
                     "Storage3.2", "Locale", "GlowIcons3.2"):
        assert expected in adfs, f"{expected}.adf missing"

    # Per-model module disks, which is what the Amiga-model prompt selects between.
    models = {a for a in adfs if a.startswith("Modules")}
    assert len(models) >= 6, f"expected several model module disks, got {models}"


@requires_iso
@requires_macos
def test_real_install_script_parses_cleanly(install_script):
    a = install_script
    assert a.balanced, "the real installer script should parse with balanced parens"
    assert a.forms > 2000, f"unexpectedly few forms: {a.forms}"
    assert a.total_bytes > 150_000


@requires_iso
@requires_macos
def test_most_of_the_script_is_localised_text_not_logic(install_script):
    """The 177 KB is mostly translations; the actual program is far smaller."""
    a = install_script
    assert a.string_fraction > 0.5, (
        f"expected over half the file to be string data, got {a.string_fraction:.0%}"
    )
    assert len(a.languages) >= 8, f"expected many language branches: {a.languages}"


@requires_iso
@requires_macos
def test_hard_constructs_are_few_and_bounded(install_script):
    """The blockers are a short, enumerable list rather than pervasive."""
    a = install_script
    hard = a.hard_calls

    assert hard.get("database", 0) <= 5, (
        f"machine queries should be few, got {hard.get('database')}"
    )
    assert hard.get("tooltype", 0) == 0, (
        "the 3.2 installer uses no (tooltype) form; it shells out to CopyToolTypes "
        "instead, so icon editing is still needed but not via this construct"
    )
    assert 10 <= hard.get("askdisk", 0) <= 30, (
        f"askdisk count outside the expected range: {hard.get('askdisk')}"
    )


@requires_iso
@requires_macos
def test_every_run_target_is_identified(install_script):
    """No `run` target should be unaccounted for.

    If this fails, a new binary is being invoked that has not been assessed -- which is
    exactly the signal a simulator would need to refuse to emit a layer.
    """
    a = install_script
    unmodellable = a.unmodellable_run_targets

    # Known to need real work: these do filesystem or icon changes we cannot infer.
    expected_hard = {"UpdateWBFiles", "IconPos", "CopyToolTypes", "Prefs/WBPattern"}
    surprises = {
        t: c for t, c in unmodellable.items()
        if not any(t.endswith(e) or e in t for e in expected_hard)
    }
    assert not surprises, (
        f"unassessed run targets: {surprises}. Each needs a shim, vamos, or observe mode."
    )

    # Guard against the audit being vacuous: some `run` forms build the command from
    # variables, so the extractor cannot see them and they must be read by hand. These
    # five were reviewed manually (AddBuffers, AmigaModel, CPU, GuessBootDev,
    # Prefs/WBPattern). If the count grows, a new unreviewed target has appeared.
    assert a.unresolved_run_calls <= 5, (
        f"{a.unresolved_run_calls} run calls have an unextractable target, up from the "
        "5 reviewed by hand. Read the new ones before trusting this analysis."
    )


@requires_iso
@requires_macos
def test_only_a_handful_of_run_calls_need_real_work(install_script):
    """Quantifies the feasibility verdict: most `run` calls are no-ops or replaceable."""
    a = install_script
    total = a.heads.get("run", 0)
    benign = sum(
        c for t, c in a.run_targets.items()
        if installer.normalise_run_target(t) in installer.BENIGN_RUN
    )
    needs_work = sum(a.unmodellable_run_targets.values())

    assert total >= 25, f"expected many run calls, got {total}"
    assert benign >= total * 0.6, (
        f"expected most run calls to be benign: {benign} of {total}"
    )
    assert needs_work <= 6, (
        f"expected only a few run calls to need emulation, got {needs_work}: "
        f"{a.unmodellable_run_targets}"
    )


@requires_iso
@requires_macos
def test_user_decisions_are_a_manageable_number(install_script):
    """The whole base-OS install comes down to a handful of real choices."""
    a = install_script
    decisions = a.calls({"askdir", "askchoice", "askoptions", "askbool"})
    assert 10 <= decisions <= 40, (
        f"expected a modest number of user decisions, got {decisions}"
    )


@requires_iso
@requires_macos
def test_report_renders_for_the_real_script(install_script, capsys):
    """Not an assertion so much as a way to see the analysis: -s to view."""
    print("\n" + installer.report(install_script))
    assert installer.report(install_script)


def test_run_target_normalisation_handles_device_and_path_prefixes():
    """`C:LoadModule`, `Install3.2:C/Delete` and `Delete` are the same command.

    Regression pin: an earlier version used lstrip("c:"), which strips *characters* and
    turned "Copy" into "opy", making a benign target look unmodellable.
    """
    n = installer.normalise_run_target
    assert n("Copy") == "copy"
    assert n("C:LoadModule") == "loadmodule"
    assert n("Install3.2:C/Delete") == "delete"
    assert n("DAControl") == "dacontrol"
    assert n("wait") == "wait"
