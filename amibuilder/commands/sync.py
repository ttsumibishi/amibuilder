"""sync -- make two trees match, one direction, content-driven.

This is the original motivation the whole tool grew around: keep a folder on the Mac and a
drive image (or one partition of it) in step without copying the whole multi-gigabyte image
each time. `sync SOURCE DEST` always copies SOURCE -> DEST. Three of the four combinations
are supported -- a host directory and an image, either way round, and two images:

    amibuilder sync card.hdf:Work ./backup      # image  -> folder   (back up the card)
    amibuilder sync ./backup card.hdf:Work      # folder -> image    (restore onto the card)
    amibuilder sync old.hdf:Work new.hdf:Work   # image  -> image    (clone a partition)

Direction is read straight off the argument order, the way `cp`, `inject` and rsync do -- not
inferred from which side is the HDF -- so it is never ambiguous which way the bytes flow.

What actually moves is decided by **content**, not timestamps: a file is copied only when it is
missing on the destination or its content differs (the capture/diff engine hashes both sides).
Unchanged files are skipped, which is the point -- only the dirty blocks are ever written, so a
restore onto an SD card touches a few hundred KiB instead of rewriting gigabytes. A
metadata-only difference (protection bits, comment) never triggers a *copy*: between two images
it is reconciled in place, and in the host directions it is left alone (see below).

By default nothing is deleted: a file present on the destination but not the source is left
alone. `--delete` turns on removal, and even then copies run **first** and deletes **last**, so
an interrupted run can never have deleted something it had not already re-created elsewhere.

Safety and scope, deliberately narrow for a first cut:

* **No raw devices, and never two host directories.** Two directories are refused (use
  `cp`/rsync), and a raw `--device` is refused outright -- the same file-only line
  `zerofree`/`compact`/`inject` hold, because a card write is the one mistake that is
  unrecoverable. Two images must also be two *different* files: one file with two open
  handles, one of them writing, is `inject`'s same-file hazard exactly.
* **AmigaDOS metadata travels in every direction, via `.uaem` sidecars on the host side.**
  Between two images, protection bits, the file comment and the modification time all cross
  directly -- the same promise `inject` makes, because both sides are real Amiga volumes and a
  faithful copy is the only sensible one. A host file has no native place for protection bits
  or a comment, so to or from a **host directory** they travel in the `.uaem` sidecars
  `layers.uaem` defines: image->folder writes them, folder->image reads them. This is **on by
  default**, and `--no-metadata` turns it off. It defaults on because the alternative is a
  backup that silently loses information -- on a real Workbench 3.2 install 84% of entries
  carry non-default protection, including the `p` (pure) bit on 83 commands in `C/` that
  `Resident` needs -- and a restore from such a backup produces a subtly broken system with
  nothing to point at. It is also the same choice `compose --format dir` already makes, with
  the same flag spelling, and a folder synced out of an image is deliberately the same shape
  as one `compose` writes, so the two artifacts are interchangeable.
* **On the way back, a missing sidecar means "no opinion", not "default".** This asymmetry is
  load-bearing. A folder with no sidecars -- assembled by hand, or produced by a version of
  this tool that did not write them -- reports default `----rwed` protection for everything,
  because that is all `DirectoryVolume` can infer. Treating that as a statement would make a
  restore *strip* the protection bits it found on the card, so a folder->image sync applies
  metadata only where the sidecar actually exists and leaves the rest of the image's entries
  alone. Such entries are reported as left-as-is rather than silently ignored.
* **Every write is pre-flighted.** For a folder->image sync the whole plan -- names, and whether
  it fits -- is checked before a single block is written, so a refusal leaves the image
  untouched rather than half-updated. A file/directory kind clash at one path (a name that is a
  file on one side and a directory on the other) refuses the whole sync rather than guessing.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from ..addressing import Address, parse
from ..errors import AddressError, ImageError, UsageError
from ..layers import blobs as B
from ..layers import capture as C
from ..layers import manifest as M
from ..layers import uaem
from ..layers.hostdir import DirectoryVolume
from ..render import Output, human_bytes
from ..volume import Volume
from . import opened_volume

#: The synthetic volume both sides are relabelled to, so a differently named image volume and
#: host directory line up on their paths *within* the volume rather than on their names. Not a
#: legal Amiga volume name, so it can never collide with a real one. (Same trick `diff` uses.)
_ALIGN_VOLUME = "*"


class SyncItem(NamedTuple):
    """One planned copy, metadata fix or delete, resolved before anything is written."""

    #: Path relative to the volume / directory root, identical on both sides ('S/Startup').
    rel: str
    is_dir: bool
    size: int
    #: 'new' or 'changed' for a copy; 'removed' for a delete; 'metadata' for a fix-up.
    reason: str
    #: Source modification time, carried across. For folder->image this is the host file's
    #: mtime (DirectoryVolume reports it); for image->folder it is the Amiga entry's.
    secs: int
    ticks: int
    #: Source protection bits and comment, in the `----rwed` spelling.
    #:
    #: **An empty `protect` means the source stated no metadata for this entry** -- either
    #: `--no-metadata` is in force, or the source is a host folder with no `.uaem` sidecar for
    #: this path. That is the marker the execute paths test before touching the destination's
    #: metadata, so "no opinion" can never be mistaken for "set it to the default".
    protect: str = ""
    comment: str = ""


class SyncPlan(NamedTuple):
    copies: list[SyncItem]
    deletes: list[SyncItem]
    #: Paths that are a file on one side and a directory on the other -- refused, not guessed.
    conflicts: list[str]
    unchanged: int
    #: Entries that differ only in something this run will not act on, and were left as-is:
    #: a case-only name difference always, plus metadata differences when `--no-metadata` is in
    #: force or when a folder->image source has no sidecar to state them.
    metadata_only: int
    warnings: list[str]
    #: Entries whose content already matches and whose protection/comment is reconciled in
    #: place -- no data is rewritten, which is the point: identical bytes should not cost
    #: blocks or card wear to re-copy.
    metadata: list[SyncItem]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_sync(args: Any, out: Output) -> int:
    src = parse(args.source)  # must exist; AddressError (exit 2) otherwise
    _refuse_device(src, "SOURCE")

    if src.is_directory:
        return _sync_to_image(args, out, src)

    # An image source goes to one of two places. The destination is only resolved as an
    # address when it already exists as something parseable: an image->folder sync
    # legitimately names a backup folder that is not there yet, and `parse` raises for it.
    dst = _dest_address(args.dest)
    if dst is not None and not dst.is_directory:
        _refuse_device(dst, "DEST")
        return _sync_image_to_image(args, out, src, dst)
    return _sync_to_host(args, out)


def _dest_address(spec: str) -> Address | None:
    """The destination as an address, or None when it is not (yet) something `parse` accepts.

    None covers the folder-to-be-created case, which is why this cannot simply be `parse`.
    """
    try:
        return parse(spec)
    except AddressError:
        return None


def _refuse_device(addr: Address, role: str) -> None:
    if addr.is_device:
        raise UsageError(
            f"{addr.spec}: sync operates on image files and host directories, not raw "
            f"devices ({role}). Pull the image off the card, sync it, then write it back.")


# ---------------------------------------------------------------------------
# Metadata: `.uaem` sidecars on the host side
# ---------------------------------------------------------------------------
# Both host directions use these. The path mapping itself (`_host_path`) lives with the
# image->folder direction, since that is where the folder layout is defined.


def _metadata_enabled(args: Any) -> bool:
    """Whether AmigaDOS metadata crosses to or from the host side on this run.

    On unless `--no-metadata` says so. Read with `getattr` so a caller that assembles an
    argument namespace by hand gets the CLI's default rather than an AttributeError.
    """
    return not getattr(args, "no_metadata", False)


def _sidecar_path(root: Path, rel: str) -> Path:
    """The `.uaem` sidecar beside a volume-relative path's host file.

    Relies on `rel` never being empty, which `capture.capture_volume` guarantees -- it does not
    record the volume root as an entry. An empty `rel` would resolve to the backup directory
    itself and put a sidecar *beside* it, outside the destination.
    """
    return Path(str(_host_path(root, rel)) + uaem.UAEM_SUFFIX)


def _states_metadata(root: str) -> Callable[[str], bool]:
    """Build `_plan`'s per-entry gate for a folder->image sync: is there a sidecar to read?

    **This is the guard that stops a restore from destroying protection bits.** A host folder
    with no sidecars reports default `----rwed` for every entry, because that is the only thing
    `DirectoryVolume` can infer from a plain file. Without this gate every entry on a card whose
    protection was anything else -- 84% of a real Workbench install -- would read as a metadata
    difference and get "fixed" to the default, quietly breaking `Resident` on the 83 pure
    commands in `C/`. Testing for the sidecar file itself is what separates "the source says
    default" from "the source says nothing", a distinction the captured entry cannot carry.
    """
    base = Path(root)

    def stated(rel: str) -> bool:
        return _sidecar_path(base, rel).exists()

    return stated


def _write_host_sidecar(target: Path, item: SyncItem) -> None:
    """Write the `.uaem` sidecar for one entry copied or fixed on the host side.

    Written for every entry this run touched, including those whose protection is the default
    and whose comment is empty. Not skipping the uninformative ones is deliberate: it means a
    folder sync produced always states its metadata in full, so the absence of a sidecar keeps
    its one unambiguous meaning for `_states_metadata` on the way back -- "this folder was not
    written by us, so it has no opinion". It also matches `compose --format dir`, which writes
    one per entry, so both produce the same shape of folder.
    """
    text = uaem.uaem_text(item.protect, item.secs, item.ticks, item.comment)
    Path(str(target) + uaem.UAEM_SUFFIX).write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Direction: host directory -> image
# ---------------------------------------------------------------------------


def _sync_to_image(args: Any, out: Output, src: Address) -> int:
    dst = parse(args.dest)  # the image must already exist -- sync writes into it, `init` makes it
    _refuse_device(dst, "DEST")
    if dst.is_directory:
        raise UsageError(
            f"both {args.source} and {args.dest} are host directories; sync copies between a "
            f"host directory and an image. Use cp or rsync for two host directories.")

    excl = _exclusions(args)
    src_dir = DirectoryVolume(src.path)

    with opened_volume(args, writable=not args.dry_run, attr="dest") as (container, vol):
        dst_entries, dst_warn = _capture(vol, excl)
        src_entries, src_warn = _capture(src_dir, excl)
        # Per entry, not per run: only a path the folder has a `.uaem` sidecar for has stated
        # its metadata, and the rest of the image must be left alone rather than defaulted.
        plan = _plan(dst_entries, src_entries, delete=args.delete,
                     warnings=src_warn + dst_warn,
                     carry_metadata=_states_metadata(src.path) if _metadata_enabled(args)
                     else False)
        _refuse_conflicts(plan)
        _preflight_image(vol, plan)

        desc = (args.source, "host directory", container.address.spec, "image")
        if args.dry_run:
            return _report(out, args, plan, desc, "to-image")

        _execute_to_image(vol, src_dir, plan, args, out)
        vol.flush()
        return _report(out, args, plan, desc, "to-image")


def _execute_to_image(vol: Volume, src_dir: DirectoryVolume, plan: SyncPlan, args: Any,
                      out: Output) -> None:
    """Copy, delete, then reconcile metadata -- the same order `_execute_image_to_image` uses.

    Metadata last for its two reasons: writing into a directory re-stamps it, so a copied
    directory's own timestamp has to be re-applied once its contents exist; and applying
    protection early could make an entry read-only before the copy or delete that still needs
    it. The pass writes no data, so running it last risks nothing.

    Every metadata call here is gated on `item.protect` being set, which is the plan's marker
    for "the source stated this". An unstated entry must be left exactly as the image has it --
    `_apply_metadata` clears the comment unconditionally, which is right for a faithful
    image->image copy but would be destructive here, where an empty comment means "the folder
    did not say" rather than "the source has none".
    """
    for item in plan.copies:
        comment = item.comment or None
        if item.is_dir:
            vol.mkdir(item.rel, parents=True, exist_ok=True,
                      protect=item.protect or None, comment=comment,
                      secs=item.secs or None, ticks=item.ticks)
        else:
            data = src_dir.read_file(item.rel)
            vol.write_file(item.rel, data, replace=True, parents=True,
                           protect=item.protect or None, comment=comment,
                           secs=item.secs or None, ticks=item.ticks)
        if args.verbose:
            out.line(f"  {_mark(item)} {item.rel}")
    # Copies are done; only now remove what the source no longer has.
    for item in _delete_roots(plan.deletes):
        if vol.exists(item.rel):
            vol.remove(item.rel, recursive=item.is_dir)
        if args.verbose:
            out.line(f"  - {item.rel}")

    for item in plan.metadata:
        _apply_metadata(vol, item)
        if args.verbose:
            out.line(f"  m {item.rel} (metadata)")
    # Writing a file into a directory re-stamps that directory, so a copied directory's own
    # stated time has to be applied *after* its contents exist or it ends up dated at the moment
    # of the restore rather than its real date. This is fidelity rather than convergence -- the
    # diff does not treat timestamps as significant, so a wrong date would not be re-reported,
    # just silently wrong. (`mkdir(exist_ok=True)` also returns an existing directory without
    # applying the metadata it was passed, so this is the only place that would set it.)
    for item in plan.copies:
        if item.is_dir and item.protect:
            _apply_metadata(vol, item)


# ---------------------------------------------------------------------------
# Direction: image -> image
# ---------------------------------------------------------------------------


def _sync_image_to_image(args: Any, out: Output, src: Address, dst: Address) -> int:
    """Sync between two Amiga volumes, carrying protection bits and comments across.

    The metadata promise is `inject`'s, for the same reason: both sides are real Amiga volumes,
    so a faithful copy is the default. `--no-metadata` still applies -- it means "do not carry
    AmigaDOS metadata" in every direction, and having it silently do nothing here would be worse
    than honouring it. There is no sidecar to consult either way round, so unlike folder->image
    the answer is the same for every entry.
    """
    _refuse_same_file(src, dst)
    excl = _exclusions(args)

    with opened_volume(args, writable=False, attr="source") as (src_c, src_vol):
        with opened_volume(args, writable=not args.dry_run, attr="dest") as (dst_c, dst_vol):
            dst_entries, dst_warn = _capture(dst_vol, excl)
            src_entries, src_warn = _capture(src_vol, excl)
            plan = _plan(dst_entries, src_entries, delete=args.delete,
                         warnings=src_warn + dst_warn,
                         carry_metadata=_metadata_enabled(args))
            _refuse_conflicts(plan)
            _preflight_image(dst_vol, plan)

            desc = (src_c.address.spec, "image", dst_c.address.spec, "image")
            if args.dry_run:
                return _report(out, args, plan, desc, "image-to-image")

            _execute_image_to_image(src_vol, dst_vol, plan, args, out)
            dst_vol.flush()
            return _report(out, args, plan, desc, "image-to-image")


def _refuse_same_file(src: Address, dst: Address) -> None:
    """Refuse both sides resolving to one file, the way `inject` does and for its reason.

    Two open handles on one image with one of them writing could let a destination write
    clobber a block the source read has not reached. Comparing the *file* path also refuses
    two partitions of the same drive (`card.hdf:Work` -> `card.hdf:Games`), which is the same
    hazard: one file, two handles.
    """
    if os.path.realpath(src.path) == os.path.realpath(dst.path):
        raise UsageError(
            f"{src.spec} and {dst.spec} are the same image file; sync copies between two "
            f"different images. Even two partitions of one drive share the file, so export "
            f"with 'get' and re-import with 'cp' to move files within a single image.")


def _execute_image_to_image(src_vol: Volume, dst_vol: Volume, plan: SyncPlan, args: Any,
                            out: Output) -> None:
    """Copy, delete, then reconcile metadata -- in that order, deliberately.

    Data first and deletes after it, as every direction does, so an interrupted run cannot
    have removed something it had not already re-created. The metadata pass comes **last**
    for two reasons: writing into a directory re-stamps it, so a directory's own timestamp has
    to be re-applied once its contents exist (`inject._restamp_dirs` exists for the same
    reason); and applying protection bits early could make an entry read-only before the copy
    or delete that still needs it. The pass writes no data, so running it last risks nothing.

    The explicit `set_*` calls are not belt-and-braces. `Volume.mkdir(exist_ok=True)` returns
    an existing directory **without** applying the metadata it was passed, so a directory
    whose protection differs would otherwise be re-reported as a difference on every single
    run -- the convergence failure this whole design is arranged to avoid.
    """
    for item in plan.copies:
        comment = item.comment or None
        if item.is_dir:
            dst_vol.mkdir(item.rel, parents=True, exist_ok=True,
                          protect=item.protect or None, comment=comment,
                          secs=item.secs or None, ticks=item.ticks)
        else:
            data = src_vol.read_file(item.rel)
            dst_vol.write_file(item.rel, data, replace=True, parents=True,
                               protect=item.protect or None, comment=comment,
                               secs=item.secs or None, ticks=item.ticks)
        if args.verbose:
            out.line(f"  {_mark(item)} {item.rel}")

    for item in _delete_roots(plan.deletes):
        if dst_vol.exists(item.rel):
            dst_vol.remove(item.rel, recursive=item.is_dir)
        if args.verbose:
            out.line(f"  - {item.rel}")

    # Metadata fix-ups, plus every copied directory re-stamped now its contents exist.
    for item in plan.metadata:
        _apply_metadata(dst_vol, item)
        if args.verbose:
            out.line(f"  {_mark(item)} {item.rel} (metadata)")
    for item in plan.copies:
        if item.is_dir:
            _apply_metadata(dst_vol, item)


def _apply_metadata(vol: Volume, item: SyncItem) -> None:
    """Set one entry's protection, comment and timestamp to the source's, in place."""
    if not vol.exists(item.rel):
        return
    if item.protect:
        vol.set_protect(item.rel, vol.parse_protect(item.protect))
    vol.set_comment(item.rel, item.comment)
    if item.secs:
        vol.set_times(item.rel, item.secs, item.ticks)


# ---------------------------------------------------------------------------
# Direction: image -> host directory
# ---------------------------------------------------------------------------


def _sync_to_host(args: Any, out: Output) -> int:
    dst_path = Path(args.dest)
    _validate_host_dest(args.dest, dst_path)

    excl = _exclusions(args)
    with opened_volume(args, writable=False, attr="source") as (container, vol):
        src_entries, src_warn = _capture(vol, excl)
        if dst_path.is_dir():
            dst_entries, dst_warn = _capture(DirectoryVolume(str(dst_path)), excl)
        else:
            dst_entries, dst_warn = [], []
        # A whole-run bool this way round: every entry's metadata can be carried, because the
        # sidecar to hold it is ours to write.
        plan = _plan(dst_entries, src_entries, delete=args.delete,
                     warnings=src_warn + dst_warn,
                     carry_metadata=_metadata_enabled(args))
        _refuse_conflicts(plan)

        desc = (container.address.spec, "image", args.dest, "host directory")
        if args.dry_run:
            return _report(out, args, plan, desc, "to-host")

        dst_path.mkdir(parents=True, exist_ok=True)
        _execute_to_host(vol, dst_path, plan, args, out)
        return _report(out, args, plan, desc, "to-host")


def _validate_host_dest(spec: str, dst_path: Path) -> None:
    """The destination of an image->folder sync must be a host directory, creating nothing yet.

    An existing directory is fine; a missing one is fine (it is created just before the copy,
    like a backup target). An existing *file* is refused. A file that parses as an image never
    reaches here -- `cmd_sync` routes that pair to the image->image path -- so what is left is
    a file that is not a readable image, and the message says so.
    """
    if dst_path.is_dir():
        return
    # parse() recognises a device by its /dev shape even when the node is absent, so this
    # refuses `sync image /dev/rdiskN` before it could try to create a "folder" there.
    try:
        addr = parse(spec)
    except AddressError:
        addr = None
    if addr is not None:
        _refuse_device(addr, "DEST")
    if dst_path.exists():
        raise UsageError(
            f"{spec}: DEST exists but is neither a host directory nor an image amibuilder can "
            f"read. sync copies between a host directory and an image, or between two images.")
    if not dst_path.parent.exists():
        raise UsageError(
            f"{spec}: parent directory does not exist, so the backup folder cannot be created "
            f"there")


def _execute_to_host(vol: Volume, dst_path: Path, plan: SyncPlan, args: Any,
                     out: Output) -> None:
    """Copy, delete, then write the metadata sidecars for entries that needed only those.

    The host file's own mtime is still set alongside the sidecar. It is redundant for reading
    back -- `DirectoryVolume` prefers the sidecar -- but it keeps the folder meaningful to
    Finder, `ls` and rsync, which know nothing about `.uaem`.
    """
    metadata = _metadata_enabled(args)
    for item in plan.copies:
        target = _host_path(dst_path, item.rel)
        if item.is_dir:
            target.mkdir(parents=True, exist_ok=True)
        else:
            data = vol.read_file(item.rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            _apply_host_mtime(target, item.secs)
        if metadata:
            _write_host_sidecar(target, item)
        if args.verbose:
            out.line(f"  {_mark(item)} {item.rel}")
    for item in _delete_roots(plan.deletes):
        target = _host_path(dst_path, item.rel)
        if item.is_dir:
            if target.is_dir():
                shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        # Remove the entry's sidecar too, so a synced backup does not accumulate orphaned
        # metadata files describing entries that are gone. Unconditional of --no-metadata: the
        # sidecar may have been written by an earlier run or by `compose --format dir`, and
        # leaving it behind would let a later sync read metadata for a file that no longer
        # exists. A directory's own sidecar sits outside the tree rmtree just removed.
        sidecar = Path(str(target) + uaem.UAEM_SUFFIX)
        if sidecar.exists():
            sidecar.unlink()
        if args.verbose:
            out.line(f"  - {item.rel}")

    if not metadata:
        return
    # Content already matches for these, so only the sidecar is rewritten -- no file is touched.
    for item in plan.metadata:
        target = _host_path(dst_path, item.rel)
        if target.exists():
            _write_host_sidecar(target, item)
        if args.verbose:
            out.line(f"  m {item.rel} (metadata)")


def _host_path(root: Path, rel: str) -> Path:
    """Map a volume-relative Amiga path to its host path, `%XX`-escaping each component.

    The exact inverse of `DirectoryVolume`'s mapping, so a folder written here reads back with
    its original Amiga names -- and so a second sync in the other direction lines the files up
    rather than seeing every one as new.
    """
    parts = [uaem.escape_name(p) for p in rel.split("/") if p]
    return root.joinpath(*parts) if parts else root


def _apply_host_mtime(target: Path, secs: int) -> None:
    """Set a host file's mtime from an Amiga-epoch second count, with no timezone step.

    Mirrors `commands.transfer`: the Amiga value is naive wall clock, so interpreting it as
    local time is the only reading that keeps the displayed time the same on both sides.
    """
    if secs <= 0:
        return
    import time

    from .. import timestamps

    try:
        stamp = time.mktime(timestamps.to_datetime(secs).timetuple())
    except (OverflowError, ValueError):
        return
    try:
        os.utime(target, (stamp, stamp))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Capture and planning
# ---------------------------------------------------------------------------


def _exclusions(args: Any) -> C.Exclusions:
    return C.Exclusions.build(
        extra=tuple(getattr(args, "exclude", None) or ()),
        defaults=not getattr(args, "no_default_excludes", False),
    )


def _capture(volish: Any, excl: C.Exclusions) -> tuple[list[M.ManifestEntry], list[str]]:
    """Hash one side into relabelled manifest entries, writing to no store.

    `volish` is either a mounted `Volume` or a `DirectoryVolume`; `capture_volume` touches only
    `name`/`walk`/`read_file`, which both provide. Entries are relabelled to the common
    alignment volume so the two sides match on their in-volume paths.
    """
    result = C.capture_volume(volish, B.HashOnlyBlobStore(), exclusions=excl)
    warnings = list(result.warnings) + list(getattr(volish, "warnings", []))
    aligned = [M.replace(e, path=M.join_path(_ALIGN_VOLUME, e.relative)) for e in result.entries]
    return aligned, warnings


def _plan(dst_entries: list[M.ManifestEntry], src_entries: list[M.ManifestEntry], *,
          delete: bool, warnings: list[str],
          carry_metadata: bool | Callable[[str], bool] = False) -> SyncPlan:
    """Turn a diff of (destination, source) into copies, metadata fixes, deletes and conflicts.

    `diff(parent=dst, current=src)` reads naturally: a path only in the source is *new* (copy
    it), one only in the destination is *deleted* (remove it, if `--delete`), and one in both
    that differs is a *change*.

    Content and newness always drive a copy. What happens to a **metadata-only** difference
    (protection bits or comment, same content) depends on whether the direction can carry it:

    * carried -- it becomes a **metadata fix**, reconciled in place, deliberately *not* a copy:
      the content already matches, so rewriting the file would burn blocks (and card wear) to
      produce identical bytes.
    * not carried -- the entry is counted in `metadata_only` and left alone, because acting on
      it would either change nothing on the far side (and so never converge) or, worse, write a
      value the source never actually stated.

    `carry_metadata` is a bool for the whole run, or a **predicate on the entry's path** for the
    folder->image direction, where the answer is per entry: only a path with a `.uaem` sidecar
    has stated metadata (see `_states_metadata`). A bool is widened to a constant predicate so
    the two cases share this one code path, and so `metadata_only` -- computed as the residual
    below -- accounts for a skipped entry automatically rather than in a second place.

    A case-only difference stays out of both lists in either mode. The content is identical
    and FFS considers the names equal, so "copying" it would write the new spelling and leave
    the old entry behind -- a rename, which is `mv`'s job and needs `--delete` to be safe.
    """
    carries: Callable[[str], bool] = (
        carry_metadata if callable(carry_metadata) else (lambda _rel: bool(carry_metadata)))
    comparison = C.diff(dst_entries, src_entries,
                        timestamps_significant=False, deletions=delete)
    copies: list[SyncItem] = []
    metadata: list[SyncItem] = []
    deletes: list[SyncItem] = []
    conflicts: list[str] = []

    for change in comparison.changes:
        if change.reason == C.REASON_DELETED:
            prev = change.previous
            assert prev is not None  # a deletion always carries the entry it removes
            deletes.append(SyncItem(prev.relative, prev.kind == M.DIR, prev.size,
                                    "removed", 0, 0))
            continue
        if C.REASON_KIND in change.reasons:
            # A file on one side, a directory on the other. Replacing one kind with the other
            # is exactly the surprising, data-losing move sync should not make unasked.
            conflicts.append(change.entry.relative)
            continue

        entry = change.entry
        carry = carries(entry.relative)
        item = SyncItem(entry.relative, entry.kind == M.DIR, entry.size, "new",
                        entry.mod_secs, entry.mod_ticks,
                        protect=entry.protect if carry else "",
                        comment=entry.comment if carry else "")

        if C.REASON_NEW in change.reasons or C.REASON_CONTENT in change.reasons:
            reason = "new" if change.reason == C.REASON_NEW else "changed"
            copies.append(item._replace(reason=reason))
        elif carry and (C.REASON_PROTECTION in change.reasons
                        or C.REASON_COMMENT in change.reasons):
            metadata.append(item._replace(reason="metadata"))
        # else: metadata the source did not state, or a case-only change -- left as-is.

    metadata_only = (len(comparison.changes) - len(copies) - len(deletes)
                     - len(conflicts) - len(metadata))
    return SyncPlan(copies=copies, deletes=deletes, conflicts=conflicts,
                    unchanged=comparison.unchanged, metadata_only=metadata_only,
                    warnings=warnings, metadata=metadata)


def _refuse_conflicts(plan: SyncPlan) -> None:
    if not plan.conflicts:
        return
    shown = ", ".join(sorted(plan.conflicts)[:5])
    more = "" if len(plan.conflicts) <= 5 else f" (and {len(plan.conflicts) - 5} more)"
    raise ImageError(
        f"{len(plan.conflicts)} path(s) are a file on one side and a directory on the other, "
        f"so sync will not replace one with the other: {shown}{more}. Remove the clashing "
        f"entry by hand and re-run.")


def _delete_roots(deletes: list[SyncItem]) -> list[SyncItem]:
    """The top of each deleted subtree, so a recursive remove of a directory is not followed by
    a doomed attempt to remove entries it already took with it."""
    all_rels = {d.rel for d in deletes}
    roots: list[SyncItem] = []
    for d in deletes:
        parent = d.rel.rsplit("/", 1)[0] if "/" in d.rel else ""
        if parent not in all_rels:
            roots.append(d)
    return roots


def _preflight_image(vol: Volume, plan: SyncPlan) -> None:
    """Refuse a folder->image sync that would fail partway: a bad name, or one that will not fit.

    Deletes free blocks too, but they run *after* the copies, so their space is deliberately not
    counted here -- the image must hold the copies with the space free before any deletion.
    """
    for item in [*plan.copies, *plan.metadata]:
        for component in item.rel.split("/"):
            vol.check_name(component)
        # Populated image->image, and folder->image for the entries a `.uaem` sidecar states.
        # A bad comment or protection spec -- from a hand-edited sidecar, say -- should refuse
        # the run rather than surface partway through it.
        if item.comment:
            vol.check_comment(item.comment)
        if item.protect:
            vol.parse_protect(item.protect)

    info = vol.info()
    needed = 0
    for item in plan.copies:
        if item.is_dir:
            needed += 0 if vol.exists(item.rel) else 1
            continue
        if vol.exists(item.rel) and not vol.is_dir(item.rel):
            needed -= vol.blocks_for(vol.stat(item.rel).size)  # the old copy's blocks are reused
        needed += vol.blocks_for(item.size)

    if needed > info.free_blocks:
        short = (needed - info.free_blocks) * info.block_size
        raise ImageError(
            f"{info.name}: this sync needs {needed} block(s) but {info.free_blocks} are free "
            f"-- {human_bytes(short)} short. Free space on the image, or sync fewer files.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _mark(item: SyncItem) -> str:
    """The one-character change marker used in the listing: + new, ~ changed."""
    return "+" if item.reason == "new" else "~"


def _report(out: Output, args: Any, plan: SyncPlan, desc: tuple[str, str, str, str],
            direction: str) -> int:
    src_spec, src_kind, dst_spec, dst_kind = desc
    dry = args.dry_run
    new = sum(1 for c in plan.copies if c.reason == "new")
    changed = len(plan.copies) - new
    copied_bytes = sum(c.size for c in plan.copies if not c.is_dir)

    if out.as_json:
        out.data({
            "source": {"spec": src_spec, "kind": src_kind},
            "dest": {"spec": dst_spec, "kind": dst_kind},
            "direction": direction,
            "delete": bool(args.delete),
            # Whether AmigaDOS protection bits and comments were carried at all. Distinct from
            # the `metadata` list below, which is the entries that needed only a metadata fix:
            # that list is legitimately empty on a run where everything already agreed.
            "metadata_enabled": _metadata_enabled(args),
            "dry_run": dry,
            "copied": [{"path": c.rel, "kind": M.DIR if c.is_dir else M.FILE,
                        "bytes": c.size, "reason": c.reason} for c in plan.copies],
            "deleted": [{"path": d.rel, "kind": M.DIR if d.is_dir else M.FILE}
                        for d in plan.deletes],
            "metadata": [{"path": m.rel, "kind": M.DIR if m.is_dir else M.FILE}
                         for m in plan.metadata],
            "counts": {"copied": len(plan.copies), "new": new, "changed": changed,
                       "deleted": len(plan.deletes), "unchanged": plan.unchanged,
                       "metadata_only": plan.metadata_only,
                       "metadata": len(plan.metadata)},
            "copied_bytes": copied_bytes,
            "warnings": plan.warnings,
        })
        return 0

    verb = "would sync" if dry else "synced"
    out.line(f"{verb} {src_spec} -> {dst_spec}   ({src_kind} -> {dst_kind})")

    if not plan.copies and not plan.deletes and not plan.metadata:
        tail = " (and nothing to delete)" if args.delete else ""
        out.line(f"already in sync -- nothing to copy{tail}")
        _report_extras(out, args, plan, direction)
        return 0

    would = "would copy" if dry else "copied"
    out.field(would, f"{len(plan.copies)}   ({new} new, {changed} changed, "
                     f"{human_bytes(copied_bytes)})")
    if args.delete or plan.deletes:
        out.field("would delete" if dry else "deleted", str(len(plan.deletes)))
    if plan.metadata:
        out.field("would fix" if dry else "metadata fixed", str(len(plan.metadata)))
    out.field("unchanged", str(plan.unchanged))
    out.line()

    for item in plan.copies:
        out.line(f"  {_mark(item)} {item.rel}"
                 + ("" if item.is_dir else f" ({human_bytes(item.size)})"))
    for item in plan.deletes:
        out.line(f"  - {item.rel}")
    for item in plan.metadata:
        out.line(f"  m {item.rel}")

    _report_extras(out, args, plan, direction)
    return 0


def _metadata_only_note(args: Any, direction: str, n: int) -> str:
    """Explain what the left-as-is entries actually differed in, which depends on the run.

    Three genuinely different situations, and saying the wrong one is worse than saying nothing:
    with `--no-metadata` nothing is carried at all; on the way *to* an image the cause is a
    source folder that does not state its metadata; otherwise all metadata was carried and the
    only thing left is a case-only name difference, which is `mv`'s job rather than sync's.
    """
    subject = "1 entry differs" if n == 1 else f"{n} entries differ"
    verb = "was" if n == 1 else "were"
    if not _metadata_enabled(args):
        return (f"{subject} only in protection bits, a comment or the case of the name -- "
                f"--no-metadata means none of those are carried, so {verb} left as-is")
    if direction == "to-image":
        return (f"{subject} only in the case of the name, or in metadata this folder does not "
                f"state (no {uaem.UAEM_SUFFIX} sidecar), so {verb} left as-is -- the image's "
                f"own bits were not touched")
    return f"{subject} only in the case of the name, so {verb} left as-is"


def _report_extras(out: Output, args: Any, plan: SyncPlan, direction: str) -> None:
    if plan.metadata_only:
        out.line(_metadata_only_note(args, direction, plan.metadata_only))
    if plan.warnings:
        out.heading(f"warnings ({len(plan.warnings)})")
        for text in plan.warnings:
            out.line(f"  {text}")


__all__ = ["cmd_sync"]
