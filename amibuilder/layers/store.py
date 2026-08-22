"""The layer store: layers, refs, recipes, candidates and the blob pool.

```
store/
  blobs/                     content-addressed, see blobs.py
  layers/<layer-id>/
    layer.json               metadata, plus the RDB drive record for base layers
    manifest.jsonl           one entry per path, sorted
  candidates/<label>/        a capture awaiting review; same two files
  refs/<label>               a file containing a layer id
  recipes/<name>.json        an ordered list of layers to compose
```

**A layer ID is the hash of what the layer *is*** -- its manifest bytes, its kind, its
parent, and its drive record -- and deliberately not of its label, creation time or source
path. Two consequences follow, both wanted: re-capturing unchanged content is idempotent
rather than accumulating near-duplicate layers, and a diff captured against a different
parent is a different layer even when the file list matches, because composing it means
something different.

**Blobs are written during capture, not at `commit`.** The design sketch put them at commit
so an abandoned candidate left nothing behind, but computing a diff already requires reading
and hashing every file, so the only additional cost is compressing the entries that actually
changed -- unchanged files deduplicate against the parent's blobs instantly. Deferring would
mean a second full read of a multi-gigabyte image to save disk that `snap gc` reclaims
anyway. Content addressing is what makes this safe: an early write is idempotent and cannot
corrupt anything already stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from ..errors import ImageError, NotFoundError, UsageError
from . import manifest as M
from .blobs import BlobStore, is_valid_hash

#: Bumped if the identity computation ever changes, so IDs from different schemes cannot
#: silently collide.
ID_SCHEME = "amibuilder-layer-v1"

KIND_BASE = "base"
KIND_DIFF = "diff"
KINDS = (KIND_BASE, KIND_DIFF)

BLOBS_DIR = "blobs"
LAYERS_DIR = "layers"
REFS_DIR = "refs"
RECIPES_DIR = "recipes"
CANDIDATES_DIR = "candidates"

LAYER_JSON = "layer.json"
MANIFEST_JSONL = "manifest.jsonl"

#: Environment override for the store location.
STORE_ENV_VAR = "AMIBUILDER_STORE"
DEFAULT_STORE = "~/.amibuilder/store"

#: Labels name refs, candidates and recipes. Kept conservative so a label is always safe as
#: a single path component and can never be mistaken for a layer ID.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

#: Shortest layer-ID prefix accepted on the command line. Four keeps accidental matches
#: unlikely while staying typeable; ambiguity is reported rather than guessed at.
MIN_PREFIX = 4


def default_store_path() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get(STORE_ENV_VAR) or DEFAULT_STORE))


def check_label(label: str) -> str:
    """Validate a ref, candidate or recipe name."""
    if not LABEL_RE.match(label or ""):
        raise UsageError(
            f"invalid label {label!r} -- use letters, digits, dot, plus, dash or "
            "underscore, starting with a letter or digit"
        )
    if is_valid_hash(label):
        raise UsageError(
            f"label {label!r} looks like a layer ID, which would be ambiguous; choose another"
        )
    return label


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing. Sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_layer_id(
    *,
    manifest_bytes: bytes,
    kind: str,
    parent: str | None,
    drive: dict[str, Any] | None,
) -> str:
    """Identity of a layer: what it contains and what it means, nothing else.

    Excludes label, creation time and source path on purpose -- those describe the act of
    capturing, not the layer, and including them would make an identical re-capture look
    like a new layer.
    """
    if kind not in KINDS:
        raise UsageError(f"unknown layer kind {kind!r} (expected one of {', '.join(KINDS)})")
    h = hashlib.sha256()
    h.update(ID_SCHEME.encode("ascii") + b"\n")
    h.update(kind.encode("ascii") + b"\n")
    h.update((parent or "").encode("ascii") + b"\n")
    h.update((canonical_json(drive) if drive is not None else "").encode("ascii") + b"\n")
    h.update(manifest_bytes)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Layer metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerStats:
    """Summary counts, recorded so `snap ls` need not read every manifest."""

    entries: int = 0
    files: int = 0
    dirs: int = 0
    whiteouts: int = 0
    links: int = 0
    #: Sum of file sizes as they appear on the Amiga.
    content_size: int = 0
    #: Sum of the layer's blobs as stored, after compression and dedup within the layer.
    stored_size: int = 0

    @classmethod
    def of(cls, entries: list[M.ManifestEntry], blobs: BlobStore | None = None) -> LayerStats:
        by_kind = M.counts(entries)
        stored = 0
        if blobs is not None:
            # Distinct blobs only: a layer holding the same content at two paths occupies
            # that content once.
            for blob_hash in {e.blob for e in entries if e.blob}:
                found = blobs.find(blob_hash)
                if found is not None:
                    stored += os.path.getsize(found[0])
        return cls(
            entries=len(entries),
            files=by_kind[M.FILE],
            dirs=by_kind[M.DIR],
            whiteouts=by_kind[M.WHITEOUT],
            links=by_kind[M.HARDLINK] + by_kind[M.SOFTLINK],
            content_size=M.total_size(entries),
            stored_size=stored,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "files": self.files,
            "dirs": self.dirs,
            "whiteouts": self.whiteouts,
            "links": self.links,
            "content_size": self.content_size,
            "stored_size": self.stored_size,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LayerStats:
        return cls(**{k: int(d.get(k, 0)) for k in cls().to_json()})


@dataclass(frozen=True)
class Layer:
    """A layer's `layer.json`."""

    id: str
    kind: str
    label: str = ""
    parent: str | None = None
    created: str = ""
    #: Where it came from: {"kind": "rdb-hdf", "path": ..., "size": ...}. Informational.
    source: dict[str, Any] = field(default_factory=dict)
    #: RDB drive record. Base layers only -- see drive.py and layers doc section 4.
    drive: dict[str, Any] | None = None
    stats: LayerStats = field(default_factory=LayerStats)
    tool_version: str = ""

    @property
    def is_base(self) -> bool:
        return self.kind == KIND_BASE

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "parent": self.parent,
            "created": self.created,
            "source": self.source,
            "stats": self.stats.to_json(),
            "tool_version": self.tool_version,
        }
        if self.drive is not None:
            d["drive"] = self.drive
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Layer:
        try:
            layer_id = d["id"]
            kind = d["kind"]
        except KeyError as e:
            raise ImageError(f"layer.json missing required field {e.args[0]!r}") from e
        return cls(
            id=layer_id,
            kind=kind,
            label=d.get("label", ""),
            parent=d.get("parent"),
            created=d.get("created", ""),
            source=d.get("source") or {},
            drive=d.get("drive"),
            stats=LayerStats.from_json(d.get("stats") or {}),
            tool_version=d.get("tool_version", ""),
        )


