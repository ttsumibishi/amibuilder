"""Parsers for AmigaDOS command output.

The point of these is to let a test compare two drives using **AmigaDOS's own view** of them rather
than amibuilder's. Everything amibuilder reports about an image it also wrote, so a shared
misunderstanding of FFS would be invisible; `Info` and `List` come from a real AmigaOS running on a
real Kickstart and know nothing about this project.

Kept as pure functions over captured text so they can be unit-tested without an emulator, which is
`test_amigados_parsing.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: `Info`'s per-volume line:
#:     DH0        39M       1762      80124   2%   0  Read/Write Workbench
#: Status may contain a slash and the name may contain spaces ("Ram Disk"), so the name takes the
#: rest of the line.
_INFO_ROW = re.compile(
    r"^(?P<unit>\S+)\s+(?P<size>\S+)\s+(?P<used>\d+)\s+(?P<free>\d+)\s+"
    r"(?P<full>\S+)\s+(?P<errs>\d+)\s+(?P<status>\S+)\s+(?P<name>.*\S)\s*$"
)

#: `List`'s directory header: Directory "SYS:Prefs" on Wednesday 19-Aug-26
_DIR_HEADER = re.compile(r'^Directory\s+"(?P<path>[^"]+)"\s+on\s+')

#: What `List` prints in the size column. A zero-length file is rendered as the **word** `empty`,
#: not `0` -- a real drive turned up `empty-file  empty ----rwed`, and a digits-only pattern
#: silently dropped it, which would have let a bug that lost every empty file pass unnoticed.
#: `Dir` is a directory. Anything else here would be dropped, so the set is deliberately explicit
#: rather than a permissive `\S+` that would also swallow totals lines.
_SIZE_TOKEN = r"\d+|Dir|empty"

#: A `List` entry. Two or more spaces separate the name from the size, which is how a filename
#: containing a single space stays intact.
_LIST_ENTRY = re.compile(
    r"^(?P<name>\S.*?)\s{2,}(?P<size>" + _SIZE_TOKEN + r")"
    r"\s+(?P<protect>[-a-zA-Z]{8})\s+(?P<when>.*\S)\s*$"
)

#: Recognises a totals line so it cannot be mistaken for an entry. The clauses are individually
#: optional because AmigaDOS omits what does not apply:
#:     9 files - 111K bytes - 11 directories - 258 blocks used
#:     1 file - 2 directories - 6 blocks used        (a directory holding only an empty file)
#:     1 directory - 2 blocks used                    (no files at all)
_TOTALS = re.compile(r"^(?:TOTAL:\s+)?\d+\s+(?:files?|director(?:y|ies))\b.*\bblocks?\s+used\b")

#: The grand total. `bytes` is optional for the same reason: a volume holding only empty files
#: reports no byte count at all.
_GRAND_TOTAL = re.compile(
    r"^TOTAL:\s+(?P<files>\d+)\s+files?\s+-\s+"
    r"(?:(?P<bytes>\S+)\s+bytes?\s+-\s+)?"
    r"(?P<dirs>\d+)\s+director(?:y|ies)\s+-\s+(?P<blocks>\d+)\s+blocks?\s+used"
)


@dataclass(frozen=True)
class InfoRow:
    """One mounted volume as `Info` sees it."""

    unit: str
    size: str
    used: int
    free: int
    full: str
    errs: int
    status: str
    name: str

    @property
    def is_healthy(self) -> bool:
        """`Errs` is a per-volume error count maintained by the filesystem itself."""
        return self.errs == 0


@dataclass(frozen=True)
class Entry:
    """One file or directory as `List` sees it. Compared as a whole."""

    size: str
    protect: str
    when: str

    @property
    def is_dir(self) -> bool:
        return self.size == "Dir"

    @property
    def is_empty_file(self) -> bool:
        """AmigaDOS prints `empty` rather than `0` in the size column."""
        return self.size == "empty"


@dataclass(frozen=True)
class Totals:
    files: int
    #: None when AmigaDOS printed no byte count, which happens when every file is empty.
    size: str | None
    dirs: int
    blocks: int


def parse_info(text: str) -> dict[str, InfoRow]:
    """Every volume line from `Info`, keyed by unit (DH0, RAM, ...).

    Lines like `DF0  No disk present` do not match and are skipped, which is wanted -- an empty
    floppy drive is not a volume.
    """
    rows: dict[str, InfoRow] = {}
    for line in text.splitlines():
        if line.startswith("Unit "):
            continue
        m = _INFO_ROW.match(line.rstrip())
        if not m:
            continue
        rows[m.group("unit")] = InfoRow(
            unit=m.group("unit"),
            size=m.group("size"),
            used=int(m.group("used")),
            free=int(m.group("free")),
            full=m.group("full"),
            errs=int(m.group("errs")),
            status=m.group("status"),
            name=m.group("name"),
        )
    return rows


def parse_list_all(text: str) -> dict[str, Entry]:
    """Every entry from `List <vol>: ALL`, keyed by **full path**.

    Path-aware on purpose. Keying by bare name loses entries: this very drive has `CLI` in both
    `SYS:` and `SYS:System`, and three such collisions turned 88 real entries into 85 apparent ones
    the first time this comparison was done by hand. A comparison that silently merges entries can
    report a match it has not actually checked.
    """
    entries: dict[str, Entry] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        header = _DIR_HEADER.match(line)
        if header:
            current = header.group("path")
            continue
        if not current or _TOTALS.match(line.strip()):
            continue
        m = _LIST_ENTRY.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        # "SYS:" is a volume root, so it already ends in the separator; deeper paths need one.
        sep = "" if current.endswith(":") else "/"
        entries[f"{current}{sep}{name}"] = Entry(
            size=m.group("size"), protect=m.group("protect"), when=m.group("when")
        )
    return entries


def parse_grand_total(text: str) -> Totals | None:
    """The `TOTAL:` line from `List ... ALL`, which AmigaDOS computes itself."""
    for line in text.splitlines():
        m = _GRAND_TOTAL.match(line.strip())
        if m:
            return Totals(
                files=int(m.group("files")),
                size=m.group("bytes"),
                dirs=int(m.group("dirs")),
                blocks=int(m.group("blocks")),
            )
    return None


#: The harness writes `--- <command>` before each command's output, so a run listing several
#: volumes produces several `List` sections in one log.
_COMMAND_MARK = re.compile(r"^--- (?P<command>.+)$", re.MULTILINE)
_LIST_COMMAND = re.compile(r"^List\s+(?P<volume>[^\s:]+):\s+ALL\s*$")


def split_list_sections(log: str) -> dict[str, str]:
    """Split a harness log into the output of each `List <volume>: ALL`, keyed by volume.

    Needed because a multi-partition run lists several volumes in one log, and feeding the whole
    thing to `parse_list_all` would merge them -- every volume's `Directory "X:"` headers landing
    in one namespace, which is only safe as long as no two volumes share a path.
    """
    marks = list(_COMMAND_MARK.finditer(log))
    sections: dict[str, str] = {}
    for index, mark in enumerate(marks):
        listed = _LIST_COMMAND.match(mark.group("command").strip())
        if not listed:
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(log)
        sections[listed.group("volume")] = log[mark.end():end]
    return sections


@dataclass
class Comparison:
    """Differences between two drives as AmigaDOS reported them."""

    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    differing: tuple[tuple[str, Entry, Entry], ...] = ()

    @property
    def is_identical(self) -> bool:
        return not self.missing and not self.extra and not self.differing

    @property
    def paths_differing(self) -> tuple[str, ...]:
        return tuple(path for path, _a, _b in self.differing)

    def describe(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append(f"missing from second: {list(self.missing)}")
        if self.extra:
            parts.append(f"only in second: {list(self.extra)}")
        for path, first, second in self.differing:
            parts.append(f"{path}: {first} != {second}")
        return "\n".join(parts) if parts else "identical"


def compare_listings(first: dict[str, Entry], second: dict[str, Entry]) -> Comparison:
    """Compare two `List ... ALL` results entry by entry.

    Nothing is filtered here. A caller that expects a known difference should assert on exactly
    which paths differ, so an unexpected one cannot hide behind an exclusion list.
    """
    return Comparison(
        missing=tuple(sorted(set(first) - set(second))),
        extra=tuple(sorted(set(second) - set(first))),
        differing=tuple(
            (path, first[path], second[path])
            for path in sorted(set(first) & set(second))
            if first[path] != second[path]
        ),
    )
