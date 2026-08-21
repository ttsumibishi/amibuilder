"""`cp` and `mkdir` -- the first commands that modify an image.

Three properties get the most attention here, because they are the ones that would be
expensive to discover later:

* **Nothing is written unless everything can be.** A name too long, a tree that will not
  fit, or a collision needing `--force` must be refused while the volume is untouched.
  A half-populated volume is the failure mode notes G1 exists to prevent.
* **The timestamp bytes are correct.** amitools' own timestamp path runs through a
  `mktime` epoch that lands an hour out for half the year (see `amibuilder.timestamps`),
  so every create passes `update_ts=False` and stamps the value itself. A regression here
  would be invisible in amitools' own display, which re-applies the same wrong offset in
  reverse, so it is asserted against the decoded triple instead.
* **The result is a valid FFS volume.** `check` runs amitools' validator over the whole
  tree, so a broken hash chain or a bitmap that disagrees with the tree is caught.

Written against the CLI rather than `Volume` directly, because the preflight, the exit
codes and the argument shapes are as much of the contract as the write itself.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest
from amibuilder.cli import main
from amibuilder.errors import ImageError, NotFoundError, UsageError
from amibuilder.image import open_container
from amibuilder.addressing import parse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


@pytest.fixture
def ok(run):
    """Run a command that must succeed, returning stdout."""

    def _ok(*argv: str) -> str:
        code, out, err = run(*argv)
        assert code == 0, f"exit {code}: {err or out}"
        return out

    return _ok


@pytest.fixture
def host(workdir: Path) -> Path:
    """A small host tree, including one nested directory."""
    root = workdir / "host"
    (root / "SysInfo" / "Docs").mkdir(parents=True)
    (root / "lha").write_bytes(b"binary-ish" * 200)
    (root / "patch.lha").write_bytes(bytes(range(256)) * 40)
    (root / "SysInfo" / "SysInfo").write_bytes(b"exe" * 500)
    (root / "SysInfo" / "Docs" / "README").write_text("hello amiga\n")
    return root


def entries(image: str, path: str = "") -> dict[str, dict]:
    """Read a directory back, keyed by name, straight from the volume."""
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return {e.name: e.as_dict() for e in vol.listdir(path)}


def read(image: str, path: str) -> bytes:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return vol.read_file(path)


def paths(image: str) -> set[str]:
    """Every path on a volume, files and directories alike."""
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            found = set()
            for dirpath, dirs, files in vol.walk():
                for e in list(dirs) + list(files):
                    found.add(e.path)
            return found


# ---------------------------------------------------------------------------
# cp: the basic case
# ---------------------------------------------------------------------------


def test_copy_one_file_into_the_volume_root(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target)
    assert read(target, "lha") == (host / "lha").read_bytes()


def test_copy_reports_what_it_wrote_and_what_is_left(run, rdb_populated, host):
    code, out, _ = run("cp", str(host / "lha"), f"{rdb_populated}:Work")
    assert code == 0
    assert "wrote 1 file(s) and 0 directory(ies)" in out
    assert "free" in out


def test_copy_several_files_at_once(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), str(host / "patch.lha"), target)
    assert set(entries(target)) == {"lha", "patch.lha"}


def test_copy_into_a_subdirectory_with_to(ok, rdb_populated, host):
    target = f"{rdb_populated}:Workbench"
    ok("cp", str(host / "lha"), target, "--to", "S")
    assert "lha" in entries(target, "S")
    # ...and not at the root.
    assert "lha" not in entries(target)


def test_to_a_missing_directory_is_refused_without_parents(run, rdb_populated, host):
    code, _, err = run("cp", str(host / "lha"), f"{rdb_populated}:Work", "--to", "New/Deep")
    assert code == 2
    assert "no such directory" in err
    assert "--parents" in err
    assert paths(f"{rdb_populated}:Work") == set(), "the volume was touched anyway"


def test_parents_creates_the_destination_chain(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target, "--to", "New/Deep", "-p")
    assert read(target, "New/Deep/lha") == (host / "lha").read_bytes()


def test_to_a_file_is_refused(run, rdb_populated, host):
    code, _, err = run("cp", str(host / "lha"), f"{rdb_populated}:Workbench",
                       "--to", "S/Shell-Startup")
    assert code == 2
    assert "not a directory" in err


# ---------------------------------------------------------------------------
# cp: directories
# ---------------------------------------------------------------------------


def test_a_directory_without_recursive_is_refused(run, rdb_populated, host):
    code, _, err = run("cp", str(host / "SysInfo"), f"{rdb_populated}:Work")
    assert code == 2
    assert "-r" in err
    assert paths(f"{rdb_populated}:Work") == set()


def test_recursive_copy_reproduces_the_tree(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", "-r", str(host / "SysInfo"), target)
    assert paths(target) == {
        "SysInfo", "SysInfo/Docs", "SysInfo/SysInfo", "SysInfo/Docs/README",
    }
    assert read(target, "SysInfo/Docs/README") == b"hello amiga\n"


def test_recursive_copy_preserves_content_byte_for_byte(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", "-r", str(host / "SysInfo"), target)
    assert read(target, "SysInfo/SysInfo") == (host / "SysInfo" / "SysInfo").read_bytes()


def test_an_empty_host_directory_still_arrives(ok, rdb_populated, workdir):
    """A directory carrying no files has nothing to imply it, so it must be created."""
    empty = workdir / "Empty"
    (empty / "Inner").mkdir(parents=True)
    target = f"{rdb_populated}:Work"
    ok("cp", "-r", str(empty), target)
    assert paths(target) == {"Empty", "Empty/Inner"}


def test_symlinks_are_reported_and_skipped(run, rdb_populated, host):
    """Following a symlink would turn a link into a duplicate, or copy a file outside
    the tree the user named."""
    os.symlink(host / "lha", host / "SysInfo" / "link-to-lha")
    code, out, _ = run("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work")
    assert code == 0
    assert "skip (symlink)" in out
    assert "SysInfo/link-to-lha" not in paths(f"{rdb_populated}:Work")


def test_host_metadata_files_are_not_copied(ok, rdb_populated, host):
    (host / "SysInfo" / ".DS_Store").write_bytes(b"junk")
    ok("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work")
    assert "SysInfo/.DS_Store" not in paths(f"{rdb_populated}:Work")


# ---------------------------------------------------------------------------
# cp: overwriting
# ---------------------------------------------------------------------------


def test_an_existing_file_is_refused_without_force(run, rdb_populated, host):
    target = f"{rdb_populated}:Workbench"
    before = read(target, "S/Shell-Startup")
    replacement = host / "Shell-Startup"
    replacement.write_bytes(b"replaced\n")

    code, _, err = run("cp", str(replacement), target, "--to", "S")
    assert code == 5
    assert "already exists" in err
    assert "--force" in err
    assert read(target, "S/Shell-Startup") == before


def test_force_replaces_the_file(ok, rdb_populated, host):
    target = f"{rdb_populated}:Workbench"
    replacement = host / "Shell-Startup"
    replacement.write_bytes(b"replaced\n")
    ok("cp", str(replacement), target, "--to", "S", "--force")
    assert read(target, "S/Shell-Startup") == b"replaced\n"


def test_replacing_frees_the_old_blocks(run, ok, rdb_populated, workdir):
    """The preflight must credit back what the old copy held, or replacing a file on a
    nearly full volume is refused even though it plainly fits.

    Measuring free space before and after would *not* catch this: the write path deletes
    then creates either way, so the space comes back regardless. The bug lives only in the
    arithmetic, so it only shows up when the arithmetic is what decides. Hence a volume
    deliberately filled to leave less room than the file being replaced.
    """
    target = f"{rdb_populated}:Work"

    def free_bytes() -> int:
        with open_container(parse(target)) as c:
            with c.open_addressed_volume() as vol:
                return vol.info().free_bytes

    victim = workdir / "victim"
    victim.write_bytes(b"x" * (4 * 1024 * 1024))
    ok("cp", str(victim), target)

    # Fill the rest so that less is free than the file we are about to replace.
    filler = workdir / "filler"
    filler.write_bytes(b"y" * max(0, free_bytes() - 512 * 1024))
    ok("cp", str(filler), target)
    assert free_bytes() < 4 * 1024 * 1024, "fixture did not fill the volume"

    # Same size in as out, so it fits -- but only if the old copy is counted as freed.
    code, _, err = run("cp", str(victim), target, "--force")
    assert code == 0, f"a same-size replace was refused: {err}"
    assert read(target, "victim") == victim.read_bytes()


def test_a_directory_in_the_way_of_a_file_is_refused(run, rdb_populated, host):
    code, _, err = run("cp", str(host / "lha"), f"{rdb_populated}:Workbench", "--to", "",
                       "--force")
    assert code == 0  # sanity: the plain case works
    dirname = host / "S"
    dirname.mkdir()
    (dirname / "x").write_text("x")
    code, _, err = run("cp", str(dirname / ".."), f"{rdb_populated}:Workbench")
    assert code == 2  # a directory without -r


def test_replacing_a_directory_with_a_file_is_refused(run, rdb_populated, host):
    src = host / "S"
    src.write_bytes(b"not a directory")
    code, _, err = run("cp", str(src), f"{rdb_populated}:Workbench", "--force")
    assert code == 5
    assert "is a directory" in err


def test_two_sources_differing_only_in_case_are_refused(run, rdb_populated, workdir):
    """FFS matches names without regard to case, so one would silently overwrite the
    other -- and which one won would depend on argument order."""
    a = workdir / "a" / "Readme"
    b = workdir / "b" / "README"
    for p, text in ((a, b"lower\n"), (b, b"upper\n")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text)

    code, _, err = run("cp", str(a), str(b), f"{rdb_populated}:Work")
    assert code == 2
    assert "ignores case" in err
    assert paths(f"{rdb_populated}:Work") == set()


# ---------------------------------------------------------------------------
# cp: preflight refusals leave the volume untouched
# ---------------------------------------------------------------------------


def test_a_name_over_the_limit_is_refused_before_anything_is_written(
    run, rdb_populated, workdir
):
    src = workdir / ("n" * 31)
    src.write_bytes(b"x")
    good = workdir / "fine"
    good.write_bytes(b"x")

    code, _, err = run("cp", str(good), str(src), f"{rdb_populated}:Work")
    assert code == 2
    assert "30" in err
    # `fine` was listed first and would have been written by a naive implementation.
    assert paths(f"{rdb_populated}:Work") == set()


def test_a_copy_that_will_not_fit_is_refused_whole(run, rdb_populated, workdir):
    big = workdir / "big"
    big.write_bytes(b"\0" * (12 * 1024 * 1024))  # partition 0 is 10 MiB
    code, _, err = run("cp", str(big), f"{rdb_populated}:Workbench")
    assert code == 5
    assert "short" in err
    assert "big" not in entries(f"{rdb_populated}:Workbench")


def test_a_missing_host_file_is_refused_before_the_image_opens(run, rdb_populated):
    code, _, err = run("cp", "/nonexistent/thing", f"{rdb_populated}:Work")
    assert code == 2
    assert "no such file" in err


def test_a_bad_protection_spec_is_a_usage_error(run, rdb_populated, host):
    code, _, err = run("cp", str(host / "lha"), f"{rdb_populated}:Work",
                       "--protect", "nonsense")
    assert code == 2, err
    assert "protection-bit spec" in err
    assert paths(f"{rdb_populated}:Work") == set()


def test_an_over_long_comment_is_refused(run, rdb_populated, host):
    code, _, err = run("cp", str(host / "lha"), f"{rdb_populated}:Work",
                       "--comment", "x" * 80)
    assert code == 2
    assert "79" in err
    assert paths(f"{rdb_populated}:Work") == set()


# ---------------------------------------------------------------------------
# cp: dry run
# ---------------------------------------------------------------------------


def test_dry_run_leaves_the_image_byte_identical(run, rdb_populated, host):
    before = Path(rdb_populated).read_bytes()
    code, out, _ = run("cp", "-r", "-n", str(host / "SysInfo"), f"{rdb_populated}:Work")
    assert code == 0
    assert "would write" in out
    assert "nothing was changed" in out
    assert Path(rdb_populated).read_bytes() == before


def test_dry_run_still_reports_the_whole_tree(run, rdb_populated, host):
    code, out, _ = run("cp", "-r", "-n", str(host / "SysInfo"), f"{rdb_populated}:Work")
    assert code == 0
    for expected in ("SysInfo/Docs", "SysInfo/SysInfo", "SysInfo/Docs/README"):
        assert expected in out


def test_dry_run_with_to_and_parents_does_not_create_the_directory(
    run, rdb_populated, host
):
    code, _, _ = run("cp", "-n", str(host / "lha"), f"{rdb_populated}:Work",
                     "--to", "New/Deep", "-p")
    assert code == 0
    assert paths(f"{rdb_populated}:Work") == set()


def test_dry_run_names_a_file_it_would_replace(run, rdb_populated, host):
    replacement = host / "Shell-Startup"
    replacement.write_bytes(b"replaced\n")
    code, out, _ = run("cp", "-n", str(replacement), f"{rdb_populated}:Workbench",
                       "--to", "S", "--force")
    assert code == 0
    assert "would be replaced" in out


# ---------------------------------------------------------------------------
# Timestamps -- the bytes, not amitools' rendering of them
# ---------------------------------------------------------------------------


def test_a_new_file_is_stamped_with_the_current_wall_clock(ok, rdb_populated, host):
    """An epoch bug shows up as a whole number of hours, so a two-minute window is a
    wide enough tolerance to be robust and a tight enough one to catch it."""
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target)
    stamped = dt.datetime.fromisoformat(entries(target)["lha"]["modified"])
    drift = abs((dt.datetime.now() - stamped).total_seconds())
    assert drift < 120, f"stamp is {drift:.0f}s from now, which smells like epoch drift"


def test_preserve_times_carries_the_host_mtime_across_exactly(ok, rdb_populated, host):
    src = host / "lha"
    want = dt.datetime(1999, 3, 14, 15, 9, 26)
    os.utime(src, (want.timestamp(), want.timestamp()))

    target = f"{rdb_populated}:Work"
    ok("cp", str(src), target, "--preserve-times")
    assert entries(target)["lha"]["modified"] == want.isoformat()


def test_preserve_times_survives_a_summer_mtime(ok, rdb_populated, host):
    """The amitools epoch is built from a January `mktime`, so a date in daylight saving
    time is where its conversion goes an hour wrong. This is that date."""
    src = host / "lha"
    want = dt.datetime(2004, 7, 4, 12, 0, 0)
    os.utime(src, (want.timestamp(), want.timestamp()))
    target = f"{rdb_populated}:Work"
    ok("cp", str(src), target, "--preserve-times")
    assert entries(target)["lha"]["modified"] == want.isoformat()


def test_preserve_times_applies_to_directories_too(ok, rdb_populated, host):
    src = host / "SysInfo"
    want = dt.datetime(1995, 5, 5, 5, 5, 5)
    os.utime(src, (want.timestamp(), want.timestamp()))
    target = f"{rdb_populated}:Work"
    ok("cp", "-r", str(src), target, "--preserve-times")
    assert entries(target)["SysInfo"]["modified"] == want.isoformat()


def test_a_pre_1978_mtime_falls_back_to_now(ok, rdb_populated, host):
    """AmigaDOS counts days from 1978, so an earlier date has no representation. Writing
    a negative day count would be worse than stamping the copy time."""
    src = host / "lha"
    old = dt.datetime(1970, 6, 1, 12, 0, 0)
    os.utime(src, (old.timestamp(), old.timestamp()))
    target = f"{rdb_populated}:Work"
    ok("cp", str(src), target, "--preserve-times")
    stamped = dt.datetime.fromisoformat(entries(target)["lha"]["modified"])
    assert stamped.year >= 1978
    assert abs((dt.datetime.now() - stamped).total_seconds()) < 120


def test_writing_stamps_the_parent_directory_with_a_correct_time(ok, rdb_populated, host):
    """A real Amiga updates the parent's date, so an image that claims otherwise is lying
    about its own history -- and amitools' own update writes it an hour out.

    Asserting only `after > before` would not catch that, because an hour-adrift "now" is
    still later than the fixture's timestamp. The window is what makes this a real guard.
    """
    target = f"{rdb_populated}:Workbench"
    before = entries(target)["S"]["modified_amiga_secs"]
    ok("cp", str(host / "lha"), target, "--to", "S")

    row = entries(target)["S"]
    assert row["modified_amiga_secs"] > before
    stamped = dt.datetime.fromisoformat(row["modified"])
    drift = abs((dt.datetime.now() - stamped).total_seconds())
    assert drift < 120, (
        f"the parent's new stamp is {drift:.0f}s from now; amitools' own timestamp path "
        f"lands ~3600s out, so this is what a regression to update_ts=True looks like"
    )


def test_mkdir_stamps_the_parent_with_a_correct_time(ok, rdb_populated):
    target = f"{rdb_populated}:Workbench"
    before = entries(target)["S"]["modified_amiga_secs"]
    ok("mkdir", target, "S/Fresh")
    row = entries(target)["S"]
    assert row["modified_amiga_secs"] > before
    stamped = dt.datetime.fromisoformat(row["modified"])
    assert abs((dt.datetime.now() - stamped).total_seconds()) < 120


def test_writing_does_not_restamp_an_untouched_sibling(ok, rdb_populated, host):
    target = f"{rdb_populated}:Workbench"
    before = entries(target)["C"]["modified_amiga_secs"]
    ok("cp", str(host / "lha"), target, "--to", "S")
    assert entries(target)["C"]["modified_amiga_secs"] == before


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_a_new_file_gets_the_amigados_default_protection(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target)
    assert entries(target)["lha"]["protect"] == "----rwed"


def test_protect_accepts_the_short_spelling(ok, rdb_populated, host):
    """argparse reads a bare `----rwed` as another option, so the short form is the one
    that needs no escaping."""
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target, "--protect", "rwd")
    assert entries(target)["lha"]["protect"] == "----rw-d"


def test_protect_accepts_the_full_spelling_with_equals(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target, "--protect=----rw-d")
    assert entries(target)["lha"]["protect"] == "----rw-d"


def test_comment_is_written(ok, rdb_populated, host):
    target = f"{rdb_populated}:Work"
    ok("cp", str(host / "lha"), target, "--comment", "the lha tool")
    assert entries(target)["lha"]["comment"] == "the lha tool"


def test_protect_does_not_apply_to_directories(ok, rdb_populated, host):
    """`e` on a directory is meaningless, and a file-shaped mask on a tree of
    directories would be a surprise."""
    target = f"{rdb_populated}:Work"
    ok("cp", "-r", str(host / "SysInfo"), target, "--protect", "rw")
    assert entries(target)["SysInfo"]["protect"] == "----rwed"
    assert entries(target, "SysInfo")["SysInfo"]["protect"] == "----rw--"


# ---------------------------------------------------------------------------
# The volume stays valid
# ---------------------------------------------------------------------------


def test_the_volume_validates_after_a_recursive_copy(ok, rdb_populated, host):
    ok("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work")
    ok("check", f"{rdb_populated}:Work")


def test_xdftool_reads_back_what_cp_wrote(ok, rdb_populated, host, workdir):
    """Read the result with amitools' CLI rather than our own reader.

    Validating our writes with our own reader cannot catch a wrong assumption the two
    share -- a hash bucket computed consistently but incorrectly, say, would round trip
    perfectly and still be unreadable to anything else. xdftool is a genuinely separate
    implementation of the lookup path.
    """
    from helpers import images

    ok("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work", "--to", "Tools", "-p")

    out = images.xdftool(rdb_populated, "open", "part=1", "+", "list").output
    assert "SysInfo" in out, out
    assert "README" in out, out

    dest = workdir / "extracted"
    images.xdftool(rdb_populated, "open", "part=1", "+", "read",
                   "Tools/SysInfo/Docs/README", str(dest))
    assert dest.read_bytes() == b"hello amiga\n"


def test_xdfscan_reports_the_written_volume_as_clean(ok, plain_hdf, host):
    """amitools' own validator, run as a separate process over the finished file."""
    from helpers import images

    ok("cp", "-r", str(host / "SysInfo"), plain_hdf)
    assert images.scan_is_ok(plain_hdf), images.xdfscan(plain_hdf).stdout


