"""Error types.

Every failure the user can reasonably cause is an AmibuilderError with a message written
for a human. The CLI catches these, prints the message and exits non-zero, without a
traceback. Anything that escapes as a bare exception is a bug in amibuilder.
"""

from __future__ import annotations


class AmibuilderError(Exception):
    """Base class for expected, reportable failures."""

    #: Process exit status. Distinct codes let scripts branch without parsing messages.
    exit_code = 1


class UsageError(AmibuilderError):
    """The command line itself does not make sense."""

    exit_code = 2


class AddressError(UsageError):
    """A source specification could not be parsed or resolved."""


class NotFoundError(AmibuilderError):
    """A path inside an image does not exist."""

    exit_code = 3


class UnsupportedError(AmibuilderError):
    """Recognised, but deliberately not handled.

    Raised for PFS3/SFS partitions, non-DOS ADFs, DMS archives and similar. The message
    must say what was found and what to use instead -- guessing at an unknown filesystem
    is how images get corrupted.
    """

    exit_code = 4


class ImageError(AmibuilderError):
    """The image is malformed, truncated or not the shape it claims to be."""

    exit_code = 5


class ValidationError(AmibuilderError):
    """`check` found structural problems."""

    exit_code = 6


class DeviceRefused(AmibuilderError):
    """A raw device operation was refused by a guard rail.

    Separate from UsageError because these refusals are the safety net that stops a
    typo from destroying a PiStorm card, and are worth distinguishing in scripts.
    """

    exit_code = 7
