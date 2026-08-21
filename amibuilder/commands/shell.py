"""`amibuilder shell` -- an interactive REPL over one open image.

Point it at an image and it gives an AmigaDOS-flavoured prompt, so walking a drive and
moving files around stops being "retype the twenty-character source spec on every
command." It is a second presentation layer over the same `Volume` the CLI commands wrap;
the host<->image work goes through `commands/transfer.py`, exactly as `get`/`cp` do, so the
two front ends cannot drift.

The design in three pieces (docs/KIP-FFS-PLAN.md, Phase 6):

* **Two current directories, FTP-style.** `image_cwd` is where `cd`/`ls`/`rm`/`cp`/`mv`
  act; `local_cwd` is where `lcd`/`lls`/`lpwd` act and the local side of `put`/`get`. The
  local commands never touch `Volume` at all.
* **No overwrites, anywhere.** `put`, `get`, in-image `cp` and `mv` refuse and do nothing
  if the destination already exists. There is no `--force` in the shell; overwriting is
  too dangerous to expose here yet. `rm`, `cp`, `mv` and `put` are file-only; `get` is the
  one verb that takes a directory (recursively), matching the CLI.
* **Flush after every mutating command.** The allocation bitmap is only written on
  `close()` (notes G29); a CLI command closes per call, but the shell holds the volume open
  all session, so `Volume.flush()` is called after each `put`/`cp`/`mv`/`rm`. The volume is
  opened inside a context manager, so quit, EOF and Ctrl-C all close cleanly regardless.

Structure is a pure `dispatch(state, line) -> (lines, new_state)` plus a thin `run_repl`
that only reads a line and prints. Everything testable lives in `dispatch`; the loop
carries no logic. Tab completion is deliberately not here yet -- it is an orthogonal
follow-up that plugs into `Volume.listdir`/`os.scandir` and touches no command logic.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..errors import AmibuilderError, ImageError, NotFoundError, UsageError
from ..render import Output, human_bytes
from ..volume import Volume
from . import opened_volume, transfer


@dataclass(frozen=True)
class ShellState:
    """Everything a command needs, and the only thing `dispatch` threads through.

    Frozen so a handler cannot mutate shared state by accident: navigation returns a new
    state via `dataclasses.replace`. `vol` is a live open handle and is shared by
    reference -- it is the one thing that is genuinely stateful, and closing it is the
    caller's job, not a command's.
    """

    vol: Volume
    #: Normalised, volume-relative, '/'-separated, no leading slash. "" is the root.
    image_cwd: str = ""
    local_cwd: Path = Path(".")
    #: Set by quit/exit so the loop knows to stop. Never observed by tests of behaviour.
    done: bool = False


Handler = Callable[[ShellState, list[str]], "tuple[list[str], ShellState]"]


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------


def resolve_image(cwd: str, arg: str) -> str:
    """Resolve an in-shell path against the image cwd, AmigaDOS-style.

    * a bare name descends: `S` -> cwd/S
    * a leading `:` is volume-absolute: `:S/Startup-Sequence` from the root
    * `:` alone is the volume root
    * a leading `/` goes up one level, `//` up two, and so on -- the Amiga idiom
    * redundant separators inside the path collapse (a mid-path `//` is not "up"; only a
      *leading* run of slashes pops, which is the behaviour the plan specifies and the one
      that does not surprise)

    Going up from the root stays at the root rather than erroring, matching how `cd ..`
    behaves at `/` in a Unix shell. The result is normalised: volume-relative, no leading
    slash, ready to hand to `Volume`.
    """
    arg = arg.strip()
    if arg == ":":
        return ""
    if arg.startswith(":"):
        parts: list[str] = []
        rest = arg[1:]
    else:
        parts = cwd.split("/") if cwd else []
        rest = arg

    # A leading run of slashes pops one level each; this is what makes `/` mean "up".
    i = 0
    while i < len(rest) and rest[i] == "/":
        if parts:
            parts.pop()
        i += 1
    rest = rest[i:]

    for component in rest.split("/"):
        if component:  # empties here are redundant separators, not "up"
            parts.append(component)
    return "/".join(parts)


def _resolve_local(cwd: Path, arg: str) -> Path:
    p = Path(arg).expanduser()
    return p if p.is_absolute() else cwd / p


def _amiga_path(vol: Volume, rel: str) -> str:
    """`Volume:path` for display and messages."""
    return f"{vol.name}:{rel}"


# ---------------------------------------------------------------------------
# listing helper
# ---------------------------------------------------------------------------


def _format_listing(rows: list[tuple[str, bool, int]]) -> list[str]:
    """Render `(name, is_dir, size)` rows: directories flagged with a trailing slash,
    files aligned with a human-readable size."""
    if not rows:
        return ["(empty)"]
    width = max(len(name) for name, _, _ in rows)
    out: list[str] = []
    for name, is_dir, size in rows:
        if is_dir:
            out.append(f"{name}/")
        else:
            out.append(f"{name.ljust(width)}  {human_bytes(size)}")
    return out


# ---------------------------------------------------------------------------
# image-side commands
# ---------------------------------------------------------------------------


def _cmd_pwd(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    return [_amiga_path(state.vol, state.image_cwd)], state


def _cmd_cd(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) > 1:
        raise UsageError("usage: cd [PATH]")
    target = "" if not argv else resolve_image(state.image_cwd, argv[0])
    if target and not state.vol.exists(target):
        raise UsageError(f"no such directory: {_amiga_path(state.vol, target)}")
    if target and not state.vol.is_dir(target):
        raise UsageError(f"not a directory: {_amiga_path(state.vol, target)}")
    return [], replace(state, image_cwd=target)


def _cmd_ls(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) > 1:
        raise UsageError("usage: ls [PATH]")
    target = state.image_cwd if not argv else resolve_image(state.image_cwd, argv[0])
    entry = state.vol.stat(target)  # NotFoundError for a missing path
    if entry.is_dir:
        rows = [(e.name, e.is_dir, e.size) for e in state.vol.listdir(target)]
    else:
        rows = [(entry.name, False, entry.size)]
    return _format_listing(rows), state


def _cmd_rm(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) != 1:
        raise UsageError("usage: rm PATH   (files only in the shell; no directories)")
    rel = resolve_image(state.image_cwd, argv[0])
    if not rel:
        raise UsageError("refusing to remove the volume root")
    entry = state.vol.stat(rel)  # NotFoundError for a missing path
    if entry.is_dir:
        raise UsageError(
            f"{_amiga_path(state.vol, rel)} is a directory; the shell removes files only"
        )
    state.vol.remove(rel, recursive=False)
    state.vol.flush()
    return [f"removed {_amiga_path(state.vol, rel)}"], state


def _cmd_cp(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) != 2:
        raise UsageError("usage: cp SOURCE DEST   (files only)")
    src = resolve_image(state.image_cwd, argv[0])
    dst = resolve_image(state.image_cwd, argv[1])
    entry = transfer.copy_in_image(state.vol, src, dst, overwrite=False)
    state.vol.flush()
    size = human_bytes(entry.size)
    return [f"copied {_amiga_path(state.vol, src)} -> "
            f"{_amiga_path(state.vol, dst)} ({size})"], state


def _cmd_mv(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) != 2:
        raise UsageError("usage: mv SOURCE DEST   (files only)")
    src = resolve_image(state.image_cwd, argv[0])
    dst = resolve_image(state.image_cwd, argv[1])
    if src == dst:
        raise UsageError("source and destination are the same")
    # Copy first: if the destination exists, copy_in_image refuses and the source is never
    # touched. amitools has no node-level rename, so a move is a copy followed by a delete
    # of the original -- which is why it needs room for both copies at once, briefly.
    transfer.copy_in_image(state.vol, src, dst, overwrite=False)
    state.vol.remove(src, recursive=False)
    state.vol.flush()
    return [f"moved {_amiga_path(state.vol, src)} -> {_amiga_path(state.vol, dst)}"], state


# ---------------------------------------------------------------------------
# host <-> image transfer
# ---------------------------------------------------------------------------


def _cmd_put(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) != 1:
        raise UsageError("usage: put LOCAL-FILE")
    host = _resolve_local(state.local_cwd, argv[0])
    dest = resolve_image(state.image_cwd, host.name)
    entry = transfer.put_file(state.vol, host, dest, overwrite=False)
    state.vol.flush()
    return [f"put {host} -> {_amiga_path(state.vol, dest)} "
            f"({human_bytes(entry.size)})"], state


def _cmd_get(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) != 1:
        raise UsageError("usage: get DISK-PATH")
    src = resolve_image(state.image_cwd, argv[0])
    lines: list[str] = []
    written = transfer.extract_path(
        state.vol, src, state.local_cwd, force=False, emit=lines.append,
    )
    total = sum(w["bytes"] for w in written)
    lines.append(f"got {len(written)} file(s), {human_bytes(total)} into {state.local_cwd}")
    return lines, state


# ---------------------------------------------------------------------------
# local-side commands (os / pathlib only, never Volume)
# ---------------------------------------------------------------------------


def _cmd_lpwd(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    return [str(state.local_cwd)], state


def _cmd_lcd(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) > 1:
        raise UsageError("usage: lcd [DIR]")
    target = Path.home() if not argv else _resolve_local(state.local_cwd, argv[0])
    if not target.exists():
        raise UsageError(f"no such directory: {target}")
    if not target.is_dir():
        raise UsageError(f"not a directory: {target}")
    return [], replace(state, local_cwd=target.resolve())


def _cmd_lls(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    if len(argv) > 1:
        raise UsageError("usage: lls [DIR]")
    target = state.local_cwd if not argv else _resolve_local(state.local_cwd, argv[0])
    if not target.exists():
        raise UsageError(f"no such file or directory: {target}")
    if target.is_dir():
        try:
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            raise UsageError(f"{target}: cannot list ({e})") from e
        rows = [(p.name, p.is_dir(), p.stat().st_size if p.is_file() else 0)
                for p in children]
    else:
        rows = [(target.name, False, target.stat().st_size)]
    return _format_listing(rows), state


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


_HELP = [
    "image commands (act on the current image directory):",
    "  pwd                 show the image directory",
    "  cd [PATH]           change it  (/=up, :=root, leading : = from root)",
    "  ls [PATH]           list it",
    "  cp SRC DST          copy a file within the image",
    "  mv SRC DST          move/rename a file within the image",
    "  rm PATH             delete a file  (files only; no directories)",
    "  put LOCAL-FILE      copy a host file in, by name, to the image directory",
    "  get DISK-PATH       copy a file or directory out to the local directory",
    "",
    "local commands (act on the host working directory):",
    "  lpwd                show the local directory",
    "  lcd [DIR]           change it  (no arg: home)",
    "  lls [DIR]           list it",
    "",
    "  help, ?             this text",
    "  quit, exit, q       leave (the image is flushed and closed cleanly)",
    "",
    "nothing is ever overwritten: a put/get/cp/mv onto an existing name is refused.",
]


def _cmd_help(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    return list(_HELP), state


def _cmd_quit(state: ShellState, argv: list[str]) -> tuple[list[str], ShellState]:
    return ["bye"], replace(state, done=True)


_COMMANDS: dict[str, Handler] = {
    "pwd": _cmd_pwd,
    "cd": _cmd_cd,
    "ls": _cmd_ls,
    "dir": _cmd_ls,
    "rm": _cmd_rm,
    "delete": _cmd_rm,
    "cp": _cmd_cp,
    "copy": _cmd_cp,
    "mv": _cmd_mv,
    "rename": _cmd_mv,
    "put": _cmd_put,
    "get": _cmd_get,
    "lpwd": _cmd_lpwd,
    "lcd": _cmd_lcd,
    "lls": _cmd_lls,
    "help": _cmd_help,
    "?": _cmd_help,
    "quit": _cmd_quit,
    "exit": _cmd_quit,
    "q": _cmd_quit,
}


# ---------------------------------------------------------------------------
# tab completion -- a pure function, plus a thin readline adapter in run_repl
#
# The whole point is that the logic here is a plain function of (state, line, text) with no
# readline in sight, so it is unit-testable exactly like dispatch. The readline callback is
# a five-line adapter that hands it the line buffer and returns candidates one at a time.
# ---------------------------------------------------------------------------

#: Offered as command completions. Aliases (dir/copy/delete/rename/q/?/exit) still *work*
#: when typed, but suggesting them too would just clutter the list.
_COMPLETABLE_COMMANDS = sorted(
    ["pwd", "cd", "ls", "rm", "cp", "mv", "put", "get", "lpwd", "lcd", "lls", "help", "quit"]
)

#: Commands whose arguments are in-image paths, and whose are host paths. Everything else
#: (pwd, lpwd, help, quit and friends) takes no path, so offers nothing.
_IMAGE_PATH_COMMANDS = frozenset(
    {"cd", "ls", "dir", "rm", "delete", "cp", "copy", "mv", "rename", "get"})
_LOCAL_PATH_COMMANDS = frozenset({"lcd", "lls", "put"})


def complete(state: ShellState, line: str, text: str) -> list[str]:
    """Completion candidates for `text`, the word at the cursor, given the whole `line`.

    Pure: the only side effect is reading directory listings (the image via `Volume`, the
    host via the filesystem), which is exactly what a completer must do. Returns the full
    replacement strings for `text` -- readline's completer delimiter is set to whitespace
    only, so `text` is the entire path fragment and a candidate is the whole fragment
    completed, directories carrying a trailing '/'.

    The word's position decides what is offered: the first word is a command; a later word
    is a path, in-image or host depending on the command.
    """
    preceding = line[: len(line) - len(text)] if text else line
    prior = preceding.split()
    if not prior:
        low = text.lower()
        return [c for c in _COMPLETABLE_COMMANDS if c.startswith(low)]

    cmd = prior[0].lower()
    if cmd in _IMAGE_PATH_COMMANDS:
        return _complete_image_path(state, text)
    if cmd in _LOCAL_PATH_COMMANDS:
        return _complete_local_path(state, text)
    return []


def _split_fragment(text: str) -> tuple[str, str, str]:
    """Split a path fragment into (directory-argument, partial-leaf, reattach-prefix).

    The directory argument is everything up to and including the last '/', resolved the
    same way `cd` resolves it -- so a leading ':' or '/' in the fragment behaves during
    completion exactly as it does when the path is entered. The prefix is what gets put
    back in front of each matched name so the completed string reads as the user typed it.
    """
    slash = text.rfind("/")
    if slash >= 0:
        prefix = text[: slash + 1]     # "S/", ":Prefs/", "/", "//"
        return prefix, text[slash + 1:], prefix
    if text.startswith(":"):
        return ":", text[1:], ":"      # ":leaf" -- absolute, at the root
    return "", text, ""                # "leaf" -- relative to the current directory


def _complete_image_path(state: ShellState, text: str) -> list[str]:
    dir_arg, leaf, prefix = _split_fragment(text)
    base = resolve_image(state.image_cwd, dir_arg) if dir_arg else state.image_cwd
    try:
        entries = state.vol.listdir(base)
    except (NotFoundError, ImageError):
        return []
    leaf_low = leaf.lower()  # FFS is case-insensitive; match loosely, return the real name
    out = [prefix + e.name + ("/" if e.is_dir else "")
           for e in entries if e.name.lower().startswith(leaf_low)]
    return sorted(out, key=str.lower)


def _complete_local_path(state: ShellState, text: str) -> list[str]:
    slash = text.rfind("/")
    if slash >= 0:
        prefix = text[: slash + 1]
        leaf = text[slash + 1:]
        head = os.path.expanduser(prefix)
        base = Path(head) if os.path.isabs(head) else state.local_cwd / head
    else:
        prefix, leaf, base = "", text, state.local_cwd
    try:
        children = list(os.scandir(base))
    except OSError:
        return []
    leaf_low = leaf.lower()
    out = [prefix + e.name + ("/" if e.is_dir() else "")
           for e in children if e.name.lower().startswith(leaf_low)]
    return sorted(out, key=str.lower)


# ---------------------------------------------------------------------------
# dispatch -- the whole testable surface
# ---------------------------------------------------------------------------


def dispatch(state: ShellState, line: str) -> tuple[list[str], ShellState]:
    """Run one command line. Never raises for an expected failure: a refusal comes back as
    an output line so the REPL keeps going and a test can assert on it.

    Parsing is `shlex`, so quoted names with spaces work (`rm "My File"`), which Amiga
    names need.
    """
    try:
        argv = shlex.split(line, posix=True)
    except ValueError as e:
        return [f"parse error: {e}"], state
    if not argv:
        return [], state

    name, rest = argv[0].lower(), argv[1:]
    handler = _COMMANDS.get(name)
    if handler is None:
        return [f"unknown command: {argv[0]}  (try 'help')"], state
    try:
        return handler(state, rest)
    except AmibuilderError as e:
        # Expected, reportable failures become output, not a dead session.
        return [str(e)], state


# ---------------------------------------------------------------------------
# the loop -- thin, no logic, the only part that needs a terminal
# ---------------------------------------------------------------------------


class _Completer:
    """readline adapter around the pure `complete()`.

    Holds the live shell state -- updated after every command, since the current directory
    moves -- and caches one line's candidates across the successive state-index calls
    readline makes for a single completion. All the logic is in `complete()`; this only
    plumbs the line buffer in and the candidates out.
    """

    def __init__(self, state: ShellState):
        self.state = state
        self._matches: list[str] = []

    def __call__(self, text: str, index: int) -> str | None:
        if index == 0:
            try:
                import readline

                self._matches = complete(self.state, readline.get_line_buffer(), text)
            except Exception:  # noqa: BLE001 - a completion error must never break input
                self._matches = []
        return self._matches[index] if index < len(self._matches) else None


def _install_readline(completer: _Completer) -> None:
    """Bind history and tab completion, best-effort. A missing or quirky readline just
    means no completion, never a crash.

    macOS ships libedit under the name `readline`, where `parse_and_bind("tab: complete")`
    is silently ignored; the libedit incantation is `bind ^I rl_complete`. Detect which one
    is loaded from the module docstring and bind accordingly.
    """
    try:
        import readline
    except Exception:  # noqa: BLE001 - readline is a nicety, never required
        return
    readline.set_completer(completer)
    # Whitespace-only delimiters, so the word being completed is the whole path fragment
    # (slashes and colons included) rather than just the segment after the last '/'.
    readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def _prompt(state: ShellState) -> str:
    return f"{state.vol.name}:{state.image_cwd}> "


def run_repl(state: ShellState, *, intro: bool = True) -> ShellState:
    """Read-eval-print until quit or EOF. Carries no command logic -- it reads a line,
    calls `dispatch`, prints the result. Ctrl-C cancels the current line; Ctrl-D quits."""
    completer = _Completer(state)
    _install_readline(completer)
    if intro:
        print(f"amibuilder shell -- {state.vol.name}. 'help' for commands, 'quit' to leave.")
    while not state.done:
        try:
            line = input(_prompt(state))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("^C")
            continue
        lines, state = dispatch(state, line)
        completer.state = state  # keep completion current as the working directory moves
        for text in lines:
            print(text)
    return state


def cmd_shell(args: Any, out: Output) -> int:
    """CLI entry: open the addressed volume writable and run the REPL.

    The volume is opened inside the context manager, so however the session ends -- `quit`,
    Ctrl-D, Ctrl-C, or an unexpected error -- `Volume.close()` runs and flushes the bitmap.
    Per-command `flush()` keeps the on-disk state current *during* the session; this is the
    final backstop.
    """
    with opened_volume(args, writable=True) as (_, vol):
        run_repl(ShellState(vol=vol, image_cwd="", local_cwd=Path.cwd()))
    return 0


__all__ = ["ShellState", "complete", "dispatch", "resolve_image", "run_repl", "cmd_shell"]
