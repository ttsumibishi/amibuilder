"""Guard rails for raw device access.

These land in Phase 1, before anything can write, because the failure mode is severe and
silent: on a PiStorm microSD card the non-0x76 MBR slots hold Emu68 itself, and on a Mac
`/dev/rdisk1` is as likely to be the boot volume as the card. A byte written to the wrong
device is not recoverable by any amount of careful code afterwards.

Four rules, in order of severity:

1. Touching a device at all requires `--device`. Naming one by accident is the common
   mistake -- `rdisk2` versus `rdisk3` is one keystroke -- and an explicit flag means the
   user has at least thought about which.
2. The boot disk is refused outright, read or write. There is no Amiga data on it, so
   there is no request to honour, only a typo to catch.
3. Writing requires typed confirmation of the device name, not a y/n. Reflexively typing
   "y" is easy; typing "rdisk4" requires reading the prompt.
4. Without a TTY, writing requires `--yes`, so a script cannot stall on a prompt or, worse,
   have one silently satisfied.
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass

from .errors import DeviceRefused

_DEVICE_PREFIXES = ("/dev/disk", "/dev/rdisk", "/dev/sd", "/dev/nvme", "/dev/mmcblk")


def is_device(path: str) -> bool:
    """True for a block or character device node."""
    if path.startswith(_DEVICE_PREFIXES):
        return True
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    whole_disk: str  #: e.g. "disk4"
    is_boot: bool
    removable: bool | None = None
    size_bytes: int = 0
    model: str = ""


def _diskutil_plist(*args: str) -> dict | None:
    """Run diskutil with -plist output, or None if unavailable."""
    if platform.system() != "Darwin" or not shutil.which("diskutil"):
        return None
    try:
        proc = subprocess.run(
            ["diskutil", *args, "-plist"] if "-plist" not in args else ["diskutil", *args],
            capture_output=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        return plistlib.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return None


def _whole_disk_name(path: str) -> str:
    """Reduce /dev/rdisk4s2 to 'disk4' so slices and raw nodes compare equal."""
    m = re.search(r"r?(disk\d+)", path)
    return m.group(1) if m else path


def boot_whole_disk() -> str | None:
    """The whole-disk identifier backing '/', or None if it cannot be determined."""
    info = _diskutil_plist("info", "-plist", "/")
    if not info:
        return None
    for key in ("ParentWholeDisk", "DeviceIdentifier"):
        val = info.get(key)
        if isinstance(val, str) and val:
            return _whole_disk_name(val)
    return None


def describe(path: str) -> DeviceInfo:
    """Gather what is known about a device, degrading gracefully off macOS."""
    whole = _whole_disk_name(path)
    boot = boot_whole_disk()
    info = _diskutil_plist("info", "-plist", whole) or {}
    return DeviceInfo(
        path=path,
        whole_disk=whole,
        is_boot=(boot is not None and whole == boot),
        removable=info.get("Removable", info.get("RemovableMedia")),
        size_bytes=int(info.get("TotalSize", 0) or 0),
        model=str(info.get("MediaName", "") or ""),
    )


def device_listing() -> str:
    """`diskutil list` output, for showing the user what the identifiers actually are."""
    if platform.system() != "Darwin" or not shutil.which("diskutil"):
        return "(diskutil unavailable on this platform)"
    try:
        proc = subprocess.run(["diskutil", "list"], capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() or "(diskutil produced no output)"
    except (OSError, subprocess.SubprocessError):
        return "(diskutil failed)"


def check_access(path: str, *, device_flag: bool, writable: bool, assume_yes: bool = False,
                 stream=None) -> DeviceInfo | None:
    """Apply every guard rail. Returns None for non-devices, DeviceInfo when allowed.

    Raises DeviceRefused otherwise. Called before any device is opened.
    """
    if not is_device(path):
        return None

    out = stream or sys.stderr

    if not device_flag:
        raise DeviceRefused(
            f"{path} is a raw device. Pass --device to confirm you mean to touch real "
            f"hardware.\n\nAttached disks:\n{device_listing()}"
        )

    info = describe(path)

    if info.is_boot:
        raise DeviceRefused(
            f"{path} is on {info.whole_disk}, which backs this Mac's boot volume. "
            f"Refusing regardless of flags -- there is no Amiga data here, so this is a "
            f"mistyped identifier.\n\nAttached disks:\n{device_listing()}"
        )

    if path.startswith("/dev/disk") and not path.startswith("/dev/rdisk"):
        # The buffered node is an order of magnitude slower and can serve stale pages.
        print(
            f"note: {path} is the buffered node; /dev/r{_whole_disk_name(path)} is far "
            f"faster for whole-image work",
            file=out,
        )

    if info.removable is False:
        print(
            f"warning: {info.whole_disk} reports as non-removable"
            + (f" ({info.model})" if info.model else "")
            + " -- check this is really your card reader",
            file=out,
        )

    if writable:
        _confirm_write(info, assume_yes=assume_yes, stream=out)

    return info


def _confirm_write(info: DeviceInfo, *, assume_yes: bool, stream) -> None:
    target = info.whole_disk
    if assume_yes:
        print(f"note: --yes given; writing to {info.path} without confirmation", file=stream)
        return

    if not sys.stdin.isatty():
        raise DeviceRefused(
            f"refusing to write to {info.path} without a terminal to confirm on. "
            f"Pass --yes if this is deliberate and scripted."
        )

    size = f" ({info.size_bytes} bytes)" if info.size_bytes else ""
    model = f" {info.model}" if info.model else ""
    print(
        f"\nAbout to WRITE to {info.path}{model}{size}.\n"
        f"This overwrites data on the physical device and cannot be undone.\n"
        f"Type the device identifier ({target}) to proceed, or anything else to abort: ",
        end="", file=stream, flush=True,
    )
    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        raise DeviceRefused("aborted") from None
    if answer != target:
        raise DeviceRefused(f"aborted: expected {target!r}, got {answer!r}")
