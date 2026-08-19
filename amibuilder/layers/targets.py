"""Writing a composed plan out to a target.

The directory target comes first because it is the only one that needs no FFS writing at all --
just host files plus metadata sidecars -- and because it removes images from the iteration loop
entirely: FS-UAE mounts a directory as a hard drive, so a stack can be composed and booted in
seconds without building an image (`KIP-FFS-LAYERS.md` §7).

**Metadata uses the `.uaem` sidecar convention**, which is what WinUAE and FS-UAE read. The
format was taken from amitools' own `MetaInfoFSUAE` rather than guessed:

    PPPPPPPP YYYY-MM-DD HH:MM:SS.TT comment\\n

Protection string, timestamp, two-digit ticks at offsets 20-21, then the comment. One sidecar
per entry named `<entry>.uaem` alongside it -- amitools writes them for directories as well as
files, and none for the volume root.

**The timestamp is rendered from the raw `(days, mins, ticks)` triple, not through amitools'
converter.** amitools' epoch constant is built with `time.mktime` and so carries the host's
January UTC offset, which puts its output an hour out under DST (`KIP-FFS-NOTES.md` §5.7). A
directory written here therefore differs from one written by `xdftool unpack ... fsuae` by that
offset, and ours is the one that matches what the Amiga stored.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import timestamps
from ..errors import UsageError
from . import manifest as M
from .blobs import BlobStore
from .compose import Plan, VolumePlan

UAEM_SUFFIX = ".uaem"
UAEM_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Characters that cannot appear in a host filename, or that cause trouble if they do. `/` and
#: `:` are already illegal in AmigaDOS names so should never arrive, but a hand-edited manifest
#: is a thing that happens and silently creating a path component would be worse than escaping.
_ESCAPE_ALWAYS = frozenset('/:\\"*?<>|')

#: Escaped so the transformation is reversible, matching the UAE `%XX` convention.
_ESCAPE_CHAR = "%"


def escape_name(name: str) -> str:
    """Make an Amiga filename safe to use on the host, UAE-style `%XX` escaping.

    Conservative on purpose: only characters that genuinely cannot be used, control characters,
    a trailing dot or space, and `%` itself so the mapping stays reversible.

    **Unverified:** that FS-UAE decodes `%XX` back on the Amiga side. WinUAE's filesystem
    emulation does, and FS-UAE shares that lineage, but I have not tested it. Since Amiga
    filenames essentially never contain these characters, callers are told when a name was
    escaped rather than having it happen silently.
    """
    out = []
    for char in name:
        if char == _ESCAPE_CHAR or char in _ESCAPE_ALWAYS or ord(char) < 0x20:
            out.append(f"%{ord(char):02x}")
        else:
            out.append(char)
    escaped = "".join(out)
    # A trailing dot or space is legal through the POSIX API but is trimmed or hidden by enough
    # tools that round-tripping it is not worth the risk.
    if escaped and escaped[-1] in " .":
        escaped = escaped[:-1] + f"%{ord(escaped[-1]):02x}"
    return escaped


def uaem_line(entry: M.ManifestEntry) -> str:
    """Render one `.uaem` sidecar's contents, including the trailing newline.

    A zero timestamp is written as `1978-01-01 00:00:00.00`, which is what the triple actually
    says. The format has no way to express "no datestamp", and inventing the current time would
    be worse than reporting the stored value.
    """
    secs, ticks = timestamps.from_triple(*entry.ts)
    when = timestamps.to_datetime(max(secs, 0)).strftime(UAEM_TS_FORMAT)
    return f"{entry.protect} {when}.{ticks:02d} {entry.comment}\n"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    """What a write actually did, or would do under `dry_run`."""

    target: str = ""
    files: int = 0
    dirs: int = 0
    bytes_written: int = 0
    sidecars: int = 0
    volumes: list[str] = field(default_factory=list)
    #: Volume directories that were cleared because their policy formats them.
    cleared: list[str] = field(default_factory=list)
    #: Partitions on an existing drive that this write did not open at all. Reported because on a
    #: partition-granular restore, "what did you *not* touch" is the reassurance the user wants.
    untouched: list[str] = field(default_factory=list)
    #: True when an existing drive was restored into rather than rebuilt from scratch.
    in_place: bool = False
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    #: Image formats only: the size chosen, and where that size came from. Reported rather than
    #: left implicit, because a silently-chosen size is how an image ends up mysteriously full.
    size_bytes: int = 0
    size_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {
            "target": self.target,
            "files": self.files,
            "dirs": self.dirs,
            "bytes_written": self.bytes_written,
            "sidecars": self.sidecars,
            "volumes": list(self.volumes),
            "cleared": list(self.cleared),
            "untouched": list(self.untouched),
            "in_place": self.in_place,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }
        if self.size_bytes:
            d["size_bytes"] = self.size_bytes
            d["size_source"] = self.size_source
        return d


#: Called with (host_path, size_bytes) as each file is written.
ProgressFn = Callable[[str, int], None]


# ---------------------------------------------------------------------------
# Directory target
# ---------------------------------------------------------------------------


def volume_dir(target: str, volume: str) -> str:
    """Where a volume's contents live under the target.

    One subdirectory per volume, always -- including the single-volume case. FS-UAE mounts one
    directory as one volume, so a multi-volume stack has to be split, and keeping the layout
    identical either way means the FS-UAE configuration line does not change shape depending on
    how many partitions a drive happened to have.
    """
    return os.path.join(target, escape_name(volume))


def _host_path(target: str, entry: M.ManifestEntry) -> str:
    volume, relative = M.split_path(entry.path)
    parts = [escape_name(part) for part in relative.split("/") if part]
    return os.path.join(volume_dir(target, volume), *parts)


def _write_sidecar(path: str, entry: M.ManifestEntry, *, dry_run: bool) -> None:
    if dry_run:
        return
    with open(path + UAEM_SUFFIX, "wb") as fh:
        fh.write(uaem_line(entry).encode("utf-8"))


def write_directory(
    plan: Plan,
    blobs: BlobStore,
    target: str,
    *,
    force: bool = False,
    metadata: bool = True,
    dry_run: bool = False,
    on_file: ProgressFn | None = None,
) -> WriteResult:
    """Write a composed plan to a host directory tree.

    Policies map onto a directory naturally: `replace` clears the volume's directory first,
    which is the format equivalent; `merge` writes into whatever is there; `preserve` creates the
    directory if absent and writes nothing.

    Refuses to clear a non-empty directory without `force`, because that is the one operation
    here that can lose data the layers do not contain.
    """
    if not plan.is_writable:
        raise UsageError(
            "the plan has unresolved problems, so nothing was written -- "
            "run with --dry-run to see them"
        )
    if not target:
        raise UsageError("no target directory given")

    result = WriteResult(target=target, dry_run=dry_run)

    # Refuse before creating anything, so a rejected run leaves no half-built tree behind.
    for vol in plan.volumes:
        if not vol.format_volume:
            continue
        path = volume_dir(target, vol.volume)
        if os.path.isdir(path) and os.listdir(path) and not force:
            raise UsageError(
                f"{path} exists and is not empty, and volume {vol.volume}: has the "
                f"'{vol.policy}' policy which clears it. Pass --force to allow that, or use "
                f"--policy {vol.volume}=merge to write into it instead"
            )

    if not dry_run:
        os.makedirs(target, exist_ok=True)

    for vol in plan.volumes:
        result.volumes.append(vol.volume)
        path = volume_dir(target, vol.volume)

        if vol.format_volume and os.path.isdir(path):
            result.cleared.append(vol.volume)
            if not dry_run:
                shutil.rmtree(path)
        if not dry_run:
            os.makedirs(path, exist_ok=True)

        if not vol.write:
            continue
        result.warnings.extend(_write_volume(vol, blobs, target, result, dry_run, metadata,
                                             on_file))
    return result


def _write_volume(
    vol: VolumePlan,
    blobs: BlobStore,
    target: str,
    result: WriteResult,
    dry_run: bool,
    metadata: bool,
    on_file: ProgressFn | None,
) -> list[str]:
    """Write one volume's entries. Entries arrive sorted, so parents precede children."""
    warnings: list[str] = []
    for plan_entry in vol.entries:
        entry = plan_entry.entry
        host = _host_path(target, entry)

        if escape_name(entry.relative.split("/")[-1] or entry.volume) != (
            entry.relative.split("/")[-1] or entry.volume
        ):
            warnings.append(
                f"{entry.path}: name contains characters escaped as %XX on the host "
                f"({os.path.basename(host)})"
            )

        if entry.kind == M.DIR:
            if not dry_run:
                os.makedirs(host, exist_ok=True)
            result.dirs += 1
        elif entry.kind == M.FILE:
            parent = os.path.dirname(host)
            if not os.path.isdir(parent) and not dry_run:
                # The manifest should carry an explicit entry for every directory. If one is
                # missing the file still lands, but its parent has no recorded protection or
                # timestamp -- worth surfacing, because it means the capture lost something.
                warnings.append(
                    f"{entry.path}: parent directory was not recorded in the manifest, so its "
                    "metadata is unknown"
                )
                os.makedirs(parent, exist_ok=True)
            data = blobs.get(entry.blob) if entry.blob else b""
            if not dry_run:
                with open(host, "wb") as fh:
                    fh.write(data)
            result.files += 1
            result.bytes_written += len(data)
            if on_file is not None:
                on_file(host, len(data))
        else:
            # Whiteouts never reach a plan (they are applied by omission), and links cannot be
            # captured yet. Anything else is a manifest this version does not understand.
            warnings.append(f"{entry.path}: skipped, cannot write a {entry.kind!r} entry")
            continue

        if metadata:
            _write_sidecar(host, entry, dry_run=dry_run)
            result.sidecars += 1

    return warnings


