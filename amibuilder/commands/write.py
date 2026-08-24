"""`cp` and `mkdir` -- putting host files into an image.

The mirror of `extract.py`, and the reason the transfer-drive dance in
`utils/scripts/make-transfer-hdf.sh` exists: until now the only thing that could write a
host file into an HDF was xdftool, which meant shelling out to another tool and trusting
its exit code. These write through `Volume`, so the timestamp and naming discipline in
`amibuilder.volume` applies.

Two argument conventions, and the tension between them is deliberate:

* `cp` puts the destination last (`cp FILE... IMAGE`), because that is what every user's
  fingers already do.
* `mkdir` puts the image first (`mkdir IMAGE PATH...`), matching every read command.

What both keep is the rule from `cli.py`: **a path inside an image is always a separate
argument**, never glued onto the spec. `card.hdf:Work` is unambiguously a volume; if a path
could be appended, `card.hdf:Work` would be either a volume named Work or a directory named
Work inside partition 0, with no way to say which was meant.

Nothing is written until the whole copy has been resolved and checked. A name too long for
the filesystem, a tree that will not fit, or a collision that needs `--force` is a message
while the volume is still untouched -- rather than a half-populated volume, which is what
notes G1 warns about.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from .. import timestamps
from ..errors import ImageError, NotFoundError, UsageError
from ..render import Output, human_bytes
from ..volume import COMMENT_LIMIT, Volume, normalise
from . import opened_volume, transfer

#: Host names never copied: metadata a host filesystem or archiver left behind, which
#: would be noise on an Amiga volume and would spend name-length budget saying nothing.
SKIP_NAMES = frozenset({".DS_Store", "__MACOSX", ".Spotlight-V100", ".fseventsd",
                        ".Trashes", ".localized", ".AppleDouble"})


class Item(NamedTuple):
    """One entry to create, resolved before anything is written."""

    host: Path
    #: Volume-relative destination path.
    dest: str
    size: int
    is_dir: bool


# ---------------------------------------------------------------------------
# cp
# ---------------------------------------------------------------------------


def cmd_cp(args: Any, out: Output) -> int:
    sources = _check_sources(args)

    # A dry run opens read-only, so it also never triggers the raw-device write
    # confirmation -- `--dry-run` on a device is genuinely harmless.
    with opened_volume(args, writable=not args.dry_run, attr="image") as (_, vol):
        into = _destination_dir(vol, args)
        items, skipped = _plan(sources, into)
        _preflight(vol, items, args)

        for path, why in skipped:
            out.line(f"  skip ({why}): {path}")

        if args.dry_run:
            return _report(vol, items, skipped, into, out, dry_run=True)

        for item in items:
            _write_one(vol, item, args, out)
        _restamp_directories(vol, items, args)
        return _report(vol, items, skipped, into, out, dry_run=False)


def _check_sources(args: Any) -> list[Path]:
    """Validate the host side before opening the image at all."""
    sources = [Path(s) for s in args.files]
    for src in sources:
        if not src.exists():
            raise UsageError(f"{src}: no such file or directory on the host")
        if src.is_dir():
            if not args.recursive:
                raise UsageError(f"{src} is a directory; pass -r to copy it")
        elif not src.is_file():
            raise UsageError(
                f"{src} is neither a regular file nor a directory, so there is nothing "
                f"meaningful to write into an Amiga volume"
            )
    return sources


def _destination_dir(vol: Volume, args: Any) -> str:
    """The volume-relative directory copies land in, created if `--parents` allows it."""
    rel = normalise(args.to or "")
    if not rel:
        return ""
    name = vol.info().name
    if vol.exists(rel):
        if not vol.is_dir(rel):
            raise UsageError(f"--to {args.to}: {name}:{rel} is a file, not a directory")
        return rel
    if not args.parents:
        raise UsageError(
            f"--to {args.to}: no such directory on {name}:. Create it with "
            f"'amibuilder mkdir {args.image} {rel}', or pass --parents"
        )
    for component in rel.split("/"):
        vol.check_name(component)
    if not args.dry_run:
        vol.mkdir(rel, parents=True, exist_ok=True)
    return rel


def _walk_host(root: Path) -> Iterator[tuple[Path, bool, str | None]]:
    """Yield `(path, is_dir, skip_reason)` under `root`, parents before their children.

    Symlinks are reported and skipped rather than followed. A host symlink has no
    AmigaDOS equivalent that could be written faithfully; following one silently turns a
    link into a duplicate copy, and a link pointing out of the tree copies something the
    user did not ask for. Device nodes, sockets and fifos are skipped for the same reason.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            raise UsageError(f"{current}: cannot list ({e})") from e
        descend = []
        for child in children:
            if child.name in SKIP_NAMES:
                continue
            if child.is_symlink():
                yield child, False, "symlink"
            elif child.is_dir():
                yield child, True, None
                descend.append(child)
            elif child.is_file():
                yield child, False, None
            else:
                yield child, False, "not a regular file"
        stack.extend(reversed(descend))


