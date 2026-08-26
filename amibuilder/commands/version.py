"""`version` -- report amibuilder's version and the versions it is running on.

The top-level `--version` flag prints just amibuilder's own version, which argparse handles
and exits on. This command exists for the two things that flag cannot do: name the
dependency versions that actually determine behaviour (amitools above all), and speak
`--json` so a bug report or a script can capture them.

It is deliberately a strict subset of `doctor`: same readers, no judgement. `doctor` decides
whether a version is acceptable; `version` only says what it is.
"""

from __future__ import annotations

import platform
import sys
from typing import Any

from ..render import Output
from .doctor import _amitools_version


def cmd_version(args: Any, out: Output) -> int:
    from ..layers.store import tool_version

    v = sys.version_info
    payload = {
        "amibuilder": tool_version(),
        "amitools": _amitools_version(),
        "python": f"{v.major}.{v.minor}.{v.micro}",
        "python_implementation": platform.python_implementation(),
        "platform": sys.platform,
    }

    out.field("amibuilder", payload["amibuilder"], width=12)
    out.field("amitools", payload["amitools"] or "not installed", width=12)
    out.field("Python", f"{payload['python']} ({payload['python_implementation']})", width=12)
    out.field("Platform", payload["platform"], width=12)
    out.data(payload)
    return 0


__all__ = ["cmd_version"]