def fsuae_config_lines(result: WriteResult, plan: Plan) -> list[str]:
    """FS-UAE configuration for the directory just written.

    Emitted because a composed directory is useless until it is mounted, and getting the
    `hard_drive_N` numbering and label right by hand is fiddly enough to be worth doing for the
    user. The bootable volume is listed first so it wins the boot election.
    """
    lines: list[str] = []
    ordered = sorted(
        plan.volumes,
        key=lambda v: not bool((v.partition or {}).get("bootable")),
    )
    index = 0
    for vol in ordered:
        if vol.volume not in result.volumes:
            continue
        path = volume_dir(result.target, vol.volume)
        lines.append(f"hard_drive_{index} = {path}")
        lines.append(f"hard_drive_{index}_label = {vol.volume}")
        if (vol.partition or {}).get("bootable"):
            lines.append(f"hard_drive_{index}_priority = 0")
        index += 1
    return lines


__all__ = [
    "UAEM_SUFFIX",
    "ProgressFn",
    "WriteResult",
    "escape_name",
    "fsuae_config_lines",
    "uaem_line",
    "volume_dir",
    "write_directory",
]


# ---------------------------------------------------------------------------
# FFS volume writing
# ---------------------------------------------------------------------------

#: DosType used when a stack carries no drive record to read one from. DOS3 (`ffs+intl`) is the
#: project's stated target (`KIP-FFS-NOTES.md` §7), so it is a documented default rather than a
#: guess -- but it is still a default, and `write_plain` says so in its result warnings.
DEFAULT_DOS_TYPE = 0x444F5303


