"""The drive record a base layer carries: geometry, partitions and boot blocks.

This is the part of the design that converts two of the riskiest unknowns into copied facts
(docs/KIP-FFS-LAYERS.md section 4):

* **`de_Mask` / `de_MaxTransfer`.** Rather than composing a drive with defaults that may
  silently corrupt data on a given controller, a base layer captured from a drive that
  already works carries that drive's proven values. This is not hypothetical -- the Emu68
  guide documents PFS3 failing on PiStorm because HDToolBox's suggested mask confines
  buffers to the first 16 MB while most Emu68 RAM sits above it.
* **Bootability.** Reproducing a known-good RDB -- right DosType, right bootable flag, right
  boot priority, right geometry -- is far likelier to boot than synthesising one.

So the record keeps the DosEnvec **verbatim** for every partition, not a curated subset.
Anything omitted here becomes a default at compose time, and a default is exactly the
failure this exists to prevent.

Boot blocks are captured too, which settles open question L2 (whether any real drive has a
non-standard boot block) by measurement instead of assumption. They are stored compactly:
the DosType and checksum always, and the boot code itself only when there is any, so an
ordinary formatted partition costs a few dozen bytes rather than a kilobyte of base64.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from ..errors import ImageError, UsageError
from ..image import DOS_ENV_FIELDS, Container, ImageKind

#: Bumped if the record's shape changes. It feeds a base layer's identity hash, so a silent
#: change of shape would alter layer IDs.
DRIVE_SCHEME = "amibuilder-drive-v1"

#: Per-volume composition policy (layers doc section 7).
POLICY_REPLACE = "replace"
POLICY_MERGE = "merge"
POLICY_PRESERVE = "preserve"
POLICIES = (POLICY_REPLACE, POLICY_MERGE, POLICY_PRESERVE)

#: How many blocks of a partition hold the boot block. Two is the FFS/OFS convention, and
#: what `DosEnvec.boot_blocks` means when it is left at zero.
BOOT_BLOCK_COUNT = 2

#: Bytes at the start of a boot block that are structural rather than code: the DosType
#: longword and the checksum longword, then the root-block pointer.
BOOT_HEADER_BYTES = 12


def default_policy(*, bootable: bool) -> str:
    """The policy suggested at capture time.

    `replace` for the bootable partition, because "put the OS back to stock" is the operation
    the whole tool exists for and formatting into a clean volume is its safest path.
    `merge` for everything else, because it destroys nothing. `preserve` cannot be inferred
    -- a save-games volume looks exactly like a work volume from outside -- so it is left for
    the user to set, and `compose` names what it would destroy before doing it.
    """
    return POLICY_REPLACE if bootable else POLICY_MERGE


def check_policy(policy: str) -> str:
    if policy not in POLICIES:
        raise UsageError(
            f"unknown policy {policy!r} -- expected one of {', '.join(POLICIES)}"
        )
    return policy


# ---------------------------------------------------------------------------
# Boot blocks
# ---------------------------------------------------------------------------


def _describe_boot_blocks(data: bytes) -> dict[str, Any]:
    """Summarise a partition's boot blocks, keeping the bytes only when they carry code."""
    record: dict[str, Any] = {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "dos_type": f"0x{int.from_bytes(data[0:4], 'big'):08x}" if len(data) >= 4 else None,
        "checksum": f"0x{int.from_bytes(data[4:8], 'big'):08x}" if len(data) >= 8 else None,
    }
    body = data[BOOT_HEADER_BYTES:]
    has_code = any(body)
    record["has_boot_code"] = has_code
    # Only a custom boot block needs its bytes preserved. An ordinary formatted partition
    # has an empty body, and storing a kilobyte of base64 zeros per partition would bloat
    # every base layer for nothing.
    record["boot_code"] = base64.b64encode(data).decode("ascii") if has_code else None
    return record


