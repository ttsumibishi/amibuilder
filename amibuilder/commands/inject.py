"""inject -- copy files from one Amiga image (or ADF) into another, without the host.

`cp` writes host files into an image and `get` reads them back out; `inject` is the third
edge of the triangle, copying straight from one Amiga volume into another so an ADF's or a
partition's contents never have to touch the host filesystem on the way. It is what you
reach for to drop a game ADF into `card.hdf:Work/Games`, or to fold one partition's tree
into another.

Because both sides are real Amiga volumes, inject always carries the source's metadata --
protection bits, file comment and modification time -- rather than resetting them the way a
host copy must. There is nothing to opt into: a faithful copy is the only sensible one.

The argument order matches `cp`'s muscle memory (source first, destination last), and the
option vocabulary is `cp`'s too: `--to` names the directory to land in, `-r` recurses,
`-f` overwrites, `-p` creates `--to`. `--from` is the one addition -- the sub-path within
the *source* to inject, defaulting to the whole volume.

Nothing is written until the whole tree has been resolved and checked -- a name too long,
a tree that will not fit, or a collision needing `--force` is a message while the
destination is still untouched, the same no-half-applied rule `cp` and `rm` follow.

One v1 limitation: source and destination must be different image files. Two open handles
on one file, one of them writing, could let a destination write clobber a block the source
read has not reached yet, so the same-file case is refused rather than risked.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

from ..addressing import parse
from ..errors import ImageError, UsageError
from ..render import Output, human_bytes
from ..volume import Volume, normalise
from . import opened_volume


class InjectItem(NamedTuple):
    """One entry to copy, resolved (with its source metadata) before anything is written."""

    #: Source volume-relative path.
    src: str
    #: Destination volume-relative path.
    dest: str
    size: int
    is_dir: bool
    #: Source metadata, carried across verbatim.
    protect: str
    comment: str
    secs: int
    ticks: int


def cmd_inject(args: Any, out: Output) -> int:
    # Refuse the same underlying file for both sides. parse() is pure -- it neither opens
    # the file nor triggers the device guard -- so this check runs before either volume is
    # mounted and before any raw-device confirmation.
    if os.path.realpath(parse(args.source).path) == os.path.realpath(parse(args.dest).path):
        raise UsageError(
            "source and destination are the same image file; inject copies between two "
            "different images. Export with 'get' and re-import with 'cp' to move files "
            "within one image."
        )

    # Source read-only; a dry run opens the destination read-only too, so it never triggers
    # the raw-device write confirmation -- a dry run on a device is genuinely harmless.
    with opened_volume(args, writable=False, attr="source") as (_, src_vol):
        with opened_volume(args, writable=not args.dry_run, attr="dest") as (_, dst_vol):
            to_dir = _dest_dir(dst_vol, args)
            items, skipped = _plan(src_vol, args, to_dir)
            _preflight(dst_vol, items, args)

            src_name = src_vol.info().name
            for path, why in skipped:
                out.line(f"  skip ({why}): {src_name}:{path}")

            if args.dry_run:
                return _report(dst_vol, items, skipped, to_dir, out, dry_run=True)

            for item in items:
                _write_one(src_vol, dst_vol, item, args, out)
            _restamp_dirs(dst_vol, items)
            return _report(dst_vol, items, skipped, to_dir, out, dry_run=False)


def _dest_dir(vol: Volume, args: Any) -> str:
    """The volume-relative directory the copy lands in, created if `--parents` allows it.

    Mirrors `cp`'s `--to` handling: an existing directory is used as-is, a missing one is
    created only with `-p`, and a path that names a file is refused.
    """
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
            f"'amibuilder mkdir {args.dest} {rel}', or pass --parents"
        )
    for component in rel.split("/"):
        vol.check_name(component)
    if not args.dry_run:
        vol.mkdir(rel, parents=True, exist_ok=True)
    return rel


def _rel_to(full: str, root: str) -> str:
    """`full` expressed relative to `root` ('' when they are equal, `full` when root is '')."""
    if root == "":
        return full
    if full == root:
        return ""
    return full[len(root) + 1:]


def _join(base: str, rel: str) -> str:
    if not rel:
        return base
    if not base:
        return rel
    return f"{base}/{rel}"


def _plan(src_vol: Volume, args: Any,
          to_dir: str) -> tuple[list[InjectItem], list[tuple[str, str]]]:
    """Resolve the source side into `(items to create, links skipped)`.

    `--from` omitted means the whole source volume, whose *contents* land directly under
    `--to`. A named directory lands as `--to/<name>/...`, and a single file as
    `--to/<name>`, exactly as `cp` treats a host directory or file.
    """
    from_path = normalise(args.from_path or "")
    src_label = src_vol.info().name
    items: list[InjectItem] = []
    skipped: list[tuple[str, str]] = []
    seen: dict[str, str] = {}

    def add(src: str, dest: str, size: int, is_dir: bool, meta: Any) -> None:
        # FFS matches names without regard to case, so two sources differing only in case
        # would collide -- one silently overwriting the other.
        key = dest.casefold()
        clash = seen.get(key)
        if clash is not None:
            raise UsageError(
                f"{clash!r} and {dest!r} both land on the same name; FFS ignores case, so "
                f"one would overwrite the other"
            )
        seen[key] = dest
        items.append(InjectItem(src, dest, size, is_dir, meta.protect_str, meta.comment,
                                meta.mod_secs, meta.mod_ticks))

    if from_path == "":
        root_entry = None
        src_is_dir = True
        base = to_dir  # whole volume: contents land directly under --to
    else:
        root_entry = src_vol.stat(from_path)  # NotFoundError (exit 3) for a missing path
        if root_entry.is_link:
            raise UsageError(
                f"{src_label}:{from_path} is a link; inject copies files and directories, "
                f"not links"
            )
        src_is_dir = root_entry.is_dir
        base = _join(to_dir, root_entry.name)

    if src_is_dir and not args.recursive:
        where = f"{src_label}:{from_path}" if from_path else f"{src_label}: (whole volume)"
        raise UsageError(f"{where} is a directory; pass -r to inject it and its contents")

    if not src_is_dir:
        add(from_path, _join(to_dir, root_entry.name), root_entry.size, False, root_entry)
        return items, skipped

    # Directory source. A named directory is created at `base` first; the whole-volume
    # root is not (its contents merge into the existing --to directory).
    if from_path != "":
        add(from_path, base, 0, True, root_entry)
    for _dirpath, subdirs, files in src_vol.walk(from_path):
        for d in subdirs:
            add(d.path, _join(base, _rel_to(d.path, from_path)), 0, True, d)
        for f in files:
            if f.is_link:
                skipped.append((f.path, "link"))
                continue
            add(f.path, _join(base, _rel_to(f.path, from_path)), f.size, False, f)
    return items, skipped


def _preflight(dst_vol: Volume, items: list[InjectItem], args: Any) -> None:
    """Refuse the whole inject for anything that would otherwise fail partway through."""
    info = dst_vol.info()

    # Names and metadata first: a name too long is the user's mistake, whereas a capacity
    # refusal is a fact about the volume, so the more useful message wins.
    for item in items:
        for component in item.dest.split("/"):
            dst_vol.check_name(component)
        if item.comment:
            dst_vol.check_comment(item.comment)
        if item.protect:
            dst_vol.parse_protect(item.protect)

    needed = 0
    for item in items:
        exists = dst_vol.exists(item.dest)
        if item.is_dir:
            if exists and not dst_vol.is_dir(item.dest):
                raise ImageError(
                    f"{info.name}:{item.dest} is a file, so the directory cannot be "
                    f"created there"
                )
            needed += 0 if exists else 1
            continue
        if exists:
            if dst_vol.is_dir(item.dest):
                raise ImageError(
                    f"{info.name}:{item.dest} is a directory, so the file cannot replace it"
                )
            if not args.force:
                raise ImageError(
                    f"{info.name}:{item.dest} already exists. Pass --force to overwrite."
                )
            # Replacing frees what the old copy held.
            needed -= dst_vol.blocks_for(dst_vol.stat(item.dest).size)
        needed += dst_vol.blocks_for(item.size)

    if needed > info.free_blocks:
        short = (needed - info.free_blocks) * info.block_size
        raise ImageError(
            f"{info.name}: this inject needs {needed} block(s) but {info.free_blocks} are "
            f"free -- {human_bytes(short)} short"
        )


def _write_one(src_vol: Volume, dst_vol: Volume, item: InjectItem, args: Any,
               out: Output) -> None:
    comment = item.comment or None
    if item.is_dir:
        dst_vol.mkdir(item.dest, parents=True, exist_ok=True,
                      protect=item.protect, comment=comment,
                      secs=item.secs, ticks=item.ticks)
    else:
        data = src_vol.read_file(item.src)
        dst_vol.write_file(item.dest, data, replace=bool(args.force), parents=True,
                           protect=item.protect, comment=comment,
                           secs=item.secs, ticks=item.ticks)
    if args.verbose:
        size = "" if item.is_dir else f" ({human_bytes(item.size)})"
        out.line(f"  injected {'dir ' if item.is_dir else 'file'} "
                 f"{dst_vol.info().name}:{item.dest}{size}")


def _restamp_dirs(dst_vol: Volume, items: list[InjectItem]) -> None:
    """Re-apply each created directory's source timestamp once its contents exist.

    Writing a file (or creating a subdirectory) stamps the parent directory, which is what
    AmigaDOS does -- but it means a directory injected earlier has since been re-stamped by
    its own children. inject always preserves, so the source time is applied again at the
    end. The destination `--to` directory is deliberately not in `items`, so its own
    timestamp is left bumped to now, which is correct: it really was modified.
    """
    for item in items:
        if item.is_dir:
            dst_vol.set_times(item.dest, item.secs, item.ticks)


def _report(dst_vol: Volume, items: list[InjectItem], skipped: list[tuple[str, str]],
            to_dir: str, out: Output, *, dry_run: bool) -> int:
    """Summarise, in the same shape for a dry run and a real one."""
    info = dst_vol.info()
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
                not item.is_dir and dst_vol.exists(item.dest)) else ""
            size = "" if item.is_dir else f" ({human_bytes(item.size)})"
            out.line(f"  would inject {'dir ' if item.is_dir else 'file'} "
                     f"{info.name}:{item.dest}{size}{note}")
        rows.append({
            "source": item.src,
            "path": item.dest,
            "bytes": item.size,
            "is_dir": item.is_dir,
        })

    where = f"{info.name}:{to_dir}" if to_dir else f"{info.name}:"
    verb = "would inject" if dry_run else "injected"
    out.line()
    out.line(f"{verb} {files} file(s) and {dirs} directory(ies), "
             f"{human_bytes(total)} into {where}")
    if skipped:
        out.line(f"skipped {len(skipped)} link(s)")
    out.line(f"{human_bytes(info.free_bytes)} free ({info.free_blocks} blocks)"
             + (" -- nothing was changed" if dry_run else ""))
    out.data({
        "volume": info.name,
        "into": to_dir,
        "entries": rows,
        "skipped": [{"path": p, "reason": why} for p, why in skipped],
        "files": files,
        "directories": dirs,
        "total_bytes": total,
        "dry_run": dry_run,
        "free_bytes": info.free_bytes,
        "free_blocks": info.free_blocks,
    })
    return 0


__all__ = ["InjectItem", "cmd_inject"]
