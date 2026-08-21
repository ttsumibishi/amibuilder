"""Mutation-test the `shell` guards (commands/shell.py + the transfer helpers it uses).

For each mutation: patch a source file, run the tests that claim to cover the property,
and require that they go RED. A guard that stays green under its own mutation is
decoration. Restores are verified byte-for-byte.

The shell is the first place a user can lose data with a one-word command, so its guards
carry weight: the no-overwrite rule on every write verb, the flush-after-every-mutation
rule (the one that keeps an unclean exit from corrupting the bitmap), the file-only
refusals, the same-path move guard, the cd checks, and the up-from-root clamp in the path
resolver.

Two of the guards are deliberately backstopped by a lower layer -- the shell's file-only
`rm` is also refused by `Volume.remove`, and the transfer layer's file-only `cp` is also
refused by `read_file`. Their tests therefore assert the *specific wording* of the shell's
own refusal, so removing the guard (and falling through to the backstop's different
message) is observable. That is why `test_rm_refuses_a_directory` looks for "files only"
and `test_cp_a_directory_is_refused` for "single file".

Python, not shell, for the same reason as the sibling harnesses: a multi-line anchor
cannot be counted reliably by `grep -c`, and a mis-restore would be silent.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python")
SHELL = ROOT / "amibuilder" / "commands" / "shell.py"
TRANSFER = ROOT / "amibuilder" / "commands" / "transfer.py"

# (label, file, find, replace, tests that must go red)
MUTATIONS: list[tuple[str, Path, str, str, list[str]]] = [
    # -- no overwrite, one write verb at a time -----------------------------
    (
        "cp overwrites an existing destination",
        SHELL,
        "    entry = transfer.copy_in_image(state.vol, src, dst, overwrite=False)",
        "    entry = transfer.copy_in_image(state.vol, src, dst, overwrite=True)  # MUTANT",
        ["test_cp_will_not_overwrite"],
    ),
    (
        "mv overwrites an existing destination",
        SHELL,
        "    transfer.copy_in_image(state.vol, src, dst, overwrite=False)\n"
        "    state.vol.remove(src, recursive=False)",
        "    transfer.copy_in_image(state.vol, src, dst, overwrite=True)  # MUTANT\n"
        "    state.vol.remove(src, recursive=False)",
        ["test_mv_onto_an_existing_name_is_refused_and_keeps_the_source"],
    ),
    (
        "put overwrites an existing destination",
        SHELL,
        "    entry = transfer.put_file(state.vol, host, dest, overwrite=False)",
        "    entry = transfer.put_file(state.vol, host, dest, overwrite=True)  # MUTANT",
        ["test_put_will_not_overwrite"],
    ),
    (
        "get overwrites an existing local file",
        SHELL,
        "        state.vol, src, state.local_cwd, force=False, emit=lines.append,",
        "        state.vol, src, state.local_cwd, force=True, emit=lines.append,  # MUTANT",
        ["test_get_will_not_overwrite_a_local_file"],
    ),
    # -- flush after every mutating command ---------------------------------
    (
        "rm does not flush",
        SHELL,
        "    state.vol.remove(rel, recursive=False)\n    state.vol.flush()",
        "    state.vol.remove(rel, recursive=False)  # MUTANT: no flush",
        ["test_a_removed_file_is_flushed_before_the_session_closes"],
    ),
    (
        "cp does not flush",
        SHELL,
        "    entry = transfer.copy_in_image(state.vol, src, dst, overwrite=False)\n"
        "    state.vol.flush()",
        "    entry = transfer.copy_in_image(state.vol, src, dst, overwrite=False)  # MUTANT: no flush",
        ["test_a_copy_is_flushed_before_the_session_closes"],
    ),
    (
        "mv does not flush",
        SHELL,
        "    state.vol.remove(src, recursive=False)\n    state.vol.flush()",
        "    state.vol.remove(src, recursive=False)  # MUTANT: no flush",
        ["test_a_move_is_flushed_before_the_session_closes"],
    ),
    (
        "put does not flush",
        SHELL,
        "    entry = transfer.put_file(state.vol, host, dest, overwrite=False)\n"
        "    state.vol.flush()",
        "    entry = transfer.put_file(state.vol, host, dest, overwrite=False)  # MUTANT: no flush",
        ["test_a_put_is_flushed_before_the_session_closes"],
    ),
    # -- file-only / same-path / cd / clamp ---------------------------------
    (
        "rm no longer refuses a directory (shell's own guard)",
        SHELL,
        "    if entry.is_dir:\n"
        "        raise UsageError(\n"
        "            f\"{_amiga_path(state.vol, rel)} is a directory; the shell removes files only\"\n"
        "        )",
        "    if False and entry.is_dir:  # MUTANT\n"
        "        raise UsageError(\n"
        "            f\"{_amiga_path(state.vol, rel)} is a directory; the shell removes files only\"\n"
        "        )",
        ["test_rm_refuses_a_directory"],
    ),
    (
        "cd no longer checks the target exists",
        SHELL,
        "    if target and not state.vol.exists(target):\n"
        "        raise UsageError(f\"no such directory: {_amiga_path(state.vol, target)}\")",
        "    if False and target and not state.vol.exists(target):  # MUTANT\n"
        "        raise UsageError(f\"no such directory: {_amiga_path(state.vol, target)}\")",
        ["test_cd_into_a_missing_directory_is_refused"],
    ),
    (
        "cd no longer checks the target is a directory",
        SHELL,
        "    if target and not state.vol.is_dir(target):\n"
        "        raise UsageError(f\"not a directory: {_amiga_path(state.vol, target)}\")",
        "    if False and target and not state.vol.is_dir(target):  # MUTANT\n"
        "        raise UsageError(f\"not a directory: {_amiga_path(state.vol, target)}\")",
        ["test_cd_into_a_file_is_refused"],
    ),
    (
        "mv no longer refuses a move onto itself",
        SHELL,
        "    if src == dst:\n"
        "        raise UsageError(\"source and destination are the same\")",
        "    if False and src == dst:  # MUTANT\n"
        "        raise UsageError(\"source and destination are the same\")",
        ["test_mv_to_the_same_path_is_refused"],
    ),
    (
        "resolve_image no longer clamps at the root",
        SHELL,
        "        if parts:\n            parts.pop()",
        "        parts.pop()  # MUTANT: up-from-root clamp removed",
        ["test_resolve_image"],
    ),
    # -- transfer layer -----------------------------------------------------
    (
        "copy_in_image no longer refuses a directory",
        TRANSFER,
        "    if entry.is_dir:\n"
        "        raise ImageError(f\"{vol.name}:{entry.path} is a directory; cp copies a single file\")",
        "    if False and entry.is_dir:  # MUTANT\n"
        "        raise ImageError(f\"{vol.name}:{entry.path} is a directory; cp copies a single file\")",
        ["test_cp_a_directory_is_refused"],
    ),
    (
        "put_file no longer requires the source to exist",
        TRANSFER,
        "    if not host.exists():\n"
        "        raise UsageError(f\"{host}: no such file on the host\")",
        "    if False and not host.exists():  # MUTANT\n"
        "        raise UsageError(f\"{host}: no such file on the host\")",
        ["test_put_a_missing_host_file_is_refused"],
    ),
    # -- tab completion -----------------------------------------------------
    (
        "completion never distinguishes command from argument",
        SHELL,
        "    if not prior:",
        "    if True:  # MUTANT",
        ["test_complete_command_vs_argument_boundary"],
    ),
    (
        "completion does not offer in-image paths",
        SHELL,
        "    if cmd in _IMAGE_PATH_COMMANDS:",
        "    if False and cmd in _IMAGE_PATH_COMMANDS:  # MUTANT",
        ["test_complete_image_paths_at_root"],
    ),
    (
        "completion does not offer host paths",
        SHELL,
        "    if cmd in _LOCAL_PATH_COMMANDS:",
        "    if False and cmd in _LOCAL_PATH_COMMANDS:  # MUTANT",
        ["test_complete_local_paths"],
    ),
    (
        "completion drops the trailing slash on directories",
        SHELL,
        "    out = [prefix + e.name + (\"/\" if e.is_dir else \"\")",
        "    out = [prefix + e.name  # MUTANT: no dir slash",
        ["test_complete_image_paths_at_root"],
    ),
]


def run_tests(names: list[str]) -> tuple[bool, str]:
    """Return (passed, tail-of-output) for the named tests.

    Bytecode caching would otherwise make this non-deterministic: Python invalidates a
    `.pyc` by comparing source mtimes truncated to whole seconds, so rewriting a source
    file and re-running within the same second can reuse the *pre-mutation* bytecode and
    report a live guard as SURVIVED. A fresh `PYTHONPYCACHEPREFIX` per run forces every
    module to compile from the current source, so the run reflects the mutation on disk.
    """
    argv = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "test/test_shell.py", "--timeout=300", "-x",
            "-k", " or ".join(names)]
    with tempfile.TemporaryDirectory() as pycache:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=pycache)
        proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, env=env)
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
