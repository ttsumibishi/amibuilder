"""Source specification parsing.

Addressing is the one piece of amibuilder that every command depends on, and a
misparse points an operation at the wrong partition -- or, on a card, at the wrong MBR
slot. These tests are deliberately exhaustive about the ambiguous cases.
"""

from __future__ import annotations

import pytest

from amibuilder.addressing import parse
from amibuilder.errors import AddressError


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "card.hdf"
    p.write_bytes(b"\x00" * 1024)
    return str(p)


# ---------------------------------------------------------------------------
# Plain paths
# ---------------------------------------------------------------------------


def test_bare_path_has_no_selector(img):
    a = parse(img)
    assert a.path == img
    assert a.partition is None
    assert a.mbr_slot is None
    assert not a.is_directory
    assert not a.is_device


def test_directory_is_recognised(tmp_path):
    a = parse(str(tmp_path))
    assert a.is_directory
    assert a.partition is None


def test_missing_path_is_an_error(tmp_path):
    with pytest.raises(AddressError, match="no such file"):
        parse(str(tmp_path / "nope.hdf"))


def test_empty_spec_is_an_error():
    with pytest.raises(AddressError, match="empty"):
        parse("")


# ---------------------------------------------------------------------------
# Partition selectors
# ---------------------------------------------------------------------------


def test_numeric_selector_stays_an_int(img):
    a = parse(f"{img}:0")
    assert a.partition == 0
    assert isinstance(a.partition, int)


def test_name_selector_stays_a_str(img):
    a = parse(f"{img}:DH0")
    assert a.partition == "DH0"
    assert isinstance(a.partition, str)


def test_numeric_and_name_selectors_are_distinguishable(img):
    """A volume literally named "0" is legal, if unwise.

    Collapsing int and str here would open the wrong volume, so the distinction is
    preserved all the way to resolution.
    """
    assert parse(f"{img}:0").partition == 0
    assert parse(f"{img}:zero").partition == "zero"


def test_empty_selector_is_an_error(img):
    with pytest.raises(AddressError, match="empty selector"):
        parse(f"{img}:")


def test_selector_on_a_directory_is_an_error(tmp_path):
    with pytest.raises(AddressError, match="host directory"):
        parse(f"{tmp_path}:0")


# ---------------------------------------------------------------------------
# MBR selectors
# ---------------------------------------------------------------------------


def test_mbr_selector(img):
    a = parse(f"{img}:0x76:1")
    assert a.mbr_type == 0x76
    assert a.mbr_slot == 1
    assert a.partition is None
    assert a.has_mbr_selector


def test_mbr_selector_with_partition(img):
    a = parse(f"{img}:0x76:1:2")
    assert (a.mbr_type, a.mbr_slot, a.partition) == (0x76, 1, 2)


def test_mbr_selector_with_named_partition(img):
    a = parse(f"{img}:0x76:1:Work")
    assert (a.mbr_slot, a.partition) == (1, "Work")


def test_mbr_slot_out_of_range(img):
    with pytest.raises(AddressError, match="4 primary"):
        parse(f"{img}:0x76:4")


def test_mbr_type_is_case_insensitive(img):
    assert parse(f"{img}:0X76:1").mbr_type == 0x76


def test_too_many_components_after_mbr_slot(img):
    with pytest.raises(AddressError, match="too many"):
        parse(f"{img}:0x76:1:2:3")


def test_unparseable_multipart_selector(img):
    with pytest.raises(AddressError, match="could not parse"):
        parse(f"{img}:foo:bar")


# ---------------------------------------------------------------------------
# The awkward cases
# ---------------------------------------------------------------------------


def test_a_filename_containing_a_colon_wins_over_a_selector(tmp_path):
    """macOS permits ':' in filenames, so the longest existing path must win.

    Parsing greedily from the right would treat `weird:name.hdf` as file `weird` with
    selector `name.hdf` and report a confusing error about a missing partition.
    """
    odd = tmp_path / "weird:name.hdf"
    odd.write_bytes(b"\x00" * 512)
    a = parse(str(odd))
    assert a.path == str(odd)
    assert a.partition is None


def test_a_colon_filename_can_still_take_a_selector(tmp_path):
    odd = tmp_path / "weird:name.hdf"
    odd.write_bytes(b"\x00" * 512)
    a = parse(f"{odd}:2")
    assert a.path == str(odd)
    assert a.partition == 2