def test_the_whole_drive_validates_after_writing_one_partition(ok, rdb_populated, host):
    ok("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work")
    ok("check", rdb_populated)


def test_writing_one_partition_leaves_the_other_alone(ok, rdb_populated, host):
    from conftest import WORKBENCH_FILES

    ok("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work")
    for rel, data in WORKBENCH_FILES.items():
        assert read(f"{rdb_populated}:Workbench", rel) == data


def test_many_small_files_stay_valid(ok, rdb_populated, workdir):
    """Enough entries to spill several hash buckets, which is where a directory's hash
    chain would break if the collision link were mishandled."""
    src = workdir / "many"
    src.mkdir()
    for i in range(150):
        (src / f"file{i:03d}").write_bytes(bytes([i % 256]) * (i * 7 + 1))

    target = f"{rdb_populated}:Work"
    ok("cp", "-r", str(src), target)
    ok("check", f"{rdb_populated}")
    assert len(entries(target, "many")) == 150
    for i in (0, 71, 72, 149):
        assert read(target, f"many/file{i:03d}") == bytes([i % 256]) * (i * 7 + 1)


def test_a_file_needing_extension_blocks_round_trips(ok, rdb_populated, workdir):
    """A file header holds 72 data pointers; past that it needs extension blocks, which
    is a separate code path in amitools and the usual place a large write goes wrong."""
    src = workdir / "big.bin"
    data = bytes((i * 7 + 3) % 256 for i in range(200 * 1024))  # ~400 data blocks
    src.write_bytes(data)
    target = f"{rdb_populated}:Work"
    ok("cp", str(src), target)
    ok("check", f"{rdb_populated}:Work")
    assert read(target, "big.bin") == data


