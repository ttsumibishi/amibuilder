"""Tokeniser and static analyser for Commodore `Installer` scripts.

The Installer language (Sylvan Technical Arts, shipped with AmigaOS 2.1 onwards) is a
LISP dialect: S-expressions, `;` line comments, double-quoted strings with backslash
escapes. That makes it straightforward to parse; the interesting question is not whether
a script *parses* but whether every construct it uses can be modelled without running
m68k code.

This module answers that question for a given script. It is a prototype of what would
become `amibuilder install-from-adf --simulate`, and it lives under test/ for now because it
has no product code to attach to yet.

Findings for the AmigaOS 3.2 installer are in docs/KIP-FFS-INSTALLERS.md.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

TokenKind = Literal["(", ")", "str", "atom"]


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------


def tokenize(text: str) -> Iterator[tuple[TokenKind, str]]:
    """Yield (kind, value) tokens, discarding `;` comments.

    Strings honour backslash escapes, so an escaped quote does not end the literal --
    which matters because these scripts embed quoted AmigaDOS command lines.
    """
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in " \t\r\n":
            i += 1
        elif c == "(":
            yield "(", "("
            i += 1
        elif c == ")":
            yield ")", ")"
            i += 1
        elif c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif text[j] == '"':
                    break
                else:
                    buf.append(text[j])
                    j += 1
            yield "str", "".join(buf)
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()";':
                j += 1
            yield "atom", text[i:j]
            i = j


def parse(text: str) -> list:
    """Parse into nested Python lists. Strings become (str, value) tuples.

    Raises ValueError on unbalanced parentheses, which is the one syntactic error worth
    detecting up front.
    """
    stack: list[list] = [[]]
    for kind, val in tokenize(text):
        if kind == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif kind == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced: too many closing parentheses")
            stack.pop()
        elif kind == "str":
            stack[-1].append(("str", val))
        else:
            stack[-1].append(val)
    if len(stack) != 1:
        raise ValueError(f"unbalanced: {len(stack) - 1} unclosed parentheses")
    return stack[0]


# ---------------------------------------------------------------------------
# Classification of the language surface
# ---------------------------------------------------------------------------

#: Functions whose effect is a file-level change a layer can record directly.
FILE_OPS = frozenset({
    "copyfiles", "copylib", "makedir", "delete", "rename", "protect", "makeassign",
    "textfile", "startup", "foreach", "fileonly", "pathonly", "expandpath", "exists",
    "getsize", "earlier", "getversion", "getdevice", "getassign", "tackon",
})

#: Functions that ask the user something. These become CLI prompts or a saved answer file.
PROMPTS = frozenset({
    "askdir", "askfile", "askoptions", "askchoice", "askbool", "askstring",
    "asknumber", "askdisk", "welcome", "message", "user", "complete", "working",
    "abort", "exit",
})

#: Pure interpreter work.
CONTROL = frozenset({
    "if", "while", "until", "procedure", "set", "and", "or", "not", "select", "trap",
    "onerror", "return", "cat", "strlen", "substr", "symbolset", "symbolval",
    "transcript", "=", "<", ">", "<=", ">=", "<>", "+", "-", "*", "/", "AND", "OR",
    "NOT", "IN", "BITAND", "BITOR", "XOR", "SHIFTLEFT", "SHIFTRIGHT",
})

#: Constructs that cannot be modelled purely, with the reason.
HARD: dict[str, str] = {
    "run": "runs an m68k binary; needs vamos, a shim, or observe mode",
    "execute": "runs an AmigaDOS script or binary",
    "tooltype": "edits .info icon files (binary format, no amitools support)",
    "iconinfo": "reads .info icon files",
    "database": "queries the host machine; answerable from a target profile",
    "askdisk": "waits for a physical floppy; satisfiable by supplying an ADF",
    "getdiskspace": "queries the target volume; answerable from image geometry",
}

#: `run` targets that are no-ops or trivially replaceable at file level.
BENIGN_RUN = {
    "wait": "timing only, no filesystem effect",
    "resident": "AmigaDOS RAM-residency optimisation, no filesystem effect",
    "addbuffers": "floppy cache tuning, no filesystem effect",
    "delete": "replaceable with our own delete",
    "copy": "replaceable with our own copy",
    "dacontrol": "mounts/ejects an ADF as a virtual floppy -- we read the ADF directly",
    "loadmodule": "soft-kicks ROM modules; irrelevant to a file-level install",
    "amigamodel": "writes a machine fact to ENV:; supply from the target profile",
    "cpu": "writes a machine fact to ENV:; supply from the target profile",
    "guessbootdev": "writes the boot device to ENV:; supply from the target profile",
}


def normalise_run_target(target: str) -> str:
    """Reduce a `run` target to a bare lowercase command name.

    Strips an AmigaDOS device prefix (`C:`) and any directory part, so
    `C:LoadModule`, `Install3.2:C/Delete` and `Delete` all reduce to the same key.

    Uses removeprefix rather than lstrip: `lstrip("c:")` strips *characters*, so it
    turns "copy" into "opy". That bug shipped here once and the suite caught it.
    """
    name = target.split("/")[-1]
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name.lower()


@dataclass
class Analysis:
    """Static analysis of one Installer script."""

    path: str
    total_bytes: int
    lines: int
    tokens: int
    forms: int
    max_depth: int
    balanced: bool
    heads: Counter = field(default_factory=Counter)
    string_bytes: int = 0
    comment_bytes: int = 0
    procedures: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    message_symbols: int = 0
    run_targets: Counter = field(default_factory=Counter)

    # -- derived views ------------------------------------------------------
    @property
    def code_bytes(self) -> int:
        return self.total_bytes - self.string_bytes - self.comment_bytes

    @property
    def string_fraction(self) -> float:
        return self.string_bytes / self.total_bytes if self.total_bytes else 0.0

    def calls(self, names) -> int:
        return sum(self.heads.get(n, 0) for n in names)

    @property
    def hard_calls(self) -> dict[str, int]:
        return {n: self.heads[n] for n in HARD if self.heads.get(n)}

    @property
    def unmodellable_run_targets(self) -> dict[str, int]:
        """`run` targets that are neither benign nor obviously replaceable."""
        return {
            t: c for t, c in self.run_targets.items()
            if normalise_run_target(t) not in BENIGN_RUN
        }

    @property
    def unresolved_run_calls(self) -> int:
        """`run` calls whose target the regex could not extract.

        These are forms like `(run (cat (tackon installPath "C/AddBuffers") ...))`,
        where the command name is assembled from variables rather than appearing as a
        leading literal. They need reading by hand.

        Exposed deliberately: a check that only inspects `run_targets` would pass while
        an unassessed binary hid inside one of these, so any audit must account for this
        number reaching zero or being explained.
        """
        return self.heads.get("run", 0) - sum(self.run_targets.values())

    @property
    def unknown_heads(self) -> list[tuple[str, int]]:
        known = FILE_OPS | PROMPTS | CONTROL | set(HARD)
        return sorted(
            ((n, c) for n, c in self.heads.items() if n not in known),
            key=lambda kv: -kv[1],
        )


_RUN_TARGET = re.compile(r'\(run\s+(?:\(cat\s+)?"?([A-Za-z0-9_:./]+)')


def analyze(text: str, path: str = "<script>") -> Analysis:
    """Statically analyse an Installer script."""
    toks = list(tokenize(text))

    heads: Counter = Counter()
    depth = max_depth = forms = 0
    expect_head = False
    for kind, val in toks:
        if kind == "(":
            depth += 1
            max_depth = max(max_depth, depth)
            forms += 1
            expect_head = True
        elif kind == ")":
            depth -= 1
            expect_head = False
        else:
            if expect_head and kind == "atom":
                heads[val] += 1
            expect_head = False

    comment_bytes = sum(len(m) for m in re.findall(r";[^\n]*", text))

    string_bytes = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            string_bytes += 1
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            string_bytes += 1

    run_targets: Counter = Counter()
    for m in _RUN_TARGET.finditer(text):
        run_targets[m.group(1)] += 1

    return Analysis(
        path=path,
        total_bytes=len(text),
        lines=text.count("\n") + 1,
        tokens=len(toks),
        forms=forms,
        max_depth=max_depth,
        balanced=(depth == 0),
        heads=heads,
        string_bytes=string_bytes,
        comment_bytes=comment_bytes,
        procedures=re.findall(r"\(procedure\s+([A-Za-z0-9_\-]+)", text),
        languages=sorted(set(re.findall(r'\(=\s*@language\s+"([a-z()\-]+)"', text))),
        message_symbols=len(set(re.findall(r"\(set\s+(#[A-Za-z0-9_\-]+)", text))),
        run_targets=run_targets,
    )


def report(a: Analysis) -> str:
    """Human-readable feasibility summary."""
    out = [
        f"script          : {a.path}",
        (f"bytes           : {a.total_bytes:,}  "
         f"(strings {a.string_fraction:.0%}, code {a.code_bytes:,})"),
        f"lines / forms   : {a.lines:,} / {a.forms:,}",
        (f"distinct heads  : {len(a.heads)}   max nesting {a.max_depth}   "
         f"balanced {a.balanced}"),
        f"procedures      : {len(a.procedures)}",
        f"languages       : {len(a.languages)}   message symbols {a.message_symbols}",
        "",
        f"file operations : {a.calls(FILE_OPS)}",
        f"prompts         : {a.calls(PROMPTS)}",
        f"control flow    : {a.calls(CONTROL)}",
    ]
    if a.hard_calls:
        out.append("")
        out.append("constructs needing care:")
        for n, c in sorted(a.hard_calls.items(), key=lambda kv: -kv[1]):
            out.append(f"  {n:<14} {c:>4}   {HARD[n]}")
    if a.run_targets:
        out.append("")
        out.append("run targets:")
        for t, c in a.run_targets.most_common():
            why = BENIGN_RUN.get(normalise_run_target(t), "NEEDS EMULATION OR A SHIM")
            out.append(f"  {t:<22} {c:>3}   {why}")
    return "\n".join(out)
