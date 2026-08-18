"""End-to-end tests for the command line.

These drive `main()` in-process and capture stdout, which keeps them fast enough to run on
every change while still exercising argument parsing, dispatch, rendering and exit codes
exactly as a user would hit them.

Exit codes are part of the interface, so they are asserted rather than merely
"non-zero": a script that reacts differently to "no such path" and "unsupported
filesystem" needs them to be stable.
"""

from __future__ import annotations

import json

import pytest
from amibuilder.cli import main
from amibuilder.errors import (
    AddressError,
    DeviceRefused,
    ImageError,
    NotFoundError,
    UnsupportedError,
    UsageError,
)


@pytest.fixture
def run(capsys):
    """Invoke the CLI and return (exit_code, stdout, stderr)."""

    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


@pytest.fixture
def run_json(run):
    """Invoke with --json and parse the result."""

    def _run(*argv: str):
        code, out, err = run(*argv, "--json")
        assert code == 0, f"expected success, got {code}: {err}"
        return json.loads(out)

    return _run


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_no_command_prints_help(run):
    code, out, _ = run()
    assert code == 2
    assert "addressing:" in out
    assert "amibuilder info card.hdf" in out


def test_version(run):
    with pytest.raises(SystemExit) as e:
        run("--version")
    assert e.value.code == 0


def test_global_flag_before_the_subcommand_is_honoured(run, rdb_populated):
    """argparse lets a subparser's defaults overwrite the top-level result.

    Without the merge in cli._merge_globals, `--json` typed first would be discarded.
    """
    code, out, _ = run("--json", "info", rdb_populated)
    assert code == 0
    assert json.loads(out)["kind"] == "rdb"


def test_global_flag_after_the_subcommand_is_honoured(run, rdb_populated):
    code, out, _ = run("info", rdb_populated, "--json")
    assert code == 0
    assert json.loads(out)["kind"] == "rdb"


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_info_rdb(run, rdb_populated):
    code, out, _ = run("info", rdb_populated)
    assert code == 0
    assert "RDB whole-disk image" in out
    assert "Workbench" in out and "Work" in out
    assert "DOS\\3" in out


def test_info_adf(run, adf):
    code, out, _ = run("info", adf)
    assert code == 0
    assert "ADF floppy image" in out
    assert "Disk1" in out


def test_info_plain_hdf(run, plain_hdf):
    code, out, _ = run("info", plain_hdf)
    assert code == 0
    assert "plain HDF" in out
    assert "no partition table" in out


def test_info_unformatted_explains_rather_than_failing(run, unformatted_hdf):
    code, out, _ = run("info", unformatted_hdf)
    assert code == 0, "an unformatted image is a legitimate thing to inspect"
    assert "No readable filesystem" in out
    assert "unformatted" in out


def test_info_json_shape(run_json, rdb_populated):
    d = run_json("info", rdb_populated)
    assert d["kind"] == "rdb"
    assert d["geometry"]["block_size"] == 512
    assert [p["volume"] for p in d["partitions"]] == ["Workbench", "Work"]
    assert d["partitions"][0]["bootable"] is True


def test_info_on_a_card_points_at_the_amiga_slot(run, amiga_card):
    card, slot = amiga_card
    code, out, _ = run("info", card)
    assert code == 0
    assert "MBR-partitioned" in out
    assert f"{card}:0x76:{slot}" in out, "must tell the user how to address the slot"


def test_info_through_an_mbr_slice(run, amiga_card):
    card, slot = amiga_card
    code, out, _ = run("info", f"{card}:0x76:{slot}")
    assert code == 0
    assert "MBR slice" in out
    assert "Workbench" in out


# ---------------------------------------------------------------------------
# partitions
# ---------------------------------------------------------------------------


def test_partitions_lists_both(run, rdb_populated):
    code, out, _ = run("partitions", rdb_populated)
    assert code == 0
    assert "DH0" in out and "DH1" in out
    assert "boot(0)" in out