def _fs_string(text: str):
    from amitools.fs.FSString import FSString

    return FSString(text)


def meta_info_for(entry: M.ManifestEntry):
    """Build an amitools MetaInfo carrying this entry's exact recorded metadata.

    The timestamp is the reason this function exists. `TimeStamp(days, mins, ticks)` assigns the
    triple **directly**; amitools' `amiga_epoch` is only consulted by `from_secs`, `parse`,
    `__str__` and `format`, none of which are used here. So the bytes that reach the disk are the
    bytes that were captured, and the hour-adrift conversion in `KIP-FFS-NOTES.md` §5.7 is
    bypassed rather than merely compensated for.
    """
    from amitools.fs.MetaInfo import MetaInfo
    from amitools.fs.ProtectFlags import ProtectFlags
    from amitools.fs.TimeStamp import TimeStamp

    flags = ProtectFlags()
    flags.parse_full(entry.protect)
    days, mins, ticks = entry.ts
    return MetaInfo(
        protect=flags.get_mask(),
        mod_ts=TimeStamp(days=days, mins=mins, ticks=ticks),
        comment=_fs_string(entry.comment) if entry.comment else None,
    )


def _child_named(node: Any, name: str) -> Any | None:
    """Find an existing child by name, case-insensitively as FFS does."""
    needle = name.casefold()
    for child in node.get_entries():
        if child.name.get_unicode_name().casefold() == needle:
            return child
    return None


class _NodeTree:
    """Creates and caches directory nodes while writing a volume.

    Exists because `ADFSDir.create_dir` is **not** recursive (notes G19): creating `S/Prefs`
    without `S` raises `FSError: Invalid Parent Directory`. Entries arrive sorted so parents
    normally precede children, but a manifest missing a directory entry would otherwise be a hard
    failure rather than a reported gap.
    """

    def __init__(self, root: Any):
        self.root = root
        self._cache: dict[str, Any] = {"": root}
        self.implicit: list[str] = []

    def dir_for(self, relative: str) -> Any:
        """The node for a directory path, creating any missing ancestor."""
        key = M.fold(relative)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        parent_rel, _, name = relative.rpartition("/")
        parent = self.dir_for(parent_rel) if relative else self.root
        existing = _child_named(parent, name)
        if existing is not None:
            self._cache[key] = existing
            return existing
        # No recorded entry for this directory, so its protection bits, comment and timestamp are
        # unknown. Created with amitools' defaults and reported.
        node = parent.create_dir(_fs_string(name), None, False)
        self.implicit.append(relative)
        self._cache[key] = node
        return node

    def add_dir(self, relative: str, entry: M.ManifestEntry) -> Any:
        """Create a recorded directory, with its metadata."""
        key = M.fold(relative)
        parent_rel, _, name = relative.rpartition("/")
        parent = self.dir_for(parent_rel)
        existing = _child_named(parent, name)
        if existing is not None:
            # Present already, from a merge into an existing volume. Its metadata is left as it
            # is rather than rewritten, because a merge is additive by definition.
            self._cache[key] = existing
            return existing
        node = parent.create_dir(_fs_string(name), meta_info_for(entry), False)
        self._cache[key] = node
        return node

    def parent_for_file(self, relative: str) -> tuple[Any, str]:
        parent_rel, _, name = relative.rpartition("/")
        return self.dir_for(parent_rel), name


