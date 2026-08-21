"""Tests for `amibuilder shell`, the interactive REPL.

Driven entirely through `dispatch(state, line)`, exactly as the design intends: a scratch
RDB fixture, a list of command-line strings, and assertions on the returned output lines
and on the volume read back afterwards. No TTY, no readline, no emulator -- the loop that
needs a terminal (`run_repl`) carries no logic and is not exercised here.

The properties that earn the most attention are the ones a shell makes easy to get wrong:

* **Nothing is ever overwritten.** `put`, `get`, `cp` and `mv` refuse a destination that
  already exists and change nothing. There is no `--force` in the shell.
* **The right things are file-only.** `rm`, `cp`, `mv` and `put` refuse a directory; `get`
  is the one verb that takes a directory, recursively.
* **The bitmap is flushed after every mutation.** amitools writes tree blocks through to
  the OS as it goes, but the allocation bitmap is only written on `close()` or an explicit
  `flush()` (notes G29). Miss the per-command flush and the on-disk bitmap goes stale while
  the tree looks fine -- so these tests run `check` on a fresh read-only open *during* the
  session (before any close could hide it), which catches the tree/bitmap disagreement.
* **The volume is still valid afterwards.** `check` runs amitools' validator over the tree.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.commands.shell import ShellState, complete, dispatch, resolve_image
from amibuilder.image import open_container

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


class _Driver:
    """Feeds lines through `dispatch`, threading the state and collecting output."""

    def __init__(self, state: ShellState):
        self.state = state

    def run(self, line: str) -> list[str]:
        lines, self.state = dispatch(self.state, line)
        return lines

    def out(self, line: str) -> str:
        """Run a line and return its output joined, for substring assertions."""
        return "\n".join(self.run(line))


@contextmanager
def shell_session(image: str, local_cwd: Path | str = "."):
    """Open a writable shell over `image` and yield a driver.

    The volume is closed on exit, which is the session's final flush -- but every test that
    cares about durability checks it *during* the session, so the close cannot paper over a
    missing per-command flush.
    """
    with open_container(parse(image), writable=True) as container:
        with container.open_addressed_volume() as vol:
            yield _Driver(ShellState(vol=vol, image_cwd="", local_cwd=Path(local_cwd)))


def entries(image: str, path: str = "") -> dict[str, dict]:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return {e.name: e.as_dict() for e in vol.listdir(path)}


def read(image: str, path: str) -> bytes:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return vol.read_file(path)


def stat(image: str, path: str):
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            return vol.stat(path)


def paths(image: str) -> set[str]:
    with open_container(parse(image)) as container:
        with container.open_addressed_volume() as vol:
            found = set()
            for dirpath, dirs, files in vol.walk():
                for e in list(dirs) + list(files):
                    found.add(e.path)
            return found


@pytest.fixture
def localdir(workdir: Path) -> Path:
    """A small host tree for the local-side and put/get tests."""
    root = workdir / "local"
    (root / "sub").mkdir(parents=True)
    (root / "note.txt").write_bytes(b"hello from the host\n")
    (root / "prog").write_bytes(bytes(range(256)) * 4)
    (root / "sub" / "inner.txt").write_bytes(b"nested\n")
    return root


# ---------------------------------------------------------------------------
# resolve_image -- the path model, tested directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cwd,arg,expected", [
    ("", "S", "S"),
    ("S", "T", "S/T"),
    ("", "Prefs/Env-Archive", "Prefs/Env-Archive"),
    ("S", "", "S"),            # empty arg keeps the cwd
    ("S", ":", ""),            # ':' alone is the root
    ("S/T", ":", ""),
    ("anything", ":Prefs/Sys", "Prefs/Sys"),   # leading ':' is volume-absolute
    ("S/T", "/", "S"),          # '/' is up one
    ("S/T", "//", ""),          # '//' is up two
    ("S/T/U", "//", "S"),
    ("", "/", ""),              # up from the root stays at the root
    ("a/b", "/c", "a/c"),       # up one, then down into c
    ("a", "//x", "x"),          # up two (clamped) then into x
    ("Work", "S//T", "Work/S/T"),   # a mid-path double slash is just a separator
])
def test_resolve_image(cwd, arg, expected):
    assert resolve_image(cwd, arg) == expected


# ---------------------------------------------------------------------------
# Navigation: pwd, cd, ls
# ---------------------------------------------------------------------------


def test_pwd_starts_at_the_root(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert sh.out("pwd") == "Workbench:"


def test_cd_descends_and_pwd_follows(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd S")
        assert sh.out("pwd") == "Workbench:S"


def test_cd_slash_goes_up_one(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd Prefs/Env-Archive")
        assert sh.out("pwd") == "Workbench:Prefs/Env-Archive"
        sh.run("cd /")
        assert sh.out("pwd") == "Workbench:Prefs"


def test_cd_colon_returns_to_the_root(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd Prefs/Env-Archive")
        sh.run("cd :")
        assert sh.out("pwd") == "Workbench:"


def test_cd_no_argument_returns_to_the_root(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd S")
        sh.run("cd")
        assert sh.out("pwd") == "Workbench:"


def test_leading_colon_is_volume_absolute(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd S")
        # From inside S, an absolute path ignores the cwd.
        assert "overscan.prefs" in sh.out("ls :Prefs/Env-Archive/Sys")


def test_cd_into_a_missing_directory_is_refused(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert "no such directory" in sh.out("cd Nope").lower()
        assert sh.out("pwd") == "Workbench:"   # cwd unchanged


def test_cd_into_a_file_is_refused(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert "not a directory" in sh.out("cd S/Startup-Sequence").lower()
        assert sh.out("pwd") == "Workbench:"


def test_ls_lists_the_current_directory(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        out = sh.out("ls")
        assert "S/" in out and "C/" in out and "Prefs/" in out


def test_ls_marks_directories_and_sizes_files(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        out = sh.out("ls S")
        assert "Startup-Sequence" in out
        # A directory listing of files shows a size column; the file is not a dir.
        assert "Startup-Sequence/" not in out


def test_ls_of_a_file_shows_just_that_file(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        out = sh.out("ls S/Startup-Sequence")
        assert "Startup-Sequence" in out


def test_ls_of_a_missing_path_is_reported(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert "no such" in sh.out("ls Nope").lower()


# ---------------------------------------------------------------------------
# Two current directories: lpwd, lcd, lls are independent of the image cwd
# ---------------------------------------------------------------------------


def test_lpwd_reports_the_local_directory(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        assert sh.out("lpwd") == str(localdir)


def test_lls_lists_the_local_directory(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        out = sh.out("lls")
        assert "note.txt" in out and "prog" in out and "sub/" in out


def test_lcd_changes_only_the_local_side(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        sh.run("cd S")                 # move the image side
        sh.run("lcd sub")              # move the local side
        assert sh.out("pwd") == "Workbench:S"          # image cwd unchanged by lcd
        assert sh.out("lpwd") == str((localdir / "sub").resolve())
        assert "inner.txt" in sh.out("lls")


def test_cd_does_not_move_the_local_side(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        sh.run("cd Prefs")
        assert sh.out("lpwd") == str(localdir)   # image cd left local alone


def test_lcd_into_a_missing_directory_is_refused(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        assert "no such directory" in sh.out("lcd nope").lower()
        assert sh.out("lpwd") == str(localdir)


# ---------------------------------------------------------------------------
# put (host -> image)
# ---------------------------------------------------------------------------


def test_put_copies_a_host_file_into_the_image_directory(rdb_populated, localdir):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target, local_cwd=localdir) as sh:
        sh.run("cd S")
        assert "put" in sh.out("put note.txt").lower()
    assert read(target, "S/note.txt") == (localdir / "note.txt").read_bytes()


def test_put_lands_in_the_current_image_directory(rdb_populated, localdir):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target, local_cwd=localdir) as sh:
        sh.run("cd C")
        sh.run("put prog")
    assert "prog" in entries(target, "C")
    assert "prog" not in entries(target, "")   # not in the root


def test_put_a_missing_host_file_is_refused(rdb_populated, localdir):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target, local_cwd=localdir) as sh:
        assert "no such file" in sh.out("put ghost.txt").lower()
    assert "ghost.txt" not in entries(target, "")


def test_put_a_host_directory_is_refused(rdb_populated, localdir):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target, local_cwd=localdir) as sh:
        assert "directory" in sh.out("put sub").lower()
    assert "sub" not in entries(target, "")


def test_put_will_not_overwrite(rdb_populated, localdir):
    target = f"{rdb_populated}:Workbench"
    before = read(target, "S/Startup-Sequence")
    # Make a host file with the same name as an existing image file.
    (localdir / "Startup-Sequence").write_bytes(b"DIFFERENT")
    with shell_session(target, local_cwd=localdir) as sh:
        sh.run("cd S")
        assert "exists" in sh.out("put Startup-Sequence").lower()
    assert read(target, "S/Startup-Sequence") == before   # untouched


# ---------------------------------------------------------------------------
# get (image -> host)
# ---------------------------------------------------------------------------


def test_get_copies_a_file_out_to_the_local_directory(rdb_populated, localdir):
    dest = localdir / "out"
    dest.mkdir()
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=dest) as sh:
        sh.run("cd S")
        sh.run("get Startup-Sequence")
    assert (dest / "Startup-Sequence").read_bytes() == read(
        f"{rdb_populated}:Workbench", "S/Startup-Sequence")


def test_get_copies_a_whole_directory_out(rdb_populated, localdir):
    dest = localdir / "tree"
    dest.mkdir()
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=dest) as sh:
        sh.run("get Prefs")
    assert (dest / "Prefs" / "Env-Archive" / "Sys" / "overscan.prefs").exists()


def test_get_a_missing_path_is_refused(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        assert "no such" in sh.out("get Nope").lower()


def test_get_will_not_overwrite_a_local_file(rdb_populated, localdir):
    dest = localdir / "out2"
    dest.mkdir()
    (dest / "Startup-Sequence").write_bytes(b"KEEP ME")
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=dest) as sh:
        sh.run("cd S")
        assert "exists" in sh.out("get Startup-Sequence").lower()
    assert (dest / "Startup-Sequence").read_bytes() == b"KEEP ME"


# ---------------------------------------------------------------------------
# cp (image -> image), metadata preserved
# ---------------------------------------------------------------------------


def test_cp_copies_a_file_within_the_image(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("cd S")
        sh.run("cp Startup-Sequence Startup-Sequence.bak")
    assert read(target, "S/Startup-Sequence.bak") == read(target, "S/Startup-Sequence")
    assert "Startup-Sequence" in entries(target, "S")   # original still there


def test_cp_preserves_protection_comment_and_time(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        # A source with deliberately non-default metadata.
        src = sh.state.vol.write_file("marker", b"payload", protect="rwe",
                                      comment="from test")
        sh.run("cp marker marker2")
    got = stat(target, "marker2")
    assert read(target, "marker2") == b"payload"
    assert got.protect_str == src.protect_str
    assert got.comment == "from test"
    assert got.mod_secs == src.mod_secs


def test_cp_a_directory_is_refused(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        # The transfer layer's own refusal ("cp copies a single file"), asserted
        # specifically so the guard is not merely shadowed by read_file rejecting a
        # directory later with a different message.
        assert "single file" in sh.out("cp S S.copy").lower()
    assert "S.copy" not in entries(target, "")


def test_cp_will_not_overwrite(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("cd C")
        assert "exists" in sh.out("cp List Dir").lower()   # Dir already exists
    # Dir is unchanged (still its own content, not a copy of List).
    assert read(target, "C/Dir") != read(target, "C/List")


def test_cp_a_missing_source_is_refused(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        assert "no such" in sh.out("cp ghost ghost2").lower()
    assert "ghost2" not in entries(target, "")


# ---------------------------------------------------------------------------
# mv (image -> image) = copy then delete
# ---------------------------------------------------------------------------


def test_mv_renames_a_file(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    before = read(target, "S/Shell-Startup")
    with shell_session(target) as sh:
        sh.run("cd S")
        sh.run("mv Shell-Startup Shell-Startup.old")
    assert "Shell-Startup" not in entries(target, "S")     # original gone
    assert read(target, "S/Shell-Startup.old") == before   # content carried across


def test_mv_moves_a_file_between_directories(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    before = read(target, "C/List")
    with shell_session(target) as sh:
        sh.run("mv C/List S/List")
    assert "List" not in entries(target, "C")
    assert read(target, "S/List") == before


def test_mv_preserves_metadata(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        src = sh.state.vol.write_file("mvsrc", b"data", protect="rwe", comment="keep me")
        sh.run("mv mvsrc mvdst")
    got = stat(target, "mvdst")
    assert got.protect_str == src.protect_str
    assert got.comment == "keep me"
    assert got.mod_secs == src.mod_secs


def test_mv_onto_an_existing_name_is_refused_and_keeps_the_source(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("cd C")
        assert "exists" in sh.out("mv List Dir").lower()
    assert "List" in entries(target, "C")     # source survived the refused move
    assert read(target, "C/Dir") != read(target, "C/List")


def test_mv_to_the_same_path_is_refused(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("cd C")
        # Assert the guard's full phrase, not a bare "same". The fixture's temp directory is
        # named after this test ("...the_same_path...") and leaks into Volume error messages
        # via the source label, so a short "same" would match even with the guard gone --
        # a vacuous assertion the mutation harness rightly flagged.
        assert "destination are the same" in sh.out("mv List List").lower()
    assert "List" in entries(target, "C")     # nothing was deleted


def test_mv_a_directory_is_refused(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        assert "directory" in sh.out("mv S S.moved").lower()
    assert "S" in entries(target, "")          # source directory untouched
    assert "S.moved" not in entries(target, "")


# ---------------------------------------------------------------------------
# rm -- file-only in the shell
# ---------------------------------------------------------------------------


def test_rm_removes_a_file(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("cd S")
        assert "removed" in sh.out("rm Shell-Startup").lower()
    assert "Shell-Startup" not in entries(target, "S")


def test_rm_refuses_a_directory(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        # The shell's own file-only refusal, worded for the shell (no "-r" to suggest),
        # asserted specifically so it is not shadowed by Volume.remove's directory guard.
        assert "files only" in sh.out("rm S").lower()
    assert "S" in entries(target, "")           # directory survived
    assert "Startup-Sequence" in entries(target, "S")


def test_rm_a_missing_file_is_reported(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        assert "no such" in sh.out("rm S/ghost").lower()


def test_rm_refuses_the_volume_root(rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        assert "root" in sh.out("rm :").lower()


# ---------------------------------------------------------------------------
# Flush: the bitmap is written after every mutation, not only on close
#
# The subtle part. amitools writes file, directory and header blocks straight through to
# the OS as it goes (it seeks constantly, and a seek flushes Python's write buffer), so the
# *tree* reflects a change immediately whether or not we flush -- checking the tree proves
# nothing about flushing. The allocation *bitmap* is different: it is only serialised by
# ADFSBitmap.write(), which happens on close() or an explicit flush(). Miss the per-command
# flush and the on-disk bitmap goes stale -- the tree shows the new file but the bitmap
# still marks its blocks free (verified: a fresh open reports the old free count), which is
# exactly the corruption note G29 warns about. amitools' own validator catches that
# disagreement, so these tests run `check` on a fresh read-only open *while the writable
# session is still open* -- before any close could paper over it -- and require it clean.
# ---------------------------------------------------------------------------


def test_a_removed_file_is_flushed_before_the_session_closes(run, rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("rm C/List")
        code, out, err = run("check", rdb_populated)   # fresh read-only open, mid-session
        assert code == 0, f"stale bitmap mid-session -- rm did not flush: {out or err}"


def test_a_put_is_flushed_before_the_session_closes(run, rdb_populated, localdir):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target, local_cwd=localdir) as sh:
        sh.run("put prog")
        code, out, err = run("check", rdb_populated)
        assert code == 0, f"stale bitmap mid-session -- put did not flush: {out or err}"


def test_a_move_is_flushed_before_the_session_closes(run, rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("mv C/List S/List")
        code, out, err = run("check", rdb_populated)
        assert code == 0, f"stale bitmap mid-session -- mv did not flush: {out or err}"


def test_a_copy_is_flushed_before_the_session_closes(run, rdb_populated):
    target = f"{rdb_populated}:Workbench"
    with shell_session(target) as sh:
        sh.run("cp C/List C/List.copy")
        code, out, err = run("check", rdb_populated)
        assert code == 0, f"stale bitmap mid-session -- cp did not flush: {out or err}"


# ---------------------------------------------------------------------------
# Partition isolation, and validity after a session
# ---------------------------------------------------------------------------


def test_a_session_on_one_partition_leaves_the_other_untouched(rdb_populated, localdir):
    # Work (partition 1) starts empty; a session on Workbench must not touch it.
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        sh.run("put note.txt")
        sh.run("rm C/List")
        sh.run("cp C/Dir C/Dir.copy")
    assert entries(f"{rdb_populated}:Work") == {}    # still empty


def test_the_volume_validates_after_a_shell_session(run, rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        sh.run("put note.txt")
        sh.run("put prog")
        sh.run("cp S/Startup-Sequence S/Startup-Sequence.bak")
        sh.run("mv note.txt note.moved")
        sh.run("rm prog")
    code, out, err = run("check", rdb_populated)
    assert code == 0, err or out


def test_work_partition_is_writable_in_its_own_session(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Work", local_cwd=localdir) as sh:
        sh.run("put note.txt")
    assert "note.txt" in entries(f"{rdb_populated}:Work")


# ---------------------------------------------------------------------------
# dispatch mechanics
# ---------------------------------------------------------------------------


def test_an_unknown_command_is_reported_not_fatal(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert "unknown command" in sh.out("frobnicate x").lower()
        # the session is still usable
        assert sh.out("pwd") == "Workbench:"


def test_an_empty_line_does_nothing(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert sh.run("") == []
        assert sh.run("   ") == []


def test_quit_sets_done(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("quit")
        assert sh.state.done is True


def test_quoted_paths_with_spaces(rdb_populated, localdir):
    """Amiga names may contain spaces, so the parser must honour quoting."""
    target = f"{rdb_populated}:Workbench"
    (localdir / "My Doc").write_bytes(b"spaced")
    with shell_session(target, local_cwd=localdir) as sh:
        sh.run('put "My Doc"')
        assert "My Doc" in entries(target, "")
        assert "removed" in sh.out('rm "My Doc"').lower()
    assert "My Doc" not in entries(target, "")


def test_commands_are_case_insensitive(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert sh.out("PWD") == "Workbench:"
        assert "S/" in sh.out("LS")


# ---------------------------------------------------------------------------
# Tab completion
#
# `complete(state, line, text)` is a pure function -- no readline -- so it is tested exactly
# like dispatch: a live state on the scratch RDB, a line buffer and the word at the cursor,
# and an assertion on the candidate list. The readline binding in run_repl is a five-line
# adapter with no logic and is not exercised here.
# ---------------------------------------------------------------------------


def test_complete_command_names_from_empty(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        cands = complete(sh.state, "", "")
        assert "cd" in cands and "put" in cands and "quit" in cands
        # aliases work when typed but are not offered as suggestions
        assert "dir" not in cands and "copy" not in cands and "q" not in cands


def test_complete_command_prefix(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert complete(sh.state, "c", "c") == ["cd", "cp"]


def test_complete_image_paths_at_root(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        cands = complete(sh.state, "ls ", "")
        assert "S/" in cands and "C/" in cands and "Prefs/" in cands
        # the WORKBENCH_FILES root is all directories, so every candidate is slash-suffixed
        assert cands and all(c.endswith("/") for c in cands)


def test_complete_image_nested(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert complete(sh.state, "ls S/S", "S/S") == ["S/Shell-Startup", "S/Startup-Sequence"]


def test_complete_image_absolute_colon_ignores_cwd(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd S")
        assert complete(sh.state, "ls :Pre", ":Pre") == [":Prefs/"]


def test_complete_is_case_insensitive(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert complete(sh.state, "ls too", "too") == ["Tools/"]


def test_complete_relative_to_the_image_cwd(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd S")
        assert complete(sh.state, "ls ", "") == ["Shell-Startup", "Startup-Sequence"]


def test_complete_up_one_level(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        sh.run("cd S")
        cands = complete(sh.state, "cd /", "/")
        assert "/S/" in cands and "/C/" in cands


def test_complete_offers_nothing_for_pathless_commands(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        assert complete(sh.state, "pwd ", "") == []
        assert complete(sh.state, "help ", "") == []


def test_complete_command_vs_argument_boundary(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        # no trailing space: still completing the command word
        assert complete(sh.state, "cd", "cd") == ["cd"]
        # trailing space: now completing the first argument, which is a path
        assert "S/" in complete(sh.state, "cd ", "")


def test_complete_cp_second_argument_is_an_image_path(rdb_populated):
    with shell_session(f"{rdb_populated}:Workbench") as sh:
        cands = complete(sh.state, "cp C/List S/", "S/")
        assert "S/Shell-Startup" in cands


def test_complete_local_paths(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        cands = complete(sh.state, "put ", "")
        assert "note.txt" in cands and "prog" in cands and "sub/" in cands
        # put's argument is a host path, so image entries must not leak in
        assert "S/" not in cands


def test_complete_local_prefix_and_nested(rdb_populated, localdir):
    with shell_session(f"{rdb_populated}:Workbench", local_cwd=localdir) as sh:
        assert complete(sh.state, "lls no", "no") == ["note.txt"]
        assert complete(sh.state, "lls sub/", "sub/") == ["sub/inner.txt"]