def test_partitions_verbose_adds_details(run, rdb_populated):
    code, out, _ = run("partitions", rdb_populated, "-v")
    assert code == 0
    assert "max transfer" in out and "0x00ffffff" in out


def test_partitions_no_probe_omits_volume_names(run, rdb_populated):
    code, out, _ = run("partitions", rdb_populated, "--no-probe")
    assert code == 0
    assert "Workbench" not in out


def test_partitions_on_a_flat_image_says_so(run, adf):
    code, out, _ = run("partitions", adf)
    assert code == 0
    assert "no partition table" in out


def test_partitions_json(run_json, rdb_populated):
    d = run_json("partitions", rdb_populated)
    assert len(d["partitions"]) == 2
    p = d["partitions"][0]
    assert p["low_cyl"] == 1
    assert p["block_size"] == 512
    assert p["supported"] is True


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def test_ls_root(run, rdb_populated):
    code, out, _ = run("ls", rdb_populated)
    assert code == 0
    assert out.split() == ["C/", "Devs/", "Prefs/", "S/", "Tools/"]


def test_ls_subdirectory(run, rdb_populated):
    code, out, _ = run("ls", rdb_populated, "S")
    assert code == 0
    assert set(out.split()) == {"Shell-Startup", "Startup-Sequence"}


def test_ls_long_shows_protection_size_and_time(run, rdb_populated):
    code, out, _ = run("ls", rdb_populated, "S", "-l")
    assert code == 0
    assert "----rwed" in out
    assert "46" in out  # Startup-Sequence size
    assert "file(s)" in out


def test_ls_recursive_labels_each_directory(run, rdb_populated):
    code, out, _ = run("ls", rdb_populated, "-R")
    assert code == 0
    assert "Workbench:" in out
    assert "Devs/DOSDrivers:" in out
    assert "Workbench::" not in out, "the volume label must not be double-colonned"


def test_ls_by_volume_name(run, rdb_populated):
    code, out, _ = run("ls", f"{rdb_populated}:Work")
    assert code == 0
    assert "(empty)" in out


def test_ls_on_a_file_lists_just_that_file(run, rdb_populated):
    code, out, _ = run("ls", rdb_populated, "S/Startup-Sequence")
    assert code == 0
    assert out.strip() == "Startup-Sequence"


def test_ls_json(run_json, rdb_populated):
    d = run_json("ls", rdb_populated, "S")
    entries = d["listings"][0]["entries"]
    assert {e["name"] for e in entries} == {"Shell-Startup", "Startup-Sequence"}
    e = next(x for x in entries if x["name"] == "Startup-Sequence")
    assert e["type"] == "file"
    assert e["size"] == 46
    assert e["modified"] and "+" not in e["modified"], "naive timestamp, no timezone"


def test_ls_missing_path(run, rdb_populated):
    code, _, err = run("ls", rdb_populated, "NoSuchDir")
    assert code == NotFoundError.exit_code
    assert "no such path" in err


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------


def test_tree_shows_the_whole_hierarchy(run, rdb_populated):
    code, out, _ = run("tree", rdb_populated)
    assert code == 0
    assert "Env-Archive" in out and "overscan.prefs" in out
    assert "8 directories, 8 files" in out


def test_tree_depth_limits(run, rdb_populated):
    code, out, _ = run("tree", rdb_populated, "--depth", "1")
    assert code == 0
    assert "Tools/" in out
    assert "Calculator" not in out


def test_tree_json_nests_children(run_json, rdb_populated):
    d = run_json("tree", rdb_populated)
    devs = next(n for n in d["tree"] if n["name"] == "Devs")
    assert devs["children"][0]["name"] == "DOSDrivers"
    assert devs["children"][0]["children"][0]["name"] == "CD0"


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_by_name_glob(run, rdb_populated):
    code, out, _ = run("find", rdb_populated, "--name", "*.info")
    assert code == 0
    assert out.strip() == "Tools/Calculator.info"


