"""The `snap` command family: capture drives into layers, and review before committing.

The shape follows the design's two-step capture (docs/KIP-FFS-LAYERS.md section 5): `snap
diff` writes a *candidate* and commits nothing, `snap review` inspects and prunes it, and
`snap commit` finalises. Trust in a diff comes from being able to look before committing,
and the first few diffs against a real machine are expected to reveal noise nobody predicted.

`snap create` has no review gate, because a base layer is "everything on this drive" -- there
is no parent to be surprised by.
"""

from __future__ import annotations

import sys
from typing import Any

from .. import render
from ..errors import UsageError
from ..image import ImageKind
from ..layers import capture as C
from ..layers import drive as D
from ..layers import manifest as M
from ..layers import store as S
from . import opened_container

#: Layer IDs are shown truncated. Twelve characters is git's habit and is unambiguous in a
#: store holding thousands of layers.
SHORT = 12


def _store(args: Any) -> S.Store:
    return S.Store(getattr(args, "store", None))


def _exclusions(args: Any) -> C.Exclusions:
    return C.Exclusions.build(
        extra=tuple(getattr(args, "exclude", None) or ()),
        defaults=not getattr(args, "no_default_excludes", False),
    )


def _progress(args: Any):
    """A progress callback that writes to stderr, so it cannot pollute `--json` on stdout."""
    if not getattr(args, "verbose", False):
        return None

    def report(path: str, size: int) -> None:
        print(f"  {path}  ({render.human_bytes(size)})", file=sys.stderr)

    return report


def _capture_summary(out: render.Output, result: C.CaptureResult) -> None:
    stats = result.stats
    out.field("volumes", ", ".join(result.volumes) or "none")
    out.field("files", stats.files)
    out.field("directories", stats.dirs)
    if stats.skipped:
        out.field("skipped", stats.skipped)
    out.field("content", render.human_bytes(stats.content_bytes))
    out.field("new blobs", f"{stats.blobs_written} ({render.human_bytes(stats.stored_bytes)})")
    out.field("deduplicated", stats.blobs_deduplicated)


def _warnings(out: render.Output, warnings: list[str]) -> None:
    if not warnings:
        return
    out.heading(f"warnings ({len(warnings)})")
    for text in warnings:
        out.line(f"  {text}")


def _check_volume_opt(args: Any, container: Any) -> None:
    """`--volume` names the volume a host directory is captured as, so it is meaningless for
    an image (which carries its own volume names). Refusing it rather than ignoring it stops a
    misdirected flag from silently doing nothing."""
    if getattr(args, "volume", None) and container.kind is not ImageKind.DIRECTORY:
        raise UsageError(
            "--volume only applies when the source is a host directory; an image carries "
            "its own volume names"
        )


# ---------------------------------------------------------------------------
# create -- a base layer
# ---------------------------------------------------------------------------


def cmd_create(args: Any, out: render.Output) -> int:
    """Capture a whole drive as a base layer, including its RDB layout."""
    store = _store(args)
    S.check_label(args.label)

    with opened_container(args) as container:
        _check_volume_opt(args, container)
        drive_record = D.capture(container, boot_blocks=not args.no_boot_blocks)
        result = C.capture_container(
            container,
            store.blobs,
            exclusions=_exclusions(args),
            on_file=_progress(args),
            directory_volume_name=getattr(args, "volume", None),
        )
        source = {
            "kind": container.kind.value,
            "path": container.address.path,
            "size": container.size_bytes,
        }

    if not result.entries:
        _warnings(out, result.warnings)
        raise UsageError(
            "nothing was captured, so no layer was created -- see the warnings above"
        )

    layer = store.write_layer(
        entries=result.entries,
        kind=S.KIND_BASE,
        label=args.label,
        source=source,
        drive=drive_record,
    )
    store.set_ref(args.label, layer.id)

    if out.as_json:
        out.data(
            {
                "layer": layer.to_json(),
                "ref": args.label,
                "capture": result.stats.as_dict(),
                "warnings": result.warnings,
            }
        )
        return 0

    out.line(f"created base layer {layer.id[:SHORT]} as '{args.label}'")
    out.line()
    _capture_summary(out, result)
    out.field("stored total", render.human_bytes(layer.stats.stored_size))
    out.heading("drive")
    out.lines([f"  {line}" for line in D.summary_lines(drive_record)])
    _warnings(out, result.warnings)
    return 0


