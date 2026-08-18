"""Amiga timestamps, kept free of timezone contamination.

AmigaDOS stores a timestamp as three numbers -- days since 1978-01-01, minutes into the
day, and ticks (1/50 s) into the minute -- and does no timezone arithmetic at all. The
value is naive local wall clock, exactly as the machine's clock read.

amitools converts through Unix time using a constant it builds at import:

    amiga_epoch = time.mktime(time.strptime("01.01.1978 00:00:00", "%d.%m.%Y %H:%M:%S"))

`mktime` interprets that as *local* time, so the constant depends on the reading
machine's UTC offset on 1 January -- 252489600 in UTC-8, against 252460800 for true UTC.
Two consequences matter:

1. Writing is off by the difference between the January offset and the offset in force
   when the file is written. Measured on 2026-08-17 in US/Pacific: the wall clock read
   16:48:59, and the bytes written to disk decode to 15:48:59 -- an hour adrift, because
   January's -08:00 was applied during August's -07:00. amitools then displays 16:48:59
   by re-applying the same wrong offset in reverse, so it is self-consistent while
   disagreeing with the bytes.
2. An image written under one timezone and read under another shifts by the difference
   in January offsets.

amibuilder therefore renders straight from the triple and never round-trips through Unix
time. The number it prints is what an Amiga would print. That means `amibuilder ls -l`
can differ from `xdftool list` by the offset described above; the difference is amitools',
and this is the side worth being on.

This also independently justifies keeping timestamps out of the default layer-diff
comparison key (docs/KIP-FFS-LAYERS.md section 5): a key including them would report
spurious differences purely from where an image was written.
"""

from __future__ import annotations

import datetime as dt

#: AmigaDOS day zero.
AMIGA_EPOCH = dt.datetime(1978, 1, 1)

#: Ticks per second in an AmigaDOS timestamp.
TICKS_PER_SECOND = 50

SECONDS_PER_DAY = 24 * 60 * 60

#: Offset from the Unix epoch to the Amiga epoch, in UTC. Provided for interoperability
#: only; amibuilder does not use it for display.
UNIX_TO_AMIGA_UTC = int((AMIGA_EPOCH - dt.datetime(1970, 1, 1)).total_seconds())


def from_triple(days: int, mins: int, ticks: int) -> tuple[int, int]:
    """Convert an on-disk (days, mins, ticks) triple to (seconds, remainder_ticks)."""
    secs = days * SECONDS_PER_DAY + mins * 60 + ticks // TICKS_PER_SECOND
    return secs, ticks % TICKS_PER_SECOND


def to_triple(secs: int, sub_ticks: int = 0) -> tuple[int, int, int]:
    """Convert seconds-since-Amiga-epoch back to an on-disk triple."""
    days, rem = divmod(int(secs), SECONDS_PER_DAY)
    mins, s = divmod(rem, 60)
    return days, mins, s * TICKS_PER_SECOND + sub_ticks


def to_datetime(secs: int) -> dt.datetime:
    """Naive wall-clock datetime for seconds since the Amiga epoch."""
    return AMIGA_EPOCH + dt.timedelta(seconds=int(secs))


def format(secs: int, ticks: int = 0, *, show_ticks: bool = False) -> str:
    """Render as `DD.MM.YYYY HH:MM:SS`, matching the AmigaDOS field order.

    Returns the AmigaDOS empty-timestamp placeholder for zero, which is a real state:
    a freshly created directory can carry no timestamp at all.
    """
    if secs <= 0:
        return "--.--.---- --:--:--"
    out = to_datetime(secs).strftime("%d.%m.%Y %H:%M:%S")
    return f"{out}.{ticks:02d}" if show_ticks else out


def iso(secs: int) -> str:
    """ISO-8601 with no timezone designator, for `--json`.

    The absence of a designator is deliberate and meaningful: the value genuinely has no
    timezone, and attaching one would invent information.
    """
    if secs <= 0:
        return ""
    return to_datetime(secs).isoformat(timespec="seconds")


def short(secs: int) -> str:
    """`ls -l` style: day and month plus time, or the year when more than ~6 months old."""
    if secs <= 0:
        return "     ---     "
    when = to_datetime(secs)
    now = dt.datetime.now()
    if abs((now - when).days) > 180:
        return when.strftime("%d %b  %Y")
    return when.strftime("%d %b %H:%M")
