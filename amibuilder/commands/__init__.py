"""Command implementations, and the plumbing they share.

Each command is a plain function `(args, out) -> int`. The return value is the process
exit status, so a command can report "worked, but found problems" (as `check` does)
without raising.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .. import device
from ..addressing import Address, parse
from ..image import Container, open_container
from ..volume import Volume


def resolve(args: Any, *, writable: bool = False) -> Address:
    """Parse the source argument and apply the device guard rails.

    Every command routes through here, so there is exactly one place where a raw device
    can be reached and exactly one place the guards can be forgotten.
    """
    addr = parse(args.source)
    device.check_access(
        addr.path,
        device_flag=getattr(args, "device", False),
        writable=writable,
        assume_yes=getattr(args, "yes", False),
    )
    return addr


@contextmanager
def opened_container(args: Any, *, writable: bool = False) -> Iterator[Container]:
    addr = resolve(args, writable=writable)
    with open_container(addr, writable=writable) as container:
        yield container


@contextmanager
def opened_volume(args: Any, *, writable: bool = False) -> Iterator[tuple[Container, Volume]]:
    """Open the container and mount the volume the address names."""
    with opened_container(args, writable=writable) as container:
        with container.open_addressed_volume() as vol:
            yield container, vol
