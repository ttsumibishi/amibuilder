"""Unit tests for the AmigaDOS output parsers.

No emulator needed: the samples below are **real output**, captured from AmigaOS 3.2.3 running
under FS-UAE on 2026-08-18 (Kickstart 47.96, Workbench 47.2). Keeping them as text means the
parsing is covered by the fast suite, so a parser bug does not need a 20-second boot to find.

It matters that these parsers are right. They are the comparison the boot test's verdict rests on,
and a parser that quietly drops entries would turn a real difference into a passing test.
"""

from __future__ import annotations

from emulator import amigados as A

# Real `Info` output, composed drive.
INFO = """
Mounted disks:
Unit      Size       Used       Free Full Errs   Status   Name
DF0       No disk present
DH0        39M       1762      80124   2%   0  Read/Write Workbench
DH1      4095M    4194304    4194303  50%   0  Read/Write RESULTS
RAM      9890K         11       9879   0%   0  Read/Write Ram Disk

Volumes available:
Ram Disk [Mounted]
RESULTS [Mounted]
Workbench [Mounted]
"""

# Real `List SYS: ALL` output, trimmed to three directories. `CLI` appears in two of them, which
# is the collision that path-aware parsing exists for.
LIST = """
Directory "SYS:" on Wednesday 19-Aug-26
CLI                            1180 ----rwed Yesterday 15:59:49
Installer                    107440 ----rwed Yesterday 15:59:50
S                               Dir ----rwed Today     09:35:32
System                          Dir ----rwed Yesterday 15:59:59
4 files - 111K bytes - 11 directories - 258 blocks used

Directory "SYS:System" on Wednesday 19-Aug-26
CLI                            1180 ----rwed Yesterday 15:59:59
Format                        14704 ----rwed Yesterday 15:59:59
2 files - 15K bytes - 34 blocks used

Directory "SYS:S" on Wednesday 19-Aug-26
Startup-Sequence               1088 ----rwed Today     09:35:32
1 file - 1K byte - 4 blocks used

TOTAL: 73 files - 797K bytes - 15 directories - 1742 blocks used
"""


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------


def test_info_finds_every_mounted_volume():
    rows = A.parse_info(INFO)
    assert set(rows) == {"DH0", "DH1", "RAM"}


def test_info_skips_an_empty_floppy_drive():
    """`DF0  No disk present` is not a volume and must not become a row with junk fields."""
    assert "DF0" not in A.parse_info(INFO)


def test_info_reads_the_numeric_fields():
    dh0 = A.parse_info(INFO)["DH0"]
    assert dh0.size == "39M"
    assert dh0.used == 1762
    assert dh0.free == 80124
    assert dh0.full == "2%"
    assert dh0.errs == 0


def test_info_keeps_a_status_containing_a_slash():
    assert A.parse_info(INFO)["DH0"].status == "Read/Write"


def test_info_keeps_a_volume_name_containing_a_space():
    """`Ram Disk` would be truncated to `Ram` by a naive final-field split."""
    assert A.parse_info(INFO)["RAM"].name == "Ram Disk"


def test_info_health_is_driven_by_the_error_count():
    rows = A.parse_info(INFO)
    assert rows["DH0"].is_healthy
    broken = A.parse_info(
        "DH9        39M       1762      80124   2%   7  Read/Write Broken\n"
    )["DH9"]
    assert not broken.is_healthy
    assert broken.errs == 7


def test_info_ignores_the_header_row():
    assert "Unit" not in A.parse_info(INFO)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_keys_entries_by_full_path():
    entries = A.parse_list_all(LIST)
    assert "SYS:Installer" in entries
    assert "SYS:System/Format" in entries
    assert "SYS:S/Startup-Sequence" in entries