# ---------------------------------------------------------------------------
# diff -- a candidate layer
# ---------------------------------------------------------------------------


def cmd_diff(args: Any, out: render.Output) -> int:
    """Capture a drive and compare it against a parent layer, writing a candidate."""
    store = _store(args)
    S.check_label(args.label)
    parent_id = store.resolve(args.parent)
    parent_entries = store.read_manifest(parent_id)

    with opened_container(args) as container:
        _check_volume_opt(args, container)
        result = C.capture_container(
            container,
            store.blobs,
            exclusions=_exclusions(args),
            on_file=_progress(args),
            directory_volume_name=getattr(args, "volume", None),
        )
        source = {
            "kind": container.kind.value,
            "path": container.address.path,
            "size": container.size_bytes,
        }

    comparison = C.diff(
        parent_entries,
        result.entries,
        timestamps_significant=args.timestamps_significant,
        deletions=not args.no_deletions,
    )

    candidate = store.write_candidate(
        args.label,
        entries=comparison.entries,
        kind=S.KIND_DIFF,
        parent=parent_id,
        source=source,
    )

    if out.as_json:
        out.data(
            {
                "candidate": candidate.to_json(),
                "parent": parent_id,
                "changes": [c.as_dict() for c in comparison.changes],
                "by_reason": comparison.by_reason(),
                "unchanged": comparison.unchanged,
                "capture": result.stats.as_dict(),
                "warnings": result.warnings + comparison.warnings,
            }
        )
        return 0

    parent_label = store.read_layer(parent_id).label or parent_id[:SHORT]
    out.line(f"candidate '{args.label}' against '{parent_label}' ({parent_id[:SHORT]})")
    out.line()
    if comparison.is_empty:
        out.line("no differences -- nothing to commit")
    else:
        out.field("changes", len(comparison.changes))
        for reason, count in comparison.by_reason().items():
            out.line(f"  {reason:<14} {count}")
    out.field("unchanged", comparison.unchanged)
    out.line()
    _capture_summary(out, result)
    _warnings(out, result.warnings)
    out.heading("next")
    if comparison.is_empty:
        # Suggesting a commit here would contradict the line above. An empty candidate is
        # still written so it can be inspected, but the only sensible next step is removal.
        out.line(f"  amibuilder snap discard {args.label}")
    else:
        out.line(f"  amibuilder snap review {args.label} --explain")
        out.line(f"  amibuilder snap commit {args.label}")
    return 0


# ---------------------------------------------------------------------------
# review -- inspect and prune a candidate
# ---------------------------------------------------------------------------


def cmd_review(args: Any, out: render.Output) -> int:
    """Inspect a candidate, optionally dropping or keeping paths by glob.

    `--drop` and `--keep` rewrite the candidate in place. That is the point: the exclusion
    list is expected to need tuning against a real machine, and this is what makes it a
    five-minute job rather than a reason to abandon the approach.
    """
    store = _store(args)
    candidate, entries = store.read_candidate(args.label)

    # Reconstruct changes from the stored manifest. Reasons are not persisted -- they
    # describe a comparison, not a layer -- so a whiteout is reported as a deletion and
    # everything else simply as present.
    changes = [
        C.DiffEntry(
            entry=e,
            reason=C.REASON_DELETED if e.kind == M.WHITEOUT else "present",
        )
        for e in entries
    ]

    dropped: dict[str, int] = {}
    if args.keep:
        changes = C.apply_keeps(changes, args.keep)
    if args.drop:
        changes, dropped = C.apply_drops(changes, args.drop)

    mutated = bool(args.drop or args.keep)
    if mutated:
        candidate = store.update_candidate(args.label, [c.entry for c in changes])

    if out.as_json:
        out.data(
            {
                "candidate": candidate.to_json(),
                "entries": [c.entry.to_json() for c in changes],
                "dropped": dropped,
                "modified": mutated,
            }
        )
        return 0

    out.line(f"candidate '{args.label}' -- {len(changes)} entries")
    if candidate.parent:
        out.field("parent", candidate.parent[:SHORT])
    out.line()

    if dropped:
        out.heading("dropped")
        for pattern, count in dropped.items():
            note = "" if count else "   (matched nothing)"
            out.line(f"  {pattern:<28} {count}{note}")
        out.line()

    table = render.Table(headers=["kind", "path", "size"], align=["l", "l", "r"])
    for change in changes:
        entry = change.entry
        table.add(
            entry.kind,
            entry.path,
            render.human_bytes(entry.size) if entry.size else "",
        )
    out.table(table)

    if args.explain:
        out.heading("by kind")
        for kind, count in M.counts([c.entry for c in changes]).items():
            if count:
                out.line(f"  {kind:<3} {count}")

    if mutated:
        out.line()
        out.line(f"candidate rewritten -- {len(changes)} entries remain")
    return 0


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def cmd_commit(args: Any, out: render.Output) -> int:
    """Promote a candidate to a layer and point a ref at it."""
    store = _store(args)
    _candidate, entries = store.read_candidate(args.label)
    if not entries and not args.allow_empty:
        raise UsageError(
            f"candidate '{args.label}' has no entries -- nothing would be recorded. "
            "Use --allow-empty to commit it anyway, or `snap discard` to remove it"
        )
    layer = store.commit_candidate(args.label, ref=args.ref)

    if out.as_json:
        out.data({"layer": layer.to_json(), "ref": args.ref or args.label})
        return 0

    out.line(f"committed {layer.id[:SHORT]} as '{args.ref or args.label}'")
    out.field("entries", layer.stats.entries)
    out.field("files", layer.stats.files)
    out.field("content", render.human_bytes(layer.stats.content_size))
    out.field("stored", render.human_bytes(layer.stats.stored_size))
    if layer.stats.whiteouts:
        out.field("deletions", layer.stats.whiteouts)
    return 0


