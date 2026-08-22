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
from .commands import browse, compose, extract, init, inspect, recipe, shell, snap, write
from .errors import AmibuilderError, UsageError
from .layers.drive import POLICIES
from .layers.store import DEFAULT_STORE, STORE_ENV_VAR
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

writing (note that cp takes the image last, like Unix cp):
  amibuilder cp ./lha card.hdf:Work
  amibuilder cp ./patch.lha ./lha card.hdf:Work --to Utils
  amibuilder cp -r ./SysInfo card.hdf:Work --to Tools -p
  amibuilder cp ./new.info card.hdf:Workbench --to S --force
  amibuilder mkdir card.hdf:Work Utils/Patches -p
  amibuilder rm card.hdf:Work Installers/AmigaOS-3.2.3.lha
  amibuilder rm card.hdf:Work Installers -r

interactive shell (cd/ls/put/get/cp/mv/rm over one open image):
  amibuilder shell card.hdf:Work

snapshots:
  amibuilder snap create card.hdf --label base-os-3.2.3
  amibuilder snap diff card.hdf --parent base-os-3.2.3 --label games
  amibuilder snap review games --explain
  amibuilder snap review games --drop 'Workbench:T/**'
  amibuilder snap commit games
  amibuilder snap ls
  amibuilder snap show games --files

