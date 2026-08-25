"""`format` -- lay a fresh Amiga filesystem onto an existing drive's partition.

`init` makes a new image and never touches an existing one. `format` is its opposite number:
it takes a partition (or a plain HDF's single volume) that already exists -- formatted, foreign,
or blank -- and writes an empty FFS/OFS filesystem onto it, erasing whatever was there. That is
the operation `open_volume` points at when it refuses an unformatted partition with "Use
'amibuilder format' to create one."

It is the most destructive verb in the tool, so it is gated the same way the rest of the
destructive paths are: a raw device goes through `device.check_access` (the `--device` flag, the
boot-disk refusal, and the typed-identifier confirmation) via `resolve(writable=True)`, and a
plain file -- which has no device confirmation -- requires `--force`. Either way the refusal
names the volume it would erase, so a mistyped selector is caught before any block is written.

**On an RDB the filesystem type is not `format`'s to change.** A partition's DosEnvec records a
DosType, and on a real Amiga that record -- not the boot block -- is what decides which
filesystem handler mounts the partition. Writing an OFS boot block into a partition the RDB calls
FFS produces a drive that mounts here (amitools reads the boot block) yet misreads on real
hardware. So on an RDB `format` writes the type the partition already declares; an explicit
`--dos-type` that disagrees is refused, with a pointer at HDToolBox for the repartition that
genuinely changing a type requires. A plain HDF has no such second record, so there any type is
fine.

Formatting needs the partition's block device but not its filesystem, so it goes through
`Container.open_blkdev`, which opens the blocks without mounting -- the one thing `open_volume`
cannot do, because there may be no filesystem there yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..blocks import decode_dos_type, parse_dos_type
from ..errors import ImageError, UsageError
from ..image import Container, ImageKind
from ..render import Output
from . import opened_container
from .init import DEFAULT_DOS_TYPE_NAME, VOLUME_NAME_LIMIT


@dataclass
class _Target:
    """The partition/volume `format` would write to, resolved before anything is erased."""

    label: str
    current_name: str | None
    current_desc: str
    #: The DosType already on record for this target -- an RDB partition's DosEnvec, or a plain
    #: HDF's current FFS/OFS boot block. None when nothing usable is recorded (an unformatted or
    #: foreign target), in which case the default filesystem is used.
    recorded_dos: int | None
    is_rdb: bool


def _check_volume_name(name: str) -> None:
    """Refuse a volume name AmigaDOS could not hold, before anything is written."""
    if not name:
        raise UsageError("a volume name cannot be empty")
    if ":" in name or "/" in name:
        raise UsageError(f"volume name {name!r} cannot contain ':' or '/'")
    if len(name) > VOLUME_NAME_LIMIT:
        raise UsageError(
            f"volume name {name!r} is {len(name)} characters; AmigaDOS allows {VOLUME_NAME_LIMIT}"
        )


def _describe_current(name: str | None, dos_type: Any) -> str:
    """What the target holds right now, for the refusal and the report."""
    if name:
        return f"{name}: ({dos_type.describe()})" if dos_type is not None else f"{name}:"
    if dos_type is None:
        return "an unknown filesystem"
    if dos_type.filesystem == "none":
        return "unformatted (nothing to erase)"
    return f"{dos_type.describe()}, unmountable"


def _resolve_target(container: Container, selector: int | str | None) -> _Target:
    """Work out which partition would be formatted, and what it currently holds."""
    if container.kind is ImageKind.DIRECTORY:
        raise UsageError(
            f"{container.address.path} is a host directory; there is no filesystem to format"
        )
    if container.kind is ImageKind.MBR:
        raise UsageError(
            f"{container.address.path} is an MBR-partitioned device; select the Amiga slice to "
            f"format, e.g. '{container.address.path}:0x76:1'"
        )
    if container.kind is ImageKind.RDB:
        index = container.resolve_partition(selector)  # AddressError names the mistake
        part = next(
            (p for p in container.partitions(probe_volumes=True) if p.index == index), None
        )
        label = f"{container.address.path}:{index}"
        if part is None:  # resolve_partition already validated the index, so this is defensive
            return _Target(label, None, "an unknown filesystem", None, True)
        return _Target(
            label=f"{label} ({part.device_name})",
            current_name=part.volume_name,
            current_desc=_describe_current(part.volume_name, part.dos_type),
            # An RDB partition always carries a DosEnvec DosType; that is what must be written.
            recorded_dos=part.dos_type.raw,
            is_rdb=True,
        )
    # Plain HDF or ADF: the boot block is the only type record, and only a real FFS/OFS one is
    # worth reusing -- a foreign or blank boot block defaults to the standard type instead.
    dos = container.boot_dos_type
    recorded = dos.raw if (dos is not None and dos.filesystem.lower() in ("ffs", "ofs")) else None
    return _Target(container.address.describe(), None, _describe_current(None, dos), recorded,
                   False)


def _resolve_dos_type(args: Any, target: _Target) -> int:
    """The DosType to write, honouring the RDB "type is not ours to change" rule."""
    if args.dos_type:
        try:
            requested = parse_dos_type(args.dos_type)
        except ValueError as e:
            raise UsageError(f"--dos-type {args.dos_type!r}: {e}") from e
        if target.is_rdb and target.recorded_dos is not None and requested != target.recorded_dos:
            recorded = decode_dos_type(target.recorded_dos)
            asked = decode_dos_type(requested)
            raise UsageError(
                f"{target.label}: the partition table records this as {recorded.describe()}, "
                f"but --dos-type asks for {asked.describe()}. Formatting to a different type "
                "would leave the RDB disagreeing with the filesystem, which mounts here but "
                "fails on a real Amiga. Repartition with HDToolBox to change the type, or omit "
                f"--dos-type to keep {recorded.label}."
            )
        return requested
    if target.recorded_dos is not None:
        return target.recorded_dos
    return parse_dos_type(DEFAULT_DOS_TYPE_NAME)


def _do_format(container: Container, selector: int | str | None, name: str, dos_type: int,
               label: str) -> None:
    """Write an empty filesystem onto the partition's blocks.

    The block device is opened unmounted, formatted with amitools' own `ADFSVolume.create`
    (the same primitive `init` uses), then every handle is closed in reverse -- volume first,
    then the RDB/raw devices underneath -- which is the order that flushes correctly.
    """
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.FSString import FSString

    blkdev, _label, closers = container.open_blkdev(selector)
    try:
        adfs = ADFSVolume(blkdev)
        adfs.create(FSString(name), None, dos_type=dos_type)
        adfs.close()
    except Exception as e:
        raise ImageError(f"could not format {label}: {e}") from e
    finally:
        for close in reversed(closers):
            try:
                close()
            except Exception:  # closing must not mask the real result
                pass


def cmd_format(args: Any, out: Output) -> int:
    # A dry run opens read-only, so it neither writes nor triggers the raw-device write
    # confirmation -- previewing a format is harmless.
    with opened_container(args, writable=not args.dry_run) as container:
        selector = container.address.partition
        target = _resolve_target(container, selector)

        dos_type = _resolve_dos_type(args, target)
        new_desc = decode_dos_type(dos_type).describe()

        name = args.volume or target.current_name
        if not name:
            raise UsageError(
                f"{target.label} has no current volume name to reuse; pass --volume NAME to "
                f"name the formatted volume"
            )
        _check_volume_name(name)

        # Destructive-write gate. A device has already passed device.check_access's typed
        # confirmation (via resolve writable=True); a plain file has not, so --force is its
        # guard. The message names the volume being erased so a mistyped selector is caught.
        if not container.address.is_device and not args.force and not args.dry_run:
            raise UsageError(
                f"formatting {target.label} as {name}: ({new_desc}) erases everything on it "
                f"(currently {target.current_desc}). Pass --force to proceed, or --dry-run to "
                "preview"
            )

        payload = {
            "target": target.label,
            "volume": name,
            "dos_type": f"0x{dos_type:08x}",
            "dos_type_name": decode_dos_type(dos_type).label,
            "current": target.current_desc,
            "dry_run": bool(args.dry_run),
            "formatted": not args.dry_run,
        }

        if args.dry_run:
            out.line(f"would format {target.label} as {name}: ({new_desc})")
            out.field("currently", target.current_desc)
            out.data(payload)
            return 0

        _do_format(container, selector, name, dos_type, target.label)

    out.line(f"formatted {target.label} as {name}: ({new_desc})")
    out.field("was", target.current_desc)
    out.data(payload)
    return 0
