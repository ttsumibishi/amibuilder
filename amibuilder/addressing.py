"""Parsing of source specifications.

A single argument has to be able to name any of six things:

    card.hdf                 whole image (plain HDF, or an RDB's only partition)
    card.hdf:0               RDB partition by index
    card.hdf:DH0             RDB partition by device name, or by volume name
    disk.adf                 a floppy image (also .adz, .adf.gz)
    /dev/rdisk4              a raw device
    /dev/rdisk4:0x76:1       MBR primary slot 1 of type 0x76, and the RDB inside it
    /dev/rdisk4:0x76:1:2     ...then partition 2 of that RDB
    ./staging                a host directory

Paths inside an image are *not* part of this grammar. Commands take `SOURCE [PATH]` as
separate arguments, which avoids an unresolvable ambiguity between a partition named
`Work` and a top-level directory named `Work`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .errors import AddressError

#: Extensions amitools recognises as floppy images.
ADF_SUFFIXES = (".adf", ".adz", ".adf.gz", ".ipf", ".dms")

#: An MBR selector: literal type, then slot. `0x76` is the only type worth addressing.
_MBR_RE = re.compile(r"^0[xX]([0-9a-fA-F]{1,2}):(\d+)$")

#: A device node: /dev/rdisk4, /dev/rdisk4s2, /dev/sda1. Deliberately excludes ':' so
#: that "/dev/rdisk4:0x76:1" is not mistaken for a device whose name contains a selector.
_DEVICE_RE = re.compile(r"^/dev/(r?disk\d+[a-z0-9]*|sd[a-z]\d*|nvme\d+n\d+(p\d+)?|mmcblk\d+(p\d+)?)$")


@dataclass(frozen=True)
class Address:
    """A resolved source specification.

    `partition` is an int for index selection and a str for name selection; the
    distinction is preserved because "0" as a *name* is legal on an Amiga, however
    unwise, and silently reinterpreting it would open the wrong volume.
    """

    path: str
    spec: str
    is_device: bool = False
    is_directory: bool = False
    mbr_type: int | None = None
    mbr_slot: int | None = None
    partition: int | str | None = None

    @property
    def has_mbr_selector(self) -> bool:
        return self.mbr_slot is not None

    @property
    def looks_like_adf(self) -> bool:
        low = self.path.lower()
        return any(low.endswith(s) for s in ADF_SUFFIXES)

    def describe(self) -> str:
        out = self.path
        if self.has_mbr_selector:
            out += f":0x{self.mbr_type:02x}:{self.mbr_slot}"
        if self.partition is not None:
            out += f":{self.partition}"
        return out

    def with_partition(self, partition: int | str | None) -> Address:
        """Return a copy selecting a different partition. Used by `--all`-style loops."""
        return Address(
            self.path, self.spec, self.is_device, self.is_directory,
            self.mbr_type, self.mbr_slot, partition,
        )


def _is_device_path(path: str) -> bool:
    """True for a macOS/Linux raw device node.

    Matched by shape as well as by stat, because a device that is busy or needs privilege
    should still be *recognised* as a device so the guard rails engage and the error
    explains itself rather than reporting "no such file". The pattern excludes ':' so a
    device path carrying a selector still splits correctly.
    """
    if _DEVICE_RE.match(path):
        return True
    try:
        import stat

        mode = os.stat(path).st_mode
        return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)
    except OSError:
        return False


def _parse_selector(sel: str, spec: str) -> tuple[int | None, int | None, int | str | None]:
    """Parse the part after the path. Returns (mbr_type, mbr_slot, partition)."""
    if sel == "":
        raise AddressError(f"{spec}: empty selector after ':'")
    parts = sel.split(":")

    # 0x76:N  or  0x76:N:P
    if len(parts) >= 2:
        m = _MBR_RE.match(":".join(parts[:2]))
        if m:
            mbr_type = int(m.group(1), 16)
            slot = int(m.group(2))
            if slot > 3:
                raise AddressError(
                    f"{spec}: MBR slot {slot} is out of range -- an MBR has 4 primary "
                    f"slots, numbered 0 to 3"
                )
            rest = parts[2:]
            if len(rest) > 1:
                raise AddressError(
                    f"{spec}: too many ':' components after the MBR slot; expected at "
                    f"most one partition selector"
                )
            part: int | str | None = None
            if rest:
                part = int(rest[0]) if rest[0].isdigit() else rest[0]
            return mbr_type, slot, part

    if len(parts) > 1:
        raise AddressError(
            f"{spec}: could not parse {sel!r}. Expected a partition index or name, or "
            f"an MBR selector like '0x76:1'."
        )

    sel = parts[0]
    return None, None, int(sel) if sel.isdigit() else sel


def parse(spec: str) -> Address:
    """Parse a source specification, resolving the path portion against the filesystem.

    The path is found by trying the longest candidate first and shortening at each ':'.
    That ordering matters: a file genuinely named `weird:name.hdf` is legal on macOS and
    must win over interpreting `:name.hdf` as a selector.
    """
    if not spec:
        raise AddressError("empty source specification")

    # Longest-first: the whole spec, then progressively shorter prefixes. The bool records
    # whether a ':' was actually split off, so that a trailing colon with nothing after it
    # is reported rather than silently ignored.
    candidates: list[tuple[str, str, bool]] = [(spec, "", False)]
    idx = len(spec)
    while True:
        idx = spec.rfind(":", 0, idx)
        if idx <= 0:  # index 0 would leave an empty path
            break
        candidates.append((spec[:idx], spec[idx + 1 :], True))

    for path, sel, split in candidates:
        if not os.path.exists(path) and not _is_device_path(path):
            continue
        is_dev = _is_device_path(path)
        is_dir = os.path.isdir(path) and not is_dev
        if not split:
            return Address(path, spec, is_device=is_dev, is_directory=is_dir)
        if is_dir:
            raise AddressError(
                f"{spec}: {path} is a host directory, so it has no partitions. Give a "
                f"path as a separate argument instead of a ':' selector."
            )
        mbr_type, slot, part = _parse_selector(sel, spec)
        return Address(path, spec, is_dev, is_dir, mbr_type, slot, part)

    # Nothing existed. Report against the most plausible candidate: the shortest prefix,
    # which is what the user most likely meant as the filename.
    likely = candidates[-1][0] if len(candidates) > 1 else spec

    if likely != spec:
        raise AddressError(
            f"{spec}: no such file or device (tried {likely!r} as the image path)"
        )
    raise AddressError(f"{spec}: no such file or device")


def parse_host_or_image(spec: str) -> Address:
    """Parse a spec that is allowed to be a host directory, creating nothing.

    Used by commands whose source or target may be either side of the boundary, such as
    the sync and compose paths in later phases.
    """
    return parse(spec)
