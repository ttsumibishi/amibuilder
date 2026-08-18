"""amibuilder -- file-level access and layered snapshots for Amiga disk images.

The FFS/OFS/RDB/ADF implementation is amitools'. It is reached only through
`amibuilder.volume` and `amibuilder.image`, so that dependency stays replaceable;
see docs/KIP-FFS-NOTES.md section 5.3 for why that matters.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
