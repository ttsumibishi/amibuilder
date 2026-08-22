"""Compare any two sources and report what differs -- without storing anything.

This is `snap diff`'s comparison engine pointed at two arbitrary sources instead of a drive
against a stored layer. Each source -- an HDF image, an RDB partition, a plain HDF, an ADF or
a host directory -- is captured through `capture_container`, and `layers.capture.diff` reports
the added, changed and removed entries between the two.

It is strictly read-only. Both sources are captured through a `HashOnlyBlobStore`, which hashes
content so it can be compared but writes nothing to any store -- the same trick
`compose --verify` uses to re-read a written image without polluting the layer store. There is
no candidate, no layer, no ref: `diff` reports and stops.

Direction: `SOURCE_A` is the "before" and `SOURCE_B` the "after", so *added* means present only
in B, *removed* means present only in A, and *changed* means present in both but differing.

Volume alignment is the one real choice here. The comparison key is the volume-qualified path
(`Work:S/foo`), which is exactly right when comparing two backups of the same drive -- their
partition names line up. But a host directory called `exported` and a partition called `Work`
hold the same files under different volume names, and comparing those volume-qualified would
report every file as both added and removed. So when each side is a single volume, entries are
compared by their path *within* the volume (`--by path`); when either side has several volumes,
they are matched volume-qualified (`--by volume`). `--by` forces either.
"""

from __future__ import annotations

from typing import Any

from .. import render
from ..errors import UsageError
from ..image import ImageKind
from ..layers import blobs as B
from ..layers import capture as C
from ..layers import manifest as M
from . import opened_container

#: The synthetic volume name entries are relabelled to for a by-path comparison, so two
#: single-volume sources line up on their contents rather than on their volume names. Not a
#: legal Amiga volume name, which is deliberate -- it never collides with a real one.
_PATH_VOLUME = "*"


def _exclusions(args: Any) -> C.Exclusions:
    return C.Exclusions.build(
        extra=tuple(getattr(args, "exclude", None) or ()),
        defaults=not getattr(args, "no_default_excludes", False),
    )


def _capture_source(args: Any, attr: str) -> tuple[C.CaptureResult, dict[str, Any]]:
    """Capture one source read-only. Writes to no store (HashOnlyBlobStore).

    A bare source captures the whole container -- every mountable volume of an RDB drive. An
    explicit partition selector (`card.hdf:Work`, `card.hdf:0`) narrows to just that volume,
    the way the read commands treat the same address, so `diff card.hdf:Work ./exported`
    compares one partition against a folder rather than the whole drive against it.
    """
    blobs = B.HashOnlyBlobStore()
    excl = _exclusions(args)
    with opened_container(args, attr=attr) as container:
        selector = container.address.partition
        if selector is not None and container.kind is ImageKind.RDB:
            with container.open_volume(selector) as vol:
                result = C.capture_volume(vol, blobs, exclusions=excl)
        else:
            result = C.capture_container(container, blobs, exclusions=excl)
        info = {
            "spec": container.address.spec,
            "path": container.address.path,
            "kind": container.kind.value,
        }
    return result, info


def _resolve_mode(
    requested: str, result_a: C.CaptureResult, info_a: dict[str, Any],
    result_b: C.CaptureResult, info_b: dict[str, Any],
) -> str:
    """Decide whether to compare by path-within-volume or by volume-qualified path."""
    single = len(result_a.volumes) <= 1 and len(result_b.volumes) <= 1
    if requested == "path" and not single:
        multi, vols = (
            (info_a["spec"], result_a.volumes)
            if len(result_a.volumes) > 1
            else (info_b["spec"], result_b.volumes)
        )
        raise UsageError(
            f"--by path compares a single volume on each side, but {multi} has "
            f"{len(vols)} ({', '.join(vols)}). Use --by volume, or address one "
            f"partition (e.g. {multi}:0)."
        )
    if requested in ("path", "volume"):
        return requested
    return "path" if single else "volume"


def _relabel(entries: list[M.ManifestEntry]) -> list[M.ManifestEntry]:
    """Rewrite every entry to a common synthetic volume, so paths match across differently
    named single-volume sources. Safe only when each side is a single volume -- otherwise two
    volumes could carry the same relative path and collide (which `manifest.index` refuses)."""
    return [M.replace(e, path=M.join_path(_PATH_VOLUME, e.relative)) for e in entries]


