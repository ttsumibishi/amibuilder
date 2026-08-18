"""Command-line entry point.

Addressing, described once here because it is the thing to learn:

    card.hdf              whole image; an RDB's first partition, or a plain HDF's volume
    card.hdf:0            RDB partition by index
    card.hdf:DH0          by AmigaDOS device name
    card.hdf:Workbench    by volume name
    disk.adf              a floppy image (.adf, .adz, .adf.gz)
    /dev/rdisk4           a raw device -- requires --device
    /dev/rdisk4:0x76:1    MBR primary slot 1, holding its own RDB (PiStorm / Emu68)
    /dev/rdisk4:0x76:1:2  ...and partition 2 of that RDB

Paths inside an image are always a separate argument, never part of the source spec, so a
partition named `Work` cannot be confused with a directory named `Work`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from . import __version__
from .commands import browse, extract, inspect
from .errors import AmibuilderError, UsageError
from .render import Output

Handler = Callable[[Any, Output], int]

EPILOG = """\
addressing:
  card.hdf                whole image (RDB partition 0, or a plain HDF's volume)
  card.hdf:0              RDB partition by index
  card.hdf:DH0            by AmigaDOS device name
  card.hdf:Workbench      by volume name
  disk.adf                floppy image (.adf, .adz, .adf.gz)
  /dev/rdisk4             raw device (requires --device)
  /dev/rdisk4:0x76:1      MBR slot 1 holding its own RDB (PiStorm / Emu68)
  /dev/rdisk4:0x76:1:2    partition 2 inside that slot

examples:
  amibuilder info card.hdf
  amibuilder partitions /dev/rdisk4:0x76:1 --device
  amibuilder ls card.hdf:Workbench S -l
  amibuilder find card.hdf:0 --name '*.info' --type f
  amibuilder get card.hdf:0 S ./backup/S
  amibuilder hexdump card.hdf --block 0
  amibuilder check card.hdf --json
"""


def _global_parser() -> argparse.ArgumentParser:
    """Flags accepted either side of the subcommand."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=False,
                   help="emit machine-readable JSON instead of text")
    p.add_argument("--device", action="store_true", default=False,
                   help="permit operating on a raw device (/dev/rdiskN)")
    p.add_argument("--yes", action="store_true", default=False,
                   help="skip interactive confirmation (scripted use)")
    p.add_argument("-v", "--verbose", action="store_true", default=False,
                   help="more detail")
    return p


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, Handler]]:
    g = _global_parser()
    parser = argparse.ArgumentParser(
        prog="amibuilder",
        description="File-level access and layered snapshots for Amiga disk images.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[g],
    )
    parser.add_argument("--version", action="version", version=f"amibuilder {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    handlers: dict[str, Handler] = {}

    def add(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text, description=help_text, parents=[g])
        handlers[name] = handler
        return sp

    # -- inspect -------------------------------------------------------------
    p = add("info", inspect.cmd_info, "Report an image's kind, geometry and volumes")
    p.add_argument("source", metavar="SOURCE")

    p = add("partitions", inspect.cmd_partitions, "List the RDB or MBR partition table")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("--no-probe", action="store_true",
                   help="do not mount partitions to read their volume names")

    p = add("check", inspect.cmd_check,
            "Validate structure (boot, root, tree, files, bitmap)")
    p.add_argument("source", metavar="SOURCE")

    # -- browse --------------------------------------------------------------
    p = add("ls", browse.cmd_ls, "List a directory")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH", nargs="?", default="")
    p.add_argument("-l", "--long", action="store_true",
                   help="show protection bits, size, timestamp and comment")
    p.add_argument("-R", "--recursive", action="store_true", help="descend into directories")

    p = add("tree", browse.cmd_tree, "Show the directory tree")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH", nargs="?", default="")
    p.add_argument("--depth", type=int, default=None, metavar="N",
                   help="limit depth to N levels")
    p.add_argument("-l", "--long", action="store_true", help="show file sizes")

    p = add("find", browse.cmd_find, "Find entries by name, type, size or comment")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH", nargs="?", default="")
    p.add_argument("--name", metavar="GLOB", help="match the entry name (case-insensitive)")
    p.add_argument("--path", dest="path_pattern", metavar="GLOB",
                   help="match the whole volume-relative path")
    p.add_argument("--type", choices=["f", "d"], help="restrict to files or directories")
    p.add_argument("--min-size", metavar="SIZE", help="files at least this large (e.g. 100K)")
    p.add_argument("--max-size", metavar="SIZE", help="files at most this large")
    p.add_argument("--comment", metavar="TEXT", help="substring match on the file comment")
    p.add_argument("-l", "--long", action="store_true", help="long listing format")

    p = add("du", browse.cmd_du, "Summarise space used, apparent and on-disk")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH", nargs="?", default="")
    p.add_argument("--depth", type=int, default=None, metavar="N",
                   help="only report to N levels below the starting path")

    # -- extract -------------------------------------------------------------
    p = add("cat", extract.cmd_cat, "Write a file's contents to stdout")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH")
    p.add_argument("--text", action="store_true",
                   help="translate Amiga CR line endings to LF")

    p = add("hexdump", extract.cmd_hexdump, "Hex dump a file, or a raw block")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH", nargs="?", default="")
    p.add_argument("--block", type=int, metavar="N",
                   help="dump raw block N instead of a file (no filesystem needed)")
    p.add_argument("--count", type=int, default=None, metavar="N",
                   help="with --block, dump N consecutive blocks")
    p.add_argument("--skip", metavar="SIZE", help="start this far into the file")
    p.add_argument("--length", metavar="SIZE", help="dump at most this many bytes")
    p.add_argument("--absolute", action="store_true",
                   help="with --block, label offsets from the start of the image")

    p = add("get", extract.cmd_get, "Copy a file or subtree out to the host")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("path", metavar="PATH", nargs="?", default="")
    p.add_argument("dest", metavar="DEST", nargs="?", default=".")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report what would be written without writing")
    p.add_argument("--preserve-times", action="store_true",
                   help="set host mtimes from the Amiga timestamps")

    return parser, handlers


def _merge_globals(argv: list[str], args: argparse.Namespace) -> None:
    """Honour global flags given before the subcommand.

    argparse applies the subparser's defaults over the top-level parser's results, so a
    flag typed before the subcommand would otherwise be silently discarded. Pre-parsing
    recovers what the user actually typed.
    """
    pre, _ = _global_parser().parse_known_args(argv)
    for flag in ("json", "device", "yes", "verbose"):
        if getattr(pre, flag, False):
            setattr(args, flag, True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser, handlers = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    _merge_globals(argv, args)

    out = Output(as_json=args.json)
    handler = handlers[args.command]
    try:
        code = handler(args, out)
    except AmibuilderError as e:
        # Expected failures are messages, not tracebacks.
        print(f"amibuilder: {e}", file=sys.stderr)
        return e.exit_code
    except BrokenPipeError:
        # `amibuilder ls ... | head` is a normal thing to do.
        try:
            sys.stdout.close()
        finally:
            return 0
    except KeyboardInterrupt:
        print("amibuilder: interrupted", file=sys.stderr)
        return 130

    try:
        out.emit()
    except BrokenPipeError:
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