def read_boot_blocks(container: Container, dos_env: dict[str, int]) -> dict[str, Any]:
    """Read a partition's boot blocks out of the container.

    The offset is derived from the partition's *own* DosEnvec geometry rather than the
    drive's, which is what amitools' `PartBlockDevice` does and is the only correct source:
    a partition may legally describe different surfaces or sectors-per-track from the RDB.
    """
    block_bytes = int(dos_env["block_size"]) * 4
    heads = int(dos_env["surfaces"])
    sectors = int(dos_env["blk_per_trk"])
    if block_bytes <= 0 or heads <= 0 or sectors <= 0:
        raise ImageError(
            f"partition geometry is unusable: block_size={block_bytes} heads={heads} "
            f"sectors={sectors}"
        )
    offset = int(dos_env["low_cyl"]) * heads * sectors * block_bytes
    want = BOOT_BLOCK_COUNT * block_bytes
    with container.stream() as fh:
        fh.seek(offset)
        data = fh.read(want)
    if len(data) < want:
        raise ImageError(
            f"image ends before the partition's boot blocks at offset {offset} "
            f"(wanted {want} bytes, got {len(data)})"
        )
    return _describe_boot_blocks(data)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture(container: Container, *, boot_blocks: bool = True) -> dict[str, Any]:
    """Build the drive record for a base layer.

    Read-only. Returns a plain dict so it can be embedded in `layer.json` and hashed.
    """
    geom = container.geometry
    parts = container.partitions(probe_volumes=True)

    record: dict[str, Any] = {
        "scheme": DRIVE_SCHEME,
        "kind": container.kind.value,
        "block_size": geom.block_size,
        "cylinders": geom.cyls,
        "heads": geom.heads,
        "sectors": geom.sectors,
        "num_blocks": geom.num_blocks,
        "total_bytes": geom.total_bytes,
        "partitions": [],
    }

    if container.kind is not ImageKind.RDB:
        # A plain HDF or ADF has no partition table. Recording that plainly is better than
        # inventing a single-partition RDB, because the shape of the target is a compose-time
        # decision and the source genuinely does not constrain it.
        record["partitions"] = []
        record["single_volume"] = True
        return record

    record["single_volume"] = False
    for part in parts:
        entry: dict[str, Any] = {
            "index": part.index,
            "device": part.device_name,
            "volume": part.volume_name,
            "volume_error": part.volume_error,
            "dos_type": f"0x{part.dos_type.raw:08x}",
            #: The on-disk signature, spelled the Amiga way: `DOS\3`.
            "dos_type_label": part.dos_type.label,
            #: The same thing named for humans: `ffs`, `pfs3`. Rendering only.
            "filesystem": part.dos_type.filesystem,
            "bootable": part.bootable,
            "automount": part.automount,
            "low_cyl": part.low_cyl,
            "high_cyl": part.high_cyl,
            "num_blocks": part.num_blocks,
            "num_bytes": part.num_bytes,
            "policy": default_policy(bootable=part.bootable),
            # Verbatim, every field. See the module docstring.
            "dos_env": {name: int(part.dos_env[name]) for name in DOS_ENV_FIELDS},
        }
        if boot_blocks:
            try:
                entry["boot_blocks"] = read_boot_blocks(container, part.dos_env)
            except ImageError as exc:
                # A partition whose boot blocks cannot be read is worth recording as such
                # rather than aborting the whole capture -- one PFS3 partition must not make
                # the drive uncapturable.
                entry["boot_blocks"] = {"error": str(exc)}
        record["partitions"].append(entry)

    validate(record)
    return record


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def partition_cylinder_ranges(record: dict[str, Any]) -> list[tuple[int, int, str]]:
    return [
        (int(p["low_cyl"]), int(p["high_cyl"]), str(p.get("device") or f"#{p.get('index')}"))
        for p in record.get("partitions", [])
    ]


