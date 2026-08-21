"""Mutation-test the boot-hdf.sh guards that matter most.

Same contract as mutate-write-guards.py: patch the script, run its suite, require the named
check to FAIL. A guard that survives its own mutation is decoration.

Focused on the guards where a silent regression would be expensive: `--ui` starting the
emulator it exists to avoid, the Settings.ini backup, and the ROM/model precedence.
"""

from __future__ import annotations

import filecmp
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "utils" / "scripts" / "boot-hdf.sh"
SUITE = ROOT / "utils" / "scripts" / "test-boot-hdf.sh"

# (label, find, replace, check labels that must go from PASS to FAIL)
MUTATIONS: list[tuple[str, str, str, list[str]]] = [
    (
        "--ui falls through and boots the emulator anyway",
        '    open -a "$LAUNCHER" || die "could not open $LAUNCHER"\n    exit 0',
        '    open -a "$LAUNCHER" || die "could not open $LAUNCHER"',
        ["the emulator is NOT started"],
    ),
    (
        "--ui passes the config on the Launcher's argv (which auto-boots)",
        'open -a "$LAUNCHER" || die',
        'open -a "$LAUNCHER" "$CONF" || die',
        ["no config on the Launcher's argv"],
    ),
    (
        "Settings.ini is edited without a backup",
        'cp -p "$SETTINGS" "$BACKUP" || die "could not back up $SETTINGS"',
        ':  # MUTANT',
        ["Settings.ini is backed up first"],
    ),
    (
        "Settings.ini is not pointed at the new config",
        '                -e "s|^config_path = .*|config_path = $INSTALLED|" \\',
        '                -e "s|^config_path_disabled = .*|config_path = $INSTALLED|" \\',
        ["Settings.ini points at the config"],
    ),
    (
        "the config is never installed where the Launcher looks",
        'cp "$CONF" "$INSTALLED" || die "could not write $INSTALLED"',
        ':  # MUTANT',
        ["config installed for the Launcher"],
    ),
    (
        "ROM inference overrides an explicit --model",
        'if [ -n "$ROM_GIVEN" ] && [ "$MODEL_GIVEN" -eq 0 ]; then',
        'if [ -n "$ROM_GIVEN" ] && [ "$MODEL_GIVEN" -ge 0 ]; then',
        ["--model beats ROM inference", "order does not matter"],
    ),
    (
        "a bare ROM name is not resolved against the ROM directory",
        'for candidate in "$ROM_DIR/$want" "$ROM_DIR/$want.rom" "$ROM_DIR/$want.ROM"; do',
        'for candidate in; do',
        ["bare ROM name resolves in ROMs/"],
    ),
    (
        "an unknown ROM name is accepted",
        "    printf 'error: --rom %s: no such ROM in %s\\n' \"$want\" \"$ROM_DIR\" >&2\n"
        "    list_roms\n    exit 2",
        "    printf '%s\\n' \"$want\"; return 0",
        # Only the message check can catch this. "lists what exists" cannot: the generic
        # "kickstart not found" path calls list_roms too, so the listing appears either way.
        # That check has its own mutation below.
        ["unknown ROM name"],
    ),
    (
        "list_roms shows nothing, so a bad name gives no help",
        "    printf '\\nAvailable in ROMs/:\\n' >&2",
        "    return 0  # MUTANT",
        ["unknown ROM lists what exists"],
    ),
    (
        "the model is not inferred from the ROM at all",
        "        MODEL=\"$inferred\"",
        "        :  # MUTANT",
        ["model follows an A3000 ROM", "A4000T ROM maps to A4000/040"],
    ),
]

RESULT = re.compile(r"^(PASS|FAIL)\s+(.*?)\s{2,}", re.M)


def run_suite() -> dict[str, str]:
    """Map each check label to PASS or FAIL."""
    proc = subprocess.run(["bash", str(SUITE)], cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    return {label.strip(): status for status, label in RESULT.findall(out)}


def main() -> int:
    print("baseline: the whole suite must be green before mutating")
    base = run_suite()
    reds = [k for k, v in base.items() if v == "FAIL"]
    if reds:
        print(f"BASELINE FAILED: {reds}")
        return 1
    print(f"  ok, {len(base)} checks green\n")

    # A mutation that names a check the suite does not have would silently test nothing.
    for label, _f, _r, checks in MUTATIONS:
        for c in checks:
            if c not in base:
                print(f"BAD MUTATION SPEC: {label!r} names unknown check {c!r}")
                return 1

    survivors: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        backup = Path(td) / SCRIPT.name
        shutil.copy2(SCRIPT, backup)
        original = SCRIPT.read_text()

        for label, find, repl, checks in MUTATIONS:
            n = original.count(find)
            if n != 1:
                print(f"SKIP      {label}\n          anchor matched {n} times, not 1")
                survivors.append(f"{label} (bad anchor)")
                continue
            try:
                SCRIPT.write_text(original.replace(find, repl))
                after = run_suite()
                still_green = [c for c in checks if after.get(c) != "FAIL"]
                if still_green:
                    print(f"SURVIVED  {label}")
                    print(f"          {still_green} stayed PASS -- vacuous guard")
                    survivors.append(label)
                else:
                    print(f"killed    {label}")
            finally:
                shutil.copy2(backup, SCRIPT)
                assert filecmp.cmp(backup, SCRIPT, shallow=False), "restore failed!"

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print("every mutation was killed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
