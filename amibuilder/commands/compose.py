"""The `compose` command: turn a layer stack back into a drive.

Only `--dry-run` is implemented so far, and that is deliberate sequencing rather than an
oversight — the planner carries every refusal, so making it inspectable before any write path
exists means the dangerous part arrives on top of something already tested.

`compose` will refuse rather than proceed in three situations, and each one names what it found:
a preflight problem, a target that already exists without `--force`, and a stack whose layout
cannot be reproduced. The design's requirement is that when it refuses it says which volumes
would have been destroyed and which preserved, so the consequence is explicit rather than
inferred.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .. import render
from ..errors import UnsupportedError, UsageError
from ..layers import compose as CP
from ..layers import drive as D
from ..layers import store as S
from ..layers import targets

SHORT = 12

#: Output formats. Only planning exists for now; each writer lands in its own change.
FORMAT_RDB = "rdb"
FORMAT_PLAIN = "plain"
FORMAT_DIR = "dir"
FORMATS = (FORMAT_RDB, FORMAT_PLAIN, FORMAT_DIR)


def _store(args: Any) -> S.Store:
    return S.Store(getattr(args, "store", None))


def _progress(args: Any):
    """Progress to stderr, so it cannot pollute `--json` on stdout."""
    if not getattr(args, "verbose", False):
        return None

    def report(path: str, size: int) -> None:
        print(f"  {path}  ({render.human_bytes(size)})", file=sys.stderr)

    return report


def _parse_policies(values: list[str] | None) -> dict[str, str]:
    """Parse repeated `--policy Volume=replace` arguments."""
    out: dict[str, str] = {}
    for raw in values or []:
        volume, sep, policy = raw.partition("=")
        if not sep or not volume.strip() or not policy.strip():
            raise UsageError(
                f"--policy expects VOLUME=POLICY, got {raw!r} "
                f"(policies: {', '.join(D.POLICIES)})"
            )
        out[volume.strip()] = D.check_policy(policy.strip().lower())
    return out


def _stack_specs(args: Any, store: S.Store) -> list[str]:
    if bool(args.recipe) == bool(args.stack):
        raise UsageError("give exactly one of --recipe or --stack")
    if args.recipe:
        recipe = store.read_recipe(args.recipe)
        specs = list(recipe.get("layers") or [])
        if not specs:
            raise UsageError(f"recipe '{args.recipe}' lists no layers")
        return specs
    return [spec.strip() for spec in args.stack.split(",") if spec.strip()]


def _existing_volumes(target: str) -> list[str]:
    """Volume names already on the target, which is what `preserve` needs to decide.

    An absent target is normal — composing to a new image or a new directory — so it reports no
    volumes rather than failing.
    """
    if not target or not os.path.exists(target):
        return []
    from ..addressing import parse
    from ..image import ImageKind, open_container

    try:
        with open_container(parse(target)) as container:
            if container.kind is ImageKind.RDB:
                return [
                    p.volume_name
                    for p in container.partitions(probe_volumes=True)
                    if p.volume_name
                ]
            with container.open_volume() as vol:
                return [vol.name]
    except Exception:
        # A target that cannot be inspected is treated as empty. It is only used to decide
        # whether `preserve` creates a volume, and a wrong guess there is caught by the
        # existing-target refusal below rather than causing a silent overwrite.
        return []


def _render_plan(out: render.Output, plan: CP.Plan, *, verbose: bool) -> None:
    out.field("layers", " + ".join(
        (layer.label or layer.id[:SHORT]) for layer in plan.layers
    ))
    out.field("files", plan.total_files)
    out.field("content", render.human_bytes(plan.total_content_bytes))

    out.heading("volumes")
    table = render.Table(
        headers=["volume", "policy", "action", "files", "content"],
        align=["l", "l", "l", "r", "r"], indent="  ",
    )
    for vol in plan.volumes:
        if vol.format_volume and vol.write:
            action = "format + write"
        elif vol.write:
            # `existed` matters here: a merge onto a target that does not exist yet creates the
            # volume, and calling that "write into existing" is simply untrue. Composing a whole
            # drive from scratch takes this path for every non-bootable volume, so the common
            # case was the one being described wrongly.
            action = "write into existing" if vol.existed else "create + write"
        elif vol.format_volume:
            action = "create empty"
        else:
            action = "untouched"
        table.add(
            vol.volume + ":",
            vol.policy,
            action,
            vol.file_count or "",
            render.human_bytes(vol.content_bytes) if vol.content_bytes else "",
        )
    out.table(table)

    if plan.destroyed_volumes:
        out.line()
        out.line(f"  DESTROYS existing data on: {', '.join(plan.destroyed_volumes)}")
    if plan.untouched_volumes:
        out.line(f"  leaves untouched: {', '.join(plan.untouched_volumes)}")
    if plan.created_empty_volumes:
        out.line(f"  creates empty: {', '.join(plan.created_empty_volumes)}")

    if plan.drive:
        out.heading("drive layout")
        out.lines([f"  {line}" for line in D.summary_lines(plan.drive)])

    if plan.conflicts:
        out.heading(f"conflicts ({len(plan.conflicts)} path(s) written by more than one layer)")
        labels = {layer.id: (layer.label or layer.id[:SHORT]) for layer in plan.layers}
        shown = plan.conflicts if verbose else plan.conflicts[:10]
        for conflict in shown:
            chain = " -> ".join(labels.get(i, i[:SHORT]) for i in conflict.layers)
            out.line(f"  {conflict.path}")
            out.line(f"      {chain}   ({labels.get(conflict.winner, '')} won)")
        if len(plan.conflicts) > len(shown):
            out.line(f"  ... and {len(plan.conflicts) - len(shown)} more (use -v)")

    if plan.warnings:
        out.heading(f"warnings ({len(plan.warnings)})")
        for text in plan.warnings:
            out.line(f"  {text}")

    if plan.problems:
        blocking = plan.blocking
        advisory = [p for p in plan.problems if not p.is_blocking]
        if blocking:
            out.heading(f"problems ({len(blocking)}) -- composition refuses")
            shown = blocking if verbose else blocking[:20]
            for problem in shown:
                where = f"{problem.path}: " if problem.path else ""
                out.line(f"  {where}{problem.message}")
            if len(blocking) > len(shown):
                out.line(f"  ... and {len(blocking) - len(shown)} more (use -v)")
        if advisory:
            out.heading(f"advisories ({len(advisory)})")
            for problem in advisory:
                where = f"{problem.path}: " if problem.path else ""
                out.line(f"  {where}{problem.message}")


def cmd_compose(args: Any, out: render.Output) -> int:
    store = _store(args)
    specs = _stack_specs(args, store)

    target = args.into or ""
    plan = CP.build_plan(
        store,
        specs,
        deletions=not args.no_deletions,
        policies=_parse_policies(args.policy),
        existing_volumes=_existing_volumes(target),
        strict_parents=args.strict_parents,
        only_volumes=args.volume or None,
    )

    if out.as_json:
        payload = dict(plan.as_dict(), target=target, format=args.format,
                       dry_run=bool(args.dry_run))
        if plan.is_writable and not args.dry_run and target:
            if args.format == FORMAT_DIR:
                written = targets.write_directory(
                    plan, store.blobs, target,
                    force=bool(args.force), metadata=not args.no_metadata,
                )
                payload["fsuae_config"] = targets.fsuae_config_lines(written, plan)
            elif args.format == FORMAT_PLAIN:
                written = targets.write_plain(
                    plan, store.blobs, target, force=bool(args.force),
                    size=render.parse_size(args.size) if args.size else None,
                )
            else:
                written = targets.write_rdb(
                    plan, store.blobs, target, force=bool(args.force),
                )
            payload["written"] = written.as_dict()
            if _should_verify(args):
                verdict = CP.verify_written(plan, target)
                payload["verify"] = verdict.as_dict()
                if not verdict.is_clean:
                    out.data(payload)
                    return 6
        out.data(payload)
        return 0 if plan.is_writable else 5

    if target:
        out.field("target", f"{target} ({args.format})")
    _render_plan(out, plan, verbose=bool(args.verbose))

    if not plan.is_writable:
        out.line()
        out.line("nothing was written: fix the problems above, or drop the offending entries")
        out.line("with `snap review <label> --drop GLOB` before committing the layer")
        return 5

    if args.dry_run:
        out.line()
        out.line("dry run: nothing written")
        return 0

    if not target:
        raise UsageError("--into is required unless --dry-run is given")

    if args.format == FORMAT_DIR:
        result = targets.write_directory(
            plan,
            store.blobs,
            target,
            force=bool(args.force),
            metadata=not args.no_metadata,
            on_file=_progress(args),
        )
    elif args.format == FORMAT_PLAIN:
        result = targets.write_plain(
            plan,
            store.blobs,
            target,
            force=bool(args.force),
            size=render.parse_size(args.size) if args.size else None,
            on_file=_progress(args),
        )
    else:
        result = targets.write_rdb(
            plan,
            store.blobs,
            target,
            force=bool(args.force),
            on_file=_progress(args),
        )

    out.line()
    out.line(f"wrote {result.files} file(s), {result.dirs} director(ies), "
             f"{render.human_bytes(result.bytes_written)}")
    if result.size_bytes:
        out.field("image size",
                  f"{render.human_bytes(result.size_bytes)} ({result.size_source})")
    if result.sidecars:
        out.field("uaem sidecars", result.sidecars)
    if result.in_place:
        out.field("restored into", "the existing drive; its partition table was not rewritten")
    if result.cleared:
        out.field("cleared first", ", ".join(result.cleared))
    if result.untouched:
        # The reassurance a partition-granular restore is actually for. Stated positively, because
        # "Work: and Saves: were not touched" is the thing the user wants to read.
        out.field("left untouched", ", ".join(result.untouched))
    if result.warnings:
        out.heading(f"warnings ({len(result.warnings)})")
        shown = result.warnings if args.verbose else result.warnings[:10]
        for text in shown:
            out.line(f"  {text}")
        if len(result.warnings) > len(shown):
            out.line(f"  ... and {len(result.warnings) - len(shown)} more (use -v)")

    if args.format == FORMAT_DIR:
        config = targets.fsuae_config_lines(result, plan)
        if config:
            out.heading("mount it in FS-UAE with")
            out.lines([f"  {line}" for line in config])

    if _should_verify(args):
        return _verify_and_report(args, out, plan, target)
    return 0


def _should_verify(args: Any) -> bool:
    """Verification is on by default, and only the image formats can be re-read.

    A directory target is left to ordinary tools -- it is plain files on the host, inspectable
    without this program, and re-reading it would mean reimplementing the sidecar parser as a
    reader purely to check the writer.
    """
    if getattr(args, "no_verify", False):
        return False
    return args.format in (FORMAT_PLAIN, FORMAT_RDB)


def _verify_and_report(args: Any, out: render.Output, plan: CP.Plan, target: str) -> int:
    out.heading("verifying")
    verdict = CP.verify_written(plan, target, on_file=_progress(args))

    for vol in verdict.volumes:
        if vol.skipped:
            out.line(f"  {vol.volume}: skipped -- {vol.skipped}")
        elif vol.is_clean:
            out.line(f"  {vol.volume}: {vol.matched} entr(ies) match")
        else:
            out.line(f"  {vol.volume}: {vol.matched} matched, "
                     f"{len(vol.missing)} missing, {len(vol.wrong)} wrong, "
                     f"{len(vol.unexpected)} unexpected")

    if verdict.is_clean:
        out.line()
        out.line(f"verified: the image holds exactly what was composed "
                 f"({verdict.matched} entr(ies))")
        return 0

    out.line()
    out.line(f"VERIFICATION FAILED: {verdict.fault_count} difference(s) between the plan and "
             "the image just written")
    for vol in verdict.volumes:
        faults = ([(p, "missing") for p in vol.missing]
                  + [(p, r) for p, r in vol.wrong]
                  + [(p, "unexpected") for p in vol.unexpected])
        if not faults:
            continue
        out.heading(f"{vol.volume} ({len(faults)})")
        shown = faults if args.verbose else faults[:10]
        for path, reason in shown:
            out.line(f"  {reason:12s} {path}")
        if len(faults) > len(shown):
            out.line(f"  ... and {len(faults) - len(shown)} more (use -v)")
    out.line()
    out.line("do not trust this image. Re-run the compose, and if it fails the same way the")
    out.line("layer store or the target medium is at fault -- `snap verify` checks the store")
    return 6