def validate(record: dict[str, Any]) -> None:
    """Sanity-check a drive record. Raises on anything that would be unsafe to compose.

    These are the guards the layers design asks for against a partition-offset bug
    scribbling into a neighbour (section 3.1). Cheap, and checked at capture time as well as
    compose time so a bad record never reaches the store.
    """
    if record.get("scheme") != DRIVE_SCHEME:
        raise ImageError(
            f"drive record has scheme {record.get('scheme')!r}, expected {DRIVE_SCHEME!r}"
        )
    cylinders = int(record.get("cylinders") or 0)
    ranges = partition_cylinder_ranges(record)

    for low, high, name in ranges:
        if low > high:
            raise ImageError(f"partition {name}: low_cyl {low} is above high_cyl {high}")
        if low < 1:
            # Cylinder 0 holds the RigidDiskBlock itself. A partition claiming it would
            # overwrite the partition table that describes it.
            raise ImageError(
                f"partition {name} starts at cylinder {low}; cylinder 0 holds the RDB"
            )
        if cylinders and high >= cylinders:
            raise ImageError(
                f"partition {name} ends at cylinder {high}, beyond the drive's {cylinders}"
            )

    ordered = sorted(ranges)
    for (a_low, a_high, a_name), (b_low, b_high, b_name) in zip(ordered, ordered[1:]):
        if b_low <= a_high:
            raise ImageError(
                f"partitions {a_name} ({a_low}-{a_high}) and {b_name} ({b_low}-{b_high}) "
                "overlap"
            )

    for part in record.get("partitions", []):
        check_policy(str(part.get("policy", POLICY_MERGE)))
        missing = [f for f in DOS_ENV_FIELDS if f not in (part.get("dos_env") or {})]
        if missing:
            raise ImageError(
                f"partition {part.get('device')}: drive record is missing DosEnvec fields "
                f"{', '.join(missing)} -- composing it would substitute defaults"
            )


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------


def find_partition(record: dict[str, Any], selector: str | int) -> dict[str, Any]:
    """Look a partition up by index, device name or volume name."""
    parts = record.get("partitions") or []
    if isinstance(selector, int) or (isinstance(selector, str) and selector.isdigit()):
        index = int(selector)
        for part in parts:
            if int(part["index"]) == index:
                return part
        raise UsageError(f"drive record has no partition {index}")
    needle = str(selector).rstrip(":").casefold()
    for part in parts:
        if str(part.get("volume") or "").casefold() == needle:
            return part
    for part in parts:
        if str(part.get("device") or "").casefold() == needle:
            return part
    known = ", ".join(
        f"{p.get('device')}={p.get('volume') or '?'}" for p in parts
    )
    raise UsageError(f"drive record has no partition matching {selector!r} (have: {known})")


def volumes(record: dict[str, Any]) -> list[str]:
    """Volume names in the record, in partition order, skipping unmountable ones."""
    return [str(p["volume"]) for p in record.get("partitions", []) if p.get("volume")]


def set_policy(record: dict[str, Any], selector: str | int, policy: str) -> dict[str, Any]:
    """Return a copy of the record with one partition's policy changed."""
    check_policy(policy)
    import copy

    updated = copy.deepcopy(record)
    find_partition(updated, selector)["policy"] = policy
    return updated


def summary_lines(record: dict[str, Any]) -> list[str]:
    """Human-readable description, for `snap show`."""
    out = [
        f"geometry     {record.get('cylinders')} cyl x {record.get('heads')} heads x "
        f"{record.get('sectors')} sectors x {record.get('block_size')} bytes",
    ]
    if record.get("single_volume"):
        out.append("layout       single volume, no partition table")
        return out
    for part in record.get("partitions", []):
        env = part.get("dos_env") or {}
        flags = []
        if part.get("bootable"):
            flags.append(f"bootable pri {env.get('boot_pri', 0)}")
        if not part.get("automount"):
            flags.append("no automount")
        boot = part.get("boot_blocks") or {}
        if boot.get("has_boot_code"):
            flags.append("custom boot block")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        out.append(
            f"{part.get('device'):<6} {str(part.get('volume') or '?') + ':':<14} "
            f"cyl {part.get('low_cyl')}-{part.get('high_cyl')}  "
            f"{part.get('dos_type_label')} ({part.get('filesystem')})"
            f"  policy={part.get('policy')}"
            f"  mask=0x{env.get('mask', 0):08x} maxtr=0x{env.get('max_transfer', 0):08x}"
            f"{suffix}"
        )
    return out


__all__ = [
    "BOOT_BLOCK_COUNT",
    "DRIVE_SCHEME",
    "POLICIES",
    "POLICY_MERGE",
    "POLICY_PRESERVE",
    "POLICY_REPLACE",
    "capture",
    "check_policy",
    "default_policy",
    "find_partition",
    "read_boot_blocks",
    "set_policy",
    "summary_lines",
    "validate",
    "volumes",
]