def test_a_zero_byte_file_round_trips(ok, rdb_populated, workdir):
    src = workdir / "empty"
    src.write_bytes(b"")
    target = f"{rdb_populated}:Work"
    ok("cp", str(src), target)
    assert read(target, "empty") == b""
    assert entries(target)["empty"]["size"] == 0


# ---------------------------------------------------------------------------
# Plain HDF and ADF targets
# ---------------------------------------------------------------------------


def test_copy_into_a_plain_hdf(ok, plain_hdf, host):
    ok("cp", str(host / "lha"), plain_hdf)
    assert read(plain_hdf, "lha") == (host / "lha").read_bytes()


def test_copy_into_an_adf(ok, adf, host):
    ok("cp", str(host / "patch.lha"), adf)
    assert read(adf, "patch.lha") == (host / "patch.lha").read_bytes()
    ok("check", adf)


def test_an_adf_that_is_too_small_is_refused(run, adf, workdir):
    big = workdir / "big"
    big.write_bytes(b"\0" * 900_000)  # a DD ADF holds 880 KiB, less overhead
    code, _, err = run("cp", str(big), adf)
    assert code == 5
    assert "short" in err


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_reports_every_entry_and_the_space_left(run, rdb_populated, host):
    code, out, _ = run("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work",
                       "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["volume"] == "Work"
    assert payload["files"] == 2
    assert payload["directories"] == 2
    assert payload["dry_run"] is False
    assert payload["free_blocks"] > 0
    assert {e["path"] for e in payload["entries"]} == {
        "SysInfo", "SysInfo/Docs", "SysInfo/SysInfo", "SysInfo/Docs/README",
    }


def test_json_dry_run_says_so(run, rdb_populated, host):
    code, out, _ = run("cp", "-n", str(host / "lha"), f"{rdb_populated}:Work", "--json")
    assert code == 0
    assert json.loads(out)["dry_run"] is True


def test_json_records_skipped_symlinks(run, rdb_populated, host):
    os.symlink(host / "lha", host / "SysInfo" / "link")
    code, out, _ = run("cp", "-r", str(host / "SysInfo"), f"{rdb_populated}:Work",
                       "--json")
    assert code == 0
    skipped = json.loads(out)["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "symlink"


# ---------------------------------------------------------------------------
# mkdir
# ---------------------------------------------------------------------------


def test_mkdir_creates_a_directory(ok, rdb_populated):
    target = f"{rdb_populated}:Work"
    ok("mkdir", target, "Utils")
    assert entries(target)["Utils"]["type"] == "dir"


def test_mkdir_creates_several_at_once(ok, rdb_populated):
    target = f"{rdb_populated}:Work"
    ok("mkdir", target, "One", "Two", "Three")
    assert {"One", "Two", "Three"} <= set(entries(target))


def test_mkdir_without_parents_refuses_a_missing_ancestor(run, rdb_populated):
    code, _, err = run("mkdir", f"{rdb_populated}:Work", "Deep/Nested")
    assert code == 3
    assert "--parents" in err
    assert paths(f"{rdb_populated}:Work") == set()


def test_mkdir_parents_creates_the_chain(ok, rdb_populated):
    target = f"{rdb_populated}:Work"
    ok("mkdir", target, "Deep/Nested/Deeper", "-p")
    assert paths(target) == {"Deep", "Deep/Nested", "Deep/Nested/Deeper"}


def test_mkdir_on_an_existing_directory_fails_without_parents(run, rdb_populated):
    code, _, err = run("mkdir", f"{rdb_populated}:Workbench", "S")
    assert code == 5
    assert "already exists" in err


def test_mkdir_parents_tolerates_an_existing_directory(run, rdb_populated):
    """`-p` means the same thing it does in Unix: make what is missing, do not complain
    about what is not, and stay quiet about it."""
    code, out, _ = run("mkdir", f"{rdb_populated}:Workbench", "S", "-p")
    assert code == 0
    assert "created 0 directory(ies)" in out


def test_mkdir_verbose_names_what_already_existed(run, rdb_populated):
    """Silence is right by default, but with several paths it is useful to know which
    ones were already there."""
    code, out, _ = run("mkdir", "-v", f"{rdb_populated}:Workbench", "S", "Fresh", "-p")
    assert code == 0
    assert "exists already" in out
    assert "created" in out
    assert "created 1 directory(ies)" in out


def test_mkdir_over_a_file_is_refused(run, rdb_populated):
    code, _, err = run("mkdir", f"{rdb_populated}:Workbench", "S/Shell-Startup", "-p")
    assert code == 5
    assert "file" in err


def test_mkdir_refuses_an_over_long_name(run, rdb_populated):
    code, _, err = run("mkdir", f"{rdb_populated}:Work", "d" * 31)
    assert code == 2
    assert "30" in err


def test_mkdir_refuses_the_volume_root(run, rdb_populated):
    code, _, err = run("mkdir", f"{rdb_populated}:Work", "/")
    assert code == 2
    assert "already exists" in err


def test_mkdir_dry_run_changes_nothing(run, rdb_populated):
    before = Path(rdb_populated).read_bytes()
    code, out, _ = run("mkdir", "-n", f"{rdb_populated}:Work", "Deep/Nested", "-p")
    assert code == 0
    assert "would create" in out
    assert Path(rdb_populated).read_bytes() == before


def test_mkdir_stamps_the_new_directory(ok, rdb_populated):
    target = f"{rdb_populated}:Work"
    ok("mkdir", target, "Utils")
    stamped = dt.datetime.fromisoformat(entries(target)["Utils"]["modified"])
    assert abs((dt.datetime.now() - stamped).total_seconds()) < 120


def test_mkdir_json(run, rdb_populated):
    code, out, _ = run("mkdir", f"{rdb_populated}:Work", "A/B", "-p", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["volume"] == "Work"
    assert payload["created"] == 1
    assert payload["directories"] == [{"path": "A/B", "created": True}]


def test_the_volume_validates_after_mkdir(ok, rdb_populated):
    ok("mkdir", f"{rdb_populated}:Work", "A/B/C/D/E", "-p")
    ok("check", rdb_populated)


# ---------------------------------------------------------------------------
# Addressing and read-only refusals
# ---------------------------------------------------------------------------


def test_cp_honours_the_partition_selector(ok, rdb_populated, host):
    ok("cp", str(host / "lha"), f"{rdb_populated}:1")
    assert "lha" in entries(f"{rdb_populated}:1")
    assert "lha" not in entries(f"{rdb_populated}:0")


def test_cp_accepts_a_device_name(ok, rdb_populated, host):
    ok("cp", str(host / "lha"), f"{rdb_populated}:DH1")
    assert "lha" in entries(f"{rdb_populated}:DH1")


def test_cp_to_a_nonexistent_image_is_reported(run, workdir, host):
    code, _, err = run("cp", str(host / "lha"), str(workdir / "nope.hdf"))
    assert code != 0
    assert "nope.hdf" in err


def test_a_read_only_volume_refuses_writes(rdb_populated):
    """The guard exists so the refusal happens before amitools allocates anything.

    Matched on the exact wording rather than just "read-only": without the guard, amitools
    fails later with `Can't write block: image file is read-only`, which contains the same
    phrase. A loose match would pass either way and prove nothing about *where* the
    refusal happened.
    """
    with open_container(parse(f"{rdb_populated}:Work")) as container:
        with container.open_addressed_volume() as vol:
            assert vol.writable is False
            for call in (lambda: vol.mkdir("Nope"),
                         lambda: vol.write_file("Nope", b"x"),
                         lambda: vol.set_times("S", 1)):
                with pytest.raises(ImageError) as exc:
                    call()
                assert "opened read-only, so it cannot be modified" in str(exc.value)


def test_a_writable_volume_says_so(rdb_populated):
    with open_container(parse(f"{rdb_populated}:Work"), writable=True) as container:
        with container.open_addressed_volume() as vol:
            assert vol.writable is True


# ---------------------------------------------------------------------------
# Volume-level unit checks
# ---------------------------------------------------------------------------


#: Sizes that straddle every boundary in the block accounting: empty, sub-block,
#: exactly one block, the 72-pointer header table, and past it into extension blocks.
BLOCK_SIZES = (0, 1, 511, 512, 513, 71 * 512, 72 * 512, 72 * 512 + 1, 144 * 512,
               200 * 1024)


@pytest.mark.parametrize("dos_type", ["ffs+intl", "ofs"])
def test_blocks_for_matches_amitools_own_accounting(workdir, dos_type):
    """`blocks_for` mirrors `ADFSFile.blocks_get_create_num`. If it drifts, the capacity
    preflight starts refusing copies that would fit, or accepting ones that would not.

    OFS is covered as well as FFS because OFS spends 24 bytes of every data block on a
    header, so the two filesystems need different data-block counts for the same file --
    and an FFS-only test cannot see the difference.
    """
    from amitools.fs.ADFSFile import ADFSFile
    from helpers import images

    path = images.make_plain_hdf(str(workdir / f"acct-{dos_type[:3]}.hdf"), size="20Mi",
                                 volume="Acct", dos_type=dos_type)
    with open_container(parse(path), writable=True) as container:
        with container.open_addressed_volume() as vol:
            amitools_vol = vol._vol  # deliberate: this test pins our copy to theirs
            for size in BLOCK_SIZES:
                node = ADFSFile(amitools_vol, amitools_vol.get_root_dir())
                node.set_file_data(bytes(size))
                assert vol.blocks_for(size) == node.blocks_get_create_num(), \
                    f"{dos_type} size={size}"


def test_illegal_characters_are_refused(rdb_populated):
    with open_container(parse(f"{rdb_populated}:Work"), writable=True) as container:
        with container.open_addressed_volume() as vol:
            for bad in ("a:b", "a/b"):
                with pytest.raises(UsageError, match="cannot contain"):
                    vol.check_name(bad)


def test_name_length_is_enforced_at_the_boundary(rdb_populated):
    """30 is legal, 31 is not.

    The check measures the Latin-1 encoded length because the on-disk field is a byte
    count. Note that this cannot differ from the character count: Latin-1 is one byte per
    character, and anything outside it is replaced with a single byte. So the byte framing
    is the *correct* way to express the limit rather than an observable difference, and
    this test deliberately claims only the boundary -- which is the part that can break.
    """
    with open_container(parse(f"{rdb_populated}:Work"), writable=True) as container:
        with container.open_addressed_volume() as vol:
            vol.check_name("n" * 30)
            vol.check_name("\xe4" * 30)  # non-ASCII, still one byte each
            with pytest.raises(UsageError, match="31 bytes"):
                vol.check_name("n" * 31)
            with pytest.raises(UsageError, match="empty"):
                vol.check_name("")


def test_free_space_is_recomputed_after_a_write(rdb_populated, host):
    """`info()` caches, so a stale figure would make the second write in one session
    believe it has more room than it does."""
    with open_container(parse(f"{rdb_populated}:Work"), writable=True) as container:
        with container.open_addressed_volume() as vol:
            before = vol.info().free_blocks
            vol.write_file("thing", b"x" * 100_000)
            assert vol.info().free_blocks < before
