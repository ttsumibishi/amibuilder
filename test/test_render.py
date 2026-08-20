"""Formatting helpers.

`human_bytes` had no tests and appears in almost every command's output, so a rounding quirk in it
misreports every size the tool prints. These pin the boundary behaviour.
"""

from __future__ import annotations

import pytest

from amibuilder.render import human_bytes, parse_size

KI = 1024
MI = 1024 ** 2
GI = 1024 ** 3


@pytest.mark.parametrize("value,expected", [
    (0, "0"),
    (1, "1"),
    (512, "512"),
    (1023, "1023"),
    (KI, "1Ki"),
    (MI, "1Mi"),
    (GI, "1Gi"),
    (4 * GI, "4Gi"),
    (512 * MI, "512Mi"),
    (1023 * MI, "1023Mi"),
    (1536 * MI, "1.5Gi"),
])
def test_exact_and_ordinary_sizes(value, expected):
    assert human_bytes(value) == expected


def test_a_size_just_under_a_unit_does_not_round_up_to_it():
    """The bug this pins: 1 GiB minus a cylinder printed as "1024Mi", i.e. exactly one GiB.

    A partition reported that way looks like it got the size it asked for when it actually came up
    a cylinder short -- and the whole point of reporting cylinder rounding is that shortfall.
    """
    assert human_bytes(GI - 128 * KI) == "1023.9Mi"


def test_the_unit_ceiling_is_never_printed():
    """Nothing should ever render as "1024" of any unit; that value belongs in the next unit up."""
    for value in (GI - 1, GI - 128 * KI, MI - 1, MI - 512, KI * 1023 + 1023,
                  GI - KI, 4 * GI - 1, MI + MI - 1):
        rendered = human_bytes(value)
        assert not rendered.startswith("1024"), f"{value} rendered as {rendered}"


def test_a_value_too_close_to_the_ceiling_for_a_decimal_moves_up_a_unit():
    """One byte under a GiB cannot be told from a GiB in *any* Mi rendering, so it becomes Gi.

    The pair with the test above is the point: a decimal is used where it can still carry the
    difference, and the unit is promoted only where it cannot.
    """
    assert human_bytes(GI - 1) == "1.0Gi"
    assert human_bytes(GI - 128 * KI) == "1023.9Mi"


def test_an_exact_unit_does_not_gain_a_decimal():
    """The promotion path must not turn a clean 1Gi into 1.0Gi."""
    assert human_bytes(GI) == "1Gi"
    assert human_bytes(2 * GI) == "2Gi"


def test_values_under_a_hundred_keep_a_decimal():
    assert human_bytes(1536) == "1.5Ki"
    assert human_bytes(int(2.5 * MI)) == "2.5Mi"


def test_large_values_stay_compact():
    """Three-digit-and-up values drop the decimal, which is why the ceiling case needed care."""
    assert human_bytes(700 * MI) == "700Mi"
    assert human_bytes(int(700.4 * MI)) == "700Mi"


def test_round_trip_against_parse_size():
    """Anything printed as a whole unit should parse back to the same number of bytes."""
    for value in (KI, MI, GI, 4 * GI, 512 * MI):
        assert parse_size(human_bytes(value)) == value