def cmd_discard(args: Any, out: render.Output) -> int:
    """Delete a candidate. Its blobs stay until `snap gc`."""
    store = _store(args)
    removed = store.delete_candidate(args.label)
    if out.as_json:
        out.data({"discarded": removed, "label": args.label})
        return 0 if removed else 1
    if not removed:
        out.line(f"no candidate named '{args.label}'")
        return 1
    out.line(f"discarded candidate '{args.label}' (blobs remain until `snap gc`)")
    return 0


# ---------------------------------------------------------------------------
# ls / show
# ---------------------------------------------------------------------------


def cmd_ls(args: Any, out: render.Output) -> int:
    """List layers, or candidates awaiting review."""
    store = _store(args)

    if args.candidates:
        rows = []
        for label in store.iter_candidates():
            candidate, entries = store.read_candidate(label)
            rows.append((label, candidate, entries))
        if out.as_json:
            out.data(
                [
                    {"label": label, "candidate": c.to_json(), "entries": len(e)}
                    for label, c, e in rows
                ]
            )
            return 0
        if not rows:
            out.line("no candidates")
            return 0
        table = render.Table(headers=["candidate", "parent", "entries", "created"])
        for label, candidate, entries in rows:
            table.add(
                label,
                (candidate.parent or "-")[:SHORT],
                len(entries),
                candidate.created,
            )
        out.table(table)
        return 0

    layers = store.list_layers()
    if out.as_json:
        out.data(
            [
                dict(layer.to_json(), refs=store.refs_for(layer.id))
                for layer in layers
            ]
        )
        return 0
    if not layers:
        out.line(f"no layers in {store.root}")
        return 0

    table = render.Table(
        headers=["id", "refs", "kind", "parent", "entries", "files", "stored", "created"],
        align=["l", "l", "l", "l", "r", "r", "r", "l"],
    )
    for layer in layers:
        table.add(
            layer.id[:SHORT],
            ",".join(store.refs_for(layer.id)) or "-",
            layer.kind,
            (layer.parent or "-")[:SHORT],
            layer.stats.entries,
            layer.stats.files,
            render.human_bytes(layer.stats.stored_size),
            layer.created,
        )
    out.table(table)
    return 0


