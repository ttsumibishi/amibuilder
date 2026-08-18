"""Manifest entries and the JSONL manifest format.

The two properties worth defending here are that the byte form is canonical -- because a
layer ID is the hash of its manifest, so two identical captures must produce identical
bytes -- and that the comparison key ignores timestamps, which is what keeps a diff
reviewable.
"""

from __future__ import annotations

import io
import json

import pytest

from amibuilder import timestamps
from amibuilder.errors import ImageError, UsageError
from amibuilder.layers import manifest as M
from amibuilder.volume import Entry

HASH_A = "a" * 64
HASH_B = "b" * 64


def file_entry(path="Workbench:C/List", blob=HASH_A, **kw):
    return M.ManifestEntry(path=path, kind=M.FILE, blob=blob, size=kw.pop("size", 10), **kw)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,volume,rel",
    [
        ("Workbench:S/Startup-Sequence", "Workbench", "S/Startup-Sequence"),
        ("Workbench:", "Workbench", ""),
        ("Work:Games", "Work", "Games"),
        ("My Volume:a/b/c", "My Volume", "a/b/c"),
        # A leading slash after the colon is tolerated and normalised away, because
        # AmigaDOS writes both 'Work:Games' and 'Work:/Games' in practice.
        ("Work:/Games", "Work", "Games"),
    ],
)
def test_split_path(path, volume, rel):
    assert M.split_path(path) == (volume, rel)


@pytest.mark.parametrize("bad", ["S/Startup-Sequence", ":orphan", "Work:a:b"])
def test_split_path_rejects_malformed(bad):
    with pytest.raises(UsageError):
        M.split_path(bad)


def test_join_path_round_trips():
    assert M.join_path("Workbench", "S/Startup-Sequence") == "Workbench:S/Startup-Sequence"
    assert M.join_path("Workbench", "") == "Workbench:"


def test_fold_is_case_insensitive():
    assert M.fold("Workbench:S/Startup-Sequence") == M.fold("workbench:s/startup-sequence")


# ---------------------------------------------------------------------------
# Entry validation
# ---------------------------------------------------------------------------


def test_file_requires_blob():
    with pytest.raises(ImageError, match="without a blob"):
        M.ManifestEntry(path="Work:x", kind=M.FILE)


def test_directory_must_not_carry_blob():
    with pytest.raises(ImageError, match="must not carry a blob"):
        M.ManifestEntry(path="Work:x", kind=M.DIR, blob=HASH_A)


def test_link_requires_target():
    with pytest.raises(ImageError, match="without a link target"):
        M.ManifestEntry(path="Work:x", kind=M.HARDLINK)


def test_non_link_must_not_carry_target():
    with pytest.raises(ImageError, match="must not carry a link target"):
        M.ManifestEntry(path="Work:x", kind=M.DIR, link_target="Work:y")


def test_unknown_kind_rejected():
    with pytest.raises(ImageError, match="unknown manifest entry kind"):
        M.ManifestEntry(path="Work:x", kind="q")


def test_path_must_be_volume_qualified():
    with pytest.raises(UsageError):
        M.ManifestEntry(path="no-volume-here", kind=M.DIR)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_round_trip_preserves_every_field():
    original = M.ManifestEntry(
        path="Workbench:S/Startup-Sequence",
        kind=M.FILE,
        blob=HASH_A,
        size=1234,
        protect="h--prwed",
        ts=(17389, 587, 12),
        comment="boot script",
    )
    assert M.ManifestEntry.from_line(original.to_line()) == original


def test_whiteout_line_is_minimal():
    """A deletion carries no metadata, so its line should be two fields and nothing else."""
    line = M.whiteout("Workbench:Devs/old.device").to_line()
    assert json.loads(line) == {"p": "Workbench:Devs/old.device", "t": "w"}


def test_defaults_are_omitted_then_restored():
    entry = M.ManifestEntry(path="Workbench:Libs", kind=M.DIR)
    parsed = json.loads(entry.to_line())
    assert parsed == {"p": "Workbench:Libs", "t": "d"}
    assert M.ManifestEntry.from_line(entry.to_line()) == entry


