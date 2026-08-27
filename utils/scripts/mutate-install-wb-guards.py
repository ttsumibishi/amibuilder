"""Mutation-test the install-wb.sh guards that matter most.

Same contract as mutate-boot-hdf-guards.py: patch the script, run its suite, and require the
*named* checks to go from PASS to FAIL. Requiring the named check rather than merely a non-zero
exit is the point -- a mutation caught by some unrelated assertion tells you nothing about the
guard you thought you were testing.

Focused where a silent regression is expensive. `--in-place` is the worst of them: without it the
install lands in boot-hdf.sh's throwaway clone, and the cost is an hour of clicking through an
interactive installer for nothing. The rest are the media manifest and the Modules-disk mapping,
where being wrong fails partway through that same hour.

A timeout counts as killed, deliberately. An early version of the suite could HANG under one of
these mutations -- the script sailed past the removed check to its confirmation prompt and waited
forever on stdin -- and a suite that hangs instead of failing is a worse outcome than either. The
suite now redirects stdin and the script only prompts on a TTY, but the bound stays as a guard
against that shape of regression returning.
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "utils" / "scripts" / "install-wb.sh"
SUITE = ROOT / "utils" / "scripts" / "test-install-wb.sh"

#: Seconds a single suite run may take. It is a few dozen dry runs, so this is generous.
TIMEOUT = 180

# (label, find, replace, check labels that must go from PASS to FAIL)
MUTATIONS: list[tuple[str, str, str, list[str]]] = [
    (
        "--in-place dropped, so the install lands in a clone",
        "BOOT_ARGS=(--in-place --turbo-floppy --no-warp",
        "BOOT_ARGS=(--turbo-floppy --no-warp",
        ["installs in place, not into a clone"],
    ),
    (
        "turbo floppy dropped, so a dozen disks take an hour",
        "BOOT_ARGS=(--in-place --turbo-floppy --no-warp",
        "BOOT_ARGS=(--in-place --no-warp",
        ["turbo floppy on"],
    ),
    (
        "warp left on, making the GUI installer unpleasant",
        "BOOT_ARGS=(--in-place --turbo-floppy --no-warp",
        "BOOT_ARGS=(--in-place --turbo-floppy",
        ["warp off"],
    ),
    (
        "the A4000 maps to the wrong Modules disk",
        "        A4000/040|A4000)  printf 'A4000D\\n' ;;",
        "        A4000/040|A4000)  printf 'A4000T\\n' ;;",
        ["A4000/040 maps to the A4000D disk"],
    ),
    (
        "the required-disk check is skipped, so it fails mid-install",
        '    [ -f "$MEDIA/$disk.adf" ] || MISSING+=("$disk")',
        "    : # mutated",
        ["a missing required disk names it", "and lists the whole required set"],
    ),
    (
        "an unknown model is accepted, yielding a nonsense disk name",
        '[ -n "$MODULES" ] || die "no Modules disk is known for model $MODEL.',
        '[ -n "$MODULES" ] || true && : "',
        ["an unknown model is refused"],
    ),
    (
        "the swap list is swept instead of curated (the truncation trap)",
        'for disk in "${REQUIRED[@]:2}"; do BOOT_ARGS+=("--add-adf=$MEDIA/$disk.adf"); done',
        'BOOT_ARGS+=("--floppy-path=$MEDIA")',
        ["does not sweep the directory"],
    ),
    (
        "a missing drive is not refused, so the install writes nowhere useful",
        '    [ -f "$DRIVE" ] || die "$DRIVE does not exist. Create it first:',
        '    [ -f "$DRIVE" ] || true && : "',
        ["a missing drive says how to make one"],
    ),
]


def run_suite() -> dict[str, str]:
    """{check label: PASS|FAIL} from one suite run. A timeout reports every check as TIMEOUT."""
    try:
        proc = subprocess.run(
            ["bash", str(SUITE)], cwd=ROOT, capture_output=True, text=True,
            timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return {}
    results: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        for status in ("PASS", "FAIL"):
            if line.startswith(status + "  "):
                # Labels are padded, and some exceed the pad width, so the detail cannot be split
                # off reliably. Store the remainder and match by prefix instead.
                results[line[len(status) + 2:].rstrip()] = status
    return results


def status_of(results: dict[str, str], label: str) -> str:
    """The status of the check whose label starts with `label`, or MISSING."""
    for text, status in results.items():
        if text.startswith(label):
            return status
    return "MISSING"


def main() -> int:
    if not SCRIPT.exists() or not SUITE.exists():
        print(f"error: need both {SCRIPT.name} and {SUITE.name}", file=sys.stderr)
        return 1

    baseline = run_suite()
    if not baseline:
        print("error: the suite did not complete before any mutation was applied",
              file=sys.stderr)
        return 1
    unexpected = [c for c in baseline if baseline[c] == "FAIL"]
    if unexpected:
        print("error: the suite is already failing; fix that first:", file=sys.stderr)
        for c in unexpected:
            print(f"  - {c}", file=sys.stderr)
        return 1
    print(f"baseline: {len(baseline)} checks, all passing\n")

    original = SCRIPT.read_text()
    survivors: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / SCRIPT.name
        shutil.copy2(SCRIPT, backup)
        for label, find, repl, checks in MUTATIONS:
            try:
                if find not in original:
                    print(f"ANCHOR    {label}")
                    print("          the text to mutate is not in the script -- update this file")
                    survivors.append(label)
                    continue
                SCRIPT.write_text(original.replace(find, repl, 1))
                after = run_suite()
                if not after:
                    # A hang is a failure; it just reports rudely. Count it as killed and say so.
                    print(f"killed    {label}  (the suite timed out rather than failing)")
                    continue
                still_green = [c for c in checks if status_of(after, c) != "FAIL"]
                if still_green:
                    print(f"SURVIVED  {label}")
                    for c in still_green:
                        print(f"          {c!r} stayed {status_of(after, c)} -- vacuous guard")
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
