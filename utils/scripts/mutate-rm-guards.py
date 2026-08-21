"""Mutation-test the `rm` guards (Volume.remove + cmd_rm).

For each mutation: patch a source file, run the tests that claim to cover the property,
and require that they go RED. A guard that stays green under its own mutation is
decoration. Restores are verified byte-for-byte.

`rm` is the first command that destroys data, so its guards matter more than most: the
write-safety guard, the two "not by accident" guards (never the root, never a directory
without `-r`), the no-half-applied-batch rule, the overlap tolerance under `-r`, and the
correct parent re-stamp. Every one is mutated here and must be caught by a named test.

Volume-level and cmd_rm-level guards are mutated separately on purpose. The two are not
redundant: cmd_rm validates the whole list up front (a property the Volume layer cannot
have, since it takes one path), while `Volume.remove` is the guard the planned shell will
sit behind directly. Mutating only one layer would let the other's copy mask the change,
so each has its own anchor and its own test -- including two Volume-level guards
(`test_remove_refuses_the_volume_root`, `test_remove_refuses_a_directory_without_recursive`)
that exist precisely so the Volume layer is not proved only through cmd_rm.

Written in Python rather than shell for the same reason as mutate-write-guards.py: a
multi-line anchor cannot be counted reliably by `grep -c`, and a mis-restore would be
silent. The anchor-matched-exactly-once check is what turns a future refactor into a loud
failure instead of a vacuous run.
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
VOL = ROOT / "amibuilder" / "volume.py"
WRITE = ROOT / "amibuilder" / "commands" / "write.py"

# (label, file, find, replace, tests that must go red)
MUTATIONS: list[tuple[str, Path, str, str, list[str]]] = [
    # -- volume.py: Volume.remove --------------------------------------------
    (
        "remove() skips the writable check",
        VOL,
        "        self._require_writable()\n"
        "        rel = _norm(path)\n"
        "        if not rel:\n"
        "            raise UsageError(f\"{self.label}: cannot remove the volume root\")",
        "        rel = _norm(path)  # MUTANT: _require_writable() removed\n"
        "        if not rel:\n"
        "            raise UsageError(f\"{self.label}: cannot remove the volume root\")",
        ["test_a_read_only_volume_refuses_writes"],
    ),
    (
        "remove() no longer refuses the volume root",
        VOL,
        "        if not rel:\n"
        "            raise UsageError(f\"{self.label}: cannot remove the volume root\")",
        "        if False:  # MUTANT: root guard removed\n"
        "            raise UsageError(f\"{self.label}: cannot remove the volume root\")",
        ["test_remove_refuses_the_volume_root"],
    ),
    (
        "remove() no longer refuses a directory without recursive",
        VOL,
        "        if node.is_dir() and not recursive:\n"
        "            raise ImageError(\n"
        "                f\"{self.label}: {rel} is a directory; pass recursive to remove it and \"\n"
        "                f\"everything under it\"\n"
        "            )",
        "        if False and node.is_dir() and not recursive:  # MUTANT: dir guard removed\n"
        "            raise ImageError(\n"
        "                f\"{self.label}: {rel} is a directory; pass recursive to remove it and \"\n"
        "                f\"everything under it\"\n"
        "            )",
        ["test_remove_refuses_a_directory_without_recursive"],
    ),
    (
        "remove() does not re-stamp the parent",
        VOL,
        "        if parent is not None:\n"
        "            self._stamp(parent)\n"
        "        else:\n"
        "            self._invalidate()\n"
        "        return entry",
        "        if parent is not None:\n"
        "            self._invalidate()  # MUTANT: parent not stamped\n"
        "        else:\n"
        "            self._invalidate()\n"
        "        return entry",
        ["test_remove_stamps_the_parent"],
    ),
    (
        # The companion to the above: prove it is *our* _stamp, not amitools' update_ts,
        # that produces a correct time. update_ts=False -> True alone would survive (the
        # _stamp overwrites it), so it is mutated together with dropping _stamp. Mirrors the
        # belt-and-braces reasoning in mutate-write-guards.py.
        "remove() relies on amitools' own (hour-adrift) stamp",
        VOL,
        "        try:\n"
        "            node.delete(wipe=False, all=recursive, update_ts=False)\n"
        "        except Exception as e:\n"
        "            raise ImageError(f\"{self.label}: cannot remove {rel}: {e}\") from e\n"
        "\n"
        "        if parent is not None:\n"
        "            self._stamp(parent)\n"
        "        else:\n"
        "            self._invalidate()\n"
        "        return entry",
        "        try:\n"
        "            node.delete(wipe=False, all=recursive, update_ts=True)  # MUTANT\n"
        "        except Exception as e:\n"
        "            raise ImageError(f\"{self.label}: cannot remove {rel}: {e}\") from e\n"
        "\n"
        "        if parent is not None:\n"
        "            self._invalidate()  # MUTANT: rely on amitools' own stamp\n"
        "        else:\n"
        "            self._invalidate()\n"
        "        return entry",
        ["test_remove_stamps_the_parent"],
    ),
    # -- commands/write.py: cmd_rm -------------------------------------------
    (
        "cmd_rm no longer refuses the volume root",
        WRITE,
        "            if not rel:\n"
        "                raise UsageError(f\"{name}: cannot remove the volume root\")",
        "            if False:  # MUTANT\n"
        "                raise UsageError(f\"{name}: cannot remove the volume root\")",
        ["test_rm_the_volume_root_is_refused"],
    ),
    (
        "cmd_rm no longer refuses a directory without -r",
        WRITE,
        "            if entry.is_dir and not args.recursive:\n"
        "                raise ImageError(\n"
        "                    f\"{name}:{rel} is a directory; pass -r to remove it and its contents\"\n"
        "                )",
        "            if False and entry.is_dir and not args.recursive:  # MUTANT\n"
        "                raise ImageError(\n"
        "                    f\"{name}:{rel} is a directory; pass -r to remove it and its contents\"\n"
        "                )",
        ["test_rm_a_directory_without_recursive_is_refused",
         "test_rm_dry_run_still_refuses_a_directory_without_recursive"],
    ),
    (
        "cmd_rm deletes during validation instead of after (half-applied batch)",
        WRITE,
        "            targets.append((rel, entry))",
        "            vol.remove(rel, recursive=args.recursive)  # MUTANT: delete during validation\n"
        "            targets.append((rel, entry))",
        ["test_rm_validates_the_whole_list_before_deleting_anything"],
    ),
    (
        "cmd_rm no longer tolerates overlapping paths under -r",
        WRITE,
        "            except NotFoundError:\n"
        "                # Reachable only when the list names both a directory and something inside\n"
        "                # it under -r: the earlier recursive delete already took this one. Report it\n"
        "                # rather than abort -- the user's intent (both gone) is satisfied.\n"
        "                out.line(f\"  already removed {name}:{rel} (was inside an earlier target)\")\n"
        "                continue",
        "            except NotFoundError:\n"
        "                raise  # MUTANT: overlap tolerance removed\n"
        "                out.line(f\"  already removed {name}:{rel} (was inside an earlier target)\")\n"
        "                continue",
        ["test_rm_overlapping_paths_under_recursive_are_tolerated"],
    ),
]


def run_tests(names: list[str]) -> tuple[bool, str]:
    """Return (passed, tail-of-output) for the named tests."""
    argv = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "test/test_write_cli.py", "--timeout=300", "-x",
            "-k", " or ".join(names)]
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-500:]


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