def test_line_is_ascii_escaped_for_stability():
    """Non-ASCII names must not vary with locale, since manifest bytes are hashed."""
    line = M.ManifestEntry(path="Work:Sm\xf6rg\xe5s", kind=M.DIR).to_line()
    assert line.isascii()
    assert M.ManifestEntry.from_line(line).path == "Work:Sm\xf6rg\xe5s"


def test_keys_are_sorted_so_bytes_are_canonical():
    line = file_entry(comment="c", ts=(1, 2, 3)).to_line()
    keys = list(json.loads(line, object_pairs_hook=lambda kv: [k for k, _ in kv]))
    assert keys == sorted(keys)


def test_bad_json_reports_the_line():
    with pytest.raises(ImageError, match="not valid JSON"):
        M.ManifestEntry.from_line("{not json")


def test_non_object_line_rejected():
    with pytest.raises(ImageError, match="must be a JSON object"):
        M.ManifestEntry.from_line("[1, 2, 3]")


def test_missing_required_field_named_in_error():
    with pytest.raises(ImageError, match="missing required field 't'"):
        M.ManifestEntry.from_line('{"p":"Work:x"}')


def test_malformed_timestamp_rejected():
    with pytest.raises(ImageError, match=r"\[days, mins, ticks\]"):
        M.ManifestEntry.from_line('{"p":"Work:x","t":"d","ts":[1,2]}')


# ---------------------------------------------------------------------------
# Comparison key -- the decision that keeps diffs small
# ---------------------------------------------------------------------------


def test_timestamp_alone_is_not_a_difference():
    a = file_entry(ts=(100, 0, 0))
    b = file_entry(ts=(999, 30, 25))
    assert a.compare_key() == b.compare_key()


def test_timestamps_significant_opts_in():
    a = file_entry(ts=(100, 0, 0))
    b = file_entry(ts=(999, 30, 25))
    assert a.compare_key(timestamps_significant=True) != b.compare_key(
        timestamps_significant=True
    )


@pytest.mark.parametrize(
    "field,value",
    [("blob", HASH_B), ("protect", "----rw-d"), ("comment", "changed")],
)
def test_content_protection_and_comment_are_differences(field, value):
    a = file_entry()
    b = M.replace(a, **{field: value})
    assert a.compare_key() != b.compare_key()


def test_kind_change_is_a_difference():
    """A path that was a file and is now a directory must not look unchanged."""
    a = file_entry(path="Work:thing")
    b = M.ManifestEntry(path="Work:thing", kind=M.DIR)
    assert a.compare_key() != b.compare_key()


def test_link_target_change_is_a_difference():
    a = M.ManifestEntry(path="Work:l", kind=M.SOFTLINK, link_target="Work:a")
    b = M.replace(a, link_target="Work:b")
    assert a.compare_key() != b.compare_key()


# ---------------------------------------------------------------------------
# Whole manifests
# ---------------------------------------------------------------------------


def test_entries_are_written_sorted_regardless_of_input_order():
    entries = [
        M.ManifestEntry(path="Work:zebra", kind=M.DIR),
        M.ManifestEntry(path="Work:alpha", kind=M.DIR),
        M.ManifestEntry(path="Work:Beta", kind=M.DIR),
    ]
    paths = [e.path for e in M.read(io.BytesIO(M.canonical_bytes(entries)))]
    assert paths == ["Work:alpha", "Work:Beta", "Work:zebra"]


def test_canonical_bytes_are_order_independent():
    """Two captures of the same content must hash identically."""
    a = [M.ManifestEntry(path="Work:a", kind=M.DIR), file_entry(path="Work:b")]
    assert M.canonical_bytes(a) == M.canonical_bytes(list(reversed(a)))


def test_write_and_read_round_trip(tmp_path):
    entries = [
        file_entry(path="Workbench:C/List", ts=(17389, 587, 12)),
        M.ManifestEntry(path="Workbench:Libs", kind=M.DIR),
        M.whiteout("Workbench:Devs/old.device"),
    ]
    target = str(tmp_path / "manifest.jsonl")
    assert M.save(target, entries) == 3
    assert M.load(target) == M.sort_entries(entries)