def _status(reason: str) -> str:
    """Friendly change word for the text table: added / removed / the reason for a change."""
    if reason == C.REASON_NEW:
        return "added"
    if reason == C.REASON_DELETED:
        return "removed"
    return reason  # content, protection, comment, case, kind, link-target, timestamp, or a combo


def _change_word(reason: str) -> str:
    """Coarse change class for JSON consumers: added / removed / changed."""
    if reason == C.REASON_NEW:
        return "added"
    if reason == C.REASON_DELETED:
        return "removed"
    return "changed"


def _display_path(change: C.DiffEntry, mode: str) -> str:
    """The path to show: relative to the volume in path mode, volume-qualified otherwise."""
    return change.entry.relative if mode == "path" else change.entry.path


def _change_dict(change: C.DiffEntry, mode: str) -> dict[str, Any]:
    d: dict[str, Any] = {
        "change": _change_word(change.reason),
        "reason": change.reason,
        "path": _display_path(change, mode),
        "kind": change.entry.kind,
    }
    if change.entry.size:
        d["size"] = change.entry.size
    return d


def _summary_counts(comparison: C.DiffResult) -> dict[str, int]:
    """Per-change-class counts for the human summary, using the same words as the table:
    `new`->added, `deleted`->removed, every other reason (content, protection, ...) kept.
    JSON keeps the raw `by_reason` so scripts see the precise capture vocabulary."""
    out: dict[str, int] = {}
    for reason, n in comparison.by_reason().items():
        key = _status(reason)
        out[key] = out.get(key, 0) + n
    return dict(sorted(out.items()))


def _source_desc(result: C.CaptureResult) -> str:
    if not result.volumes:
        return "no mountable volume"
    n = result.stats.files + result.stats.dirs
    return f"{n} entr{'y' if n == 1 else 'ies'}, {', '.join(result.volumes)}"


def _warnings(out: render.Output, warnings: list[str]) -> None:
    if not warnings:
        return
    out.heading(f"warnings ({len(warnings)})")
    for text in warnings:
        out.line(f"  {text}")


def cmd_diff(args: Any, out: render.Output) -> int:
    """Compare two sources and report added, changed and removed entries.

    Exit status is 0 whether or not there are differences: a difference is a finding, not an
    error. Scripts should branch on the `identical` field of `--json`.
    """
    result_a, info_a = _capture_source(args, "a")
    result_b, info_b = _capture_source(args, "b")

    mode = _resolve_mode(args.by, result_a, info_a, result_b, info_b)
    entries_a = _relabel(result_a.entries) if mode == "path" else result_a.entries
    entries_b = _relabel(result_b.entries) if mode == "path" else result_b.entries

    comparison = C.diff(
        entries_a,
        entries_b,
        timestamps_significant=args.timestamps_significant,
        deletions=not args.no_deletions,
    )
    warnings = result_a.warnings + result_b.warnings

    if out.as_json:
        out.data(
            {
                "a": {**info_a, "volumes": result_a.volumes,
                      "entries": len(result_a.entries), "warnings": result_a.warnings},
                "b": {**info_b, "volumes": result_b.volumes,
                      "entries": len(result_b.entries), "warnings": result_b.warnings},
                "by": mode,
                "identical": comparison.is_empty,
                "changes": [_change_dict(c, mode) for c in comparison.changes],
                "by_reason": comparison.by_reason(),
                "unchanged": comparison.unchanged,
            }
        )
        return 0

    out.field("A", f"{info_a['spec']}  ({_source_desc(result_a)})")
    out.field("B", f"{info_b['spec']}  ({_source_desc(result_b)})")
    out.field("by", mode)
    out.line()

    if comparison.is_empty:
        out.line("identical -- no differences")
    else:
        out.field("changes", len(comparison.changes))
        for change_class, count in _summary_counts(comparison).items():
            out.line(f"  {change_class:<14} {count}")
        out.line()
        table = render.Table(headers=["change", "path", "size"], align=["l", "l", "r"])
        for change in comparison.changes:
            table.add(
                _status(change.reason),
                _display_path(change, mode),
                render.human_bytes(change.entry.size) if change.entry.size else "",
            )
        out.table(table)
    out.field("unchanged", comparison.unchanged)
    _warnings(out, warnings)
    return 0