def test_find_is_case_insensitive_like_the_filesystem(run, rdb_populated):
    code, out, _ = run("find", rdb_populated, "--name", "*.INFO")
    assert code == 0
    assert "Calculator.info" in out


def test_find_type_filters(run, rdb_populated):
    code, out, _ = run("find", rdb_populated, "--type", "d")
    assert code == 0
    assert "Tools/" in out
    assert "Calculator" not in out


def test_find_by_size(run, rdb_populated):
    code, out, _ = run("find", rdb_populated, "--type", "f", "--min-size", "1K")
    assert code == 0
    names = out.split()
    assert "Tools/Calculator" in names
    assert "S/Shell-Startup" not in names


def test_find_by_path_pattern(run, rdb_populated):
    code, out, _ = run("find", rdb_populated, "--path", "Prefs/*")
    assert code == 0
    assert "Prefs/Env-Archive" in out


def test_find_no_matches_exits_one(run, rdb_populated):
    """Composable in shell conditionals, the way grep is."""
    code, out, _ = run("find", rdb_populated, "--name", "definitely-absent")
    assert code == 1
    assert "(no matches)" in out


def test_find_json_reports_count(run_json, rdb_populated):
    d = run_json("find", rdb_populated, "--name", "*.info")
    assert d["count"] == 1
    assert d["matches"][0]["path"] == "Tools/Calculator.info"


# ---------------------------------------------------------------------------
# du
# ---------------------------------------------------------------------------


def test_du_reports_apparent_and_on_disk(run, rdb_populated):
    code, out, _ = run("du", rdb_populated)
    assert code == 0
    assert "apparent" in out and "on-disk" in out
    assert "block overhead" in out


def test_du_on_disk_exceeds_apparent(run_json, rdb_populated):
    """Small files waste most of a block, which is the point of reporting both."""
    d = run_json("du", rdb_populated)
    root = next(e for e in d["entries"] if e["path"] == "")
    assert root["disk_bytes"] > root["apparent_bytes"]
    assert root["files"] == 8


def test_du_parents_include_children(run_json, rdb_populated):
    d = run_json("du", rdb_populated)
    by_path = {e["path"]: e for e in d["entries"]}
    assert by_path[""]["apparent_bytes"] >= by_path["Tools"]["apparent_bytes"]
    assert by_path["Prefs"]["files"] == by_path["Prefs/Env-Archive"]["files"]


def test_du_depth_limits(run_json, rdb_populated):
    d = run_json("du", rdb_populated, "--depth", "1")
    paths = {e["path"] for e in d["entries"]}
    assert "Prefs" in paths
    assert "Prefs/Env-Archive" not in paths


# ---------------------------------------------------------------------------
# cat
# ---------------------------------------------------------------------------


def test_cat_writes_exact_bytes(run, rdb_populated, capsysbinary=None):
    code, out, _ = run("cat", rdb_populated, "S/Shell-Startup")
    assert code == 0
    assert out == 'Prompt "%N.%S> "\n'


def test_cat_text_mode_normalises_line_endings(run, workdir):
    from helpers import images

    path = images.make_plain_hdf(str(workdir / "cr.hdf"), size="10Mi", volume="CR")
    images.write_files(path, {"CRFile": b"one\rtwo\r"})
    code, out, _ = run("cat", path, "CRFile", "--text")
    assert code == 0
    assert out == "one\ntwo\n"


def test_cat_json_is_refused(run, rdb_populated):
    """Raw bytes cannot go through JSON; say so rather than corrupting them."""
    code, _, err = run("cat", rdb_populated, "S/Shell-Startup", "--json")
    assert code == UsageError.exit_code
    assert "cannot be represented as JSON" in err
    assert "hexdump" in err


def test_cat_a_directory_is_refused(run, rdb_populated):
    code, _, err = run("cat", rdb_populated, "S")
    assert code == ImageError.exit_code
    assert "is a directory" in err


# ---------------------------------------------------------------------------
# hexdump
# ---------------------------------------------------------------------------