def test_list_distinguishes_the_same_name_in_two_directories():
    """The bug this parser was written to avoid: `CLI` exists twice and must count twice."""
    entries = A.parse_list_all(LIST)
    assert "SYS:CLI" in entries
    assert "SYS:System/CLI" in entries
    assert entries["SYS:CLI"].when != entries["SYS:System/CLI"].when


def test_list_finds_every_entry():
    """4 + 2 + 1 across three directories."""
    assert len(A.parse_list_all(LIST)) == 7


def test_list_marks_directories():
    entries = A.parse_list_all(LIST)
    assert entries["SYS:S"].is_dir
    assert not entries["SYS:Installer"].is_dir


def test_list_reads_size_protection_and_date():
    entry = A.parse_list_all(LIST)["SYS:Installer"]
    assert entry.size == "107440"
    assert entry.protect == "----rwed"
    assert entry.when == "Yesterday 15:59:50"


def test_list_does_not_mistake_a_totals_line_for_an_entry():
    """`4 files - 111K bytes - ...` has the shape of a name followed by numbers."""
    paths = A.parse_list_all(LIST)
    assert not [p for p in paths if "bytes" in p or "blocks" in p]


def test_list_ignores_entries_before_any_directory_header():
    """Without a header there is no path to key on, so such a line must be dropped, not guessed."""
    assert A.parse_list_all("Stray                         12 ----rwed Today 10:00\n") == {}


def test_grand_total_is_read_from_the_total_line():
    totals = A.parse_grand_total(LIST)
    assert totals is not None
    assert (totals.files, totals.size, totals.dirs, totals.blocks) == (73, "797K", 15, 1742)


def test_grand_total_prefers_the_total_line_over_a_per_directory_one():
    """Per-directory totals share the format; only the `TOTAL:` line describes the whole drive."""
    assert A.parse_grand_total(LIST).files == 73


def test_grand_total_is_none_when_absent():
    assert A.parse_grand_total("Directory \"SYS:\" on Wednesday 19-Aug-26\n") is None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_identical_listings_compare_clean():
    entries = A.parse_list_all(LIST)
    result = A.compare_listings(entries, dict(entries))
    assert result.is_identical
    assert result.describe() == "identical"


def test_comparison_reports_a_missing_entry():
    first = A.parse_list_all(LIST)
    second = dict(first)
    del second["SYS:System/Format"]
    result = A.compare_listings(first, second)
    assert result.missing == ("SYS:System/Format",)
    assert not result.is_identical


def test_comparison_reports_an_extra_entry():
    first = A.parse_list_all(LIST)
    second = dict(first, **{"SYS:Intruder": A.Entry("10", "----rwed", "Today 10:00")})
    assert A.compare_listings(first, second).extra == ("SYS:Intruder",)


def test_comparison_reports_a_changed_entry_with_both_values():
    first = A.parse_list_all(LIST)
    second = dict(first)
    second["SYS:Installer"] = A.Entry("999", "----rwed", "Yesterday 15:59:50")
    result = A.compare_listings(first, second)
    assert result.paths_differing == ("SYS:Installer",)
    path, before, after = result.differing[0]
    assert before.size == "107440" and after.size == "999"
    assert "SYS:Installer" in result.describe()


def test_comparison_notices_a_protection_change():
    """Protection bits are part of the entry, so a changed `e` bit must not pass as identical."""
    first = A.parse_list_all(LIST)
    second = dict(first)
    second["SYS:Installer"] = A.Entry("107440", "----rw-d", "Yesterday 15:59:50")
    assert A.compare_listings(first, second).paths_differing == ("SYS:Installer",)


def test_comparison_filters_nothing_by_itself():
    """Exclusions are the caller's business, so an unexpected difference cannot hide in here."""
    first = A.parse_list_all(LIST)
    second = dict(first)
    second["SYS:S/Startup-Sequence"] = A.Entry("1088", "----rwed", "Today 09:36:33")
    assert A.compare_listings(first, second).paths_differing == ("SYS:S/Startup-Sequence",)
