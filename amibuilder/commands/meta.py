"""touch, protect and comment -- set metadata on entries already on a volume.

`cp` can set the protection bits, comment and timestamp of a *new* file
(`--protect`/`--comment`/`--preserve-times`); these three change them on entries that
already exist, without rewriting the file. They are the AmigaDOS `SetDate`, `Protect` and
`FileNote` verbs.

Each takes the image first (like every write command except `cp`) and one or more paths, and
each validates its whole path list before changing anything -- a typo in the list changes
nothing, the same no-half-applied rule `cp` and `rm` follow.
"""

from __future__ import annotations

from typing import Any

from .. import timestamps
from ..errors import NotFoundError, UsageError
from ..render import Output
from ..volume import Volume, normalise
from . import opened_volume


def _resolve_existing(vol: Volume, paths: list[str], name: str, verb: str) -> list[str]:
    """Normalise and confirm every path exists, before anything is changed.

    Raises NotFoundError (exit 3) for a missing path and UsageError (exit 2) for the volume
    root, so a batch is refused whole rather than applied halfway.
    """
    rels: list[str] = []
    for raw in paths:
        rel = normalise(raw)
        if not rel:
            raise UsageError(f"{name}: cannot {verb} the volume root")
        vol.stat(rel)  # raises NotFoundError for a missing path
        rels.append(rel)
    return rels


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


def cmd_touch(args: Any, out: Output) -> int:
    """Set entries' modification time to now; create an empty file for a missing path.

    Like Unix `touch`: an existing entry is restamped, a missing one is created empty, and
    `-c`/`--no-create` skips a missing path instead of creating it. Parent directories are
    not created -- touching a path in a directory that does not exist is an error, as it is
    on the host.
    """
    secs, ticks = timestamps.now()
    with opened_volume(args, writable=not args.dry_run) as (_, vol):
        name = vol.info().name

        # Plan first: resolve each path and validate a name that would be created, so a
        # bad name is refused before any timestamp is written.
        plan: list[tuple[str, bool]] = []  # (rel, exists)
        for raw in args.paths:
            rel = normalise(raw)
            if not rel:
                raise UsageError(f"{name}: the volume root has no timestamp to set")
            exists = vol.exists(rel)
            if not exists and not args.no_create:
                # Validate the name that would be created and confirm its parent exists.
                # touch, like the host tool, does not create parents; checking here means a
                # bad path in the batch is refused up front rather than found only after
                # earlier entries have already been stamped.
                for component in rel.split("/"):
                    vol.check_name(component)
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if parent:
                    if not vol.exists(parent):
                        raise NotFoundError(
                            f"{name}: cannot create {rel}: {parent} does not exist "
                            "(touch does not create parent directories)")
                    if not vol.is_dir(parent):
                        raise UsageError(
                            f"{name}: cannot create {rel}: {parent} is a file")
            plan.append((rel, exists))

        results: list[dict[str, Any]] = []
        for rel, exists in plan:
            if not exists and args.no_create:
                out.line(f"  skipped {name}:{rel} (does not exist)")
                results.append({"path": rel, "existed": False, "created": False,
                                "touched": False})
                continue
            if args.dry_run:
                out.line(f"  would {'touch' if exists else 'create'} {name}:{rel}")
            elif exists:
                vol.set_times(rel, secs, ticks)
                if args.verbose:
                    out.line(f"  touched {name}:{rel}")
            else:
                vol.write_file(rel, b"", secs=secs, ticks=ticks)
                if args.verbose:
                    out.line(f"  created {name}:{rel}")
            results.append({"path": rel, "existed": exists,
                            "created": not exists, "touched": True})

        touched = sum(1 for r in results if r["touched"])
        created = sum(1 for r in results if r["created"])
        out.line()
        out.line(f"{'would touch' if args.dry_run else 'touched'} {touched} entr"
                 f"{'y' if touched == 1 else 'ies'}"
                 + (f", {created} created" if created else ""))
        out.data({"volume": name, "entries": results, "touched": touched,
                  "created": created, "dry_run": bool(args.dry_run)})
    return 0


# ---------------------------------------------------------------------------
# protect
# ---------------------------------------------------------------------------


def cmd_protect(args: Any, out: Output) -> int:
    """Set the protection bits of existing entries.

    `--bits` takes the same spec as `cp --protect`: the eight-character form written
    `--bits=----rwed`, or the short form naming only the permitted bits, `--bits rwed`
    (`--bits=-d` for a leading-dash form). The bits are stored inverted, so `----rwed`
    (mask 0) is the all-permitted default a fresh file carries.
    """
    mask = Volume.parse_protect(args.bits)  # UsageError (exit 2) on a bad spec, before opening
    with opened_volume(args, writable=not args.dry_run) as (_, vol):
        name = vol.info().name
        rels = _resolve_existing(vol, args.paths, name, "set protection on")

        results: list[dict[str, Any]] = []
        for rel in rels:
            if args.dry_run:
                out.line(f"  would set {args.bits} on {name}:{rel}")
                results.append({"path": rel, "protect": args.bits})
                continue
            entry = vol.set_protect(rel, mask)
            if args.verbose:
                out.line(f"  {entry.protect_str}  {name}:{rel}")
            results.append({"path": rel, "protect": entry.protect_str})

        out.line()
        out.line(f"{'would set' if args.dry_run else 'set'} protection on {len(rels)} "
                 f"entr{'y' if len(rels) == 1 else 'ies'}")
        out.data({"volume": name, "entries": results, "count": len(rels),
                  "dry_run": bool(args.dry_run)})
    return 0


# ---------------------------------------------------------------------------
# comment
# ---------------------------------------------------------------------------


def cmd_comment(args: Any, out: Output) -> int:
    """Set the file comment (AmigaDOS FileNote) of existing entries.

    `--text` is required; an empty string (`--text ''`) clears the comment. The comment is
    limited to the 79 bytes AmigaDOS allows, checked before anything is written.
    """
    with opened_volume(args, writable=not args.dry_run) as (_, vol):
        name = vol.info().name
        vol.check_comment(args.text)  # UsageError (exit 2) if too long, before any write
        rels = _resolve_existing(vol, args.paths, name, "comment")

        for rel in rels:
            if args.dry_run:
                shown = repr(args.text) if args.text else "(cleared)"
                out.line(f"  would set comment on {name}:{rel} -> {shown}")
                continue
            vol.set_comment(rel, args.text)
            if args.verbose:
                out.line(f"  {name}:{rel}")

        n = len(rels)
        plural = "y" if n == 1 else "ies"
        if args.text:
            verb = "would set" if args.dry_run else "set"
        else:
            verb = "would clear" if args.dry_run else "cleared"
        out.line("")
        out.line(f"{verb} the comment on {n} entr{plural}")
        out.data({"volume": name, "paths": rels, "comment": args.text,
                  "count": len(rels), "dry_run": bool(args.dry_run)})
    return 0


__all__ = ["cmd_touch", "cmd_protect", "cmd_comment"]