def test_hexdump_block_zero_of_an_rdb(run, rdb_populated):
    code, out, _ = run("hexdump", rdb_populated, "--block", "0")
    assert code == 0
    assert "rigiddisk" in out
    assert "checksum=ok" in out
    assert "|RDSK" in out
    import re

    dump_lines = [ln for ln in out.splitlines() if re.match(r"^[0-9a-f]{8}  ", ln)]
    assert len(dump_lines) == 32, "512 bytes at 16 bytes per line"
    assert dump_lines[-1].startswith("000001f0")


def test_hexdump_multiple_blocks(run, rdb_populated):
    code, out, _ = run("hexdump", rdb_populated, "--block", "0", "--count", "2")
    assert code == 0
    assert "rigiddisk" in out and "partition" in out


def test_hexdump_works_without_a_mountable_filesystem(run, unformatted_hdf):
    """Dumping blocks is most useful precisely when the volume will not mount."""
    code, out, _ = run("hexdump", unformatted_hdf, "--block", "0")
    assert code == 0
    assert "empty" in out


def test_hexdump_a_file(run, rdb_populated):
    code, out, _ = run("hexdump", rdb_populated, "S/Shell-Startup")
    assert code == 0
    assert "|Prompt" in out


def test_hexdump_file_slicing(run, rdb_populated):
    code, out, _ = run("hexdump", rdb_populated, "C/List", "--skip", "16", "--length", "16")
    assert code == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("00000010")


def test_hexdump_block_past_the_end(run, rdb_populated):
    code, _, err = run("hexdump", rdb_populated, "--block", "99999999")
    assert code == ImageError.exit_code
    assert "past the end" in err


def test_hexdump_requires_a_target(run, rdb_populated):
    code, _, err = run("hexdump", rdb_populated)
    assert code == UsageError.exit_code
    assert "--block" in err


def test_hexdump_rejects_both_target_forms(run, rdb_populated):
    code, _, err = run("hexdump", rdb_populated, "S", "--block", "0")
    assert code == UsageError.exit_code
    assert "not both" in err


def test_hexdump_json_includes_raw_hex(run_json, rdb_populated):
    d = run_json("hexdump", rdb_populated, "--block", "0")
    assert d["blocks"][0]["kind"] == "rigiddisk"
    assert d["blocks"][0]["hex"].startswith("5244534b")  # 'RDSK'


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_a_single_file(run, rdb_populated, workdir):
    target = workdir / "out.txt"
    code, out, _ = run("get", rdb_populated, "S/Shell-Startup", str(target))
    assert code == 0
    assert target.read_bytes() == b'Prompt "%N.%S> "\n'
    assert "1 file(s)" in out


def test_get_a_subtree(run, rdb_populated, workdir):
    dest = workdir / "extracted"
    code, _, _ = run("get", rdb_populated, "S", str(dest))
    assert code == 0
    assert (dest / "S" / "Startup-Sequence").exists()
    assert (dest / "S" / "Shell-Startup").exists()


def test_get_the_whole_volume(run, rdb_populated, workdir):
    from conftest import WORKBENCH_FILES

    dest = workdir / "whole"
    code, _, _ = run("get", rdb_populated, "", str(dest))
    assert code == 0
    for path in WORKBENCH_FILES:
        assert (dest / path).exists(), f"{path} missing from the extraction"


def test_get_preserves_content_exactly(run, rdb_populated, workdir):
    dest = workdir / "exact"
    code, _, _ = run("get", rdb_populated, "C/List", str(dest / "List"))
    assert code == 0
    assert (dest / "List").read_bytes() == bytes(range(256)) * 8


def test_get_refuses_to_overwrite_by_default(run, rdb_populated, workdir):
    target = workdir / "out.txt"
    target.write_bytes(b"existing")
    code, _, err = run("get", rdb_populated, "S/Shell-Startup", str(target))
    assert code == ImageError.exit_code
    assert "--force" in err
    assert target.read_bytes() == b"existing"


