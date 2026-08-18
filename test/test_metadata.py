"""Amiga metadata round-trip: protection bits, comments, timestamps, volume identity.

A naive copy to a host directory loses all of it. amitools offers two carriers, and the
`.uaem` variant matches the WinUAE / FS-UAE directory-hard-drive convention, which means
an unpacked backup is directly mountable in an emulator.

See docs/KIP-FFS-NOTES.md section 5.2.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from helpers import images


def test_unpack_writes_an_xdfmeta_sidecar_file(populated_hdf, workdir):
    """Default metadata mode writes <outdir>.xdfmeta -- note: beside, not inside."""
    out = workdir / "unpacked"
    images.xdftool(populated_hdf, "open", "+", "unpack", str(out))

    meta = Path(str(out) + ".xdfmeta")
    assert meta.exists(), "expected the metadata file next to the output directory"

    text = meta.read_text()
    # First line describes the volume: name, DosType and three timestamps.
    first = text.splitlines()[0]
    assert first.startswith("Pop:DOS3"), f"unexpected volume line: {first!r}"
    assert "S/Startup-Sequence:" in text


def test_xdfmeta_records_protection_and_comment(workdir):
    """protection bits and comments must survive into the metadata file."""
    path = images.make_plain_hdf(str(workdir / "m.hdf"), size="10Mi", volume="Meta")
    images.write_files(path, {"thing": b"data"})
    images.xdftool(path, "open", "+", "protect", "thing", "rwed+s")

    out = workdir / "up"
    images.xdftool(path, "open", "+", "unpack", str(out))
    text = Path(str(out) + ".xdfmeta").read_text()

    line = [ln for ln in text.splitlines() if ln.startswith("thing:")][0]
    flags = line.split(":", 1)[1].split(",")[0]
    assert "s" in flags, f"script bit missing from {line!r}"


def test_uaem_sidecars_match_the_uae_convention(populated_hdf, workdir):
    """`unpack <dir> fsuae` writes per-entry .uaem files in WinUAE/FS-UAE format.

    The format is '<8 protection chars> <YYYY-MM-DD HH:MM:SS.tt> <comment>', which is
    what makes an unpacked directory usable as an emulator directory hard drive.
    """
    out = workdir / "uaem"
    images.xdftool(populated_hdf, "open", "+", "unpack", str(out), "fsuae")

    sidecars = list(out.rglob("*.uaem"))
    assert sidecars, "expected .uaem sidecar files"

    pat = re.compile(r"^[-a-z]{8} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{2}")
    for sc in sidecars:
        first = sc.read_text().splitlines()[0]
        assert pat.match(first), f"{sc.name} not in .uaem format: {first!r}"

    # Every real entry should have a sidecar.
    reals = [p for p in out.rglob("*") if p.suffix != ".uaem"]
    assert len(sidecars) >= len([p for p in reals if p.is_file()])


def test_protection_string_is_eight_characters_hsparwed(workdir):
    """The canonical display form is 8 chars; RWED bits read as granted when clear."""
    path = images.make_plain_hdf(str(workdir / "p.hdf"), size="10Mi", volume="P")
    images.write_files(path, {"f": b"x"})

    out = workdir / "up"
    images.xdftool(path, "open", "+", "unpack", str(out), "fsuae")
    line = (out / "f.uaem").read_text().splitlines()[0]

    flags = line.split(" ")[0]
    assert len(flags) == 8, f"expected 8 protection characters, got {flags!r}"
    assert flags.endswith("rwed"), (
        "a normal file shows rwed because the deny bits are clear -- the RWED bits are "
        "inverted relative to intuition"
    )


def test_full_round_trip_preserves_content_and_identity(populated_hdf, workdir):
    """unpack then pack must restore volume name, DosType, flags and bytes."""
    out = workdir / "rt"
    images.xdftool(populated_hdf, "open", "+", "unpack", str(out))

    rebuilt = str(workdir / "rebuilt.hdf")
    images.xdftool(rebuilt, "create", "size=20Mi", "+", "pack", str(out))

    listing = images.xdftool(rebuilt, "open", "+", "list").output
    assert "Pop" in listing, "volume name should be restored from the metadata file"
    assert "DOS3" in listing, "DosType should be restored"
    assert "Startup-Sequence" in listing

    extracted = str(workdir / "ss.txt")
    images.xdftool(rebuilt, "open", "+", "read", "S/Startup-Sequence", extracted)
    assert Path(extracted).read_bytes() == b'Echo "hello"\n'

    assert images.scan_is_ok(rebuilt)


@pytest.mark.slow
def test_round_trip_of_a_large_tree_is_byte_identical(workdir):
    """The highest-value cheap test: many files out and back with no drift."""
    tree, count, total = images.workbench_like_tree(str(workdir / "tree"))
    assert count > 100

    path = str(workdir / "big.hdf")
    images.xdftool(path, "create", "size=200Mi", "+", "format", "Big", "ffs+intl",
                   "+", "pack", tree)

    out = workdir / "back"
    images.xdftool(path, "open", "+", "unpack", str(out))

    import filecmp

    mismatches: list[str] = []
    for root, _dirs, files in os.walk(tree):
        for name in files:
            src = Path(root) / name
            rel = src.relative_to(tree)
            dst = out / rel
            if not dst.exists():
                mismatches.append(f"missing: {rel}")
            elif not filecmp.cmp(src, dst, shallow=False):
                mismatches.append(f"differs: {rel}")

    assert not mismatches, f"{len(mismatches)} problems, first few: {mismatches[:5]}"
    assert images.scan_is_ok(path)
