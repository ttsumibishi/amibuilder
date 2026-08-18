"""`cat`, `hexdump` and `get` -- getting data out of an image."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .. import blocks as blk
from ..errors import ImageError, UsageError
from ..render import Output, human_bytes, hexdump, parse_size
from ..volume import Entry, Volume
from . import opened_container, opened_volume

# ---------------------------------------------------------------------------
# cat
# ---------------------------------------------------------------------------


def cmd_cat(args: Any, out: Output) -> int:
    if out.as_json:
        raise UsageError(
            "cat writes raw file contents, which cannot be represented as JSON. "
            "Use 'get' to write to a file, or 'hexdump' for a textual view."
        )
    with opened_volume(args) as (_, vol):
        data = vol.read_file(args.path)
        if args.text:
            # Amiga text is Latin-1 with CR or LF line endings.
            text = data.decode("latin-1")
            out.binary(text.replace("\r\n", "\n").replace("\r", "\n").encode())
        else:
            out.binary(data)
    return 0


# ---------------------------------------------------------------------------
# hexdump
# ---------------------------------------------------------------------------


def cmd_hexdump(args: Any, out: Output) -> int:
    if args.block is not None and args.path:
        raise UsageError("give either --block or a PATH, not both")
    if args.block is None and not args.path:
        raise UsageError("give a PATH inside the image, or --block N for a raw block")

    if args.block is not None:
        return _hexdump_blocks(args, out)
    return _hexdump_file(args, out)


def _hexdump_blocks(args: Any, out: Output) -> int:
    """Dump raw blocks, with an identification line per block.

    Deliberately does not require a mountable filesystem: dumping blocks is most useful
    exactly when the filesystem will not mount.
    """
    with opened_container(args) as c:
        bs = c.geometry.block_size
        count = args.count or 1
        rows: list[dict[str, Any]] = []
        with c.stream() as f:
            for i in range(count):
                num = args.block + i
                try:
                    data = blk.read_block(f, num, bs)
                except EOFError as e:
                    raise ImageError(
                        f"{c.address.path}: block {num} is past the end of the "
                        f"container ({c.geometry.num_blocks} blocks)"
                    ) from e
                ident = blk.identify_block(data, num)
                label = ident.kind
                if ident.name:
                    label += f" {ident.name!r}"
                if ident.checksum_ok is not None:
                    label += f"  checksum={'ok' if ident.checksum_ok else 'BAD'}"
                if ident.detail:
                    label += f"  ({ident.detail})"
                if i:
                    out.line()
                out.line(f"block {num} @ byte {num * bs}: {label}")
                out.lines(hexdump(data, base=num * bs if args.absolute else 0))
                rows.append({
                    "block": num,
                    "byte_offset": num * bs,
                    "kind": ident.kind,
                    "name": ident.name,
                    "checksum_ok": ident.checksum_ok,
                    "detail": ident.detail,
                    "hex": data.hex(),
                })
        out.data({"container": c.as_dict(), "blocks": rows})
    return 0


def _hexdump_file(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        data = vol.read_file(args.path)
        start = parse_size(args.skip) if args.skip else 0
        length = parse_size(args.length) if args.length else len(data) - start
        chunk = data[start : start + length]
        out.lines(hexdump(chunk, base=start))
        out.data({
            "path": args.path,
            "size": len(data),
            "offset": start,
            "length": len(chunk),
            "hex": chunk.hex(),
        })
    return 0


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def cmd_get(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        src = args.path or ""
        entry = vol.stat(src)
        dest = Path(args.dest) if args.dest else Path(".")

        if entry.is_dir:
            written = _get_tree(vol, entry, dest, args, out)
        else:
            written = [_get_file(vol, entry, _file_dest(entry, dest), args, out)]

        total = sum(w["bytes"] for w in written)
        out.line()
        out.line(f"{len(written)} file(s), {human_bytes(total)} written")
        out.data({
            "volume": vol.info().name,
            "source": src,
            "dest": str(dest),
            "files": written,
            "total_bytes": total,
        })
    return 0


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


def _get_tree(vol: Volume, root: Entry, dest: Path, args: Any, out: Output) -> list[dict]:
    base = dest / _safe_name(root.name) if root.path else dest
    written: list[dict] = []
    for dirpath, _dirs, files in vol.walk(root.path):
        rel = dirpath[len(root.path) :].strip("/") if root.path else dirpath
        target_dir = base / Path(*[_safe_name(p) for p in rel.split("/")]) if rel else base
        if not args.dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.is_link:
                out.line(f"  skip (link): {f.path}")
                continue
            written.append(_get_file(vol, f, target_dir / _safe_name(f.name), args, out))
    return written


def _get_file(vol: Volume, entry: Entry, target: Path, args: Any, out: Output) -> dict:
    if target.exists() and not args.force and not args.dry_run:
        raise ImageError(
            f"{target} exists. Pass --force to overwrite."
        )
    data = b"" if args.dry_run else vol.read_file(entry.path)
    if not args.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if args.preserve_times and entry.mod_secs:
            _apply_mtime(target, entry)
    if args.verbose or args.dry_run:
        prefix = "would write" if args.dry_run else "wrote"
        out.line(f"  {prefix} {target} ({human_bytes(entry.size)})")
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