def cmd_show(args: Any, out: render.Output) -> int:
    """Show one layer's metadata, drive record and optionally its file list."""
    store = _store(args)
    layer_id = store.resolve(args.ref)
    layer = store.read_layer(layer_id)

    if out.as_json:
        payload: dict[str, Any] = {
            "layer": layer.to_json(),
            "refs": store.refs_for(layer_id),
            "chain": [entry.id for entry in store.chain(layer_id)],
        }
        if args.files:
            payload["entries"] = [e.to_json() for e in store.read_manifest(layer_id)]
        out.data(payload)
        return 0

    out.field("id", layer.id)
    out.field("label", layer.label or "-")
    out.field("refs", ", ".join(store.refs_for(layer_id)) or "-")
    out.field("kind", layer.kind)
    out.field("parent", layer.parent[:SHORT] if layer.parent else "-")
    out.field("created", layer.created)
    out.field("tool", layer.tool_version)
    if layer.source:
        out.field("source", f"{layer.source.get('kind')} {layer.source.get('path')}")
    out.line()
    out.field("entries", layer.stats.entries)
    out.field("files", layer.stats.files)
    out.field("directories", layer.stats.dirs)
    if layer.stats.whiteouts:
        out.field("deletions", layer.stats.whiteouts)
    out.field("content", render.human_bytes(layer.stats.content_size))
    out.field("stored", render.human_bytes(layer.stats.stored_size))

    if layer.kind == S.KIND_DIFF:
        chain = store.chain(layer_id)
        out.heading("stack")
        for depth, entry in enumerate(chain):
            out.line(f"  {'  ' * depth}{entry.id[:SHORT]}  {entry.label or '-'}")

    if layer.drive:
        out.heading("drive")
        out.lines([f"  {line}" for line in D.summary_lines(layer.drive)])

    if args.files:
        out.heading("entries")
        table = render.Table(headers=["kind", "path", "size", "protect"],
                             align=["l", "l", "r", "l"], indent="  ")
        for entry in store.read_manifest(layer_id):
            table.add(
                entry.kind,
                entry.path,
                render.human_bytes(entry.size) if entry.size else "",
                entry.protect if entry.kind != M.WHITEOUT else "",
            )
        out.table(table)
    return 0


# ---------------------------------------------------------------------------
# verify / gc / rm
# ---------------------------------------------------------------------------


def cmd_verify(args: Any, out: render.Output) -> int:
    """Check that every blob a layer references is present and hashes correctly."""
    store = _store(args)
    targets = [store.resolve(args.ref)] if args.ref else list(store.iter_layer_ids())

    findings: dict[str, list[str]] = {}
    for layer_id in targets:
        problems = store.verify_layer(layer_id)
        if problems:
            findings[layer_id] = problems

    if out.as_json:
        out.data({"checked": len(targets), "problems": findings})
        return 6 if findings else 0

    if not targets:
        out.line("no layers to verify")
        return 0
    for layer_id, problems in findings.items():
        layer = store.read_layer(layer_id)
        out.heading(f"{layer_id[:SHORT]} ({layer.label or '-'}): {len(problems)} problem(s)")
        for text in problems:
            out.line(f"  {text}")
    if findings:
        out.line()
        out.line(f"{len(findings)} of {len(targets)} layers have problems")
        return 6
    out.line(f"{len(targets)} layer(s) verified, no problems")
    return 0


def cmd_gc(args: Any, out: render.Output) -> int:
    """Drop blobs that no layer or candidate references."""
    store = _store(args)
    count, freed = store.gc(dry_run=args.dry_run)
    if out.as_json:
        out.data({"blobs": count, "bytes": freed, "dry_run": args.dry_run})
        return 0
    verb = "would free" if args.dry_run else "freed"
    out.line(f"{verb} {render.human_bytes(freed)} from {count} unreferenced blob(s)")
    return 0


def cmd_rm(args: Any, out: render.Output) -> int:
    """Remove a layer. Blobs stay until `snap gc`, so this is cheap and undoable-ish."""
    store = _store(args)
    layer_id = store.resolve(args.ref)
    layer = store.read_layer(layer_id)
    refs = store.remove_layer(layer_id, force=args.force)
    if out.as_json:
        out.data({"removed": layer_id, "refs": refs})
        return 0
    out.line(f"removed {layer_id[:SHORT]} ({layer.label or '-'})")
    if refs:
        out.field("refs dropped", ", ".join(refs))
    out.line("blobs remain until `snap gc`")
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

SUBCOMMANDS = {
    "create": cmd_create,
    "diff": cmd_diff,
    "review": cmd_review,
    "commit": cmd_commit,
    "discard": cmd_discard,
    "ls": cmd_ls,
    "show": cmd_show,
    "verify": cmd_verify,
    "gc": cmd_gc,
    "rm": cmd_rm,
}


def cmd_snap(args: Any, out: render.Output) -> int:
    action = getattr(args, "snap_command", None)
    if not action:
        raise UsageError(
            "snap needs a subcommand: " + ", ".join(sorted(SUBCOMMANDS))
        )
    return SUBCOMMANDS[action](args, out)