def _plan(sources: list[Path], into: str) -> tuple[list[Item], list[tuple[Path, str]]]:
    """Resolve every source into `(items to create, things skipped)`."""
    items: list[Item] = []
    skipped: list[tuple[Path, str]] = []
    seen: dict[str, str] = {}

    def add(host: Path, dest: str, size: int, is_dir: bool) -> None:
        # FFS matches names without regard to case, so two sources differing only in case
        # would collide on the volume -- one silently overwriting the other.
        key = dest.casefold()
        clash = seen.get(key)
        if clash is not None:
            raise UsageError(
                f"{clash!r} and {dest!r} both land on the same name; FFS ignores case, so "
                f"one would overwrite the other"
            )
        seen[key] = dest
        items.append(Item(host, dest, size, is_dir))

    for src in sources:
        base = f"{into}/{src.name}" if into else src.name
        if not src.is_dir():
            add(src, base, src.stat().st_size, False)
            continue
        add(src, base, 0, True)
        for path, is_dir, why in _walk_host(src):
            if why is not None:
                skipped.append((path, why))
                continue
            rel = path.relative_to(src).as_posix()
            add(path, f"{base}/{rel}", 0 if is_dir else path.stat().st_size, is_dir)
    return items, skipped


def _preflight(vol: Volume, items: list[Item], args: Any) -> None:
    """Refuse the whole copy for anything that would otherwise fail partway through."""
    info = vol.info()

    # Names first: a name that is too long is the user's typo, whereas a capacity refusal
    # is a fact about the volume. Reporting the typo is the more useful of the two.
    for item in items:
        for component in item.dest.split("/"):
            vol.check_name(component)
    if args.comment:
        vol.check_comment(args.comment)
    if args.protect:
        vol.parse_protect(args.protect)  # raises UsageError on a bad spec

    needed = 0
    for item in items:
        exists = vol.exists(item.dest)
        if item.is_dir:
            if exists and not vol.is_dir(item.dest):
                raise ImageError(
                    f"{info.name}:{item.dest} is a file, so the directory {item.host} "
                    f"cannot be created there"
                )
            needed += 0 if exists else 1
            continue
        if exists:
            if vol.is_dir(item.dest):
                raise ImageError(
                    f"{info.name}:{item.dest} is a directory, so {item.host} cannot "
                    f"replace it"
                )
            if not args.force:
                raise ImageError(
                    f"{info.name}:{item.dest} already exists. Pass --force to overwrite."
                )
            # Replacing frees what the old copy held.
            needed -= vol.blocks_for(vol.stat(item.dest).size)
        needed += vol.blocks_for(item.size)

    if needed > info.free_blocks:
        short = (needed - info.free_blocks) * info.block_size
        raise ImageError(
            f"{info.name}: this copy needs {needed} block(s) but {info.free_blocks} are "
            f"free -- {human_bytes(short)} short"
        )


def _write_one(vol: Volume, item: Item, args: Any, out: Output) -> None:
    stamp = _stamp_kwargs(item.host, args)
    if item.is_dir:
        vol.mkdir(item.dest, parents=True, exist_ok=True, **stamp)
    else:
        try:
            data = item.host.read_bytes()
        except OSError as e:
            raise UsageError(f"{item.host}: cannot read ({e})") from e
        vol.write_file(item.dest, data, replace=bool(args.force), parents=True,
                       protect=args.protect, comment=args.comment, **stamp)
    if args.verbose:
        size = "" if item.is_dir else f" ({human_bytes(item.size)})"
        out.line(f"  wrote {'dir ' if item.is_dir else 'file'} "
                 f"{vol.info().name}:{item.dest}{size}")