def test_a_path_in_the_selector_is_refused_not_swallowed(img):
    """`card.hdf:Work/Utils` used to parse as a partition *named* `Work/Utils`.

    That failed several layers later as a missing partition, which sends the reader looking
    at their RDB rather than at their command line. It is the spelling people reach for --
    `commands/inject.py`'s own docstring writes `card.hdf:Work/Games` -- so it earns a real
    message. Rejected deliberately, not for want of an unambiguous parse; see the plan's
    Phase 6 ergonomics item 3.
    """
    with pytest.raises(AddressError) as exc:
        parse(f"{img}:Work/Utils")
    assert "selects a partition" in str(exc.value)
    # The suggestion has to be the *correct* command, not a restatement of the mistake.
    assert f"'{img}:Work Utils'" in str(exc.value)

    # Split at the FIRST slash: everything after it is one path. Splitting at the last would
    # suggest `card.hdf:Work/Utils Patches`, which is the same mistake with a comma moved.
    with pytest.raises(AddressError) as exc:
        parse(f"{img}:Work/Utils/Patches")
    assert f"'{img}:Work Utils/Patches'" in str(exc.value)


def test_every_selector_shape_reports_a_path_the_same_way(img):
    """The whole point of the fix: one mistake, one message.

    Before, a slash was swallowed into a partition name for a plain or `:name` selector but
    refused as an unparseable selector after an MBR slot -- the same typo reported two
    different ways depending on a detail the user was not thinking about.
    """
    for sel in ("Work/Utils", "Work/Utils/Patches", "0/Utils", "/Utils",
                "0x76:1/Utils", "0x76:1:2/Utils", "My Work/Utils"):
        with pytest.raises(AddressError, match="selects a partition"):
            parse(f"{img}:{sel}")


def test_the_suggestion_drops_the_colon_when_no_partition_was_named(img):
    """`image:/Path` names no partition, so the fix is the bare image plus the path."""
    with pytest.raises(AddressError) as exc:
        parse(f"{img}:/Utils")
    assert f"'{img} Utils'" in str(exc.value)


def test_a_trailing_slash_suggests_the_spec_without_it(img):
    with pytest.raises(AddressError) as exc:
        parse(f"{img}:Work/")
    assert f"'{img}:Work'" in str(exc.value)


def test_the_suggestion_is_clean_when_the_path_has_stray_slashes(img):
    """The suggestion exists to be copy-pasted, so stray slashes must not survive into it.

    `Work/` alone cannot prove this -- there is nothing after the slash to trim -- so both
    the trailing and the doubled case are needed to keep the trim honest.
    """
    with pytest.raises(AddressError) as exc:
        parse(f"{img}:Work/Utils/")
    assert f"'{img}:Work Utils'" in str(exc.value)

    with pytest.raises(AddressError) as exc:
        parse(f"{img}://Utils")
    assert f"'{img} Utils'" in str(exc.value)

    with pytest.raises(AddressError) as exc:
        parse(f"{img}:Work//Utils")
    assert f"'{img}:Work Utils'" in str(exc.value)


def test_a_slash_in_the_image_path_is_not_mistaken_for_a_path_selector(tmp_path):
    """The image's own slashes must stay out of it -- they are in the path portion.

    This is why the rule is safe at all: `parse` isolates the path by splitting at ':' and
    checking the filesystem *before* the selector is ever examined.
    """
    nested = tmp_path / "sub" / "dir"
    nested.mkdir(parents=True)
    img = nested / "card2.hdf"
    img.write_bytes(b"\x00" * 512)
    assert parse(f"{img}:Work").partition == "Work"
    assert parse(str(img)).partition is None


def test_a_bare_device_path_is_all_slashes_and_still_parses():
    """`_parse_selector` is only reached when a ':' was split off, so a device is untouched."""
    assert parse("/dev/rdisk99").is_device
    assert parse("/dev/rdisk99:0x76:1").mbr_slot == 1


def test_error_names_the_likely_image_path(tmp_path):
    with pytest.raises(AddressError, match="tried"):
        parse(f"{tmp_path / 'ghost.hdf'}:0")


def test_device_paths_are_recognised_without_existing():
    """A device that is busy or needs privilege must still parse as a device.

    Otherwise the guard rails never engage and the user gets "no such file" instead of
    an explanation.
    """
    a = parse("/dev/rdisk99")
    assert a.is_device
    assert a.path == "/dev/rdisk99"


def test_device_with_mbr_selector():
    a = parse("/dev/rdisk99:0x76:1")
    assert a.is_device and a.mbr_slot == 1


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_describe_round_trips_the_interesting_parts(img):
    assert parse(f"{img}:0").describe() == f"{img}:0"
    assert parse(f"{img}:0x76:1").describe() == f"{img}:0x76:1"
    assert parse(f"{img}:0x76:1:2").describe() == f"{img}:0x76:1:2"


def test_with_partition_preserves_everything_else(img):
    a = parse(f"{img}:0x76:1")
    b = a.with_partition(3)
    assert (b.mbr_type, b.mbr_slot, b.partition) == (0x76, 1, 3)
    assert b.path == a.path


@pytest.mark.parametrize("name", ["d.adf", "d.adz", "d.adf.gz", "D.ADF"])
def test_adf_extensions_are_detected(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 512)
    assert parse(str(p)).looks_like_adf


def test_hdf_is_not_an_adf(img):
    assert not parse(img).looks_like_adf