composition:
  amibuilder recipe new a1200 --layers base-os-3.2.3,games
  amibuilder compose --recipe a1200 --into card.hdf --dry-run
  amibuilder compose --recipe a1200 --into card.hdf
  amibuilder compose --recipe a1200 --into wb.hdf --format plain --size 100M
  amibuilder compose --recipe a1200 --into ./wbdir --format dir
  amibuilder compose --stack base-os-3.2.3 --volume Workbench --dry-run
  amibuilder compose --recipe a1200 --policy Saves=preserve --dry-run
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

    # -- writing -------------------------------------------------------------
    # `cp` takes the image last, unlike every read command, because that is the order
    # Unix cp established and typing it the other way round is a constant small friction.
    # The in-image path stays a separate argument (--to) rather than being glued onto the
    # spec, so that `card.hdf:Work` cannot mean two different things.
    p = add("cp", write.cmd_cp, "Copy host files or directories into an image")
    p.add_argument("files", metavar="FILE", nargs="+",
                   help="host file(s) or directory(ies) to copy")
    p.add_argument("image", metavar="IMAGE",
                   help="destination image, e.g. card.hdf:Work")
    p.add_argument("--to", metavar="PATH", default="",
                   help="directory inside the image to copy into (default: its root)")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="copy directories and their contents")
    p.add_argument("-f", "--force", action="store_true",
                   help="replace files that already exist in the image")
    p.add_argument("-p", "--parents", action="store_true",
                   help="create --to and any missing parent directories")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report what would be written without writing")
    p.add_argument("--preserve-times", action="store_true",
                   help="take each entry's Amiga timestamp from the host mtime instead "
                        "of the current time")
    # argparse reads a bare '----rwed' as another option, so the short spelling is the
    # one that needs no escaping. Both are accepted; see Volume.parse_protect.
    p.add_argument("--protect", metavar="BITS", default=None,
                   help="protection bits for new files: name the permitted ones as "
                        "'rwed', or give the full form as --protect=----rwed "
                        "(default: ----rwed, as AmigaDOS gives a new file). Directories "
                        "always get the default")
    p.add_argument("--comment", metavar="TEXT", default=None,
                   help=f"file comment, up to {write.COMMENT_LIMIT} characters")

    p = add("mkdir", write.cmd_mkdir, "Create directories inside an image")
    p.add_argument("source", metavar="IMAGE", help="image to create them in")
    p.add_argument("paths", metavar="PATH", nargs="+",
                   help="volume-relative path(s) to create")
    p.add_argument("-p", "--parents", action="store_true",
                   help="create missing parents, and do not fail if the target exists")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report what would be created without creating it")

    # rm takes the image first, like mkdir and every read command. It deletes as AmigaDOS
    # does -- the bytes are freed but not wiped -- and refuses a directory without -r.
    p = add("rm", write.cmd_rm, "Delete files (or directories with -r) inside an image")
    p.add_argument("source", metavar="IMAGE", help="image to delete from")
    p.add_argument("paths", metavar="PATH", nargs="+",
                   help="volume-relative path(s) to remove")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="remove a directory and everything under it")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report what would be removed without removing it")

    # -- interactive shell ---------------------------------------------------
    # Holds one volume open and gives an AmigaDOS-style prompt, so a session of file
    # shuffling does not mean retyping the source spec on every command. Opens writable.
    p = add("shell", shell.cmd_shell,
            "Open an interactive prompt on an image (cd, ls, put, get, cp, mv, rm)")
    p.add_argument("source", metavar="IMAGE", help="image to open, e.g. card.hdf:Work")
    p.add_argument("--no-color", action="store_true",
                   help="disable the coloured prompt and directory listings")

    # -- creating images -----------------------------------------------------
    p = add("init", init.cmd_init, "Create a new disk image")
    p.add_argument("target", metavar="PATH")
    p.add_argument("--size", metavar="SIZE", required=True,
                   help="image size: 1-1000M or 1-16G (binary, so 4G is 4 GiB)")
    p.add_argument("--partition", action="append", metavar="SPEC", default=None,
                   help="add a partition: 'NAME=SIZE[,bootable][,dostype=ffs+intl]'. SIZE may be "
                        "'rest' for the remaining space. Repeatable; without any, the image is "
                        "left blank and unpartitioned")
    p.add_argument("--no-format", action="store_true",
                   help="create the partitions but leave them without a filesystem")
    p.add_argument("--plain", action="store_true",
                   help="make a single-volume image with no partition table (what emulators "
                        "traditionally mount) instead of an RDB drive")
    p.add_argument("--volume", metavar="NAME", default=None,
                   help="with --plain, the name of the one volume to create")
    p.add_argument("--dos-type", metavar="TYPE", default=None,
                   help=f"with --plain, the filesystem (default {init.DEFAULT_DOS_TYPE_NAME})")

    # -- snapshots -----------------------------------------------------------
    store_opt = argparse.ArgumentParser(add_help=False)
    store_opt.add_argument(
        "--store", metavar="PATH", default=None,
        help=f"layer store location (default: ${STORE_ENV_VAR} or {DEFAULT_STORE})")

    capture_opt = argparse.ArgumentParser(add_help=False)
    capture_opt.add_argument(
        "--exclude", action="append", metavar="GLOB", default=None,
        help="skip matching paths; repeatable. '**' crosses directories, '*' does not")
    capture_opt.add_argument(
        "--no-default-excludes", action="store_true",
        help="capture T/, Trashcan and other normally-skipped paths as well")

    snap_p = add("snap", snap.cmd_snap, "Capture and manage layered snapshots")
    snap_sub = snap_p.add_subparsers(dest="snap_command", metavar="SUBCOMMAND")

    def add_snap(name: str, help_text: str, *extra: argparse.ArgumentParser):
        return snap_sub.add_parser(
            name, help=help_text, description=help_text,
            parents=[g, store_opt, *extra])

    sp = add_snap("create", "Capture a whole drive as a base layer, with its RDB layout",
                  capture_opt)
    sp.add_argument("source", metavar="SOURCE")
    sp.add_argument("--label", required=True, metavar="NAME", help="name for the new layer")
    sp.add_argument("--no-boot-blocks", action="store_true",
                    help="do not record each partition's boot blocks")
    sp.add_argument("--volume", metavar="NAME", default=None,
                    help="volume name to record when SOURCE is a host directory "
                         "(default: the directory's own name)")

    sp = add_snap("diff", "Capture a drive and compare it against a parent layer",
                  capture_opt)
    sp.add_argument("source", metavar="SOURCE")
    sp.add_argument("--parent", required=True, metavar="REF",
                    help="layer the capture is compared against")
    sp.add_argument("--label", required=True, metavar="NAME", help="name for the candidate")
    sp.add_argument("--timestamps-significant", action="store_true",
                    help="treat a timestamp-only change as a difference (noisy)")
    sp.add_argument("--no-deletions", action="store_true",
                    help="do not record paths the parent had and this capture lacks")
    sp.add_argument("--volume", metavar="NAME", default=None,
                    help="volume name to record when SOURCE is a host directory "
                         "(default: the directory's own name)")

    sp = add_snap("review", "Inspect a candidate, and drop or keep paths by glob")
    sp.add_argument("label", metavar="LABEL")
    sp.add_argument("--explain", action="store_true", help="add a per-kind breakdown")
    sp.add_argument("--drop", action="append", metavar="GLOB", default=None,
                    help="remove matching entries from the candidate; repeatable")
    sp.add_argument("--keep", action="append", metavar="GLOB", default=None,
                    help="keep only matching entries; applied before --drop")

    sp = add_snap("commit", "Turn a candidate into a layer and point a ref at it")
    sp.add_argument("label", metavar="LABEL")
    sp.add_argument("--ref", metavar="NAME", default=None,
                    help="ref name to create (defaults to the candidate's label)")
    sp.add_argument("--allow-empty", action="store_true",
                    help="commit even when the candidate records nothing")

    sp = add_snap("discard", "Delete a candidate without committing it")
    sp.add_argument("label", metavar="LABEL")

    sp = add_snap("ls", "List layers, or candidates awaiting review")
    sp.add_argument("--candidates", action="store_true", help="list candidates instead")

    sp = add_snap("show", "Show a layer's metadata, drive record and contents")
    sp.add_argument("ref", metavar="REF")
    sp.add_argument("--files", action="store_true", help="list every recorded entry")

    sp = add_snap("verify", "Check that referenced blobs are present and hash correctly")
    sp.add_argument("ref", metavar="REF", nargs="?", default=None,
                    help="one layer, or every layer when omitted")

    sp = add_snap("gc", "Drop blobs no layer or candidate references")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="report what would be freed without deleting")

    sp = add_snap("rm", "Remove a layer (blobs remain until gc)")
    sp.add_argument("ref", metavar="REF")
    sp.add_argument("-f", "--force", action="store_true",
                    help="remove even when another layer names it as parent")

    # -- recipes -------------------------------------------------------------
    recipe_p = add("recipe", recipe.cmd_recipe, "Name an ordered stack of layers")
    recipe_sub = recipe_p.add_subparsers(dest="recipe_command", metavar="SUBCOMMAND")

    def add_recipe(name: str, help_text: str):
        return recipe_sub.add_parser(name, help=help_text, description=help_text,
                                    parents=[g, store_opt])

    sp = add_recipe("new", "Record an ordered list of layers under a name")
    sp.add_argument("name", metavar="NAME")
    sp.add_argument("--layers", required=True, metavar="A,B,C",
                    help="comma-separated layer refs, in composition order")
    sp.add_argument("--policy", action="append", metavar="VOLUME=POLICY", default=None,
                    help=f"record a volume's compose policy ({', '.join(POLICIES)}); "
                         "repeatable. compose --recipe applies these, and --policy overrides them")
    sp.add_argument("--description", metavar="TEXT", default=None)

    add_recipe("ls", "List recipes")

    sp = add_recipe("show", "Show a recipe, resolving each layer")
    sp.add_argument("name", metavar="NAME")

    sp = add_recipe("rm", "Remove a recipe")
    sp.add_argument("name", metavar="NAME")

    # -- compose -------------------------------------------------------------
    p = add("compose", compose.cmd_compose,
            "Build a drive from a layer stack")
    p.add_argument("--store", metavar="PATH", default=None,
                   help=f"layer store location (default: ${STORE_ENV_VAR} or {DEFAULT_STORE})")
    p.add_argument("--recipe", metavar="NAME", default=None,
                   help="compose the layers named by a recipe")
    p.add_argument("--stack", metavar="A,B,C", default=None,
                   help="compose these layers, in order")
    p.add_argument("--into", metavar="TARGET", default=None,
                   help="destination image, device or directory")
    p.add_argument("--format", choices=list(compose.FORMATS), default=compose.FORMAT_RDB,
                   help="output shape (default: rdb)")
    p.add_argument("--volume", action="append", metavar="NAME", default=None,
                   help="restrict to these volumes; repeatable, keeps the blast radius small")
    p.add_argument("--policy", action="append", metavar="VOLUME=POLICY", default=None,
                   help=f"override a volume's policy ({', '.join(POLICIES)}); repeatable")
    p.add_argument("--no-deletions", action="store_true",
                   help="ignore whiteouts, treating every layer as purely additive")
    p.add_argument("--no-metadata", action="store_true",
                   help="with --format dir, skip the .uaem sidecars that carry protection "
                        "bits, timestamps and comments")
    p.add_argument("--size", metavar="SIZE", default=None,
                   help="with --format plain, image size (e.g. 100M, 4G); defaults to the "
                        "size recorded for the volume in the base layer's drive record")
    p.add_argument("--strict-parents", action="store_true",
                   help="refuse when a layer's recorded parent is absent from the stack")
    p.add_argument("--no-verify", action="store_true",
                   help="skip re-reading the written image to confirm it matches the plan; "
                        "verification is on by default for the plain and rdb formats")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report the plan without writing")
    p.add_argument("-f", "--force", action="store_true",
                   help="overwrite an existing target")

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