def _restamp_directories(vol: Volume, items: list[Item], args: Any) -> None:
    """Re-apply directory timestamps once their contents exist.

    Writing a file into a directory stamps that directory, which is what AmigaDOS does and
    what an interactive copy should do -- but it means a directory created earlier in this
    same copy has already been restamped by its own children by the time the copy ends. So
    with `--preserve-times` the host mtime is applied again at the end.

    Only reachable with `--preserve-times`: without it every entry is stamped "now"
    anyway, and a second pass would be busywork.
    """
    if not args.preserve_times:
        return
    for item in items:
        if not item.is_dir:
            continue
        stamp = _stamp_kwargs(item.host, args)
        if stamp:
            vol.set_times(item.dest, stamp["secs"], stamp["ticks"])


def _stamp_kwargs(host: Path, args: Any) -> dict[str, Any]:
    """`secs`/`ticks` for a new entry, or nothing at all to mean "now".

    Mirrors `get --preserve-times` in the opposite direction, and defaults the same way:
    off, so a copy is stamped with the time the copy happened. Turning it on carries the
    host mtime across, which is what makes a round trip through the host non-destructive.
    """
    if not args.preserve_times:
        return {}
    try:
        secs, ticks = timestamps.from_unix(host.stat().st_mtime)
    except (OSError, OverflowError, ValueError):
        return {}
    if secs <= 0:
        # AmigaDOS cannot represent a date before 1978. Stamping "now" is better than a
        # negative day count, and the entry's date shows plainly that it happened.
        return {}
    return {"secs": secs, "ticks": ticks}


def _report(vol: Volume, items: list[Item], skipped: list[tuple[Path, str]], into: str,
            out: Output, *, dry_run: bool) -> int:
    """Summarise, in the same shape for a dry run and a real one."""
    info = vol.info()
    rows: list[dict[str, Any]] = []
    total = files = dirs = 0
    for item in items:
        if item.is_dir:
            dirs += 1
        else:
            files += 1
            total += item.size
        if dry_run:
            note = "  (exists, would be replaced)" if (
                not item.is_dir and vol.exists(item.dest)) else ""
            size = "" if item.is_dir else f" ({human_bytes(item.size)})"
            out.line(f"  would write {'dir ' if item.is_dir else 'file'} "
                     f"{info.name}:{item.dest}{size}{note}")
        rows.append({
            "host": str(item.host),
            "path": item.dest,
            "bytes": item.size,
            "is_dir": item.is_dir,
        })

    where = f"{info.name}:{into}" if into else f"{info.name}:"
    verb = "would write" if dry_run else "wrote"
    out.line()
    out.line(f"{verb} {files} file(s) and {dirs} directory(ies), "
             f"{human_bytes(total)} to {where}")
    if skipped:
        out.line(f"skipped {len(skipped)}")
    out.line(f"{human_bytes(info.free_bytes)} free ({info.free_blocks} blocks)"
             + (" -- nothing was changed" if dry_run else ""))
    out.data({
        "volume": info.name,
        "into": into,
        "entries": rows,
        "skipped": [{"host": str(p), "reason": why} for p, why in skipped],
        "files": files,
        "directories": dirs,
        "total_bytes": total,
        "dry_run": dry_run,
        "free_bytes": info.free_bytes,
        "free_blocks": info.free_blocks,
    })
    return 0


# ---------------------------------------------------------------------------
# mkdir
# ---------------------------------------------------------------------------


