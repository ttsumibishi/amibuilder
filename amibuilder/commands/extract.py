"""`cat`, `hexdump` and `get` -- getting data out of an image."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import blocks as blk
from ..errors import ImageError, UsageError
from ..render import Output, human_bytes, hexdump, parse_size
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


def cmd_get(args: Any, out: Output) -> int:
    with opened_volume(args) as (_, vol):
        src = args.path or ""
        dest = Path(args.dest) if args.dest else Path(".")

        # The recursive orchestration and the "target exists" rule live in transfer.py, so
        # the shell's `get` behaves identically to this one. This command owns only the
        # argparse-to-keyword translation and the summary.
        written = transfer.extract_path(
            vol, src, dest,
            force=args.force, dry_run=args.dry_run,
            preserve_times=args.preserve_times, verbose=args.verbose,
            emit=out.line,
        )

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
