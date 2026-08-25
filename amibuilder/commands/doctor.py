"""`doctor` -- a read-only self-check of the environment amibuilder runs on.

It reports on the runtime rather than on any image: the Python version, the amitools
dependency (and whether its version is inside the band the workarounds were calibrated
against), whether this host can hole-punch so `compact` can reclaim space, whether the
backup store's filesystem holds files sparsely, plus a few informational lines (readline
flavour, the raw-device guard, amibuilder's own version).

Exit status follows `check`: 0 when everything passes or only warns, non-zero only when a
hard requirement is missing (no amitools, Python too old). Warnings never fail, so
`doctor` is safe as a scripted preflight: `amibuilder doctor && amibuilder compose ...`.

The F_PUNCHHOLE constants are borrowed from `compact` rather than redefined, so the one
copy of those magic numbers stays in one place.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from ..errors import UsageError
from ..render import Output, Table
from .compact import _F_PUNCHHOLE, _PAGE, _PUNCHHOLE_STRUCT

# Status labels. FAIL is the only one that changes the exit code; WARN and INFO do not.
_OK = "ok"
_WARN = "warn"
_FAIL = "fail"
_INFO = "info"

#: Minimum interpreter, mirroring `requires-python` in pyproject.toml.
_PYTHON_FLOOR = (3, 10)
#: amitools floor (the pyproject dependency) and the (major, minor) band the amitools
#: workarounds in volume.py / targets.py / image.py were calibrated against.
_AMITOOLS_FLOOR = (0, 8, 1)
_AMITOOLS_BAND = (0, 8)


def _parse_version(text: str) -> tuple[int, ...]:
    """The leading numeric components of a version, e.g. '0.8.1.dev3' -> (0, 8, 1)."""
    parts: list[int] = []
    for chunk in text.split("."):
        m = re.match(r"\d+", chunk)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts)


def _amitools_version() -> str | None:
    """The installed amitools version, or None if it is not installed."""
    try:
        return version("amitools")
    except PackageNotFoundError:
        return None


def _probe_sparse(directory: str) -> dict[str, Any]:
    """Punch a hole in a scratch file under `directory` and see if the disk frees it.

    Returns {supported, reclaimed, reason}: `supported` is whether the punch itself was
    accepted (the mechanism works here), `reclaimed` whether the on-disk allocation
    actually dropped (the filesystem holds files sparsely). macOS only -- off Darwin it
    reports unsupported without issuing the fcntl, because command 99 is F_PUNCHHOLE only
    on macOS.
    """
    if sys.platform != "darwin":
        return {"supported": False, "reclaimed": False,
                "reason": "hole-punching via F_PUNCHHOLE is macOS-only"}
    if not os.path.isdir(directory):
        return {"supported": False, "reclaimed": False,
                "reason": f"{directory}: not a directory"}

    pages = 64  # 256 KiB of real data, so a punched hole is comfortably measurable
    payload = b"\xa5" * (_PAGE * pages)
    fd, tmp = tempfile.mkstemp(prefix=".amibuilder-doctor-", dir=directory)
    try:
        os.write(fd, payload)
        os.fsync(fd)
        before = os.fstat(fd).st_blocks
        arg = struct.pack(_PUNCHHOLE_STRUCT, 0, 0, _PAGE, _PAGE * (pages // 2))
        try:
            fcntl.fcntl(fd, _F_PUNCHHOLE, arg)
        except OSError as e:
            return {"supported": False, "reclaimed": False,
                    "reason": f"F_PUNCHHOLE not supported here ({e.strerror})"}
        os.fsync(fd)
        reclaimed = os.fstat(fd).st_blocks < before
        reason = "" if reclaimed else (
            "the punch was accepted but freed no space -- this filesystem cannot store "
            "files sparsely")
        return {"supported": True, "reclaimed": reclaimed, "reason": reason}
    finally:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _result(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def _check_amibuilder() -> dict[str, Any]:
    from ..layers.store import tool_version
    ver = tool_version()
    return _result("amibuilder", _INFO, ver, version=ver)


def _check_python() -> dict[str, Any]:
    v = sys.version_info
    got = f"{v.major}.{v.minor}.{v.micro}"
    floor = ".".join(str(n) for n in _PYTHON_FLOOR)
    if (v.major, v.minor) >= _PYTHON_FLOOR:
        return _result("python", _OK, got, version=got, required=floor)
    return _result("python", _FAIL, f"{got} is below the required {floor}",
                   version=got, required=floor)


def _check_amitools() -> dict[str, Any]:
    floor = ".".join(str(n) for n in _AMITOOLS_FLOOR)
    ver = _amitools_version()
    if ver is None:
        return _result("amitools", _FAIL, f"not installed (required >= {floor})",
                       version=None, floor=floor)
    if _parse_version(ver) < _AMITOOLS_FLOOR:
        return _result("amitools", _FAIL, f"{ver} is below the required {floor}",
                       version=ver, floor=floor)
    if _parse_version(ver)[:2] != _AMITOOLS_BAND:
        band = ".".join(str(n) for n in _AMITOOLS_BAND)
        return _result("amitools", _WARN,
                       f"{ver} is newer than the tested {band}.x; the amitools workarounds "
                       f"may not hold",
                       version=ver, floor=floor)
    return _result("amitools", _OK, ver, version=ver, floor=floor)


def _check_hole_punch() -> dict[str, Any]:
    probe = _probe_sparse(tempfile.gettempdir())
    if probe["supported"]:
        return _result("hole-punching", _OK, "F_PUNCHHOLE available", **probe)
    return _result("hole-punching", _WARN,
                   f"unavailable -- {probe['reason']}; `compact` cannot reclaim disk here",
                   **probe)


def _check_store(store: str) -> dict[str, Any]:
    probe = _probe_sparse(store)
    if probe["reclaimed"]:
        return _result("sparse store", _OK, f"{store} stores files sparsely",
                       store=store, **probe)
    if probe["supported"]:
        return _result("sparse store", _WARN,
                       f"{store}: hole-punch freed no space; back up compressed (.zst/.gz) "
                       f"instead",
                       store=store, **probe)
    return _result("sparse store", _WARN,
                   f"{store}: {probe['reason']}; back up compressed (.zst/.gz) for portability",
                   store=store, **probe)


def _check_readline() -> dict[str, Any]:
    try:
        import readline
    except Exception:  # readline is a nicety, never required -- same stance as shell.py
        return _result("readline", _INFO, "not available (shell line-editing degraded)",
                       flavour=None)
    if "libedit" in (readline.__doc__ or ""):
        return _result("readline", _INFO, "libedit (macOS)", flavour="libedit")
    return _result("readline", _INFO, "GNU readline", flavour="gnu")


def _check_device_guard() -> dict[str, Any]:
    from ..device import is_device
    wired = is_device("/dev/rdisk99") and not is_device(os.devnull + "-not-a-device")
    if wired:
        return _result("device guard", _INFO, "active (raw /dev/* writes are gated)",
                       recognised=True)
    return _result("device guard", _WARN, "device classifier is not behaving as expected",
                   recognised=False)


def cmd_doctor(args: Any, out: Output) -> int:
    store = args.store or os.getcwd()
    if args.store is not None and not os.path.isdir(store):
        raise UsageError(f"{store}: not a directory")

    checks = [
        _check_amibuilder(),
        _check_python(),
        _check_amitools(),
        _check_hole_punch(),
        _check_store(store),
        _check_readline(),
        _check_device_guard(),
    ]

    counts = {_OK: 0, _WARN: 0, _FAIL: 0, _INFO: 0}
    for c in checks:
        counts[c["status"]] += 1

    t = Table(["check", "status", "detail"], align=["l", "l", "l"], indent="  ")
    for c in checks:
        t.add(c["name"], c["status"].upper(), c["detail"])
    out.heading("amibuilder doctor")
    out.table(t)
    out.line()

    if counts[_FAIL]:
        first = next(c for c in checks if c["status"] == _FAIL)
        out.line(f"{counts[_FAIL]} check(s) failed -- {first['name']}: {first['detail']}")
    elif counts[_WARN]:
        out.line(f"{counts[_WARN]} warning(s); nothing blocking.")
    else:
        out.line("all checks passed.")

    out.data({
        "ok": counts[_FAIL] == 0,
        "store": store,
        "checks": checks,
        "summary": {"ok": counts[_OK], "warn": counts[_WARN],
                    "fail": counts[_FAIL], "info": counts[_INFO]},
    })
    return 1 if counts[_FAIL] else 0


__all__ = ["cmd_doctor"]
