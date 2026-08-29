#!/usr/bin/env python3
"""Check that every `amibuilder` command line in the docs actually works.

A copy-pasteable example that fails is worse than no example, and it is the kind of rot that
accumulates invisibly: a flag gets renamed, and the four places the docs used it stay put. This
walks the markdown, pulls out anything that looks like a real invocation, and validates the
command and its options against the **live argparse parser** -- the same source
`commands/completion.py` is generated from, so the check cannot drift from the real CLI.

It found `compose --format dir --metadata uaem` in `docs/KIP-FFS-LAYERS.md`, which exited 2 with
"unrecognized arguments": the option had shipped as `--no-metadata` and the example was never
updated.

    utils/scripts/audit-docs.py            # the user-facing docs at the repo root
    utils/scripts/audit-docs.py --all      # also docs/, which is aspirational in places
    utils/scripts/audit-docs.py --verbose  # list every invocation checked

Exit 0 when clean, 1 when something is wrong, 2 on a usage problem -- so it can be a gate.

**Scope is deliberately the repo root by default.** README/USAGE/FAQ/STATISTICS document what
exists and every example in them should run. `docs/KIP-FFS-*.md` are working notes and design
records that describe planned capability on purpose -- `KIP-FFS-LAYERS.md` names four commands
it sketches that were never built -- so auditing them reports intent as breakage. Use `--all`
when you want to review those anyway.

**What counts as an invocation, and why the rule is narrow.** Only lines inside a fenced code
block, or text inside backticks. Prose mentions like "amibuilder reads the RDB" are not command
lines, and an earlier version of this script reported nine of them as unknown commands. Lines
carrying placeholder syntax (`<name>`, `...`) are skipped too, since a syntax sketch is not
meant to run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

try:
    from amibuilder.cli import build_parser
except ImportError as exc:  # pragma: no cover - a dev-environment problem, not a doc problem
    print(f"cannot import amibuilder ({exc}). Run from the repo with the venv active, e.g.\n"
          f"  .venv/bin/python utils/scripts/audit-docs.py", file=sys.stderr)
    raise SystemExit(2) from exc

#: `$ ` prompts and the module form are both real invocations.
_LEADER = re.compile(r"^(?:\$\s*)?(?:\.venv/bin/python\s+-m\s+)?amibuilder\s+(?P<rest>.+)$")

#: Placeholder syntax: a sketch, not something anyone can run.
_PLACEHOLDER = re.compile(r"[<>]|\.\.\.")


def parser_index() -> tuple[dict[str, set[str]], set[str]]:
    """Map each command (and `snap x` / `recipe x` pair) to the options it accepts.

    Reads argparse's `_actions` and `choices` privates on purpose, exactly as
    `commands/completion.py` does and for the same reason: argparse exposes no public way to
    enumerate a subparser's options, and deriving the answer from the real parser is the only
    version that cannot fall out of date. A hand-maintained list of commands and flags would be
    a second description of the CLI to keep in step -- which is the bug this script exists to
    catch. Expect linters to flag these four accesses.
    """
    root, _handlers = build_parser()
    globals_: set[str] = set()
    for act in root._actions:
        globals_.update(act.option_strings)

    commands: dict[str, set[str]] = {}
    for act in root._actions:
        subs = getattr(act, "choices", None)
        if not isinstance(subs, dict):
            continue
        for name, sub in subs.items():
            opts: set[str] = set()
            for a in sub._actions:
                opts.update(a.option_strings)
                nested = getattr(a, "choices", None)
                if isinstance(nested, dict):
                    for nname, nsub in nested.items():
                        nopts: set[str] = set()
                        for na in nsub._actions:
                            nopts.update(na.option_strings)
                        commands[f"{name} {nname}"] = nopts | globals_
            commands[name] = opts | globals_
    return commands, globals_


def invocations(text: str) -> list[tuple[int, str]]:
    """Every (line number, invocation) in one document, code contexts only."""
    found: list[tuple[int, str]] = []
    in_fence = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        candidates: list[str] = []
        if in_fence:
            candidates.append(stripped)
        else:
            # Backticked spans only, so prose cannot be mistaken for a command line.
            candidates.extend(re.findall(r"`([^`]+)`", raw))
        for cand in candidates:
            m = _LEADER.match(cand.strip())
            if m:
                found.append((lineno, m.group("rest").strip()))
    return found


def check(rest: str, commands: dict[str, set[str]]) -> list[str]:
    """Problems with one invocation's command and flags, or []."""
    # A trailing comment is documentation, not an argument.
    rest = rest.split(" #", 1)[0].strip()
    tokens = rest.split()
    if not tokens or tokens[0].startswith("-"):
        return []                                  # `amibuilder --version` and friends
    cmd = tokens[0]
    if len(tokens) > 1 and f"{cmd} {tokens[1]}" in commands:
        cmd = f"{cmd} {tokens[1]}"
    if cmd not in commands:
        return [f"unknown command {cmd!r}"]
    valid = commands[cmd]
    problems = []
    for tok in tokens:
        if not tok.startswith("-") or tok in ("-", "--"):
            continue
        flag = tok.split("=", 1)[0].rstrip(",.;:)`'\"")
        if flag.startswith("-") and flag not in valid:
            problems.append(f"`{cmd}` has no {flag}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true",
                    help="also audit docs/, which describes planned capability on purpose")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list every invocation checked, not just the problems")
    args = ap.parse_args()

    files = sorted(REPO.glob("*.md"))
    if args.all:
        files += sorted((REPO / "docs").glob("*.md"))
    if not files:
        print(f"no markdown found under {REPO}", file=sys.stderr)
        return 2

    commands, _ = parser_index()
    problems: list[str] = []
    checked = skipped = 0

    for path in files:
        rel = path.relative_to(REPO)
        for lineno, rest in invocations(path.read_text()):
            if _PLACEHOLDER.search(rest):
                skipped += 1
                continue
            checked += 1
            if args.verbose:
                print(f"  {rel}:{lineno}: amibuilder {rest}")
            for problem in check(rest, commands):
                problems.append(f"{rel}:{lineno}: {problem}\n    amibuilder {rest}")

    print(f"checked {checked} invocation(s) across {len(files)} file(s)"
          f"{f', skipped {skipped} placeholder sketch(es)' if skipped else ''}")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        return 1
    print("every documented command and flag exists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
