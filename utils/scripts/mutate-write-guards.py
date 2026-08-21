"""Mutation-test the `cp`/`mkdir` guards.

For each mutation: patch a source file, run the tests that claim to cover the property,
and require that they go RED. A guard that stays green under its own mutation is
decoration. Restores are verified byte-for-byte.

Written in Python rather than shell on purpose: `grep -c` counts *lines*, so a
multi-line anchor cannot be counted reliably, and a mis-restore would be silent.
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python")
# Anchors are exact source lines. That is brittle on purpose: a refactor that moves one
# reports "anchor matched 0 times" and fails loudly, rather than quietly testing nothing.
VOL = ROOT / "amibuilder" / "volume.py"
WRITE = ROOT / "amibuilder" / "commands" / "write.py"

# (label, file, find, replace, tests that must go red)
MUTATIONS: list[tuple[str, Path, str, str, list[str]]] = [
    (
        "preflight is skipped entirely",
        WRITE,
        "        _preflight(vol, items, args)",
        "        pass  # MUTANT",
        ["test_a_name_over_the_limit_is_refused_before_anything_is_written",
         "test_a_copy_that_will_not_fit_is_refused_whole",
         "test_an_existing_file_is_refused_without_force"],
    ),
    (
        "name checks run per-write instead of up front",
        WRITE,
        "    for item in items:\n        for component in item.dest.split(\"/\"):\n"
        "            vol.check_name(component)",
        "    for item in []:  # MUTANT\n        for component in item.dest.split(\"/\"):\n"
        "            vol.check_name(component)",
        ["test_a_name_over_the_limit_is_refused_before_anything_is_written"],
    ),
    (
        "case-collision detection removed",
        WRITE,
        "        clash = seen.get(key)",
        "        clash = None  # MUTANT",
        ["test_two_sources_differing_only_in_case_are_refused"],
    ),
    (
        "capacity preflight removed",
        WRITE,
        "    if needed > info.free_blocks:",
        "    if False:  # MUTANT",
        ["test_a_copy_that_will_not_fit_is_refused_whole",
         "test_an_adf_that_is_too_small_is_refused"],
    ),
    (
        "replace does not credit back the old file's blocks",
        WRITE,
        "            needed -= vol.blocks_for(vol.stat(item.dest).size)",
        "            pass  # MUTANT",
        ["test_replacing_frees_the_old_blocks"],
    ),
    (
        "symlinks are followed instead of skipped",
        WRITE,
        "            if child.is_symlink():\n                yield child, False, \"symlink\"",
        "            if False:  # MUTANT\n                yield child, False, \"symlink\"",
        ["test_symlinks_are_reported_and_skipped", "test_json_records_skipped_symlinks"],
    ),
    (
        "host metadata files are copied",
        WRITE,
        "            if child.name in SKIP_NAMES:",
        "            if False:  # MUTANT",
        ["test_host_metadata_files_are_not_copied"],
    ),
    (
        "directories are not re-stamped after their contents",
        WRITE,
        "        _restamp_directories(vol, items, args)",
        "        pass  # MUTANT",
        ["test_preserve_times_applies_to_directories_too"],
    ),
    (
        "--preserve-times is ignored",
        WRITE,
        "    if not args.preserve_times:\n        return {}",
        "    if True:  # MUTANT\n        return {}",
        ["test_preserve_times_carries_the_host_mtime_across_exactly",
         "test_preserve_times_survives_a_summer_mtime"],
    ),
    (
        "a pre-1978 mtime is written as a negative day count",
        WRITE,
        "    if secs <= 0:\n        # AmigaDOS cannot represent",
        "    if False:  # MUTANT\n        # AmigaDOS cannot represent",
        ["test_a_pre_1978_mtime_falls_back_to_now"],
    ),
    (
        "protect is applied to directories too",
        WRITE,
        "        vol.mkdir(item.dest, parents=True, exist_ok=True, **stamp)",
        "        vol.mkdir(item.dest, parents=True, exist_ok=True, protect=args.protect,"
        " **stamp)  # MUTANT",
        ["test_protect_does_not_apply_to_directories"],
    ),
    # -- volume.py ---------------------------------------------------------
    # Plain `update_ts=False` -> `True` is NOT included, and that is a finding rather
    # than an omission: `_stamp` runs immediately afterwards and overwrites both the
    # parent's mod_ts and the volume's disk_ts with a correctly computed value, so
    # amitools' broken stamp is unobservable in this code path. The two mutations below
    # establish that: removing `_stamp` alone is caught, and `update_ts=True` combined
    # with no `_stamp` is caught. So `update_ts=False` here is belt-and-braces (it is
    # load-bearing in layers/targets.py, where nothing re-stamps afterwards).
    (
        "no explicit parent stamp at all",
        VOL,
        "            node.change_meta_info(MetaInfo(mod_ts=stamp))",
        "            pass  # MUTANT",
        ["test_writing_stamps_the_parent_directory_with_a_correct_time",
         "test_mkdir_stamps_the_parent_with_a_correct_time"],
    ),
    (
        "amitools stamps the parent instead of us (update_ts=True, no _stamp)",
        VOL,
        "            node.change_meta_info(MetaInfo(mod_ts=stamp))",
        "            node.change_meta_info(MetaInfo())  # MUTANT\n"
        "            from amitools.fs.MetaInfo import MetaInfo as _MI\n"
        "            _mi = _MI(); _mi.set_current_as_mod_time()\n"
        "            node.change_meta_info(_mi)",
        ["test_writing_stamps_the_parent_directory_with_a_correct_time"],
    ),
    (
        "G22: overwrite without deleting first",
        VOL,
        "                existing.delete(wipe=False, all=False, update_ts=False)",
        "                pass  # MUTANT",
        ["test_force_replaces_the_file"],
    ),
    (
        "info() cache is never invalidated",
        VOL,
        "        self._info = None",
        "        pass  # MUTANT",
        ["test_free_space_is_recomputed_after_a_write"],
    ),
    (
        "blocks_for forgets extension blocks",
        VOL,
        "        return 1 + data_blocks + ext_blocks",
        "        return 1 + data_blocks  # MUTANT",
        ["test_blocks_for_matches_amitools_own_accounting"],
    ),
    (
        "blocks_for ignores the OFS per-block header",
        VOL,
        "        per_block = bs if getattr(self._vol, \"is_ffs\", True) else bs - 24",
        "        per_block = bs  # MUTANT",
        ["test_blocks_for_matches_amitools_own_accounting"],
    ),
    (
        "illegal characters are allowed in names",
        VOL,
        "        for bad in ILLEGAL_NAME_CHARS:",
        "        for bad in ():  # MUTANT",
        ["test_illegal_characters_are_refused"],
    ),
    (
        "the name-length limit is not enforced",
        VOL,
        "        if encoded > self.name_limit:",
        "        if False:  # MUTANT",
        ["test_mkdir_refuses_an_over_long_name",
         "test_name_length_is_enforced_at_the_boundary"],
    ),
    (
        "read-only volumes accept writes",
        VOL,
        "        if not self._writable:",
        "        if False:  # MUTANT",
        ["test_a_read_only_volume_refuses_writes"],
    ),
    (
        "timestamps.now() routes through the amitools epoch",
        ROOT / "amibuilder" / "timestamps.py",
        "    return from_datetime(dt.datetime.now())",
        "    import time as _t\n"
        "    return from_datetime(dt.datetime.fromtimestamp(\n"
        "        _t.mktime(_t.localtime()) - 252489600 + 252460800))  # MUTANT",
        ["test_a_new_file_is_stamped_with_the_current_wall_clock"],
    ),
]


def run_tests(names: list[str]) -> tuple[bool, str]:
    """Return (passed, tail-of-output) for the named tests."""
    argv = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "test/test_write_cli.py", "--timeout=300", "-x",
            "-k", " or ".join(names)]
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-400:]


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    failures: list[str] = []

    print("baseline: confirming the guards pass unmutated")
    all_names = sorted({n for _, _, _, _, ns in MUTATIONS for n in ns})
    ok, tail = run_tests(all_names)
    if not ok:
        print("BASELINE FAILED -- fix that before mutating\n" + tail)
        return 1
    print(f"  ok, {len(all_names)} guard test(s) green\n")

    with tempfile.TemporaryDirectory() as td:
        backup_dir = Path(td)
        for label, path, find, repl, names in MUTATIONS:
            if only and only not in label:
                continue
            backup = backup_dir / path.name
            shutil.copy2(path, backup)
            original = path.read_text()

            count = original.count(find)
            if count != 1:
                print(f"SKIP  {label}\n      anchor matched {count} times, not 1")
                failures.append(f"{label} (bad anchor)")
                continue

            try:
                path.write_text(original.replace(find, repl))
                ok, tail = run_tests(names)
                if ok:
                    print(f"SURVIVED  {label}")
                    print(f"          {', '.join(names)} stayed GREEN -- vacuous guard")
                    failures.append(label)
                else:
                    print(f"killed    {label}")
            finally:
                shutil.copy2(backup, path)
                assert filecmp.cmp(backup, path, shallow=False), f"restore of {path} failed"

    print()
    if failures:
        print(f"{len(failures)} mutation(s) survived:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("every mutation was killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
