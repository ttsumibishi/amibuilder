"""`ls`, `tree`, `find` and `du` -- moving around inside a volume."""

from __future__ import annotations

import fnmatch
from typing import Any

from .. import timestamps
from ..errors import UsageError
from ..render import Output, Table, human_bytes, parse_size
from ..volume import Entry, Volume
from . import opened_volume


def _target_path(args: Any) -> str:
    return getattr(args, "path", None) or ""


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def cmd_ls(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        path = _target_path(args)
        entry = vol.stat(path)

        if not entry.is_dir:
            # `ls` on a file lists that file, matching POSIX ls.
            groups = [(entry.path, [entry])]
        elif args.recursive:
            groups = [(d, dirs + files) for d, dirs, files in vol.walk(path)]
        else:
            groups = [(path, vol.listdir(path))]

        data = {
            "volume": vol.info().name,
            "path": path,
            "listings": [
                {"path": p, "entries": [e.as_dict() for e in ents]} for p, ents in groups
            ],
        }

        multi = len(groups) > 1
        for i, (p, entries) in enumerate(groups):
            if multi:
                if i:
                    out.line()
                # The volume root prints as "Workbench:", subdirectories as "S:".
                out.line(f"{p}:" if p else f"{vol.info().name}:")
            _emit_listing(entries, out, long=args.long, indent="  " if multi else "")
        out.data(data)
    return 0


def _emit_listing(entries: list[Entry], out: Output, *, long: bool, indent: str = "") -> None:
    if not entries:
        out.line(f"{indent}(empty)")
        return

    if not long:
        for e in entries:
            out.line(f"{indent}{e.name}{'/' if e.is_dir else ''}")
        return

    t = Table([], align=["l", "r", "l", "l"], indent=indent)
    for e in entries:
        size = "<dir>" if e.is_dir else str(e.size)
        name = e.name + ("/" if e.is_dir else "")
        if e.is_link:
            name += f" -> ({e.link_kind} link)"
        row = [e.protect_str, size, timestamps.short(e.mod_secs), name]
        if e.comment:
            row.append(f": {e.comment}")
        t.add(*row)
    out.table(t)

    total = sum(e.size for e in entries if not e.is_dir)
    n_dirs = sum(1 for e in entries if e.is_dir)
    n_files = len(entries) - n_dirs
    out.line(f"{indent}{n_files} file(s) {human_bytes(total)}, {n_dirs} dir(s)")


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------


def cmd_tree(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        path = _target_path(args)
        root_label = path or f"{vol.info().name}:"
        out.line(root_label)

        counts = {"dirs": 0, "files": 0, "bytes": 0}
        nodes: list[dict[str, Any]] = []
        _tree_walk(vol, path, out, prefix="", depth=0, max_depth=args.depth,
                   counts=counts, sink=nodes, long=args.long)
        out.line()
        out.line(f"{counts['dirs']} directories, {counts['files']} files, "
                 f"{human_bytes(counts['bytes'])}")
        out.data({"volume": vol.info().name, "path": path, "tree": nodes, **counts})
    return 0


def _tree_walk(vol: Volume, path: str, out: Output, *, prefix: str, depth: int,
               max_depth: int | None, counts: dict[str, int], sink: list[dict[str, Any]],
               long: bool) -> None:
    if max_depth is not None and depth >= max_depth:
        return
    entries = vol.listdir(path)
    for i, e in enumerate(entries):
        last = i == len(entries) - 1
        branch = "`-- " if last else "|-- "
        label = e.name + ("/" if e.is_dir else "")
        if long and not e.is_dir:
            label += f"  ({human_bytes(e.size)})"
        out.line(f"{prefix}{branch}{label}")

        node = e.as_dict()
        sink.append(node)
        if e.is_dir:
            counts["dirs"] += 1
            if not e.is_link:
                children: list[dict[str, Any]] = []
                node["children"] = children
                _tree_walk(vol, e.path, out, prefix=prefix + ("    " if last else "|   "),
                           depth=depth + 1, max_depth=max_depth, counts=counts,
                           sink=children, long=long)
        else:
            counts["files"] += 1
            counts["bytes"] += e.size


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def cmd_find(args: Any, out: Output) -> int:
    if args.type not in (None, "f", "d"):
        raise UsageError("--type takes 'f' (file) or 'd' (directory)")

    min_size = parse_size(args.min_size) if args.min_size else None
    max_size = parse_size(args.max_size) if args.max_size else None

    with opened_volume(args) as (_, vol):
        path = _target_path(args)
        matches: list[Entry] = []
        for _dirpath, dirs, files in vol.walk(path):
            for e in dirs + files:
                if _matches(e, args, min_size, max_size):
                    matches.append(e)

        matches.sort(key=lambda e: e.path.lower())
        if args.long:
            _emit_listing(matches, out, long=True)
        else:
            for e in matches:
                out.line(e.path + ("/" if e.is_dir else ""))
        if not matches:
            out.line("(no matches)")
        out.data({
            "volume": vol.info().name,
            "path": path,
            "count": len(matches),
            "matches": [e.as_dict() for e in matches],
        })
    # Exit 1 on no matches, so `find` composes in shell conditionals like grep does.
    return 0 if matches else 1


def _matches(e: Entry, args: Any, min_size: int | None, max_size: int | None) -> bool:
    if args.type == "f" and e.is_dir:
        return False
    if args.type == "d" and not e.is_dir:
        return False

    if args.name:
        # Amiga filesystems are case-insensitive, so matching is too.
        if not fnmatch.fnmatch(e.name.lower(), args.name.lower()):
            return False
    if args.path_pattern:
        if not fnmatch.fnmatch(e.path.lower(), args.path_pattern.lower()):
            return False
    if min_size is not None and (e.is_dir or e.size < min_size):
        return False
    if max_size is not None and (e.is_dir or e.size > max_size):
        return False
    if args.comment and args.comment.lower() not in e.comment.lower():
        return False
    return True


# ---------------------------------------------------------------------------
# du
# ---------------------------------------------------------------------------


def cmd_du(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        path = _target_path(args)
        info = vol.info()
        bs = info.block_size

        # Totals are accumulated bottom-up so a parent includes its children.
        sizes: dict[str, int] = {}
        blocks_used: dict[str, int] = {}
        counts: dict[str, int] = {}
        order: list[str] = []

        for dirpath, _dirs, files in vol.walk(path):
            order.append(dirpath)
            sizes.setdefault(dirpath, 0)
            blocks_used.setdefault(dirpath, 0)
            counts.setdefault(dirpath, 0)
            for f in files:
                sizes[dirpath] += f.size
                # An FFS file occupies whole data blocks plus its header, so apparent
                # size understates usage -- markedly so for a tree of small icons.
                data_blocks = (f.size + bs - 1) // bs if f.size else 0
                blocks_used[dirpath] += data_blocks + 1
                counts[dirpath] += 1

        # Roll child totals into parents.
        for dirpath in sorted(order, key=lambda p: p.count("/"), reverse=True):
            parent = dirpath.rsplit("/", 1)[0] if "/" in dirpath else ""
            if parent != dirpath and parent in sizes:
                sizes[parent] += sizes[dirpath]
                blocks_used[parent] += blocks_used[dirpath]
                counts[parent] += counts[dirpath]

        rows = sorted(order, key=lambda p: (-sizes[p], p.lower()))
        if args.depth is not None:
            base_depth = path.count("/") + 1 if path else 0
            rows = [p for p in rows if (p.count("/") + (1 if p else 0)) - base_depth
                    <= args.depth]

        t = Table(["apparent", "on-disk", "files", "path"], align=["r", "r", "r", "l"])
        for p in rows:
            t.add(human_bytes(sizes[p]), human_bytes(blocks_used[p] * bs),
                  counts[p], p or f"{info.name}:")
        out.table(t)

        root = path or ""
        overhead = blocks_used.get(root, 0) * bs - sizes.get(root, 0)
        out.line()
        out.line(
            f"total apparent {human_bytes(sizes.get(root, 0))}, "
            f"on-disk {human_bytes(blocks_used.get(root, 0) * bs)} "
            f"({human_bytes(overhead)} block overhead across {counts.get(root, 0)} files)"
        )
        out.data({
            "volume": info.name,
            "path": path,
            "block_size": bs,
            "entries": [
                {
                    "path": p,
                    "apparent_bytes": sizes[p],
                    "disk_bytes": blocks_used[p] * bs,
                    "blocks": blocks_used[p],
                    "files": counts[p],
                }
                for p in rows
            ],
        })
    return 0