def test_read_skips_blank_lines():
    data = b'{"p":"Work:a","t":"d"}\n\n{"p":"Work:b","t":"d"}\n'
    assert len(list(M.read(io.BytesIO(data)))) == 2


def test_read_reports_the_offending_line_number():
    data = b'{"p":"Work:a","t":"d"}\n{"p":"Work:b"}\n'
    with pytest.raises(ImageError, match="manifest line 2"):
        list(M.read(io.BytesIO(data)))


def test_read_accepts_text_streams():
    """Convenient when a manifest arrives from a diff or a pipe rather than a file."""
    entries = list(M.read(io.StringIO('{"p":"Work:a","t":"d"}\n')))
    assert entries[0].path == "Work:a"


def test_index_rejects_case_colliding_paths():
    """FFS cannot hold both, so a manifest with both was built wrongly."""
    entries = [
        M.ManifestEntry(path="Work:Thing", kind=M.DIR),
        M.ManifestEntry(path="Work:thing", kind=M.DIR),
    ]
    with pytest.raises(ImageError, match="same path"):
        M.index(entries)


def test_index_keys_on_folded_path():
    entries = [M.ManifestEntry(path="Work:Thing", kind=M.DIR)]
    assert "work:thing" in M.index(entries)


def test_total_size_counts_only_files():
    entries = [
        file_entry(path="Work:a", size=100),
        file_entry(path="Work:b", blob=HASH_B, size=250),
        M.ManifestEntry(path="Work:d", kind=M.DIR),
        M.whiteout("Work:gone"),
    ]
    assert M.total_size(entries) == 350


def test_counts_reports_every_kind():
    entries = [file_entry(path="Work:a"), M.ManifestEntry(path="Work:d", kind=M.DIR)]
    got = M.counts(entries)
    assert got[M.FILE] == 1 and got[M.DIR] == 1 and got[M.WHITEOUT] == 0


# ---------------------------------------------------------------------------
# Building from a mounted volume
# ---------------------------------------------------------------------------


def test_from_volume_entry_qualifies_the_path():
    entry = Entry(name="List", path="C/List", is_dir=False, size=42)
    built = M.ManifestEntry.from_volume_entry("Workbench", entry, blob=HASH_A)
    assert built.path == "Workbench:C/List"
    assert built.kind == M.FILE
    assert built.size == 42


def test_from_volume_entry_maps_directories():
    entry = Entry(name="Libs", path="Libs", is_dir=True, size=0)
    built = M.ManifestEntry.from_volume_entry("Workbench", entry)
    assert built.kind == M.DIR and built.blob is None


@pytest.mark.parametrize("link_kind,expected", [("hard", M.HARDLINK), ("soft", M.SOFTLINK)])
def test_from_volume_entry_maps_links(link_kind, expected):
    entry = Entry(name="l", path="l", is_dir=False, link_kind=link_kind)
    built = M.ManifestEntry.from_volume_entry("Work", entry, link_target="Work:target")
    assert built.kind == expected and built.link_target == "Work:target"


def test_from_volume_entry_preserves_the_timestamp_triple_exactly():
    """The whole point of storing the triple: a round trip must not drift."""
    days, mins, ticks = 17389, 587, 37
    secs, sub = timestamps.from_triple(days, mins, ticks)
    entry = Entry(name="f", path="f", is_dir=False, mod_secs=secs, mod_ticks=sub)
    built = M.ManifestEntry.from_volume_entry("Work", entry, blob=HASH_A)
    assert built.ts == (days, mins, ticks)


def test_from_volume_entry_carries_protection_and_comment():
    entry = Entry(
        name="f", path="f", is_dir=False, protect_str="hs--rwed", comment="a note"
    )
    built = M.ManifestEntry.from_volume_entry("Work", entry, blob=HASH_A)
    assert built.protect == "hs--rwed" and built.comment == "a note"
