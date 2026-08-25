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


# Real `List Work: ALL` output from a three-partition drive. This is the sample that exposed two
# parser bugs: AmigaDOS prints `empty` rather than `0` for a zero-length file, and a directory
# holding only an empty file gets a totals line with no `bytes` clause at all.
WORK_LIST = """
Directory "Work:" on Wednesday 19-Aug-26
Games                           Dir ----rwed Today     11:27:42
Docs                            Dir ----rwed Today     11:27:42
empty-file                    empty ----rwed Today     11:27:43
1 file - 2 directories - 6 blocks used
Directory "Work:Games" on Wednesday 19-Aug-26
Readme                           27 h------- Today     11:27:42
Lemmings                        Dir ----rwed Today     11:27:42
1 file - 27 bytes - 1 directory - 4 blocks used
Directory "Work:Docs/Deep" on Wednesday 19-Aug-26
Deeper                          Dir ----rwed Today     11:27:43
1 directory - 2 blocks used
TOTAL: 5 files - 5K bytes - 5 directories - 31 blocks used
"""


def test_an_empty_file_is_not_dropped():
    """The bug: `empty` in the size column did not match a digits-only pattern.

    Silently losing empty files would let a composition bug that dropped every one of them pass
    a comparison unnoticed, which is exactly the failure this whole test file guards against.
    """
    entries = A.parse_list_all(WORK_LIST)
    assert "Work:empty-file" in entries
    assert entries["Work:empty-file"].is_empty_file
    assert not entries["Work:empty-file"].is_dir


def test_a_directory_holding_only_an_empty_file_is_counted_correctly():
    """`1 file - 2 directories - 6 blocks used` has no bytes clause and must not become an entry."""
    entries = A.parse_list_all(WORK_LIST)
    assert len(entries) == 6, sorted(entries)
    assert not [p for p in entries if "blocks" in p or "director" in p]


def test_a_totals_line_without_a_bytes_clause_is_recognised():
    assert A.parse_list_all("Directory \"X:\" on Today\n1 directory - 2 blocks used\n") == {}


def test_grand_total_tolerates_a_missing_bytes_clause():
    """A volume of only empty files reports no byte count, which must not lose the other figures."""
    totals = A.parse_grand_total(
        'Directory "X:" on Today\nTOTAL: 2 files - 1 directory - 4 blocks used\n'
    )
    assert totals is not None
    assert (totals.files, totals.size, totals.dirs, totals.blocks) == (2, None, 1, 4)


def test_protection_variety_is_visible_across_directories():
    """`h-------` on one file and `----rwed` on others: the variety the boot test relies on."""
    entries = A.parse_list_all(WORK_LIST)
    assert entries["Work:Games/Readme"].protect == "h-------"
    assert len({e.protect for e in entries.values()}) > 1


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
# Splitting a multi-volume log
# ---------------------------------------------------------------------------

MULTI_LOG = """AMIBUILDER-BEGIN
--- Version
Kickstart 47.96, Workbench 47.2
--- Info
DH0        29M       1758      59680   3%   0  Read/Write Workbench
--- List SYS: ALL
Directory "SYS:" on Wednesday 19-Aug-26
CLI                            1180 ----rwed Today     11:27:35
TOTAL: 1 file - 1K bytes - 0 directories - 4 blocks used
--- List Work: ALL
Directory "Work:" on Wednesday 19-Aug-26
empty-file                    empty ----rwed Today     11:27:43
TOTAL: 1 file - 0 directories - 2 blocks used
--- List Saves: ALL
Directory "Saves:" on Wednesday 19-Aug-26
index                            10 hs------ Today     11:27:44
TOTAL: 1 file - 10 bytes - 0 directories - 2 blocks used
AMIBUILDER-END
"""


def test_split_finds_every_listed_volume():
    assert set(A.split_list_sections(MULTI_LOG)) == {"SYS", "Work", "Saves"}


def test_split_ignores_commands_that_are_not_listings():
    """`Version` and `Info` are in the same log and must not become sections."""
    assert "Version" not in A.split_list_sections(MULTI_LOG)
    assert "Info" not in A.split_list_sections(MULTI_LOG)


def test_split_keeps_each_volume_separate():
    """Parsing the whole log at once would merge volumes into one namespace."""
    sections = A.split_list_sections(MULTI_LOG)
    assert "Work:empty-file" in A.parse_list_all(sections["Work"])
    assert "Work:empty-file" not in A.parse_list_all(sections["SYS"])


def test_split_stops_each_section_at_the_next_command():
    """The last section must not swallow the end marker, nor a section the next command owns."""
    sections = A.split_list_sections(MULTI_LOG)
    assert "index" not in sections["Work"]
    assert A.parse_grand_total(sections["Saves"]).size == "10"


def test_split_handles_the_final_section_running_to_the_end():
    sections = A.split_list_sections(MULTI_LOG)
    assert "Saves:index" in A.parse_list_all(sections["Saves"])


def test_split_returns_nothing_when_no_volume_was_listed():
    assert A.split_list_sections("AMIBUILDER-BEGIN\n--- Version\nKickstart 47.96\n") == {}


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
    _path, before, after = result.differing[0]
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
