"""The `.uaem` sidecar format, in both directions.

This module is the single home for the metadata convention WinUAE and FS-UAE use for a
directory mounted as a hard drive, so that the writer (`layers.targets.write_directory`)
and the reader (`layers.hostdir.DirectoryVolume`) cannot drift apart. If the escaping or the
timestamp rendering disagreed between the two, a directory written by `compose --format dir`
would not read back identically with `snap create`, and a round trip is the whole point.

The format was taken from amitools' own `MetaInfoFSUAE` rather than guessed:

    PPPPPPPP YYYY-MM-DD HH:MM:SS.TT comment\\n

An 8-character protection string, a space, the timestamp with two-digit ticks, a space, then
the comment (which may be empty, leaving a trailing space before the newline). One sidecar per
entry named `<entry>.uaem` alongside it -- for directories as well as files -- and none for the
volume root.

**Timestamps are rendered and parsed straight from the wall-clock datetime, never through
amitools' converter.** amitools' epoch constant is built with `time.mktime`, so it carries the
reading host's January UTC offset and lands an hour out under DST (`KIP-FFS-NOTES.md` §5.7).
`timestamps.to_datetime`/`from_datetime` are pure subtraction against the Amiga epoch with no
timezone step, so a value written here and read back here is exact. That is the reason
`parse_uaem` exists rather than calling amitools -- parsing through amitools would reintroduce
precisely the drift the writer avoids.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .. import timestamps

UAEM_SUFFIX = ".uaem"
UAEM_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Characters that cannot appear in a host filename, or that cause trouble if they do. `/` and
#: `:` are already illegal in AmigaDOS names so should never arrive, but a hand-edited manifest
#: is a thing that happens and silently creating a path component would be worse than escaping.
_ESCAPE_ALWAYS = frozenset('/:\\"*?<>|')

#: Escaped so the transformation is reversible, matching the UAE `%XX` convention.
_ESCAPE_CHAR = "%"

#: A `%XX` escape: `%` followed by exactly two hex digits. `escape_name` only ever emits
#: lower-case, but a hand-edited name might use upper, so both are accepted when decoding.
_ESCAPE_RE = re.compile(r"%([0-9a-fA-F]{2})")

#: The eight AmigaDOS protection flag letters, plus '-' for an unset bit. Matched against the
#: first `.uaem` token so a hand-edited line with a mangled first field is rejected as such
#: rather than silently accepted and then failing much later at compose time.
_PROTECT_RE = re.compile(r"[-hsparwedHSPARWED]{8}")


class SidecarError(ValueError):
    """A `.uaem` line that does not parse. A subclass of ValueError so a caller that just
    wants "bad or good" can catch the built-in, while one that wants to be specific can catch
    this."""


def escape_name(name: str) -> str:
    """Make an Amiga filename safe to use on the host, UAE-style `%XX` escaping.

    Conservative on purpose: only characters that genuinely cannot be used, control characters,
    a trailing dot or space, and `%` itself so the mapping stays reversible.

    **Unverified:** that FS-UAE decodes `%XX` back on the Amiga side. WinUAE's filesystem
    emulation does, and FS-UAE shares that lineage, but I have not tested it. Since Amiga
    filenames essentially never contain these characters, callers are told when a name was
    escaped rather than having it happen silently.
    """
    out = []
    for char in name:
        if char == _ESCAPE_CHAR or char in _ESCAPE_ALWAYS or ord(char) < 0x20:
            out.append(f"%{ord(char):02x}")
        else:
            out.append(char)
    escaped = "".join(out)
    # A trailing dot or space is legal through the POSIX API but is trimmed or hidden by enough
    # tools that round-tripping it is not worth the risk.
    if escaped and escaped[-1] in " .":
        escaped = escaped[:-1] + f"%{ord(escaped[-1]):02x}"
    return escaped


def unescape_name(name: str) -> str:
    """Invert `escape_name`: turn `%XX` back into the character it stood for.

    The exact inverse of what the directory target writes, so a host tree produced by
    `compose --format dir` reads back with its original Amiga names. A bare `%` not followed by
    two hex digits is left alone -- it cannot occur in output this module produced (a literal
    `%` is written `%25`), so leaving it verbatim is the least surprising thing to do with a
    hand-made name.
    """
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), name)


def uaem_line(entry: Any) -> str:
    """Render one `.uaem` sidecar's contents for a `manifest.ManifestEntry`, newline included.

    A zero timestamp is written as `1978-01-01 00:00:00.00`, which is what the triple actually
    says. The format has no way to express "no datestamp", and inventing the current time would
    be worse than reporting the stored value.
    """
    secs, ticks = timestamps.from_triple(*entry.ts)
    when = timestamps.to_datetime(max(secs, 0)).strftime(UAEM_TS_FORMAT)
    return f"{entry.protect} {when}.{ticks:02d} {entry.comment}\n"


def parse_uaem(text: str) -> tuple[str, int, int, str]:
    """Parse a `.uaem` sidecar into `(protect_str, mod_secs, mod_ticks, comment)`.

    The inverse of `uaem_line`. Only the first line is read -- the format is one line per
    entry -- and the timestamp is taken back to Amiga-epoch seconds through
    `timestamps.from_datetime`, so no timezone conversion happens and a value round-trips
    exactly.

    Raises `SidecarError` for anything that is not a well-formed line, so the caller can fall
    back to the host file's own mtime and default protection rather than propagate a corrupt
    value. The comment is returned verbatim, including any interior spaces, because only the
    first three space-separated fields are fixed-shape.
    """
    line = text.split("\n", 1)[0]
    line = line.removesuffix("\r")
    # maxsplit=3: protect, date, time+ticks each contain no space, so the fourth field is the
    # whole comment even when it contains spaces of its own.
    parts = line.split(" ", 3)
    if len(parts) < 3:
        raise SidecarError(f"not a .uaem line (need protect, date and time): {line!r}")
    protect, date_field, time_field = parts[0], parts[1], parts[2]
    comment = parts[3] if len(parts) == 4 else ""

    if not _PROTECT_RE.fullmatch(protect):
        raise SidecarError(f"first field is not an 8-character protection string: {protect!r}")

    time_part, _, ticks_field = time_field.partition(".")
    try:
        when = dt.datetime.strptime(f"{date_field} {time_part}", UAEM_TS_FORMAT)
    except ValueError as e:
        raise SidecarError(f"unparseable timestamp: {date_field} {time_field!r}") from e

    ticks = int(ticks_field) if ticks_field.isdigit() else 0
    # from_datetime returns sub-second ticks from microseconds, which strptime never produces
    # here; the ticks come from the explicit `.TT` field instead.
    secs, _sub = timestamps.from_datetime(when)
    return protect, secs, ticks, comment


def read_sidecar(host_path: str) -> tuple[str, int, int, str] | None:
    """Read and parse `<host_path>.uaem`, or return None if it is absent.

    A present-but-corrupt sidecar raises `SidecarError`; an absent one is not an error, because
    a directory tree the user assembled by hand will have no sidecars at all and must still be
    capturable from the host files' own metadata.
    """
    try:
        with open(host_path + UAEM_SUFFIX, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    return parse_uaem(raw.decode("utf-8", errors="replace"))


__all__ = [
    "UAEM_SUFFIX",
    "UAEM_TS_FORMAT",
    "SidecarError",
    "escape_name",
    "parse_uaem",
    "read_sidecar",
    "uaem_line",
    "unescape_name",
]
