"""`cat`, `hexdump` and `get` -- getting data out of an image."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .. import blocks as blk
from ..errors import AmibuilderError, ImageError, NotFoundError, UsageError
from ..render import Output, human_bytes, hexdump, parse_size
from ..volume import Volume
from . import opened_container, opened_volume, transfer

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


def _split_glob(pattern: str) -> tuple[str, str]:
    """Split an image glob into `(directory to list, leaf pattern)`.

    Volume-relative and plain: the directory part is a literal path and only the last
    component may hold wildcards. This is deliberately simpler than the shell's split, which
    resolves the directory against an interactive working directory -- a CLI path already
    names the volume separately (`card.hdf:Work`), so `Volume` normalises the rest.
    """
    slash = pattern.rfind("/")
    if slash >= 0:
        return pattern[:slash], pattern[slash + 1:]
    return "", pattern


def _extract_glob(vol: Volume, pattern: str, dest: Path, args: Any, out: Output) -> tuple[list[dict], int]:
    """Extract every image entry matching a wildcard pattern. Returns `(written, skipped)`.

    A file that already exists on the host is skipped with a warning and the run continues,
    which is what makes `get card.hdf:Work 'S/*.prefs'` usable in one shot; `--force`
    overwrites instead. FFS is case-insensitive, so matching is too.
    """
    base, leaf = _split_glob(pattern)
    if transfer.is_glob(base):
        raise UsageError(
            f"wildcards are only supported in the last path component, not in {base!r}"
        )
    # A wildcard implies "possibly many", so every match lands inside DEST as a directory.
    # Refusing a file DEST stops matches from silently colliding on one filename; creating a
    # missing one matches the way a subtree `get` already makes the directories it needs.
    if dest.exists() and not dest.is_dir():
        raise UsageError(
            f"a wildcard get writes matches into a directory, but {dest} is a file"
        )
    if not dest.exists() and not args.dry_run:
        dest.mkdir(parents=True)
    # A missing directory (NotFoundError) or a file where a directory was expected
    # (ImageError) propagates with its own exit code, exactly as a literal `get` of that
    # path would fail.
    entries = vol.listdir(base)
    leaf_low = leaf.lower()
    matched = [e for e in entries if fnmatch.fnmatch(e.name.lower(), leaf_low)]
    if not matched:
        raise NotFoundError(f"no entries match {pattern!r}")

    written: list[dict] = []
    skipped = 0
    for entry in matched:
        src = f"{base}/{entry.name}" if base else entry.name
        target = dest / entry.name if dest.is_dir() else dest
        if not args.force and not args.dry_run and target.exists():
            out.line(f"warning: skipped {entry.name} "
                     f"({target} already exists; pass --force to overwrite)")
            skipped += 1
            continue
        try:
            written.extend(transfer.extract_path(
                vol, src, dest,
                force=args.force, dry_run=args.dry_run,
                preserve_times=args.preserve_times, verbose=args.verbose,
                emit=out.line,
            ))
        except AmibuilderError as exc:
            out.line(f"warning: skipped {entry.name}: {exc}")
            skipped += 1
    return written, skipped


def cmd_get(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        src = args.path or ""
        dest = Path(args.dest) if args.dest else Path(".")

        # The recursive orchestration and the "target exists" rule live in transfer.py, so
        # the shell's `get` behaves identically to this one. This command owns only the
        # argparse-to-keyword translation, wildcard expansion, and the summary.
        skipped = 0
        if transfer.is_glob(src):
            written, skipped = _extract_glob(vol, src, dest, args, out)
        else:
            written = transfer.extract_path(
                vol, src, dest,
                force=args.force, dry_run=args.dry_run,
                preserve_times=args.preserve_times, verbose=args.verbose,
                emit=out.line,
            )

        total = sum(w["bytes"] for w in written)
        out.line()
        summary = f"{len(written)} file(s), {human_bytes(total)} written"
        if skipped:
            summary += f", {skipped} skipped"
        out.line(summary)
        out.data({
            "volume": vol.info().name,
            "source": src,
            "dest": str(dest),
            "files": written,
            "skipped": skipped,
            "total_bytes": total,
        })
    return 0