def write_volume_entries(
    volume: Any,
    vol: VolumePlan,
    blobs: BlobStore,
    *,
    on_file: ProgressFn | None = None,
) -> tuple[int, int, int, list[str]]:
    """Write one volume plan's entries into a mounted amitools volume.

    Returns `(files, dirs, bytes, warnings)`.

    Every create passes `update_ts=False`. With the default `True`, `_create_node` calls
    `update_dir_mod_time()`, which stamps the parent with `time.mktime(time.localtime())` through
    the same broken epoch — so composing would silently rewrite the timestamps of every directory
    it touched. That single argument is what makes the write faithful.
    """
    tree = _NodeTree(volume.get_root_dir())
    warnings: list[str] = []
    files = dirs = written = 0

    for plan_entry in vol.entries:
        entry = plan_entry.entry
        relative = entry.relative

        if entry.kind == M.DIR:
            tree.add_dir(relative, entry)
            dirs += 1
            continue
        if entry.kind != M.FILE:
            warnings.append(f"{entry.path}: skipped, cannot write a {entry.kind!r} entry")
            continue

        parent, name = tree.parent_for_file(relative)
        existing = _child_named(parent, name)
        if existing is not None:
            # amitools refuses to overwrite (notes G22), so replacing means deleting first. Only
            # reachable on a merge, since a formatted volume starts empty and the plan resolves
            # each path exactly once.
            warnings.append(f"{entry.path}: replaced an existing file on the target")
            existing.delete(wipe=False, all=False, update_ts=False)

        data = blobs.get(entry.blob) if entry.blob else b""
        parent.create_file(_fs_string(name), data, meta_info_for(entry), False)
        files += 1
        written += len(data)
        if on_file is not None:
            on_file(entry.path, len(data))

    for relative in tree.implicit:
        warnings.append(
            f"{vol.volume}:{relative}: directory was not recorded in the manifest, so it was "
            "created with default protection bits and no timestamp"
        )
    return files, dirs, written, warnings


def _dos_type_for(vol: VolumePlan) -> tuple[int, bool]:
    """`(dos_type, was_defaulted)` for a volume."""
    raw = (vol.partition or {}).get("dos_type")
    if raw is None:
        return DEFAULT_DOS_TYPE, True
    try:
        return (int(str(raw), 16) if isinstance(raw, str) else int(raw)), False
    except (TypeError, ValueError):
        return DEFAULT_DOS_TYPE, True


def _size_for(
    vol: VolumePlan, requested: int | None, drive: dict[str, Any] | None
) -> tuple[int, str]:
    """`(bytes, source)` for a new plain image, where source explains where it came from."""
    if requested:
        return int(requested), "requested"
    recorded = int((vol.partition or {}).get("num_bytes") or 0)
    if recorded:
        return recorded, "the partition's recorded size"
    # A plain HDF captured as a base layer has no partition table, so its volume size is the
    # image size -- which the drive record does hold. That is a recorded fact, not a guess.
    if drive and drive.get("single_volume"):
        whole = int(drive.get("total_bytes") or 0)
        if whole:
            return whole, "the source image's recorded size"
    # Nothing recorded and nothing asked for. Rather than invent a size that might not fit, the
    # caller decides -- silently choosing one is how an image ends up mysteriously full.
    raise UsageError(
        f"volume {vol.volume}: no size is recorded for it and none was given. "
        "Pass --size (for example --size 100M)"
    )


