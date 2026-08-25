"""Amiga timestamp handling.

The behaviour under test is a deliberate divergence from amitools, so it is worth stating
plainly. amitools builds its epoch constant with `time.mktime`, which makes it depend on
the reading machine's January UTC offset. amibuilder renders straight from the on-disk
(days, mins, ticks) triple instead, which is what an Amiga does.

See amibuilder/timestamps.py for the measurement that established this.
"""

from __future__ import annotations

import datetime as dt

import pytest

from amibuilder import timestamps as T

# ---------------------------------------------------------------------------
# The triple is the ground truth
# ---------------------------------------------------------------------------


def test_epoch_is_1978():
    assert dt.datetime(1978, 1, 1) == T.AMIGA_EPOCH


def test_zero_triple_is_the_epoch():
    secs, ticks = T.from_triple(0, 0, 0)
    assert (secs, ticks) == (0, 0)
    assert T.to_datetime(secs) == dt.datetime(1978, 1, 1)


def test_from_triple_matches_amiga_field_semantics():
    # 1 day, 90 minutes, 125 ticks -> 1 day 01:30:02 with 25 ticks left over.
    secs, ticks = T.from_triple(1, 90, 125)
    assert secs == 86400 + 90 * 60 + 2
    assert ticks == 25
    assert T.to_datetime(secs) == dt.datetime(1978, 1, 2, 1, 30, 2)


def test_ticks_are_fiftieths_of_a_second():
    assert T.TICKS_PER_SECOND == 50
    assert T.from_triple(0, 0, 50) == (1, 0)
    assert T.from_triple(0, 0, 49) == (0, 49)


@pytest.mark.parametrize("days,mins,ticks", [(0, 0, 0), (1, 1, 1), (17760, 948, 2950),
                                             (10000, 1439, 2999)])
def test_triple_round_trips(days, mins, ticks):
    secs, sub = T.from_triple(days, mins, ticks)
    back = T.to_triple(secs, sub)
    # Re-deriving seconds is the invariant that matters; the tick split may normalise.
    assert T.from_triple(*back) == (secs, sub)


# ---------------------------------------------------------------------------
# The measured divergence from amitools
# ---------------------------------------------------------------------------


def test_the_reference_sample_renders_as_the_on_disk_value():
    """days=17760 mins=948 is 17.08.2026 15:48:59, not 16:48:59.

    This exact triple was produced by amitools on 2026-08-17 in US/Pacific while the wall
    clock read 16:48:59. amitools writes an hour adrift because its epoch constant carries
    January's offset, then displays 16:48:59 by re-applying the same error in reverse.
    amibuilder reports what the bytes say, which is what an Amiga would show.
    """
    secs, ticks = T.from_triple(17760, 948, 2950)
    assert T.format(secs, ticks) == "17.08.2026 15:48:59"
    assert T.iso(secs) == "2026-08-17T15:48:59"


def test_rendering_is_independent_of_the_host_timezone(monkeypatch):
    """The whole point: no tz lookup happens, so TZ cannot shift the answer."""
    import os
    import time

    secs, _ = T.from_triple(17760, 948, 2950)
    baseline = T.format(secs)
    for zone in ("UTC", "America/Los_Angeles", "Europe/Berlin", "Pacific/Kiritimati"):
        monkeypatch.setitem(os.environ, "TZ", zone)
        if hasattr(time, "tzset"):
            time.tzset()
        assert T.format(secs) == baseline
    monkeypatch.undo()
    if hasattr(time, "tzset"):
        time.tzset()


def test_utc_offset_constant_is_the_true_utc_one():
    """Provided for interop, and must not be amitools' mktime-derived value."""
    assert T.UNIX_TO_AMIGA_UTC == 252460800


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_zero_renders_as_the_amigados_placeholder():
    assert T.format(0) == "--.--.---- --:--:--"
    assert T.iso(0) == ""


def test_negative_is_treated_as_absent():
    assert T.format(-1) == "--.--.---- --:--:--"


def test_ticks_are_shown_only_on_request():
    secs, ticks = T.from_triple(100, 200, 375)
    assert "." not in T.format(secs, ticks).split(" ")[1]
    assert T.format(secs, ticks, show_ticks=True).endswith(f".{ticks:02d}")


def test_iso_has_no_timezone_designator():
    """Attaching one would invent information the format does not carry."""
    s = T.iso(T.from_triple(17760, 948, 0)[0])
    assert "+" not in s and not s.endswith("Z")


def test_short_form_switches_to_a_year_for_old_entries():
    recent = int((dt.datetime.now() - T.AMIGA_EPOCH).total_seconds())
    old = int((dt.datetime.now() - dt.timedelta(days=400) - T.AMIGA_EPOCH).total_seconds())
    assert ":" in T.short(recent)
    assert ":" not in T.short(old)


def test_short_form_of_absent_is_blank_but_aligned():
    assert T.short(0).strip() == "---"


# ---------------------------------------------------------------------------
# Agreement with amitools where it should agree
# ---------------------------------------------------------------------------


def test_seconds_arithmetic_matches_amitools_timestamp():
    """The triple-to-seconds step is shared; only the epoch handling differs."""
    from amitools.fs.TimeStamp import TimeStamp

    for days, mins, ticks in ((0, 0, 0), (1, 90, 125), (17760, 948, 2950)):
        ts = TimeStamp(days, mins, ticks)
        secs, sub = T.from_triple(days, mins, ticks)
        assert ts.get_secs() == secs
        assert ts.get_sub_secs() == sub
