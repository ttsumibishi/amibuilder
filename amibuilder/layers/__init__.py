"""The layered snapshot model: blobs, manifests, layers, refs and recipes.

A base layer is a full capture of a drive plus its RDB layout; a diff layer records what
changed relative to a parent. Composing a stack rebuilds a drive from scratch, which is
why capture and composition never mutate an image in place.

Design: docs/KIP-FFS-LAYERS.md. The decisions worth knowing before reading the code:

* Manifest paths are **volume-qualified** (`Workbench:S/Startup-Sequence`). Physical
  placement lives only in a base layer's drive record, so multi-partition and multi-drive
  layouts are indistinguishable at the layer level (layers doc section 3.1).
* Timestamps are stored as the raw on-disk `(days, mins, ticks)` triple and are **not**
  part of the default comparison key. Both follow from AmigaDOS datestamps carrying no
  timezone -- see `amibuilder.timestamps` for the measurement behind that.
* Manifests are JSON Lines, sorted by path, so ordinary `diff` reviews a layer.
"""

from __future__ import annotations
