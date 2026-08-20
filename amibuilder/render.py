"""Output formatting: human-readable text and `--json`.

Every command emits through here so that `--json` is uniform rather than bolted onto each
command separately. The JSON shape is part of the interface -- later phases and any
scripting around snapshots will read it -- so it carries explicit keys and never relies on
positional formatting.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

#: Binary unit suffixes. Amiga tooling conventionally writes "20Mi"; that spelling is kept
#: so output lines up with rdbtool and xdftool.
_UNITS = ("", "Ki", "Mi", "Gi", "Ti")


def human_bytes(n: int, precision: int = 1) -> str:
    """Format a byte count with binary units, e.g. 20Mi, 1.5Gi, 512."""
    if n < 1024:
        return str(n)
    size = float(n)
    units = _UNITS[1:]
    for index, unit in enumerate(units):
        size /= 1024.0
        last = index == len(units) - 1
        if size >= 1024.0 and not last:
            continue
        text = _fit_unit(size, precision)
        if text is None and not last:
            # Rounds to this unit's ceiling however it is written, so it belongs in the next one.
            continue
        return f"{text if text is not None else f'{size:.{precision}f}'}{unit}"
    return str(n)


def _fit_unit(size: float, precision: int) -> str | None:
    """Shortest faithful rendering of `size`, or None if it only rounds to the unit's ceiling.

    The ceiling case matters more than it looks. `1024Mi` is not a form anyone writes, and it reads
    as *exactly* one GiB -- so a partition that came up one cylinder short of the gigabyte it asked
    for would be reported as having got it, which is precisely the shortfall `init` exists to
    surface. Where a decimal can still tell the truth (1023.9Mi) it is used; where it cannot
    (1 GiB less one byte) the caller moves up a unit instead.
    """
    if size == int(size):
        return f"{size:.0f}"
    if size >= 100:
        # Three digits and up drop the decimal to stay compact, unless that lands on the ceiling.
        whole = f"{size:.0f}"
        if float(whole) < 1024.0:
            return whole
    decimal = f"{size:.{precision}f}"
    return None if float(decimal) >= 1024.0 else decimal


def parse_size(spec: str) -> int:
    """Parse a size such as 512, 20M, 4G, 100Mi, 1.5GiB into bytes.

    Accepts both the 'M'/'Mi'/'MiB' spellings and treats all of them as binary, because
    an Amiga context has no use for decimal megabytes and silently differing by 4.8%
    would be worse than rejecting the input.
    """
    s = spec.strip()
    if not s:
        raise ValueError("empty size")
    num = s.rstrip("iIbB")
    suffix = ""
    if num and num[-1].isalpha():
        suffix = num[-1].upper()
        num = num[:-1]
    try:
        value = float(num)
    except ValueError as e:
        raise ValueError(f"cannot parse size {spec!r}") from e
    if value < 0:
        raise ValueError(f"negative size {spec!r}")
    shift = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}.get(suffix)
    if shift is None:
        raise ValueError(f"unknown size suffix in {spec!r} (use K, M, G or T)")
    return int(value * (1024**shift))


@dataclass
class Table:
    """A simple column-aligned table.

    Alignment is computed from the data rather than fixed, because Amiga names run to 30
    characters (110 with long filenames) and a fixed width would either waste space or
    truncate.
    """

    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)
    #: 'l' or 'r' per column; defaults to left.
    align: list[str] = field(default_factory=list)
    indent: str = ""

    def add(self, *cells: Any) -> None:
        self.rows.append(["" if c is None else str(c) for c in cells])

    def render(self) -> list[str]:
        if not self.rows and not self.headers:
            return []
        cols = len(self.headers) if self.headers else max(len(r) for r in self.rows)
        widths = [0] * cols
        for row in ([self.headers] if self.headers else []) + self.rows:
            for i, cell in enumerate(row[:cols]):
                widths[i] = max(widths[i], len(cell))

        align = list(self.align) + ["l"] * (cols - len(self.align))

        def fmt(row: list[str]) -> str:
            out = []
            for i in range(cols):
                cell = row[i] if i < len(row) else ""
                out.append(cell.rjust(widths[i]) if align[i] == "r" else cell.ljust(widths[i]))
            return (self.indent + "  ".join(out)).rstrip()

        lines = []
        if self.headers:
            lines.append(fmt(self.headers))
            lines.append(self.indent + "  ".join("-" * w for w in widths))
        lines.extend(fmt(r) for r in self.rows)
        return lines


class Output:
    """Collects a command's result as both text lines and a JSON-able structure.

    Commands populate whichever they need; `emit` prints the right one. Keeping both in
    one object means a command cannot accidentally support `--json` for part of its
    output and not the rest.
    """

    def __init__(self, as_json: bool = False, stream: TextIO | None = None):
        self.as_json = as_json
        self.stream = stream or sys.stdout
        self._lines: list[str] = []
        self._data: Any = None

    # -- text ----------------------------------------------------------------
    def line(self, text: str = "") -> None:
        self._lines.append(text)

    def lines(self, texts: list[str]) -> None:
        self._lines.extend(texts)

    def table(self, table: Table) -> None:
        self._lines.extend(table.render())

    def field(self, label: str, value: Any, width: int = 18) -> None:
        """A `label: value` line, for `info`-style output."""
        self._lines.append(f"{label + ':':<{width}} {value}")

    def heading(self, text: str) -> None:
        if self._lines:
            self._lines.append("")
        self._lines.append(text)

    # -- json ----------------------------------------------------------------
    def data(self, value: Any) -> None:
        self._data = value

    # -- emit ----------------------------------------------------------------
    def emit(self) -> None:
        if self.as_json:
            payload = self._data if self._data is not None else {"output": self._lines}
            json.dump(payload, self.stream, indent=2, default=str)
            self.stream.write("\n")
        else:
            for line in self._lines:
                self.stream.write(line + "\n")

    # -- raw binary ----------------------------------------------------------
    def binary(self, data: bytes) -> None:
        """Write raw bytes to stdout, for `cat` of a binary file.

        Bypasses the text buffer entirely; writing image contents through the JSON path
        would corrupt them, so `cat --json` is rejected at the CLI instead.
        """
        buf = getattr(self.stream, "buffer", None)
        if buf is None:
            self.stream.write(data.decode("latin-1"))
        else:
            buf.write(data)


def hexdump(data: bytes, base: int = 0, width: int = 16) -> list[str]:
    """Classic offset / hex / ASCII dump.

    Identical runs are not collapsed: on a disk image a long run of identical bytes is
    usually meaningful (unallocated space, a wiped block) and hiding it would obscure
    exactly what the command exists to show.
    """
    out = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        # Split into two groups of 8 for readability.
        if width == 16 and len(chunk) > 8:
            hexpart = " ".join(f"{b:02x}" for b in chunk[:8]) + "  " + " ".join(
                f"{b:02x}" for b in chunk[8:]
            )
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{base + off:08x}  {hexpart:<{width * 3 + 1}} |{text}|")
    return out


def protect_str(bits: int) -> str:
    """Render AmigaDOS protection bits as HSPARWED.

    The low four bits are *inverted* on the Amiga: a set bit means the action is
    forbidden. amitools already accounts for that in get_protect_str(); this exists for
    rendering bits obtained from raw blocks.
    """
    # Bit order, high to low: h s p a r w e d
    names = "hsparwed"
    out = []
    for i, ch in enumerate(names):
        bit = 7 - i
        on = bool(bits & (1 << bit))
        if bit <= 3:  # rwed are active-low
            on = not on
        out.append(ch.upper() if bit >= 4 and on else (ch if on else "-"))
    return "".join(out)