def now_iso() -> str:
    """UTC, second precision. Metadata only -- never used for comparison."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("amibuilder")
    except Exception:  # pragma: no cover - source checkout without metadata
        return "unknown"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """A layer store on disk. Created lazily; reading an absent store is not an error."""

    def __init__(self, root: str | None = None):
        self.root = os.path.abspath(os.path.expanduser(root or default_store_path()))
        self.blobs = BlobStore(os.path.join(self.root, BLOBS_DIR))

    # -- layout -------------------------------------------------------------

    @property
    def layers_root(self) -> str:
        return os.path.join(self.root, LAYERS_DIR)

    @property
    def refs_root(self) -> str:
        return os.path.join(self.root, REFS_DIR)

    @property
    def recipes_root(self) -> str:
        return os.path.join(self.root, RECIPES_DIR)

    @property
    def candidates_root(self) -> str:
        return os.path.join(self.root, CANDIDATES_DIR)

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.layers_root)

    def init(self) -> None:
        for path in (
            self.root,
            os.path.join(self.root, BLOBS_DIR),
            self.layers_root,
            self.refs_root,
            self.recipes_root,
            self.candidates_root,
        ):
            os.makedirs(path, exist_ok=True)

    def layer_dir(self, layer_id: str) -> str:
        return os.path.join(self.layers_root, layer_id)

    def has_layer(self, layer_id: str) -> bool:
        return os.path.isfile(os.path.join(self.layer_dir(layer_id), LAYER_JSON))

    # -- writing layers -----------------------------------------------------

    def write_layer(
        self,
        *,
        entries: list[M.ManifestEntry],
        kind: str,
        label: str = "",
        parent: str | None = None,
        source: dict[str, Any] | None = None,
        drive: dict[str, Any] | None = None,
    ) -> Layer:
        """Create a layer from entries whose blobs are already stored.

        Idempotent: writing identical content twice returns the existing layer rather than
        duplicating it, which is what makes re-capture cheap and safe.
        """
        self.init()
        ordered = M.sort_entries(entries)
        manifest_bytes = M.canonical_bytes(ordered)
        layer_id = compute_layer_id(
            manifest_bytes=manifest_bytes, kind=kind, parent=parent, drive=drive
        )
        if self.has_layer(layer_id):
            return self.read_layer(layer_id)

        layer = Layer(
            id=layer_id,
            kind=kind,
            label=label,
            parent=parent,
            created=now_iso(),
            source=source or {},
            drive=drive,
            stats=LayerStats.of(ordered, self.blobs),
            tool_version=tool_version(),
        )
        self._write_layer_dir(self.layer_dir(layer_id), layer, manifest_bytes)
        return layer

    def _write_layer_dir(self, target: str, layer: Layer, manifest_bytes: bytes) -> None:
        """Materialise a layer directory atomically.

        Staged in a sibling temporary directory and renamed, so an interrupted write leaves
        no half-formed layer for a later command to trust.
        """
        os.makedirs(os.path.dirname(target), exist_ok=True)
        staging = tempfile.mkdtemp(dir=os.path.dirname(target), prefix=".layer-")
        try:
            with open(os.path.join(staging, MANIFEST_JSONL), "wb") as fh:
                fh.write(manifest_bytes)
            with open(os.path.join(staging, LAYER_JSON), "w", encoding="utf-8") as fh:
                json.dump(layer.to_json(), fh, indent=2, sort_keys=True)
                fh.write("\n")
            try:
                os.rename(staging, target)
                staging = ""
            except OSError:
                # Another process won the race, or the directory already exists. Either way
                # the content is identical, because the name is its hash.
                if not self.has_layer(layer.id):
                    raise
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)

    # -- reading layers -----------------------------------------------------

    def read_layer(self, layer_id: str) -> Layer:
        path = os.path.join(self.layer_dir(layer_id), LAYER_JSON)
        if not os.path.isfile(path):
            raise NotFoundError(f"no such layer: {layer_id}")
        with open(path, encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as e:
                raise ImageError(f"layer {layer_id}: layer.json is not valid JSON ({e})") from e
        layer = Layer.from_json(data)
        if layer.id != layer_id:
            raise ImageError(
                f"layer {layer_id}: layer.json claims id {layer.id!r}; the store is inconsistent"
            )
        return layer

    def manifest_path(self, layer_id: str) -> str:
        path = os.path.join(self.layer_dir(layer_id), MANIFEST_JSONL)
        if not os.path.isfile(path):
            raise NotFoundError(f"layer {layer_id} has no manifest")
        return path

    def read_manifest(self, layer_id: str) -> list[M.ManifestEntry]:
        return M.load(self.manifest_path(layer_id))

    def iter_manifest(self, layer_id: str) -> Iterator[M.ManifestEntry]:
        """Stream a manifest, for layers too large to hold comfortably."""
        with open(self.manifest_path(layer_id), "rb") as fh:
            yield from M.read(fh)

    def iter_layer_ids(self) -> Iterator[str]:
        if not os.path.isdir(self.layers_root):
            return
        for name in sorted(os.listdir(self.layers_root)):
            if is_valid_hash(name) and self.has_layer(name):
                yield name

    def list_layers(self) -> list[Layer]:
        return [self.read_layer(i) for i in self.iter_layer_ids()]

    # -- refs ---------------------------------------------------------------

    def ref_path(self, label: str) -> str:
        return os.path.join(self.refs_root, check_label(label))

    def set_ref(self, label: str, layer_id: str) -> None:
        if not self.has_layer(layer_id):
            raise NotFoundError(f"cannot point {label!r} at unknown layer {layer_id}")
        self.init()
        path = self.ref_path(label)
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(layer_id + "\n")
        os.replace(tmp, path)

    def get_ref(self, label: str) -> str | None:
        path = os.path.join(self.refs_root, label)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
        if not is_valid_hash(value):
            raise ImageError(f"ref {label!r} does not contain a layer id: {value[:80]!r}")
        return value

    def delete_ref(self, label: str) -> bool:
        path = os.path.join(self.refs_root, label)
        if not os.path.isfile(path):
            return False
        os.unlink(path)
        return True

    def iter_refs(self) -> Iterator[tuple[str, str]]:
        if not os.path.isdir(self.refs_root):
            return
        for name in sorted(os.listdir(self.refs_root)):
            if name.startswith(".") or name.endswith(".new"):
                continue
            value = self.get_ref(name)
            if value:
                yield name, value

    def refs_for(self, layer_id: str) -> list[str]:
        """Every label pointing at a layer. A layer can have several."""
        return [name for name, value in self.iter_refs() if value == layer_id]

    # -- resolution ---------------------------------------------------------

    def resolve(self, spec: str) -> str:
        """Turn a ref name, full layer ID or unambiguous ID prefix into a layer ID.

        Refs win over ID prefixes. That is the useful precedence: labels are what a person
        types, and `check_label` already forbids a label that looks like an ID, so the two
        namespaces cannot genuinely collide.
        """
        if not spec:
            raise UsageError("no layer specified")
        ref = self.get_ref(spec)
        if ref is not None:
            if not self.has_layer(ref):
                raise ImageError(
                    f"ref {spec!r} points at missing layer {ref} -- the store is inconsistent"
                )
            return ref
        if is_valid_hash(spec) and self.has_layer(spec):
            return spec

        lowered = spec.lower()
        if len(lowered) >= MIN_PREFIX and all(c in "0123456789abcdef" for c in lowered):
            matches = [i for i in self.iter_layer_ids() if i.startswith(lowered)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                listed = ", ".join(m[:12] for m in matches[:5])
                raise UsageError(
                    f"layer id prefix {spec!r} is ambiguous, matching {len(matches)}: {listed}"
                )
        raise NotFoundError(f"no layer, ref or id prefix matching {spec!r}")

    # -- candidates ---------------------------------------------------------

    def candidate_dir(self, label: str) -> str:
        return os.path.join(self.candidates_root, check_label(label))

    def has_candidate(self, label: str) -> bool:
        return os.path.isfile(os.path.join(self.candidate_dir(label), LAYER_JSON))

    def write_candidate(
        self,
        label: str,
        *,
        entries: list[M.ManifestEntry],
        kind: str,
        parent: str | None = None,
        source: dict[str, Any] | None = None,
        drive: dict[str, Any] | None = None,
    ) -> Layer:
        """Stage a capture for review. Overwrites any existing candidate of that name.

        The recorded ID is provisional: `snap review --drop` changes the manifest, so the
        final ID is only computed at commit.
        """
        self.init()
        ordered = M.sort_entries(entries)
        manifest_bytes = M.canonical_bytes(ordered)
        provisional = compute_layer_id(
            manifest_bytes=manifest_bytes, kind=kind, parent=parent, drive=drive
        )
        layer = Layer(
            id=provisional,
            kind=kind,
            label=label,
            parent=parent,
            created=now_iso(),
            source=source or {},
            drive=drive,
            stats=LayerStats.of(ordered, self.blobs),
            tool_version=tool_version(),
        )
        target = self.candidate_dir(label)
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, MANIFEST_JSONL), "wb") as fh:
            fh.write(manifest_bytes)
        with open(os.path.join(target, LAYER_JSON), "w", encoding="utf-8") as fh:
            json.dump(layer.to_json(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        return layer

    def read_candidate(self, label: str) -> tuple[Layer, list[M.ManifestEntry]]:
        base = self.candidate_dir(label)
        meta = os.path.join(base, LAYER_JSON)
        if not os.path.isfile(meta):
            raise NotFoundError(
                f"no candidate named {label!r} -- `snap diff` creates one, `snap ls --candidates` "
                "lists them"
            )
        with open(meta, encoding="utf-8") as fh:
            layer = Layer.from_json(json.load(fh))
        return layer, M.load(os.path.join(base, MANIFEST_JSONL))

    def update_candidate(self, label: str, entries: list[M.ManifestEntry]) -> Layer:
        """Replace a candidate's manifest, as `snap review --drop` does."""
        layer, _ = self.read_candidate(label)
        return self.write_candidate(
            label,
            entries=entries,
            kind=layer.kind,
            parent=layer.parent,
            source=layer.source,
            drive=layer.drive,
        )

    def iter_candidates(self) -> Iterator[str]:
        if not os.path.isdir(self.candidates_root):
            return
        for name in sorted(os.listdir(self.candidates_root)):
            if not name.startswith(".") and self.has_candidate(name):
                yield name

    def delete_candidate(self, label: str) -> bool:
        target = self.candidate_dir(label)
        if not os.path.isdir(target):
            return False
        shutil.rmtree(target)
        return True

    def commit_candidate(self, label: str, *, ref: str | None = None) -> Layer:
        """Promote a candidate to a real layer and point a ref at it.

        The candidate is removed only after the layer exists, so an interruption leaves the
        candidate intact and the operation repeatable.
        """
        candidate, entries = self.read_candidate(label)
        layer = self.write_layer(
            entries=entries,
            kind=candidate.kind,
            label=candidate.label or label,
            parent=candidate.parent,
            source=candidate.source,
            drive=candidate.drive,
        )
        self.set_ref(ref or label, layer.id)
        self.delete_candidate(label)
        return layer

    # -- recipes ------------------------------------------------------------

    def recipe_path(self, name: str) -> str:
        return os.path.join(self.recipes_root, check_label(name) + ".json")

    def write_recipe(self, name: str, layers: list[str], *, description: str = "",
                     policies: dict[str, str] | None = None) -> dict[str, Any]:
        """Record an ordered list of layers, and optionally a per-volume compose policy.

        Stored as written -- ref names stay ref names -- so a recipe follows a moving label
        rather than freezing to whatever it pointed at on the day. Resolution happens at
        compose time, where an unknown entry can be reported usefully.

        `policies` records intent that the drive record cannot: a save-games volume looks
        exactly like a work volume from outside, so `preserve` can only ever be stated, never
        inferred (`layers.drive.default_policy`). Recording it in the recipe -- which is
        rewritable and outside the layer identity hash, unlike the drive record -- is what lets
        `compose --recipe` apply it without the caller re-typing `--policy` every time. The
        values are validated by the caller before they reach here.
        """
        if not layers:
            raise UsageError("a recipe needs at least one layer")
        for spec in layers:
            self.resolve(spec)  # fail now rather than at compose time
        self.init()
        recipe = {
            "name": check_label(name),
            "layers": list(layers),
            "policies": dict(policies or {}),
            "description": description,
            "created": now_iso(),
        }
        path = self.recipe_path(name)
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(recipe, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return recipe

    def read_recipe(self, name: str) -> dict[str, Any]:
        path = os.path.join(self.recipes_root, name + ".json")
        if not os.path.isfile(path):
            raise NotFoundError(f"no recipe named {name!r}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def iter_recipes(self) -> Iterator[str]:
        if not os.path.isdir(self.recipes_root):
            return
        for name in sorted(os.listdir(self.recipes_root)):
            if name.endswith(".json") and not name.startswith("."):
                yield name[: -len(".json")]

    def delete_recipe(self, name: str) -> bool:
        path = os.path.join(self.recipes_root, name + ".json")
        if not os.path.isfile(path):
            return False
        os.unlink(path)
        return True

    # -- ancestry -----------------------------------------------------------

    def chain(self, layer_id: str) -> list[Layer]:
        """A layer and its ancestors, base first.

        Detects a cycle rather than looping forever. A cycle should be impossible, since a
        parent must exist before its child can be hashed, but a hand-edited store is a
        thing that happens.
        """
        out: list[Layer] = []
        seen: set[str] = set()
        current: str | None = layer_id
        while current:
            if current in seen:
                raise ImageError(f"layer ancestry contains a cycle at {current[:12]}")
            seen.add(current)
            layer = self.read_layer(current)
            out.append(layer)
            current = layer.parent
        out.reverse()
        return out

    def children(self, layer_id: str) -> list[Layer]:
        return [layer for layer in self.list_layers() if layer.parent == layer_id]

    # -- integrity and housekeeping ----------------------------------------

    def verify_layer(self, layer_id: str) -> list[str]:
        """Check a layer against itself and the blob pool. Returns problem descriptions.

        Three checks: the manifest still hashes to the layer's own ID, every referenced
        blob is present, and every present blob matches its own hash.
        """
        problems: list[str] = []
        layer = self.read_layer(layer_id)
        entries = self.read_manifest(layer_id)

        recomputed = compute_layer_id(
            manifest_bytes=M.canonical_bytes(entries),
            kind=layer.kind,
            parent=layer.parent,
            drive=layer.drive,
        )
        if recomputed != layer_id:
            problems.append(
                f"manifest does not match the layer id (recomputed {recomputed[:12]}); "
                "the manifest has been modified since it was written"
            )

        if layer.parent and not self.has_layer(layer.parent):
            problems.append(f"parent layer {layer.parent[:12]} is missing from the store")

        for blob_hash in sorted({e.blob for e in entries if e.blob}):
            if not self.blobs.has(blob_hash):
                problems.append(f"blob missing: {blob_hash[:12]}")
            elif not self.blobs.verify(blob_hash):
                problems.append(f"blob corrupt: {blob_hash[:12]}")
        return problems

    def reachable_blobs(self) -> set[str]:
        """Blobs referenced by any layer or candidate.

        Candidates count as roots. They represent work in progress, and reclaiming their
        blobs would quietly empty a capture the user has not finished reviewing.
        """
        reachable: set[str] = set()
        for layer_id in self.iter_layer_ids():
            for entry in self.iter_manifest(layer_id):
                if entry.blob:
                    reachable.add(entry.blob)
        for label in self.iter_candidates():
            _layer, entries = self.read_candidate(label)
            for entry in entries:
                if entry.blob:
                    reachable.add(entry.blob)
        return reachable

    def unreferenced_blobs(self) -> list[str]:
        reachable = self.reachable_blobs()
        return sorted(h for h in self.blobs.iter_hashes() if h not in reachable)

    def gc(self, *, dry_run: bool = False) -> tuple[int, int]:
        """Drop blobs no layer or candidate references.

        Returns `(count, bytes)`. Also clears `.part` files from interrupted writes.
        """
        self.blobs.clean_partials()
        victims = self.unreferenced_blobs()
        freed = 0
        for blob_hash in victims:
            found = self.blobs.find(blob_hash)
            if found is None:
                continue
            freed += os.path.getsize(found[0])
            if not dry_run:
                self.blobs.delete(blob_hash)
        return len(victims), freed

    def remove_layer(self, layer_id: str, *, force: bool = False) -> list[str]:
        """Delete a layer. Returns the refs that were removed with it.

        Refuses when another layer names this one as its parent, because that would leave a
        chain that cannot be composed. `force` overrides, for cleaning up a store where the
        child is also going.
        """
        layer = self.read_layer(layer_id)
        kids = self.children(layer_id)
        if kids and not force:
            names = ", ".join((k.label or k.id[:12]) for k in kids[:5])
            raise UsageError(
                f"layer {layer.label or layer_id[:12]} is the parent of {len(kids)} "
                f"other layer(s): {names}. Remove those first, or pass --force"
            )
        removed = self.refs_for(layer_id)
        for name in removed:
            self.delete_ref(name)
        shutil.rmtree(self.layer_dir(layer_id), ignore_errors=True)
        return removed


__all__ = [
    "CANDIDATES_DIR",
    "ID_SCHEME",
    "KIND_BASE",
    "KIND_DIFF",
    "Layer",
    "LayerStats",
    "STORE_ENV_VAR",
    "Store",
    "canonical_json",
    "check_label",
    "compute_layer_id",
    "default_store_path",
    "now_iso",
    "replace",
]
