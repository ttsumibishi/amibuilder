"""Create a new disk image: blank, partitioned, or partitioned and formatted.

Three shapes, because they answer three different needs:

* `--size` alone gives a **blank, unpartitioned, unformatted** image. That is the original ask,
  and it is what you want when the plan is to partition it yourself with HDToolBox on the Amiga.
* `--partition` builds an **RDB** drive, formatted unless `--no-format`. This is the one to use
  when the next step is running an AmigaOS installer onto it.
* `--plain` gives a **single-volume HDF** with no partition table, which is what emulators
  traditionally mount and what `compose --format plain` writes.

The RDB path deliberately goes through the same writer `compose` uses. `init` builds a drive
record of exactly the shape `snap create` records, then hands it to `targets._add_partitions`, so
there is one piece of code that writes an Amiga partition table rather than two that can drift.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from .. import render
from ..blocks import parse_dos_type
from ..errors import UsageError

#: DOS3 (`ffs+intl`), the project's stated target. See `KIP-FFS-NOTES.md` §7.
DEFAULT_DOS_TYPE_NAME = "ffs+intl"

#: Bounds from the original brief: "a number from 1-1000 followed by M ... or 1-16 followed by G
#: ... so we don't allow it to create a 1000GB file". Fractions are allowed within those ranges,
#: which is what keeps `1.5G` expressible -- a literal integers-only reading would leave every
#: size between 1000M and 2G unreachable, since 1000M is less than 1G.
SIZE_LIMITS = {"M": (1, 1000), "G": (1, 16)}

#: Beyond this, whether a partition works depends on the controller and filesystem rather than on
#: anything written here, so it is worth a word. Not a refusal: 2 GiB partitions are in everyday
#: use on real hardware, and this tool is in no position to know what a given machine supports.
LARGE_PARTITION_BYTES = 2 * 1024**3

#: AmigaDOS volume name limit. Longer names are truncated by the filesystem rather than rejected,
#: which is worse than refusing here.
VOLUME_NAME_LIMIT = 30

_SIZE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)\s*$")


def parse_init_size(spec: str) -> int:
    """Parse and range-check an image size, returning bytes.

    Only M and G are accepted, and both are binary (MiB/GiB) for the reason `render.parse_size`
    gives: in an Amiga context decimal megabytes buy nothing and being quietly 4.8% out would be
    worse than refusing the input.
    """
    match = _SIZE.match(spec or "")
    if not match:
        raise UsageError(
            f"cannot read {spec!r} as a size. Give a number followed by M or G, e.g. 100M or 4G"
        )

    unit = match.group("unit").upper().rstrip("B").rstrip("I") or "?"
    if unit not in SIZE_LIMITS:
        raise UsageError(
            f"unknown size unit in {spec!r}. Use M for megabytes or G for gigabytes"
        )

    value = float(match.group("value"))
    low, high = SIZE_LIMITS[unit]
    if not (low <= value <= high):
        other = "G" if unit == "M" else "M"
        other_low, other_high = SIZE_LIMITS[other]
        raise UsageError(
            f"{spec!r} is outside the permitted range: {low}-{high}{unit} "
            f"(or {other_low}-{other_high}{other}). The cap exists so a typo cannot ask for a "
            "1000 GB file"
        )

    shift = 2 if unit == "M" else 3
    num_bytes = int(value * (1024**shift))
    if num_bytes <= 0:
        raise UsageError(f"{spec!r} works out to zero bytes")
    return num_bytes


#: `rest` as a partition size means "whatever is left", which is how a layout adds up despite
#: cylinder rounding without the user doing the arithmetic.
REST = "rest"


@dataclass
class PartitionRequest:
    """One partition asked for on the command line."""

    volume: str
    #: None means "the remaining space".
    size_bytes: int | None
    bootable: bool = False
    dos_type: int = 0
    dos_type_name: str = DEFAULT_DOS_TYPE_NAME
    #: The text this came from, so an error can quote what the user typed.
    spec: str = ""

    @property
    def takes_rest(self) -> bool:
        return self.size_bytes is None


def parse_partition_spec(spec: str) -> PartitionRequest:
    """Parse `NAME=SIZE[,bootable][,dostype=X]`, where SIZE may be `rest`.

    A bare `NAME` is accepted as shorthand for `NAME=rest`, since the last partition in a layout
    usually wants whatever is left over.
    """
    text = (spec or "").strip()
    if not text:
        raise UsageError("empty --partition")

    head, _, tail = text.partition(",")
    name, sep, size_text = head.partition("=")
    name = name.strip()
    if not name:
        raise UsageError(f"--partition {spec!r} has no volume name")
    if ":" in name or "/" in name:
        raise UsageError(
            f"--partition {spec!r}: a volume name cannot contain ':' or '/'"
        )
    if len(name) > VOLUME_NAME_LIMIT:
        raise UsageError(
            f"--partition {spec!r}: volume name is {len(name)} characters, and AmigaDOS allows "
            f"{VOLUME_NAME_LIMIT}"
        )

    size_text = size_text.strip()
    if not sep or size_text.lower() == REST:
        size_bytes = None
    else:
        size_bytes = parse_init_size(size_text)

    request = PartitionRequest(volume=name, size_bytes=size_bytes, spec=text)

    for flag in (f.strip() for f in tail.split(",")):
        if not flag:
            continue
        key, _, value = flag.partition("=")
        key = key.strip().lower()
        if key in ("boot", "bootable"):
            if value and value.strip().lower() in ("0", "false", "no"):
                request.bootable = False
            else:
                request.bootable = True
        elif key in ("dostype", "dos_type", "fs"):
            if not value.strip():
                raise UsageError(f"--partition {spec!r}: dostype= needs a value")
            request.dos_type_name = value.strip()
        else:
            raise UsageError(
                f"--partition {spec!r}: unknown option {key!r}. "
                "Expected 'bootable' or 'dostype=NAME'"
            )

    try:
        request.dos_type = parse_dos_type(request.dos_type_name)
    except ValueError as exc:
        raise UsageError(f"--partition {spec!r}: {exc}") from exc
    return request


def parse_partition_specs(specs: list[str] | None) -> list[PartitionRequest]:
    """Parse every `--partition`, rejecting the combinations that cannot be laid out.

    Two refusals worth having up front: duplicate volume names, because AmigaDOS would mount only
    one of them and the other would be invisible rather than reported; and more than one `rest`,
    which has no sensible division.
    """
    requests = [parse_partition_spec(spec) for spec in (specs or [])]
    if not requests:
        return requests

    seen: dict[str, str] = {}
    for request in requests:
        key = request.volume.casefold()
        if key in seen:
            raise UsageError(
                f"two partitions are both called {request.volume!r} "
                f"({seen[key]!r} and {request.spec!r}); AmigaDOS would mount only one"
            )
        seen[key] = request.spec

    rest = [r for r in requests if r.takes_rest]
    if len(rest) > 1:
        names = ", ".join(r.volume for r in rest)
        raise UsageError(
            f"only one partition can take the remaining space, but {len(rest)} do ({names})"
        )
    if rest and rest[0] is not requests[-1]:
        raise UsageError(
            f"{rest[0].volume!r} takes the remaining space, so it has to be the last "
            "--partition given"
        )

    bootable = [r for r in requests if r.bootable]
    if len(bootable) > 1:
        names = ", ".join(r.volume for r in bootable)
        raise UsageError(
            f"only one partition should be bootable, but {len(bootable)} are marked ({names}). "
            "Boot priority decides between several, and this command does not set it"
        )
    return requests


# ---------------------------------------------------------------------------
# Geometry and cylinder layout
# ---------------------------------------------------------------------------


@dataclass
class PartitionLayout:
    """Where a requested partition actually landed, once cylinders were dealt with."""

    index: int
    device: str
    volume: str
    low_cyl: int
    high_cyl: int
    dos_type: int
    dos_type_name: str
    bootable: bool
    #: What the user asked for, or None if this one took the remainder.
    requested_bytes: int | None
    actual_bytes: int

    @property
    def cylinders(self) -> int:
        return self.high_cyl - self.low_cyl + 1

    @property
    def rounded(self) -> int:
        """Signed difference between what was asked for and what was allocated, in bytes."""
        if self.requested_bytes is None:
            return 0
        return self.actual_bytes - self.requested_bytes


@dataclass
class DriveLayout:
    """A complete plan for a new drive: geometry, partitions, and what rounding did."""

    geometry: Any  # image.Geometry
    partitions: list[PartitionLayout] = field(default_factory=list)
    rdb_cylinders: int = 1
    #: Cylinders past the last partition that no partition claimed.
    spare_cylinders: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def bytes_per_cylinder(self) -> int:
        return self.geometry.heads * self.geometry.sectors * self.geometry.block_size

    def as_record(self) -> dict[str, Any]:
        """A drive record of the shape `snap create` writes, for the shared RDB writer.

        Deliberately the same schema `layers/drive.py` produces, so `init` can hand it to
        `targets._add_partitions` -- the code `compose` uses -- instead of carrying a second
        implementation of "write an Amiga partition table" that could drift from it.
        """
        record = dict(self.geometry.as_dict())
        record.update(
            {
                "scheme": "amibuilder-drive-v1",
                "kind": "hdf",
                "single_volume": False,
                "partitions": [
                    {
                        "index": part.index,
                        "device": part.device,
                        "volume": part.volume,
                        "low_cyl": part.low_cyl,
                        "high_cyl": part.high_cyl,
                        "dos_type": part.dos_type,
                        "bootable": part.bootable,
                        "dos_env": {},
                    }
                    for part in self.partitions
                ],
            }
        )
        return record


def choose_geometry(total_bytes: int, block_size: int = 512) -> Any:
    """Pick a cylinder/head/sector geometry for a drive of this size.

    Delegated to amitools rather than invented here, so an image made by `init` is laid out the
    same way `rdbtool` would lay it out. Measured on the sizes this command permits, its answers
    are byte-exact -- no capacity is lost choosing a geometry.
    """
    from amitools.fs.blkdev.DiskGeometry import DiskGeometry

    from ..image import Geometry

    geo = DiskGeometry()
    got = geo.setup({"size": total_bytes})
    if not got:
        raise UsageError(
            f"no usable disk geometry for {render.human_bytes(total_bytes)}; "
            "try a different size"
        )
    return Geometry(
        block_size=geo.block_bytes,
        num_blocks=geo.cyls * geo.heads * geo.secs,
        cyls=geo.cyls,
        heads=geo.heads,
        sectors=geo.secs,
    )


def plan_layout(
    total_bytes: int,
    requests: list[PartitionRequest],
    *,
    rdb_cylinders: int = 1,
) -> DriveLayout:
    """Lay requested partitions out on cylinder boundaries.

    Amiga partitions start and end on a cylinder, so a requested size is almost never the size
    you get. That is unavoidable; reporting it is not. Every partition records what was asked for
    alongside what was allocated, and the caller prints the difference.

    A partition asking for `rest` absorbs everything left, which is how a layout adds up despite
    rounding. Dave's 1G+2G+1G on a 4G drive is the motivating case: those are exactly 8192+16384
    +8192 cylinders, the RDB takes one, and so the arithmetic cannot work without someone giving
    up a cylinder. Better the partition that volunteered than a silent shortfall on all three.
    """
    geometry = choose_geometry(total_bytes)
    layout = DriveLayout(geometry=geometry, rdb_cylinders=rdb_cylinders)
    if not requests:
        return layout

    per_cylinder = layout.bytes_per_cylinder
    available = geometry.cyls - rdb_cylinders
    if available < len(requests):
        raise UsageError(
            f"{render.human_bytes(total_bytes)} is too small for {len(requests)} partition(s): "
            f"the drive has {geometry.cyls} cylinder(s) and {rdb_cylinders} "
            "is reserved for the partition table"
        )

    # Sized partitions first; whatever is left goes to the one asking for the rest.
    wanted: list[int] = []
    for request in requests:
        if request.takes_rest:
            wanted.append(0)
            continue
        cylinders = round(request.size_bytes / per_cylinder)
        wanted.append(max(1, cylinders))

    fixed = sum(cylinders for cylinders, r in zip(wanted, requests) if not r.takes_rest)
    rest_index = next((i for i, r in enumerate(requests) if r.takes_rest), None)

    if rest_index is not None:
        remaining = available - fixed
        if remaining < 1:
            raise UsageError(
                f"nothing left for {requests[rest_index].volume!r}: the sized partitions already "
                f"ask for {render.human_bytes(fixed * per_cylinder)} of "
                f"{render.human_bytes(available * per_cylinder)} usable"
            )
        wanted[rest_index] = remaining
    elif fixed > available:
        over = (fixed - available) * per_cylinder
        raise UsageError(
            f"the partitions ask for {render.human_bytes(fixed * per_cylinder)} but only "
            f"{render.human_bytes(available * per_cylinder)} is usable on a "
            f"{render.human_bytes(total_bytes)} drive -- "
            f"{render.human_bytes(over)} too much. One cylinder is reserved for the partition "
            "table, so a layout adding up to exactly the drive size will not fit; give the last "
            "partition the size 'rest' instead"
        )

    low = rdb_cylinders
    for index, (request, cylinders) in enumerate(zip(requests, wanted)):
        high = low + cylinders - 1
        layout.partitions.append(
            PartitionLayout(
                index=index,
                device=f"DH{index}",
                volume=request.volume,
                low_cyl=low,
                high_cyl=high,
                dos_type=request.dos_type,
                dos_type_name=request.dos_type_name,
                bootable=request.bootable,
                requested_bytes=request.size_bytes,
                actual_bytes=cylinders * per_cylinder,
            )
        )
        low = high + 1

    layout.spare_cylinders = geometry.cyls - low
    _add_layout_warnings(layout, total_bytes)
    return layout


def _add_layout_warnings(layout: DriveLayout, total_bytes: int) -> None:
    """Everything about the layout worth saying out loud before an image is written."""
    if not any(part.bootable for part in layout.partitions):
        # A drive with no bootable partition will not boot, which is a discovery best not made
        # after half an hour in an installer. The first partition is the overwhelmingly common
        # choice, so it is taken -- and said, because a silent default here would be worse.
        layout.partitions[0].bootable = True
        layout.warnings.append(
            f"no partition was marked bootable, so {layout.partitions[0].volume}: was made "
            "bootable (a drive with none will not boot). Use ',bootable' to choose another"
        )

    for part in layout.partitions:
        if part.actual_bytes > LARGE_PARTITION_BYTES:
            layout.warnings.append(
                f"{part.volume}: is {render.human_bytes(part.actual_bytes)}. Above "
                f"{render.human_bytes(LARGE_PARTITION_BYTES)}, whether a partition works depends "
                "on the controller and filesystem supporting large addressing, which this tool "
                "cannot check"
            )

    if layout.spare_cylinders:
        spare = layout.spare_cylinders * layout.bytes_per_cylinder
        layout.warnings.append(
            f"{render.human_bytes(spare)} at the end of the drive is not in any partition. "
            "Give the last partition the size 'rest' to use all of it"
        )


# ---------------------------------------------------------------------------
# Creating the image
# ---------------------------------------------------------------------------


@dataclass
class InitResult:
    """What `init` created."""

    target: str = ""
    kind: str = ""  # "blank", "rdb" or "plain"
    size_bytes: int = 0
    layout: Any = None  # DriveLayout, for the rdb kind
    volume: str = ""    # for the plain kind
    formatted: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "target": self.target,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "formatted": self.formatted,
            "warnings": list(self.warnings),
        }
        if self.volume:
            d["volume"] = self.volume
        if self.layout is not None:
            d["geometry"] = self.layout.geometry.as_dict()
            d["rdb_cylinders"] = self.layout.rdb_cylinders
            d["partitions"] = [
                {
                    "index": part.index,
                    "device": part.device,
                    "volume": part.volume,
                    "dos_type": part.dos_type,
                    "dos_type_name": part.dos_type_name,
                    "bootable": part.bootable,
                    "low_cyl": part.low_cyl,
                    "high_cyl": part.high_cyl,
                    "cylinders": part.cylinders,
                    "requested_bytes": part.requested_bytes,
                    "size_bytes": part.actual_bytes,
                    "rounded_bytes": part.rounded,
                }
                for part in self.layout.partitions
            ]
        return d


def create_blank(target: str, total_bytes: int) -> InitResult:
    """Create an empty image with no partition table and no filesystem.

    This is the original ask -- "a brand new, empty, blank, unformatted, unpartitioned HDF file" --
    and it is what you want when the plan is to partition it on the Amiga with HDToolBox.

    Written sparse, so a 4 GB image costs almost nothing until something fills it.
    """
    with open(target, "wb") as handle:
        handle.truncate(total_bytes)
    return InitResult(target=target, kind="blank", size_bytes=total_bytes)


def create_plain(target: str, total_bytes: int, volume: str, dos_type: int) -> InitResult:
    """Create a single-volume image with no partition table, formatted and ready to mount.

    The shape emulators traditionally mount, and what `compose --format plain` writes.
    """
    from amitools.fs.ADFSVolume import ADFSVolume
    from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
    from amitools.fs.FSString import FSString

    blkdev = BlkDevFactory().create(target, force=True, options={"size": total_bytes})
    try:
        adfs = ADFSVolume(blkdev)
        adfs.create(FSString(volume), None, dos_type=dos_type)
        adfs.close()
    finally:
        blkdev.close()
    return InitResult(
        target=target, kind="plain", size_bytes=total_bytes, volume=volume, formatted=True
    )


def create_rdb(target: str, layout: DriveLayout, *, format_volumes: bool = True) -> InitResult:
    """Create an RDB drive with the planned partitions, formatted unless told not to.

    Goes through `compose`'s writer rather than carrying its own. A plan is built whose volumes
    have **no entries**: `format_volume` with `write` false means "format this and put nothing in
    it", which is exactly what a new drive needs. So `init` and `compose` share one implementation
    of writing an Amiga partition table, and a drive made by `init` is structurally identical to a
    composed one by construction rather than by inspection.
    """
    from ..layers import compose as CP
    from ..layers import drive as D
    from ..layers import targets as T
    from ..layers.blobs import HashOnlyBlobStore

    record = layout.as_record()
    plan = CP.Plan(
        drive=record,
        volumes=[
            CP.VolumePlan(
                volume=part.volume,
                policy=D.POLICY_REPLACE,
                partition=record["partitions"][part.index],
                # No entries, so `write` is false and nothing is copied in. `format_volume`
                # decides whether the volume gets a filesystem at all.
                format_volume=format_volumes,
                write=False,
                existed=False,
            )
            for part in layout.partitions
        ],
    )

    # The blob store is never consulted: `write_volume_entries` is only reached when a volume has
    # entries to write, and none here do. A store that holds nothing and cannot serve content is
    # the honest thing to pass.
    written = T.write_rdb(plan, HashOnlyBlobStore(), target)

    result = InitResult(
        target=target,
        kind="rdb",
        size_bytes=layout.geometry.total_bytes,
        layout=layout,
        formatted=format_volumes,
    )
    result.warnings.extend(layout.warnings)
    # Warnings from the writer are about this drive too, but the "left unformatted on this new
    # drive" note is expected under --no-format and would only be noise.
    result.warnings.extend(
        w for w in written.warnings if not (not format_volumes and "unformatted" in w)
    )
    return result


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def cmd_init(args: Any, out: render.Output) -> int:
    """Create a new image.

    **An existing file is never overwritten, and there is no flag to make it happen.** That is
    deliberate and comes straight from the brief. Every other destructive path in this tool has a
    `--force`, because in those cases the user has named an existing drive on purpose. Here the
    whole point is that the target is new, so a `--force` could only ever serve a mistake --
    and the mistake it would serve is "I meant to make a fresh drive and instead destroyed the
    one I already had". `rm` is one command away if that really is what you want.
    """
    target = args.target
    if os.path.exists(target):
        raise UsageError(
            f"{target} already exists, and init never overwrites. Delete it first if you mean to "
            "replace it, or choose another path"
        )

    total_bytes = parse_init_size(args.size)
    requests = parse_partition_specs(getattr(args, "partition", None))

    if args.plain and requests:
        raise UsageError(
            "--plain makes a single-volume image with no partition table, so it cannot be "
            "combined with --partition. Drop one of them"
        )
    if args.plain and not args.volume:
        raise UsageError("--plain needs --volume NAME to name the one volume it creates")
    if args.volume and not args.plain:
        raise UsageError(
            "--volume names the single volume of a --plain image. For a partitioned drive, name "
            "each volume in its --partition instead"
        )
    if getattr(args, "no_format", False) and not requests:
        raise UsageError(
            "--no-format applies to the partitions of an RDB drive, and none were given. "
            "Without --partition, init already creates an unformatted image"
        )

    try:
        if args.plain:
            dos_type = parse_dos_type(args.dos_type or DEFAULT_DOS_TYPE_NAME)
            result = create_plain(target, total_bytes, args.volume, dos_type)
        elif requests:
            layout = plan_layout(total_bytes, requests)
            result = create_rdb(target, layout, format_volumes=not args.no_format)
        else:
            result = create_blank(target, total_bytes)
    except Exception:
        # A half-written image is worse than none: it looks usable and is not. `init` promised a
        # new file, so on failure it leaves nothing behind rather than a plausible-looking stub.
        if os.path.exists(target):
            os.unlink(target)
        raise

    if out.as_json:
        out.data(result.as_dict())
        return 0

    _render_init(out, result)
    return 0


def _render_init(out: render.Output, result: InitResult) -> None:
    out.field("created", result.target)
    out.field("size", render.human_bytes(result.size_bytes))

    if result.kind == "blank":
        out.line()
        out.line("no partition table, no filesystem: partition it with HDToolBox on the Amiga,")
        out.line("or re-run with --partition to have this build the layout for you")
    elif result.kind == "plain":
        out.field("volume", f"{result.volume}: (single volume, no partition table)")

    if result.layout is not None:
        geometry = result.layout.geometry
        out.field(
            "geometry",
            f"{geometry.cyls} cyl x {geometry.heads} heads x {geometry.sectors} sectors "
            f"x {geometry.block_size} bytes",
        )
        out.field(
            "reserved",
            f"{result.layout.rdb_cylinders} cylinder(s) for the partition table "
            f"({render.human_bytes(result.layout.rdb_cylinders * result.layout.bytes_per_cylinder)})",
        )

        table = render.Table(
            headers=["#", "device", "volume", "dostype", "cyls", "size", "asked for", "flags"],
            align=["r", "l", "l", "l", "r", "r", "r", "l"], indent="  ",
        )
        for part in result.layout.partitions:
            asked = "rest" if part.requested_bytes is None else render.human_bytes(
                part.requested_bytes
            )
            flags = []
            if part.bootable:
                flags.append("bootable")
            if not result.formatted:
                flags.append("unformatted")
            table.add(
                part.index, part.device, f"{part.volume}:", part.dos_type_name,
                f"{part.low_cyl}-{part.high_cyl}",
                render.human_bytes(part.actual_bytes), asked, " ".join(flags) or "-",
            )
        out.line()
        out.lines(table.render())

        rounded = [p for p in result.layout.partitions if p.rounded]
        if rounded:
            out.line()
            out.line("cylinder rounding (partitions must start and end on a cylinder):")
            for part in rounded:
                sign = "+" if part.rounded > 0 else "-"
                out.line(
                    f"  {part.volume}: {sign}{render.human_bytes(abs(part.rounded))} "
                    f"vs the {render.human_bytes(part.requested_bytes)} asked for"
                )

    if result.warnings:
        out.heading(f"warnings ({len(result.warnings)})")
        for text in result.warnings:
            out.line(f"  {text}")

    if result.kind == "rdb" and result.formatted:
        out.line()
        out.line("ready to use. Mount it in FS-UAE with:")
        out.line(f"  hard_drive_0 = {os.path.abspath(result.target)}")
