"""zerofree -- overwrite an image's free blocks with zeros so it compresses and sparsifies.

This is the answer to the finding that shaped the project (notes S1): deleting a file in FFS
clears its bitmap bits and unlinks its header but never touches the data blocks, so a 4 GiB
image with 200 MiB live drags every byte ever written along in every backup. `zerofree` walks
the FFS allocation bitmap and writes zeros over every free block, leaving the volume
byte-for-byte valid and directly usable but no longer carrying the ghosts of deleted files.
Afterwards it compresses to roughly the size of the live data, dedups, and -- once `compact`
punches the zero runs into holes -- costs only the live data on disk.

Safety, because a misread bitmap here would zero a *live* block and destroy data:

* **`--verify` is on by default.** Every file's content and metadata is hashed before and
  after; if a single byte or timestamp differs, or the free-block count moved, the run is
  declared a bitmap misparse and thrown away. It turns "I hope the bitmap parser is right"
  into "the tool demonstrated it did no harm."
* **A verified temp copy is renamed over the original by default.** The original is untouched
  until an atomic rename swaps in a copy that passed verify, so a crash or a misparse cannot
  corrupt it. `--in-place` opts out for speed, and then verify can only *detect* damage after
  the fact, not prevent it -- the message says so.
* **A `check` gate** runs first: a volume that is not already structurally sound is refused,
  because zeroing free space is not a repair and must not run on a broken tree.

Scope: all of an RDB's FFS/OFS partitions by default, or one via a `:selector`. Unsupported
filesystems (PFS3/SFS) are skipped with a note -- their free space needs their own bitmap.
v1 operates on image files only; a raw device cannot be replaced by an atomic rename, and its
write path cannot be exercised without risking a real card, so it is refused.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from ..addressing import parse
from ..errors import ImageError, UnsupportedError, UsageError
from ..image import Container, ImageKind, open_container
from ..render import Output, human_bytes
from . import opened_container
from .inspect import _check_one


def cmd_zerofree(args: Any, out: Output) -> int:
    verify = not args.no_verify
    in_place = bool(args.in_place)
    do_compact = bool(getattr(args, "compact", False))

    with opened_container(args, writable=False, attr="source") as c:
        _guard(c)
        targets, skipped = _resolve_targets(c)
        if not targets:
            _refuse_no_targets(c, skipped)

        # Check gate: refuse a volume that is not already structurally sound.
        for selector, label in targets:
            res = _check_one(c, selector, args)
            if res.get("exit", 0) != 0:
                raise ImageError(
                    f"{label}: fails 'check', so it is not structurally sound. zerofree only "
                    f"zeros free space -- it is not a repair -- so it refuses to run here. Fix "
                    f"or re-create the volume first."
                )

        free_before = {label: _free_blocks(c, selector) for selector, label in targets}
        block_size = c.geometry.block_size
        before = _capture(c) if verify else None
        path = c.address.path

    total_free = sum(free_before.values())

    if args.dry_run:
        out.line(f"would zero {total_free} free block(s) "
                 f"({human_bytes(total_free * block_size)}) across {len(targets)} volume(s)"
                 " -- nothing was changed")
        for _selector, label in targets:
            out.line(f"  {label}: {free_before[label]} free block(s)")
        if do_compact:
            out.line("  then compact: punch the freed zeros into holes to reclaim disk")
        _report_skipped(skipped, out)
        out.data(_payload(path, targets, skipped, free_before, total_free, block_size,
                          in_place=in_place, verified=False, dry_run=True,
                          compacted=do_compact))
        return 0

    work = path if in_place else _temp_path(path)
    renamed = False
    try:
        if not in_place:
            _clone_or_copy(path, work)

        zeroed = 0
        wc = open_container(parse(work), writable=True)
        try:
            for selector, _label in targets:
                with wc.open_volume(selector) as vol:
                    zeroed += vol.zero_free_blocks()
        finally:
            wc.close()

        if verify:
            _verify(path, work, before, targets, free_before, in_place=in_place)

        if not in_place:
            os.replace(work, path)  # atomic on the same filesystem
            renamed = True
    finally:
        if not in_place and not renamed and os.path.exists(work):
            os.remove(work)

    # With --compact, punch the zeros just written into holes, in the same pass. The image
    # now at `path` is the final, zeroed one, so this reclaims exactly what was freed.
    reclaimed: int | None = None
    if do_compact:
        from .compact import _du_bytes, _scan_zero_runs

        du_before = _du_bytes(path)
        _scan_zero_runs(path, punch=True)
        reclaimed = max(0, du_before - _du_bytes(path))

    out.line(f"zeroed {zeroed} free block(s) ({human_bytes(zeroed * block_size)}) "
             f"across {len(targets)} volume(s)")
    _report_skipped(skipped, out)
    if verify:
        out.line("verified: every file is byte-identical and the allocation is unchanged; "
                 "only free space was zeroed")
    out.line(f"written to {path}"
             + (" in place" if in_place else " via a verified temp copy"))
    if reclaimed is not None:
        out.line(f"compacted: reclaimed {human_bytes(reclaimed)} on disk")
    out.data(_payload(path, targets, skipped, free_before, zeroed, block_size,
                      in_place=in_place, verified=verify, dry_run=False,
                      compacted=do_compact, reclaimed_bytes=reclaimed))
    return 0


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _guard(c: Container) -> None:
    if c.kind is ImageKind.DIRECTORY:
        raise UsageError(
            f"{c.address.path}: is a host directory; zerofree operates on disk images")
    if c.kind is ImageKind.MBR:
        raise ImageError(
            f"{c.address.path}: MBR container -- select an Amiga slot, e.g. "
            f"'{c.address.path}:0x76:1'")
    if c.address.is_device:
        raise UsageError(
            f"{c.address.spec}: zerofree operates on image files, not raw devices. Pull the "
            f"image off the card, zerofree it, then write it back.")


def _resolve_targets(c: Container) -> tuple[list[tuple[Any, str]], list[tuple[str, str]]]:
    """`(targets, skipped)` where each target is `(selector, label)` for an FFS/OFS volume.

    All of an RDB's supported partitions by default, or the one a `:selector` names.
    Unsupported filesystems (PFS3/SFS) go to `skipped` -- their free space needs their own
    bitmap, which amibuilder does not read.
    """
    if c.kind is ImageKind.RDB:
        only = c.resolve_partition(c.address.partition) if c.address.partition is not None \
            else None
        targets: list[tuple[Any, str]] = []
        skipped: list[tuple[str, str]] = []
        for p in c.partitions(probe_volumes=False):
            if only is not None and p.index != only:
                continue
            label = f"{c.address.path}:{p.index}"
            if p.dos_type.supported:
                targets.append((p.index, label))
            else:
                skipped.append((label, p.dos_type.filesystem))
        return targets, skipped

    # A plain HDF or ADF holds a single volume.
    dt = c.boot_dos_type
    if dt is None or not dt.supported:
        what = dt.describe() if dt is not None else "no boot block"
        raise UnsupportedError(
            f"{c.address.describe()}: not an FFS/OFS volume ({what}); nothing to zerofree")
    return [(None, c.address.describe())], []


def _refuse_no_targets(c: Container, skipped: list[tuple[str, str]]) -> None:
    if skipped:
        names = ", ".join(f"{label} ({fs})" for label, fs in skipped)
        raise UnsupportedError(
            f"{c.address.path}: no FFS/OFS volume to zero. Only unsupported filesystems are "
            f"present: {names}. Their free space needs their own bitmap, which amibuilder "
            f"does not read.")
    raise UnsupportedError(f"{c.address.path}: no volume to zero")


# ---------------------------------------------------------------------------
# Capture / verify
# ---------------------------------------------------------------------------


def _capture(container: Container) -> Any:
    """Hash every file's content and metadata, writing to no store.

    Exclusions are disabled so `T/`, `Trashcan` and everything else is compared too: zeroing
    free space must not disturb a single live block, wherever it lives.
    """
    from ..layers import blobs as B
    from ..layers import capture as C

    excl = C.Exclusions.build(extra=(), defaults=False)
    return C.capture_container(container, B.HashOnlyBlobStore(), exclusions=excl)


def _free_blocks(container: Container, selector: Any) -> int:
    with container.open_volume(selector) as vol:
        return vol.count_free_blocks()


def _verify(orig_path: str, work_path: str, before: Any, targets: list[tuple[Any, str]],
            free_before: dict[str, int], *, in_place: bool) -> None:
    """Prove zeroing changed no file content, metadata or allocation, or raise.

    The strong check is a full before/after content-and-metadata diff, with timestamps
    significant, so even a touched datestamp fails. The allocation is checked separately: a
    changed free-block count means a bit moved, which zeroing must never do.
    """
    from ..layers import capture as C

    # A misparse severe enough to hit the root or bitmap blocks leaves the volume
    # unmountable, so re-reading raises rather than returning a clean diff. Either way it
    # means "I cannot prove this is safe" -- which is a verify failure, not a crash.
    try:
        wc = open_container(parse(work_path), writable=False)
        try:
            after = _capture(wc)
            free_after = {label: _free_blocks(wc, sel) for sel, label in targets}
        finally:
            wc.close()
    except (ImageError, UnsupportedError) as e:
        _fail(orig_path, in_place, f"the image could not be re-read after zeroing ({e})")

    for _selector, label in targets:
        if free_after[label] != free_before[label]:
            _fail(orig_path, in_place,
                  f"the free-block count for {label} changed "
                  f"({free_before[label]} -> {free_after[label]})")

    comparison = C.diff(before.entries, after.entries,
                        timestamps_significant=True, deletions=True)
    if not comparison.is_empty:
        n = len(comparison.changes)
        _fail(orig_path, in_place,
              f"{n} file(s) or their metadata differ after zeroing -- the bitmap was misread")


def _fail(orig_path: str, in_place: bool, why: str) -> None:
    if in_place:
        tail = (f" The image at {orig_path} was written in place and may now be corrupt -- "
                f"restore it from a backup.")
    else:
        tail = f" Nothing was written to {orig_path}; the temp copy was discarded."
    raise ImageError(f"verify failed: {why}.{tail}")


# ---------------------------------------------------------------------------
# Temp copy
# ---------------------------------------------------------------------------


def _temp_path(path: str) -> str:
    """A sibling temp file, so the atomic rename and any COW clone stay on one filesystem.

    The original extension is preserved: amitools' `BlkDevFactory` detects a plain HDF's
    block-device type from the file extension, so a `.tmp` copy would open as "unknown".
    """
    ap = os.path.abspath(path)
    root, ext = os.path.splitext(os.path.basename(ap))
    return os.path.join(os.path.dirname(ap),
                        f".{root}.zerofree-{os.getpid()}{ext or '.hdf'}")


def _clone_or_copy(src: str, dst: str) -> None:
    """Copy `src` to `dst`, preferring an APFS copy-on-write clone (instant, no extra space).

    A clone means the multi-gigabyte copy the safe default implies costs nothing until the
    zeroing actually dirties blocks. `cp -c` falls back to a full copy on a filesystem that
    cannot clone.
    """
    try:
        subprocess.run(["cp", "-c", src, dst], check=True, capture_output=True)
        return
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        if os.path.exists(dst):
            os.remove(dst)
    shutil.copyfile(src, dst)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _report_skipped(skipped: list[tuple[str, str]], out: Output) -> None:
    for label, fs in skipped:
        out.line(f"  skipped {label} ({fs}: not an FFS/OFS volume)")


def _payload(path: str, targets: list[tuple[Any, str]], skipped: list[tuple[str, str]],
             free_before: dict[str, int], blocks: int, block_size: int, *,
             in_place: bool, verified: bool, dry_run: bool,
             compacted: bool = False, reclaimed_bytes: int | None = None) -> dict[str, Any]:
    payload = {
        "path": path,
        "volumes": [label for _, label in targets],
        "skipped": [{"volume": label, "filesystem": fs} for label, fs in skipped],
        "free_blocks": {label: free_before[label] for _, label in targets},
        "zeroed_blocks": blocks,
        "zeroed_bytes": blocks * block_size,
        "in_place": in_place,
        "verified": verified,
        "compacted": compacted,
        "dry_run": dry_run,
    }
    if reclaimed_bytes is not None:
        payload["reclaimed_bytes"] = reclaimed_bytes
    return payload


__all__ = ["cmd_zerofree"]
