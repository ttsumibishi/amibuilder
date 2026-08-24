"""Host <-> image transfer, shared by the CLI commands and the interactive shell.

The point of this module is that `get`/`put`/in-image `cp` exist in exactly one place. The
`cli.py` commands and `commands/shell.py` are two presentation layers over the same
`Volume`; the orchestration that copies bytes across the host boundary -- and the
non-negotiable "destination must not exist / source must exist" rule -- lives here so the
two presentations cannot drift apart or enforce that rule differently.

Nothing here knows about argparse or the REPL. Progress is reported through an `emit`
callback (the CLI passes `Output.line`; the shell appends to its own buffer), and options
are plain keyword arguments rather than an `args` namespace. Everything in-image goes
through `Volume`, which owns the timestamp, naming and no-overwrite discipline; the only
host-filesystem access is here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ..errors import ImageError, UsageError
from ..render import human_bytes
from ..volume import Entry, Volume

#: Progress sink. The CLI passes `out.line`; the shell passes a list's `append`.
Emit = Callable[[str], None]

#: A path carrying one of these is a wildcard pattern; anything else is a literal name and
#: keeps the single-item behaviour, hard errors and all. Shared so the CLI `get` and the
#: interactive shell agree on what counts as a glob.
GLOB_CHARS = frozenset("*?[")


def is_glob(text: str) -> bool:
    return any(c in GLOB_CHARS for c in text)


def split_glob(pattern: str) -> tuple[str, str]:
    """Split an image glob into `(directory to list, leaf pattern)`.

    Volume-relative and plain: the directory part is a literal path and only the last
    component may hold wildcards. Simpler than the shell's split, which resolves the
    directory against an interactive working directory -- a CLI path already names the
    volume separately (`card.hdf:Work`), so `Volume` normalises the rest. Shared by the
    `get` and `rm` commands so they agree on where a pattern may appear.
    """
    slash = pattern.rfind("/")
    if slash >= 0:
        return pattern[:slash], pattern[slash + 1:]
    return "", pattern


def _noop(_text: str) -> None:
    pass


# ---------------------------------------------------------------------------
# image -> host (get)
# ---------------------------------------------------------------------------


def extract_path(vol: Volume, src: str, dest: Path, *, force: bool = False,
                 dry_run: bool = False, preserve_times: bool = False,
                 verbose: bool = False, emit: Emit = _noop) -> list[dict]:
    """Copy a file or a whole subtree out of the image to the host.

    Returns one record per file written (`{path, target, bytes}`), so a caller can total
    the bytes and render its own summary. A directory source is copied recursively; a file
    source lands at `dest` (or inside it, if `dest` is an existing directory) -- the same
    rule `cp` uses. This is the one verb that accepts a directory.
    """
    entry = vol.stat(src)  # raises NotFoundError for a missing source
    if entry.is_dir:
        return _get_tree(vol, entry, dest, force=force, dry_run=dry_run,
                         preserve_times=preserve_times, verbose=verbose, emit=emit)
    target = _file_dest(entry, dest)
    return [_get_file(vol, entry, target, force=force, dry_run=dry_run,
                      preserve_times=preserve_times, verbose=verbose, emit=emit)]


def _file_dest(entry: Entry, dest: Path) -> Path:
    """Where a single extracted file lands.

    A destination that exists as a directory receives the file by name; anything else is
    treated as the target filename, matching `cp`.
    """
    return dest / entry.name if dest.is_dir() else dest


def _safe_name(name: str) -> str:
    """Make an Amiga filename safe on a host filesystem.

    Amiga names may contain characters that are legal there and hostile here -- most
    importantly they may be absolute-looking or contain path separators after decoding.
    Anything suspicious is replaced rather than rejected, so one odd name in a tree does
    not abort the whole extraction.
    """
    cleaned = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    if cleaned in ("", ".", ".."):
        cleaned = "_" + cleaned
    return cleaned


def _get_tree(vol: Volume, root: Entry, dest: Path, *, force: bool, dry_run: bool,
              preserve_times: bool, verbose: bool, emit: Emit) -> list[dict]:
    base = dest / _safe_name(root.name) if root.path else dest
    written: list[dict] = []
    for dirpath, _dirs, files in vol.walk(root.path):
        rel = dirpath[len(root.path):].strip("/") if root.path else dirpath
        target_dir = base / Path(*[_safe_name(p) for p in rel.split("/")]) if rel else base
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.is_link:
                emit(f"  skip (link): {f.path}")
                continue
            written.append(_get_file(vol, f, target_dir / _safe_name(f.name),
                                     force=force, dry_run=dry_run,
                                     preserve_times=preserve_times, verbose=verbose,
                                     emit=emit))
    return written


def _get_file(vol: Volume, entry: Entry, target: Path, *, force: bool, dry_run: bool,
              preserve_times: bool, verbose: bool, emit: Emit) -> dict:
    if target.exists() and not force and not dry_run:
        raise ImageError(f"{target} exists. Pass --force to overwrite.")
    data = b"" if dry_run else vol.read_file(entry.path)
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if preserve_times and entry.mod_secs:
            _apply_mtime(target, entry)
    if verbose or dry_run:
        prefix = "would write" if dry_run else "wrote"
        emit(f"  {prefix} {target} ({human_bytes(entry.size)})")
    return {"path": entry.path, "target": str(target), "bytes": entry.size}


def _apply_mtime(target: Path, entry: Entry) -> None:
    """Set the host mtime from the Amiga timestamp.

    The Amiga value is naive local wall clock (see amibuilder.timestamps), so it is
    interpreted as local time here. That is the only reading that keeps the displayed
    time the same on both sides.
    """
    import time

    from .. import timestamps

    when = timestamps.to_datetime(entry.mod_secs)
    try:
        stamp = time.mktime(when.timetuple())
    except (OverflowError, ValueError):
        return
    os.utime(target, (stamp, stamp))


# ---------------------------------------------------------------------------
# host -> image (put)
# ---------------------------------------------------------------------------


def put_file(vol: Volume, host: Path, dest: str, *, overwrite: bool = False,
             preserve_times: bool = False) -> Entry:
    """Write one host file into the image at `dest`.

    Files only: a host directory is refused here rather than silently copied shallowly.
    The recursive host->image copy is `cp`'s job on the command line, deliberately not the
    shell's (the shell keeps `put` to a single file so there is never a surprise about how
    much a one-word command moved).

    The no-overwrite rule is enforced by `Volume.write_file(replace=overwrite)`, which
    raises rather than clobbering an existing entry -- the shell always passes
    `overwrite=False`, so a `put` onto an existing name is a clear error, never a
    silent replace.
    """
    if not host.exists():
        raise UsageError(f"{host}: no such file on the host")
    if host.is_dir():
        raise UsageError(f"{host} is a directory; put copies a single file")
    if not host.is_file():
        raise UsageError(f"{host} is not a regular file")
    try:
        data = host.read_bytes()
    except OSError as e:
        raise UsageError(f"{host}: cannot read ({e})") from e

    stamp: dict[str, int] = {}
    if preserve_times:
        from .. import timestamps
        try:
            secs, ticks = timestamps.from_unix(host.stat().st_mtime)
            if secs > 0:
                stamp = {"secs": secs, "ticks": ticks}
        except (OSError, OverflowError, ValueError):
            stamp = {}

    return vol.write_file(dest, data, replace=overwrite, parents=False, **stamp)


# ---------------------------------------------------------------------------
# image -> image (cp / the copy half of mv)
# ---------------------------------------------------------------------------


def copy_in_image(vol: Volume, src: str, dst: str, *, overwrite: bool = False) -> Entry:
    """Copy one file to another path inside the same volume, metadata and all.

    The source's protection bits, comment and modification time are carried across, so a
    copy or a rename does not silently reset them -- a rename that changed a file's date
    would be a nasty surprise on a volume whose whole purpose is faithful snapshots.
    `Entry.protect_str` round-trips through `Volume.parse_protect` for every real AmigaDOS
    protection value, so passing it back through `write_file` reproduces the bits exactly.

    Files only (directories and links are refused), and the no-overwrite rule is again the
    `write_file(replace=overwrite)` one.
    """
    entry = vol.stat(src)  # raises NotFoundError for a missing source
    if entry.is_dir:
        raise ImageError(f"{vol.name}:{entry.path} is a directory; cp copies a single file")
    if entry.is_link:
        raise ImageError(f"{vol.name}:{entry.path} is a link, which cp cannot reproduce")
    data = vol.read_file(entry.path)
    return vol.write_file(
        dst, data, replace=overwrite, parents=False,
        protect=entry.protect_str,
        comment=entry.comment or None,
        # mod_secs == 0 means "no timestamp on disk"; passing it through would date the
        # copy to 1978 rather than leaving write_file to stamp it now.
        secs=entry.mod_secs or None,
        ticks=entry.mod_ticks,
    )