def write_plain(
    plan: Plan,
    blobs: BlobStore,
    target: str,
    *,
    force: bool = False,
    size: int | None = None,
    dry_run: bool = False,
    on_file: ProgressFn | None = None,
) -> WriteResult:
    """Write a single-volume plan to a plain HDF: no partition table, one filesystem.

    A plain HDF holds exactly one volume, so a multi-volume stack is refused with a pointer at
    `--format rdb` rather than silently composing only part of it.
    """
    if not plan.is_writable:
        raise UsageError(
            "the plan has unresolved problems, so nothing was written -- "
            "run with --dry-run to see them"
        )
    if not target:
        raise UsageError("no target image given")

    writable = [vol for vol in plan.volumes if vol.write or vol.format_volume]
    if len(writable) != 1:
        names = ", ".join(f"{v.volume}:" for v in writable) or "none"
        raise UsageError(
            f"a plain HDF holds one volume, but this plan covers {len(writable)} ({names}). "
            "Use --format rdb for a partitioned drive, or --volume NAME to pick one"
        )
    vol = writable[0]

    if os.path.exists(target) and not force:
        raise UsageError(
            f"{target} already exists. Pass --force to overwrite it, or choose another target"
        )

    result = WriteResult(target=target, dry_run=dry_run, volumes=[vol.volume])
    dos_type, defaulted = _dos_type_for(vol)
    if defaulted:
        result.warnings.append(
            f"volume {vol.volume}: no DosType recorded, defaulting to DOS3 (ffs+intl)"
        )
    num_bytes, size_source = _size_for(vol, size, plan.drive)
    result.size_bytes = num_bytes
    result.size_source = size_source

    if dry_run:
        result.files = vol.file_count
        result.dirs = sum(1 for e in vol.entries if e.entry.kind == M.DIR)
        result.bytes_written = vol.content_bytes
        return result

    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory

    exists = os.path.exists(target)
    # An existing image is only recreated when the policy actually says to format. Unlinking it
    # regardless would let `merge` destroy exactly what it promises to keep.
    merging = exists and not vol.format_volume

    if merging:
        blkdev = BlkDevFactory().open(target, read_only=False)
    else:
        if exists:
            result.cleared.append(vol.volume)
            os.unlink(target)
        elif not vol.format_volume:
            # Nothing to merge into. Creating the image is the only possible action, and saying so
            # avoids the plan's "write into existing" line looking like a contradiction.
            result.warnings.append(
                f"volume {vol.volume}: the '{vol.policy}' policy writes into an existing volume, "
                "but the target does not exist, so it was created and formatted"
            )
        blkdev = BlkDevFactory().create(target, force=True, options={"size": num_bytes})

    try:
        volume = ADFSVolume(blkdev)
        if merging:
            volume.open()
            result.size_bytes = 0  # not ours to report; the image was already sized
        else:
            volume.create(_fs_string(vol.volume), None, dos_type=dos_type)
        try:
            files, dirs, written, warnings = write_volume_entries(
                volume, vol, blobs, on_file=on_file
            )
            result.files, result.dirs, result.bytes_written = files, dirs, written
            result.warnings.extend(warnings)
        finally:
            volume.close()
    finally:
        blkdev.close()
    return result


# ---------------------------------------------------------------------------
# RDB target
# ---------------------------------------------------------------------------

#: DosEnvec fields written through amitools' `more_dos_env` escape hatch, verbatim from the
#: recorded drive.
#:
#: The geometry-derived fields are deliberately **absent**: `add_partition` computes `surfaces`,
#: `blk_per_trk`, `block_size` and `sec_per_blk` from the RDB it is adding to, and overriding them
#: could produce a partition whose own geometry disagrees with its drive's -- which is exactly the
#: arithmetic that decides where the partition starts. They are verified against the record
#: afterwards instead.
#:
#: `low_cyl` and `high_cyl` are also absent because they arrive as `cyl_range`.
REPRODUCED_DOS_ENV_FIELDS = (
    "sec_org",
    "reserved",
    "pre_alloc",
    "interleave",
    "num_buffer",
    "buf_mem_type",
    "max_transfer",
    "mask",
    "baud",
    "control",
    "boot_blocks",
)

#: Fields `add_partition` derives, checked against the record rather than forced.
DERIVED_DOS_ENV_FIELDS = ("surfaces", "blk_per_trk", "block_size", "sec_per_blk")


def _partition_flags(part: dict[str, Any]) -> int:
    from amitools.fs.block.rdb.PartitionBlock import PartitionBlock

    flags = 0
    if part.get("bootable"):
        flags |= PartitionBlock.FLAG_BOOTABLE
    if not part.get("automount", True):
        flags |= PartitionBlock.FLAG_NO_AUTOMOUNT
    return flags


def _rdb_reserved_cylinders(drive: dict[str, Any]) -> int:
    """Cylinders reserved for the RDB itself, taken from where the first partition starts.

    Derived rather than defaulted to 1, so a drive whose RDB area is larger is reproduced as it
    was. Cylinder 0 always belongs to the RDB (notes §3.8).
    """
    lows = [int(p["low_cyl"]) for p in drive.get("partitions") or [] if "low_cyl" in p]
    return max(1, min(lows)) if lows else 1


