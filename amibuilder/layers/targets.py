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
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "files": self.files,
            "dirs": self.dirs,
            "bytes_written": self.bytes_written,
            "sidecars": self.sidecars,
            "volumes": list(self.volumes),
            "cleared": list(self.cleared),
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


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
