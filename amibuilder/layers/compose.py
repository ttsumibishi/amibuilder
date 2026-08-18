"""Planning a composition: turning a layer stack back into a drive.

Nothing here writes. The whole point of separating the plan from the write is that every
decision and every refusal happens while the target is still untouched — so a naming limit, a
capacity overflow or a partition-offset mistake is a message rather than a half-written card.

Three things about the model are worth understanding before reading the code.

**Deletion happens by omission, not by deleting.** A whiteout removes a path from the flattened
set, so the composed drive simply never receives that file. That is why composing into a fresh
volume is the safe path (`KIP-FFS-LAYERS.md` §1): there is no delete operation to get wrong, and
it sidesteps amitools' refusal to overwrite an existing file (notes G22) entirely, because each
path is written exactly once.

**Last-wins is the requested behaviour, and collisions are expected.** Several Amiga installers
ship the same `reqtools.library`. The planner reports which layers supplied a path and which one
won, rather than complaining.

**Per-volume policy decides how much is destroyed**, and it is the only thing here that can lose
data. `replace` formats, `merge` writes into what is already there, `preserve` writes nothing.
The plan states this per volume so the consequence is explicit rather than inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from ..errors import UsageError
from . import drive as D
from . import manifest as M
from .store import KIND_BASE, Layer, Store

#: A problem that must be fixed before writing. Composition refuses.
BLOCKING = "blocking"
#: Worth saying, but not a refusal.
ADVISORY = "advisory"

#: Classic FFS filename limit, per path component (notes G1).
NAME_LIMIT = 30
#: Long-filename FFS (DOS6/DOS7) raises it.
NAME_LIMIT_LNFS = 110
#: Comment field is 80 bytes, so 79 usable characters (notes G2).
COMMENT_LIMIT = 79
#: Illegal in an AmigaDOS filename.
ILLEGAL_NAME_CHARS = (":", "/")

#: Low byte of the DosType for the two long-filename variants. Spelled out rather than derived
#: from a bitmask because the DOS4-7 values are an enumeration, not cleanly bitwise: 4 and 5
#: mean dircache while 6 and 7 mean long filenames, and both sets imply international hashing.
LNFS_DOS_TYPES = (0x06, 0x07)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """Something wrong with the plan. Discovered before anything is written."""

    severity: str
    message: str
    path: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.severity == BLOCKING

    def as_dict(self) -> dict[str, Any]:
        d = {"severity": self.severity, "message": self.message}
        if self.path:
            d["path"] = self.path
        return d


@dataclass(frozen=True)
class Conflict:
    """A path supplied by more than one layer. Legal, expected, reported."""

    path: str
    #: Layer IDs in stack order; the last is the winner.
    layers: tuple[str, ...]

    @property
    def winner(self) -> str:
        return self.layers[-1]

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "layers": list(self.layers), "winner": self.winner}


@dataclass(frozen=True)
class PlanEntry:
    """One path in the composed result, and where it came from."""

    entry: M.ManifestEntry
    #: Layer ID that supplied the winning version.
    source: str
    #: Layer IDs whose version was overwritten, in stack order.
    shadowed: tuple[str, ...] = ()


@dataclass(frozen=True)
class VolumePlan:
    """What happens to one volume."""

    volume: str
    policy: str
    #: The drive record's partition entry, when the stack carries a drive record.
    partition: dict[str, Any] | None
    format_volume: bool
    write: bool
    entries: tuple[PlanEntry, ...] = ()
    #: Whiteouts that cannot take effect, because the volume is merged rather than formatted so
    #: any pre-existing copy on the target survives. Reported rather than silently dropped.
    ineffective_whiteouts: tuple[str, ...] = ()

    @property
    def destroys_existing_data(self) -> bool:
        return self.format_volume

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries if e.entry.kind in M.CONTENT_KINDS)

    @property
    def content_bytes(self) -> int:
        return sum(e.entry.size for e in self.entries if e.entry.kind in M.CONTENT_KINDS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume": self.volume,
            "policy": self.policy,
            "format": self.format_volume,
            "write": self.write,
            "entries": len(self.entries),
            "files": self.file_count,
            "content_bytes": self.content_bytes,
            "destroys_existing_data": self.destroys_existing_data,
            "ineffective_whiteouts": list(self.ineffective_whiteouts),
        }


@dataclass
class Plan:
    """A complete composition plan. Inspectable before anything is written."""

    layers: list[Layer] = field(default_factory=list)
    volumes: list[VolumePlan] = field(default_factory=list)
    drive: dict[str, Any] | None = None
    conflicts: list[Conflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def blocking(self) -> list[Problem]:
        return [p for p in self.problems if p.is_blocking]

    @property
    def is_writable(self) -> bool:
        return not self.blocking

    @property
    def entries(self) -> list[PlanEntry]:
        return [e for vol in self.volumes for e in vol.entries]

    @property
    def total_files(self) -> int:
        return sum(vol.file_count for vol in self.volumes)

    @property
    def total_content_bytes(self) -> int:
        return sum(vol.content_bytes for vol in self.volumes)

    @property
    def destroyed_volumes(self) -> list[str]:
        return [vol.volume for vol in self.volumes if vol.destroys_existing_data]

    @property
    def preserved_volumes(self) -> list[str]:
        return [vol.volume for vol in self.volumes if not vol.write]

    def volume(self, name: str) -> VolumePlan:
        needle = name.rstrip(":").casefold()
        for vol in self.volumes:
            if vol.volume.casefold() == needle:
                return vol
        raise UsageError(f"plan has no volume named {name!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "layers": [
                {"id": layer.id, "label": layer.label, "kind": layer.kind}
                for layer in self.layers
            ],
            "volumes": [vol.as_dict() for vol in self.volumes],
            "total_files": self.total_files,
            "total_content_bytes": self.total_content_bytes,
            "destroys": self.destroyed_volumes,
            "preserves": self.preserved_volumes,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "warnings": list(self.warnings),
            "problems": [p.as_dict() for p in self.problems],
            "writable": self.is_writable,
        }


# ---------------------------------------------------------------------------
# Stack resolution
# ---------------------------------------------------------------------------


def resolve_stack(store: Store, specs: Sequence[str]) -> list[Layer]:
    """Turn refs, IDs or ID prefixes into layers, in the order given."""
    if not specs:
        raise UsageError("a composition needs at least one layer")
    return [store.read_layer(store.resolve(spec)) for spec in specs]


def stack_from_recipe(store: Store, name: str) -> list[Layer]:
    recipe = store.read_recipe(name)
    return resolve_stack(store, recipe.get("layers") or [])


def check_provenance(layers: Sequence[Layer]) -> list[str]:
    """Warn where a diff layer's recorded parent is absent from the stack.

    A warning rather than a refusal, deliberately: for an additive software layer this is almost
    always fine, and blocking it would be obstructive (`KIP-FFS-LAYERS.md` §6). `--strict-parents`
    is what turns these into errors.
    """
    present = {layer.id for layer in layers}
    warnings: list[str] = []
    for layer in layers:
        if layer.kind == KIND_BASE or layer.parent is None:
            continue
        if layer.parent not in present:
            warnings.append(
                f"layer '{layer.label or layer.id[:12]}' was captured against "
                f"{layer.parent[:12]}, which is not in this stack"
            )
    return warnings


def base_layer(layers: Sequence[Layer]) -> Layer | None:
    """The layer carrying the drive record. The first base layer in the stack wins."""
    for layer in layers:
        if layer.kind == KIND_BASE and layer.drive:
            return layer
    return None


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def _descendant_prefix(path: str) -> str:
    """Prefix that matches everything beneath `path`, case-folded."""
    return M.fold(path) + "/"


def flatten(
    layer_entries: Sequence[tuple[Layer, Iterable[M.ManifestEntry]]],
    *,
    deletions: bool = True,
) -> tuple[dict[str, PlanEntry], list[Conflict]]:
    """Composite a stack into the final path set, last layer winning.

    Whiteouts remove paths rather than being recorded as deletions to perform — see the module
    docstring. A whiteout naming a directory also removes everything beneath it, which matters
    because a later layer must not be able to leave an orphaned file inside a directory an
    earlier layer removed.

    `deletions=False` ignores whiteouts entirely, making every layer purely additive.
    """
    resolved: dict[str, PlanEntry] = {}
    seen_from: dict[str, list[str]] = {}

    for layer, entries in layer_entries:
        for entry in entries:
            key = entry.folded

            if entry.kind == M.WHITEOUT:
                if not deletions:
                    continue
                resolved.pop(key, None)
                prefix = _descendant_prefix(entry.path)
                for victim in [k for k in resolved if k.startswith(prefix)]:
                    del resolved[victim]
                seen_from.setdefault(key, []).append(layer.id)
                continue

            prior = resolved.get(key)
            shadowed = (*prior.shadowed, prior.source) if prior is not None else ()
            resolved[key] = PlanEntry(entry=entry, source=layer.id, shadowed=shadowed)
            seen_from.setdefault(key, []).append(layer.id)

    conflicts = [
        Conflict(path=resolved[key].entry.path, layers=tuple(sources))
        for key, sources in seen_from.items()
        if len(sources) > 1 and key in resolved
    ]
    conflicts.sort(key=lambda c: M.fold(c.path))
    return resolved, conflicts


def collect_whiteouts(
    layer_entries: Sequence[tuple[Layer, Iterable[M.ManifestEntry]]]
) -> dict[str, list[str]]:
    """Whiteout paths per volume, so a merge plan can report what it cannot remove."""
    out: dict[str, list[str]] = {}
    for _layer, entries in layer_entries:
        for entry in entries:
            if entry.kind == M.WHITEOUT:
                out.setdefault(entry.volume, []).append(entry.path)
    return out


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def name_limit_for(partition: dict[str, Any] | None) -> int:
    """Filename limit for a partition, from its DosType."""
    if not partition:
        return NAME_LIMIT
    raw = partition.get("dos_type")
    try:
        value = int(str(raw), 16) if isinstance(raw, str) else int(raw or 0)
    except (TypeError, ValueError):
        return NAME_LIMIT
    return NAME_LIMIT_LNFS if (value & 0xFF) in LNFS_DOS_TYPES else NAME_LIMIT


def check_names(
    entries: Iterable[PlanEntry], *, name_limit: int = NAME_LIMIT
) -> list[Problem]:
    """Check every path against the FFS naming rules.

    Run over the whole plan before writing, because the alternative is aborting halfway and
    leaving a partially populated volume — which is exactly what notes G1 warns about.
    """
    problems: list[Problem] = []
    for plan_entry in entries:
        entry = plan_entry.entry
        _volume, relative = M.split_path(entry.path)
        for component in relative.split("/"):
            if not component:
                continue
            if len(component) > name_limit:
                problems.append(
                    Problem(
                        BLOCKING,
                        f"name is {len(component)} characters, limit is {name_limit}: "
                        f"{component!r}",
                        entry.path,
                    )
                )
            for bad in ILLEGAL_NAME_CHARS:
                if bad in component:
                    problems.append(
                        Problem(BLOCKING, f"name contains {bad!r}: {component!r}", entry.path)
                    )
        if len(entry.comment) > COMMENT_LIMIT:
            problems.append(
                Problem(
                    BLOCKING,
                    f"comment is {len(entry.comment)} characters, limit is {COMMENT_LIMIT}",
                    entry.path,
                )
            )
    return problems


def estimate_blocks(entries: Iterable[PlanEntry], block_bytes: int) -> int:
    """Rough block count a volume's contents will occupy.

    One header block per entry plus the data blocks each file needs. Deliberately an
    approximation — it ignores file-extension blocks and the bitmap itself — so it is used to
    catch an overflow that is not close, and to advise when the fit is tight. Claiming precision
    here would be worse than admitting the estimate.
    """
    if block_bytes <= 0:
        return 0
    total = 0
    for plan_entry in entries:
        entry = plan_entry.entry
        total += 1  # header block for a file or directory
        if entry.kind in M.CONTENT_KINDS and entry.size:
            total += -(-entry.size // block_bytes)  # ceiling division
    return total


def check_capacity(volume_plan: VolumePlan) -> list[Problem]:
    """Compare the estimate against the partition's real capacity."""
    part = volume_plan.partition
    if not part:
        return []
    env = part.get("dos_env") or {}
    block_bytes = int(env.get("block_size", 0)) * 4 or int(part.get("block_size") or 0)
    available = int(part.get("num_blocks") or 0)
    if not block_bytes or not available:
        return []

    needed = estimate_blocks(volume_plan.entries, block_bytes)
    if needed > available:
        return [
            Problem(
                BLOCKING,
                f"volume {volume_plan.volume}: contents need about {needed} blocks but the "
                f"partition holds {available}",
            )
        ]
    if available and needed > available * 0.9:
        return [
            Problem(
                ADVISORY,
                f"volume {volume_plan.volume}: contents need about {needed} of "
                f"{available} blocks, leaving little free space",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Building the plan
# ---------------------------------------------------------------------------


def _policy_actions(policy: str, *, volume_exists: bool) -> tuple[bool, bool]:
    """`(format_volume, write)` for a policy.

    `preserve` formats only when the volume does not exist yet, and never writes — so on a
    genuinely new drive it creates an empty volume, and thereafter is untouched.
    """
    if policy == D.POLICY_REPLACE:
        return True, True
    if policy == D.POLICY_MERGE:
        return False, True
    if policy == D.POLICY_PRESERVE:
        return (not volume_exists), False
    raise UsageError(f"unknown policy {policy!r}")


def build_plan(
    store: Store,
    specs: Sequence[str],
    *,
    deletions: bool = True,
    policies: dict[str, str] | None = None,
    existing_volumes: Sequence[str] = (),
    strict_parents: bool = False,
    only_volumes: Sequence[str] | None = None,
) -> Plan:
    """Resolve a stack and work out exactly what composing it would do.

    `policies` overrides the drive record's recorded policy per volume, keyed by volume name.
    `existing_volumes` names volumes already present on the target, which is what `preserve`
    needs to decide whether to create one. `only_volumes` restricts the plan, which is how
    partition-granular restores keep their blast radius to one volume.
    """
    layers = resolve_stack(store, specs)
    plan = Plan(layers=layers)

    provenance = check_provenance(layers)
    if strict_parents and provenance:
        plan.problems.extend(Problem(BLOCKING, text) for text in provenance)
    else:
        plan.warnings.extend(provenance)

    base = base_layer(layers)
    plan.drive = base.drive if base else None
    if plan.drive is None:
        plan.warnings.append(
            "no layer in this stack carries a drive record, so the target's layout cannot be "
            "reproduced -- only the 'dir' and 'plain' formats can be composed"
        )

    layer_entries = [(layer, store.read_manifest(layer.id)) for layer in layers]
    resolved, conflicts = flatten(layer_entries, deletions=deletions)
    plan.conflicts = conflicts
    whiteouts = collect_whiteouts(layer_entries) if deletions else {}

    # Group by volume, preserving a stable order: drive-record order first, then any volume the
    # layers mention that the record does not.
    by_volume: dict[str, list[PlanEntry]] = {}
    for plan_entry in resolved.values():
        by_volume.setdefault(plan_entry.entry.volume, []).append(plan_entry)
    for entries in by_volume.values():
        entries.sort(key=lambda pe: pe.entry.sort_key)

    ordered_names: list[str] = []
    if plan.drive:
        ordered_names.extend(D.volumes(plan.drive))
    for name in sorted(by_volume, key=M.fold):
        if name not in ordered_names:
            ordered_names.append(name)

    wanted = None
    if only_volumes is not None:
        wanted = {v.rstrip(":").casefold() for v in only_volumes}

    existing = {v.rstrip(":").casefold() for v in existing_volumes}
    overrides = {k.rstrip(":").casefold(): v for k, v in (policies or {}).items()}

    for name in ordered_names:
        if wanted is not None and name.casefold() not in wanted:
            continue
        partition = None
        if plan.drive:
            try:
                partition = D.find_partition(plan.drive, name)
            except UsageError:
                partition = None

        policy = overrides.get(
            name.casefold(),
            str((partition or {}).get("policy") or D.POLICY_MERGE),
        )
        D.check_policy(policy)
        format_volume, write = _policy_actions(
            policy, volume_exists=name.casefold() in existing
        )

        entries = tuple(by_volume.get(name, ())) if write else ()
        ineffective: tuple[str, ...] = ()
        if write and not format_volume and whiteouts.get(name):
            # The stack wants these gone, but a merge does not delete, so a pre-existing copy on
            # the target survives. Silence here would look like the deletion had been applied.
            ineffective = tuple(sorted(whiteouts[name], key=M.fold))

        volume_plan = VolumePlan(
            volume=name,
            policy=policy,
            partition=partition,
            format_volume=format_volume,
            write=write,
            entries=entries,
            ineffective_whiteouts=ineffective,
        )
        plan.volumes.append(volume_plan)

        if entries:
            plan.problems.extend(
                check_names(entries, name_limit=name_limit_for(partition))
            )
            plan.problems.extend(check_capacity(volume_plan))
        if ineffective:
            plan.warnings.append(
                f"volume {name}: {len(ineffective)} deletion(s) will not take effect under "
                f"the '{policy}' policy; any existing copy on the target remains"
            )

    for name in by_volume:
        if plan.drive and name not in D.volumes(plan.drive):
            plan.warnings.append(
                f"volume {name}: the layers reference it but the drive record does not define "
                "a partition for it"
            )

    if not plan.volumes:
        plan.problems.append(Problem(BLOCKING, "nothing to compose: the plan covers no volumes"))

    return plan


__all__ = [
    "ADVISORY",
    "BLOCKING",
    "COMMENT_LIMIT",
    "Conflict",
    "NAME_LIMIT",
    "NAME_LIMIT_LNFS",
    "Plan",
    "PlanEntry",
    "Problem",
    "VolumePlan",
    "base_layer",
    "build_plan",
    "check_capacity",
    "check_names",
    "check_provenance",
    "collect_whiteouts",
    "estimate_blocks",
    "flatten",
    "name_limit_for",
    "replace",
    "resolve_stack",
    "stack_from_recipe",
]