def write_rdb(
    plan: Plan,
    blobs: BlobStore,
    target: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    on_file: ProgressFn | None = None,
) -> WriteResult:
    """Write a plan to a whole-disk RDB image, reproducing the recorded drive layout.

    This is the format that works everywhere the project targets: ZuluSCSI reads it directly, both
    emulators accept it, and its bytes are what a PiStorm `0x76` MBR partition contains
    (`KIP-FFS-NOTES.md` §9.1).

    The DosEnvec is reproduced field by field rather than defaulted, which is the entire reason a
    base layer captures it. Defaults are documented to corrupt data on real controllers -- the
    Emu68 guide records PFS3 failing on PiStorm because HDToolBox's suggested mask confines
    buffers below 16 MB while most of its RAM sits above.
    """
    # Checked before the plan's own problems: this is a structural mismatch between the stack and
    # the requested format, true regardless of what else the plan says, and the useful advice is
    # "pick another format" rather than "go read the problem list".
    drive = plan.drive
    if not drive or drive.get("single_volume") or not drive.get("partitions"):
        raise UsageError(
            "this stack carries no partition table, so there is no RDB layout to reproduce. "
            "Use --format plain for a single-volume image, or --format dir for a directory"
        )
    if not plan.is_writable:
        raise UsageError(
            "the plan has unresolved problems, so nothing was written -- "
            "run with --dry-run to see them"
        )
    if not target:
        raise UsageError("no target image given")

    result = WriteResult(target=target, dry_run=dry_run)
    total_bytes = int(drive.get("total_bytes") or 0)
    if not total_bytes:
        raise UsageError("the drive record has no total size, so the image cannot be created")
    result.size_bytes = total_bytes
    result.size_source = "the recorded drive geometry"

    if dry_run:
        for vol in plan.volumes:
            result.volumes.append(vol.volume)
            result.files += vol.file_count
            result.dirs += sum(1 for e in vol.entries if e.entry.kind == M.DIR)
            result.bytes_written += vol.content_bytes
        return result

    existing = _existing_rdb_layout(target)
    if existing is not None:
        # Restoring into a drive that is already in use. This is the workflow the whole project
        # exists for -- put Workbench: back to stock and leave Work: and Saves: alone -- so the
        # drive is opened and only the planned partitions are touched. Rebuilding it from the
        # record instead would silently destroy every partition the plan does not cover, which is
        # precisely the data the user is trying to keep.
        differences = _rdb_layout_differences(existing, drive)
        if differences:
            raise UsageError(
                f"{target} is an RDB drive, but its layout does not match this stack's record, so "
                "restoring into it would write to the wrong places:\n  "
                + "\n  ".join(differences)
                + f"\nDelete {target} first to build a fresh drive from the record instead."
            )
        if not force:
            destroyed = ", ".join(f"{v.volume}:" for v in plan.volumes if v.format_volume)
            raise UsageError(
                f"{target} already exists. Pass --force to restore into it"
                + (f", which will replace {destroyed}" if destroyed else "")
            )
        return _write_rdb_in_place(plan, blobs, target, result, on_file)

    if os.path.exists(target) and not force:
        raise UsageError(
            f"{target} already exists. Pass --force to overwrite it, or choose another target"
        )
    return _write_rdb_fresh(plan, blobs, target, drive, result, on_file)


def _write_rdb_fresh(
    plan: Plan,
    blobs: BlobStore,
    target: str,
    drive: dict[str, Any],
    result: WriteResult,
    on_file: ProgressFn | None,
) -> WriteResult:
    """Build the whole drive from the record, partition table included."""
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.DiskGeometry import DiskGeometry
    from amitools.fs.blkdev.PartBlockDevice import PartBlockDevice
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
    from amitools.fs.rdb.RDisk import RDisk

    if os.path.exists(target):
        os.unlink(target)

    block_bytes = int(drive.get("block_size") or 512)
    geo = DiskGeometry(
        cyls=int(drive["cylinders"]),
        heads=int(drive["heads"]),
        secs=int(drive["sectors"]),
        block_bytes=block_bytes,
    )

    rawblk = RawBlockDevice(target, block_bytes=block_bytes)
    rawblk.create(geo.cyls * geo.heads * geo.secs)
    try:
        rdisk = RDisk(rawblk)
        rdisk.create(geo, rdb_cyls=_rdb_reserved_cylinders(drive))
        try:
            result.warnings.extend(_add_partitions(rdisk, drive))
            rdisk.close()
        except Exception:
            rdisk.close()
            raise

        # Re-open so the partitions just written are read back as amitools sees them, rather
        # than trusting the objects that created them.
        rdisk = RDisk(rawblk)
        rdisk.open()
        try:
            result.warnings.extend(
                _fill_partitions(rdisk, plan, blobs, result, on_file, ADFSVolume, PartBlockDevice)
            )
        finally:
            rdisk.close()
    finally:
        rawblk.close()
    return result