def test_get_force_overwrites(run, rdb_populated, workdir):
    target = workdir / "out.txt"
    target.write_bytes(b"existing")
    code, _, _ = run("get", rdb_populated, "S/Shell-Startup", str(target), "--force")
    assert code == 0
    assert target.read_bytes() != b"existing"


def test_get_dry_run_writes_nothing(run, rdb_populated, workdir):
    dest = workdir / "nothing"
    code, out, _ = run("get", rdb_populated, "S", str(dest), "-n")
    assert code == 0
    assert "would write" in out
    assert not dest.exists()


def test_get_preserve_times_uses_the_amiga_timestamp(run, rdb_populated, workdir):
    """The host mtime must match the naive rendering of the stored Amiga triple.

    Asserted against the volume's own recorded value rather than against "now", so a
    timezone or epoch error of hours would fail rather than slipping through.
    """
    import datetime as dt

    from amibuilder import timestamps
    from amibuilder.addressing import parse
    from amibuilder.image import open_container

    with open_container(parse(rdb_populated)) as c:
        with c.open_volume(0) as vol:
            expected_secs = vol.stat("S/Shell-Startup").mod_secs
    assert expected_secs > 0, "fixture has no timestamp to preserve"
    expected = timestamps.to_datetime(expected_secs)

    target = workdir / "stamped.txt"
    code, _, _ = run("get", rdb_populated, "S/Shell-Startup", str(target),
                     "--preserve-times")
    assert code == 0

    got = dt.datetime.fromtimestamp(target.stat().st_mtime)
    drift = abs((got - expected).total_seconds())
    assert drift < 2, f"mtime {got} does not match the stored timestamp {expected}"


def test_get_without_preserve_times_leaves_a_current_mtime(run, rdb_populated, workdir):
    import datetime as dt

    target = workdir / "unstamped.txt"
    code, _, _ = run("get", rdb_populated, "S/Shell-Startup", str(target))
    assert code == 0
    age = abs((dt.datetime.now()
               - dt.datetime.fromtimestamp(target.stat().st_mtime)).total_seconds())
    assert age < 120


def test_get_json_lists_every_file(run_json, rdb_populated, workdir):
    d = run_json("get", rdb_populated, "S", str(workdir / "j"))
    assert len(d["files"]) == 2
    assert d["total_bytes"] == 46 + 17


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_a_healthy_image(run, rdb_populated):
    code, out, _ = run("check", rdb_populated)
    assert code == 0
    assert out.count("ok") >= 2, "both partitions reported"


def test_check_every_partition_by_default(run_json, rdb_populated):
    d = run_json("check", rdb_populated)
    assert len(d["checks"]) == 2
    assert all(c["errors"] == 0 for c in d["checks"])


def test_check_a_single_partition(run_json, rdb_populated):
    d = run_json("check", f"{rdb_populated}:1")
    assert len(d["checks"]) == 1
    assert d["checks"][0]["volume"] == "Work"


def test_check_an_adf(run, adf):
    code, out, _ = run("check", adf)
    assert code == 0
    assert "ok" in out


def test_check_classifies_dircache_findings_as_validator_limitations(run, dircache_hdf):
    """A healthy DOS5 volume must not be reported as corrupt.

    amitools' validator has no dircache support, so every dircache block produces a
    bitmap error. Classifying rather than suppressing keeps genuine corruption visible.
    """
    code, out, _ = run("check", dircache_hdf)
    assert code == 0, "a healthy dircache volume must not fail the check"
    assert "validator limitation" in out
    assert "dircache" in out


def test_check_dircache_json_explains_each_finding(run_json, dircache_hdf):
    d = run_json("check", dircache_hdf)
    c = d["checks"][0]
    assert c["errors"] == 0
    assert c["limitations"] > 0
    lim = [f for f in c["findings"] if f["classification"] == "validator-limitation"]
    assert lim, "expected reclassified findings"
    assert all("dircache" in f["explanation"] for f in lim)
    assert all(f["blocks"] for f in lim), "each finding names the blocks it covers"