def cmd_mkdir(args: Any, out: Output) -> int:
    with opened_volume(args, writable=not args.dry_run) as (_, vol):
        name = vol.info().name
        made: list[dict[str, Any]] = []

        for raw in args.paths:
            rel = normalise(raw)
            if not rel:
                raise UsageError(f"{raw!r}: the volume root already exists")
            for component in rel.split("/"):
                vol.check_name(component)

            existed = vol.exists(rel)
            if args.dry_run:
                if existed and not args.parents:
                    raise ImageError(f"{name}:{rel} already exists")
                out.line(f"  {'exists already' if existed else 'would create'} "
                         f"{name}:{rel}")
                made.append({"path": rel, "created": not existed})
                continue

            # `-p` implies both "create ancestors" and "an existing target is fine",
            # exactly as Unix mkdir does.
            entry = vol.mkdir(rel, parents=args.parents, exist_ok=args.parents)
            if args.verbose or not existed:
                out.line(f"  {'exists already' if existed else 'created'} "
                         f"{name}:{entry.path}")
            made.append({"path": entry.path, "created": not existed})

        created = sum(1 for m in made if m["created"])
        info = vol.info()
        out.line()
        out.line(f"{'would create' if args.dry_run else 'created'} {created} "
                 f"directory(ies); {human_bytes(info.free_bytes)} free")
        out.data({
            "volume": info.name,
            "directories": made,
            "created": created,
            "dry_run": bool(args.dry_run),
            "free_bytes": info.free_bytes,
            "free_blocks": info.free_blocks,
        })
    return 0


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def cmd_rm(args: Any, out: Output) -> int:
    """Delete files, or directories with -r, inside an image.

    Takes the image first, like every read command (only `cp` is reversed). Every path is
    resolved and checked before anything is deleted, so a typo in the list removes nothing
    (design rule 6: no half-applied bulk operation).
    """
    with opened_volume(args, writable=not args.dry_run) as (_, vol):
        name = vol.info().name

        # -- expand wildcards, then validate the whole list -----------------
        # A path whose last component holds a wildcard matches entries in that directory,
        # case-insensitively as FFS is. A wildcard stays bounded: without -r a matched
        # directory is skipped with a warning rather than removed, so `rm '*'` can never
        # quietly take a subtree. A literal path keeps its hard errors (a directory without
        # -r is refused, not skipped). No match at all is an error, like a literal miss.
        targets: list[tuple[str, Any]] = []
        skipped_dirs: list[str] = []
        seen: set[str] = set()

        def _add(rel: str, entry: Any) -> None:
            key = rel.casefold()
            if key not in seen:
                seen.add(key)
                targets.append((rel, entry))

        for raw in args.paths:
            if transfer.is_glob(raw):
                base, leaf = transfer.split_glob(raw)
                if transfer.is_glob(base):
                    raise UsageError(
                        f"wildcards are only supported in the last path component, "
                        f"not in {base!r}"
                    )
                # A missing directory (NotFoundError) or a file where a directory was
                # expected (ImageError) propagates with its own exit code.
                entries = vol.listdir(normalise(base))
                leaf_low = leaf.lower()
                matched = [e for e in entries if fnmatch.fnmatch(e.name.lower(), leaf_low)]
                if not matched:
                    raise NotFoundError(f"{name}: no entries match {raw!r}")
                for e in matched:
                    if e.is_dir and not args.recursive:
                        skipped_dirs.append(e.path)
                        continue
                    _add(e.path, e)
            else:
                rel = normalise(raw)
                if not rel:
                    raise UsageError(f"{name}: cannot remove the volume root")
                entry = vol.stat(rel)  # raises NotFoundError (exit 3) for a missing path
                if entry.is_dir and not args.recursive:
                    raise ImageError(
                        f"{name}:{rel} is a directory; pass -r to remove it and its contents"
                    )
                _add(rel, entry)

        for rel in skipped_dirs:
            out.line(f"  skipped {name}:{rel} (a directory; pass -r to remove it)")

        # -- then act -------------------------------------------------------
        removed: list[dict[str, Any]] = []
        for rel, entry in targets:
            if args.dry_run:
                kind = "directory" if entry.is_dir else "file"
                out.line(f"  would remove {kind} {name}:{rel}")
                removed.append(_removed_row(rel, entry))
                continue

            try:
                result = vol.remove(rel, recursive=args.recursive)
            except NotFoundError:
                # Reachable only when the list names both a directory and something inside
                # it under -r: the earlier recursive delete already took this one. Report it
                # rather than abort -- the user's intent (both gone) is satisfied.
                out.line(f"  already removed {name}:{rel} (was inside an earlier target)")
                continue
            if args.verbose:
                kind = "directory" if result.is_dir else "file"
                out.line(f"  removed {kind} {name}:{rel}")
            removed.append(_removed_row(result.path, result))

        info = vol.info()
        verb = "would remove" if args.dry_run else "removed"
        out.line()
        tail = f"; {len(skipped_dirs)} directory(ies) skipped" if skipped_dirs else ""
        out.line(f"{verb} {len(removed)} item(s){tail}; {human_bytes(info.free_bytes)} free "
                 f"({info.free_blocks} blocks)")
        out.data({
            "volume": name,
            "removed": removed,
            "count": len(removed),
            "skipped": skipped_dirs,
            "dry_run": bool(args.dry_run),
            "free_bytes": info.free_bytes,
            "free_blocks": info.free_blocks,
        })
    return 0


def _removed_row(path: str, entry: Any) -> dict[str, Any]:
    return {
        "path": path,
        "type": "dir" if entry.is_dir else "file",
        "bytes": 0 if entry.is_dir else entry.size,
    }


__all__ = ["COMMENT_LIMIT", "SKIP_NAMES", "Item", "cmd_cp", "cmd_mkdir", "cmd_rm"]