def _write_rdb_in_place(
    plan: Plan,
    blobs: BlobStore,
    target: str,
    result: WriteResult,
    on_file: ProgressFn | None,
) -> WriteResult:
    """Restore into an existing drive, touching only the partitions the plan covers.

    The partition table is left exactly as it is. It has already been checked to match the record,
    and rewriting it would move partitions whose contents are being deliberately kept.
    """
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.PartBlockDevice import PartBlockDevice
    from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
    from amitools.fs.rdb.RDisk import RDisk

    result.in_place = True
    rawblk = RawBlockDevice(target, block_bytes=int(plan.drive.get("block_size") or 512))
    rawblk.open()
    try:
        rdisk = RDisk(rawblk)
        rdisk.open()
        try:
            result.warnings.extend(
                _fill_partitions(
                    rdisk, plan, blobs, result, on_file, ADFSVolume, PartBlockDevice,
                    existing=True,
                )
            )
        finally:
            rdisk.close()
    finally:
        rawblk.close()
    return result


def _existing_rdb_layout(target: str) -> dict[str, Any] | None:
    """The layout of an existing RDB drive, or None if `target` is absent or not a readable RDB.

    Returning None for an unreadable file is deliberate: a truncated or garbage target is not
    something to restore *into*, and the caller falls through to building a fresh drive (still
    behind `--force`).

    Only the errors that actually mean "not a readable RDB" are caught. A blanket `except
    Exception` here silently turned an attribute typo in this very function into "no existing
    drive", which sent a partition-granular restore down the rebuild path and destroyed the
    partitions it was meant to preserve -- the bug this function exists to fix, reintroduced by
    the way it reported failure.
    """
    if not os.path.exists(target):
        return None

    from ..errors import ImageError

    try:
        from ..addressing import parse
        from ..image import ImageKind, open_container

        with open_container(parse(target)) as container:
            if container.kind is not ImageKind.RDB:
                return None
            geometry = container.geometry.as_dict()
            return {
                "block_size": geometry["block_size"],
                "cylinders": geometry["cylinders"],
                "heads": geometry["heads"],
                "sectors": geometry["sectors"],
                "partitions": [
                    {
                        "index": part.index,
                        "device": part.device_name,
                        "low_cyl": part.low_cyl,
                        "high_cyl": part.high_cyl,
                        "dos_type": part.dos_type.raw,
                    }
                    for part in container.partitions()
                ],
            }
    except (ImageError, OSError, ValueError):
        return None


def _rdb_layout_differences(existing: dict[str, Any], drive: dict[str, Any]) -> list[str]:
    """Every way an existing drive's layout disagrees with the record, in readable form.

    Every difference is reported rather than the first, because a drive that disagrees in three
    ways is a different drive and the user should see that at once.
    """
    problems: list[str] = []
    for field_name, label in (
        ("block_size", "block size"), ("cylinders", "cylinders"),
        ("heads", "heads"), ("sectors", "sectors"),
    ):
        want = int(drive.get(field_name) or 0)
        got = int(existing.get(field_name) or 0)
        if want and want != got:
            problems.append(f"{label}: drive has {got}, the record says {want}")

    recorded = drive.get("partitions") or []
    present = existing.get("partitions") or []
    if len(recorded) != len(present):
        problems.append(
            f"partition count: drive has {len(present)}, the record says {len(recorded)}"
        )
        return problems

    for want, got in zip(recorded, present):
        name = want.get("device") or f"partition {want.get('index')}"
        if str(want.get("device")) != str(got.get("device")):
            problems.append(
                f"partition {got.get('index')}: drive calls it {got.get('device')}, "
                f"the record says {want.get('device')}"
            )
        for key, label in (("low_cyl", "start cylinder"), ("high_cyl", "end cylinder")):
            if int(want.get(key, -1)) != int(got.get(key, -2)):
                problems.append(
                    f"{name} {label}: drive has {got.get(key)}, the record says {want.get(key)}"
                )
        want_type = want.get("dos_type")
        want_int = int(str(want_type), 16) if isinstance(want_type, str) else int(want_type or 0)
        if want_int and want_int != int(got.get("dos_type") or 0):
            problems.append(
                f"{name} DosType: drive has {got.get('dos_type'):#x}, the record says {want_int:#x}"
            )
    return problems