def test_check_verbose_lists_the_limitations(run, dircache_hdf):
    code, out, _ = run("check", dircache_hdf, "-v")
    assert code == 0
    assert "Invalid bitmap allocation" in out


def test_check_skips_an_unformatted_partition_without_failing(run, unformatted_hdf):
    code, out, _ = run("check", unformatted_hdf)
    assert code == 0
    assert "SKIPPED" in out


def test_check_detects_real_corruption(run, workdir):
    """Flipping bytes inside the root block must be reported as an error.

    This is what proves the dircache reclassification is not simply swallowing
    everything.
    """
    from helpers import images

    path = images.make_plain_hdf(str(workdir / "bad.hdf"), size="10Mi", volume="Bad")
    images.write_files(path, {"File": b"x" * 100})

    from amibuilder import blocks

    with open(path, "r+b") as f:
        # The root block sits at num_blocks // 2.
        size = workdir.joinpath("bad.hdf").stat().st_size
        root = (size // 512) // 2
        f.seek(root * 512 + 40)
        f.write(b"\xde\xad\xbe\xef")
        del blocks

    code, out, err = run("check", path)
    assert code != 0, f"corruption not detected: {out}{err}"


# ---------------------------------------------------------------------------
# Device guard rails
# ---------------------------------------------------------------------------


def test_device_requires_the_device_flag(run):
    code, _, err = run("info", "/dev/rdisk99")
    assert code == DeviceRefused.exit_code
    assert "--device" in err


def test_device_refusal_shows_the_disk_list(run):
    code, _, err = run("info", "/dev/rdisk99")
    assert code == DeviceRefused.exit_code
    assert "Attached disks:" in err


def test_boot_disk_is_refused_even_with_the_flag(run):
    """No Amiga data lives on the Mac's boot disk, so this can only be a typo."""
    from amibuilder import device

    boot = device.boot_whole_disk()
    if not boot:
        pytest.skip("could not determine the boot disk on this platform")
    code, _, err = run("info", f"/dev/r{boot}", "--device")
    assert code == DeviceRefused.exit_code
    assert "boot volume" in err
    assert "Refusing regardless of flags" in err


# ---------------------------------------------------------------------------
# Addressing errors surface cleanly
# ---------------------------------------------------------------------------


def test_missing_file(run, workdir):
    code, _, err = run("info", str(workdir / "ghost.hdf"))
    assert code == AddressError.exit_code
    assert "no such file" in err


def test_unknown_partition(run, rdb_populated):
    code, _, err = run("ls", f"{rdb_populated}:99")
    assert code == AddressError.exit_code
    assert "no partition 99" in err


def test_unknown_volume_name_lists_alternatives(run, rdb_populated):
    code, _, err = run("ls", f"{rdb_populated}:Nope")
    assert code == AddressError.exit_code
    assert "DH0" in err


def test_addressing_a_fat_slot_as_amiga_is_refused(run, amiga_card):
    card, _ = amiga_card
    code, _, err = run("info", f"{card}:0x76:0", "--device")
    assert code == AddressError.exit_code
    assert "Refusing" in err
    assert "Emu68" in err


def test_mbr_container_tells_you_how_to_address_a_slot(run, amiga_card):
    card, _ = amiga_card
    code, _, err = run("ls", card)
    assert code == AddressError.exit_code
    assert "0x76:1" in err


def test_no_traceback_leaks_for_expected_failures(run, rdb_populated):
    """Every expected failure is a message, not a stack trace."""
    for argv in (
        ("ls", f"{rdb_populated}:99"),
        ("ls", rdb_populated, "Missing"),
        ("cat", rdb_populated, "S"),
        ("info", "/dev/rdisk99"),
    ):
        code, out, err = run(*argv)
        assert code != 0
        assert "Traceback" not in err and "Traceback" not in out
        assert err.startswith("amibuilder: ")