def _add_partitions(rdisk: Any, drive: dict[str, Any]) -> list[str]:
    """Recreate every partition in the record, DosEnvec included."""
    warnings: list[str] = []
    for part in drive.get("partitions") or []:
        env = part.get("dos_env") or {}
        dos_type = int(str(part["dos_type"]), 16) if isinstance(part.get("dos_type"), str) \
            else int(part.get("dos_type") or DEFAULT_DOS_TYPE)

        recorded_block_bytes = int(env.get("block_size") or 0) * 4 or None
        more = [
            (name, int(env[name])) for name in REPRODUCED_DOS_ENV_FIELDS if name in env
        ]
        created = rdisk.add_partition(
            # amitools asserts on the type rather than coercing it.
            _fs_string(str(part.get("device") or f"DH{part.get('index', 0)}")),
            (int(part["low_cyl"]), int(part["high_cyl"])),
            dev_flags=0,
            flags=_partition_flags(part),
            dos_type=dos_type,
            boot_pri=int(env.get("boot_pri", 0)),
            more_dos_env=more,
            fs_block_size=recorded_block_bytes,
        )

        # Verify rather than force the geometry-derived fields. A mismatch means the recorded
        # partition geometry disagrees with the recorded drive geometry, which would move the
        # partition's start block and silently produce a different layout from the source.
        written = created.part_blk.dos_env
        for name in DERIVED_DOS_ENV_FIELDS:
            if name not in env:
                continue
            want, got = int(env[name]), int(getattr(written, name))
            if want != got:
                warnings.append(
                    f"partition {part.get('device')}: recorded {name}={want} but the drive's "
                    f"geometry gives {got}; the source layout is not being reproduced exactly"
                )
    return warnings


def _fill_partitions(
    rdisk: Any,
    plan: Plan,
    blobs: BlobStore,
    result: WriteResult,
    on_file: ProgressFn | None,
    adfs_volume_cls: Any,
    part_blkdev_cls: Any,
    *,
    existing: bool = False,
) -> list[str]:
    """Format and populate each partition according to its volume's policy.

    `existing` says the drive was already in use, which changes two decisions:

    * A partition the plan does not cover is left **completely alone** -- not opened, not
      formatted. On a fresh drive the same case is a partition created empty, which is worth a
      warning; on an existing drive it is the entire point of a partition-granular restore.
    * A policy that writes without formatting opens the volume instead of creating it, so `merge`
      adds to what is there rather than replacing it.
    """
    warnings: list[str] = []

    for index in range(rdisk.get_num_partitions()):
        partition = rdisk.get_partition(index)
        device = str(partition.get_drive_name())

        vol = _volume_plan_for(plan, index, device)
        if vol is None:
            if existing:
                result.untouched.append(device)
            else:
                warnings.append(
                    f"partition {device}: no volume in the plan corresponds to it, so it was "
                    "created but left unformatted"
                )
            continue

        result.volumes.append(vol.volume)
        if not vol.format_volume and not vol.write:
            # `preserve`: keep whatever is there. On an existing drive that is a real preservation
            # and needs no comment; on a fresh drive there is nothing to preserve, so say so.
            if existing:
                result.untouched.append(device)
            else:
                warnings.append(
                    f"partition {device} ({vol.volume}:): '{vol.policy}' policy, left unformatted "
                    "on this new drive"
                )
            continue

        blkdev = part_blkdev_cls(rdisk.rawblk, partition.part_blk)
        blkdev.open()
        try:
            volume = adfs_volume_cls(blkdev)
            # Opening rather than creating is what makes `merge` keep what it promises to keep.
            merging = existing and not vol.format_volume
            if merging:
                volume.open()
            else:
                dos_type, defaulted = _dos_type_for(vol)
                if defaulted:
                    warnings.append(
                        f"volume {vol.volume}: no DosType recorded, defaulting to DOS3 (ffs+intl)"
                    )
                volume.create(_fs_string(vol.volume), None, dos_type=dos_type)
            try:
                if vol.write:
                    files, dirs, written, vol_warnings = write_volume_entries(
                        volume, vol, blobs, on_file=on_file
                    )
                    result.files += files
                    result.dirs += dirs
                    result.bytes_written += written
                    warnings.extend(vol_warnings)
                if vol.format_volume:
                    result.cleared.append(vol.volume)
            finally:
                volume.close()
        finally:
            blkdev.close()
    return warnings


def _volume_plan_for(plan: Plan, index: int, device: str) -> VolumePlan | None:
    """Match an RDB partition to its volume plan, or None if nothing corresponds to it.

    Matched on the recorded partition index the plan already carries, so a drive with two
    partitions of the same size cannot be confused, then on the device name. There is
    deliberately no fall back to ordinal position: an unmatched partition is left unformatted
    with a warning, which is recoverable, whereas guessing would write a volume's contents into
    the wrong partition.
    """
    for vol in plan.volumes:
        part = vol.partition or {}
        if part and int(part.get("index", -1)) == index:
            return vol
    for vol in plan.volumes:
        if str((vol.partition or {}).get("device") or "").casefold() == device.casefold():
            return vol
    return None
