"""compact -- punch holes over an image's zero runs to reclaim host disk space.

`zerofree` makes an image's free space compress and dedup well by filling it with zeros;
`compact` is the second, purely host-side half: it scans the image file for runs of zero
bytes and punches holes over them with `F_PUNCHHOLE`, so those pages stop occupying disk. The
image's contents do not change at all -- a punched region reads back as the zeros it already
held -- so `ls -l` still shows the full size while `du` drops to the live data.

It is safe by construction: a hole is only ever punched over a range already confirmed to be
all zeros, so reading it back yields exactly the same bytes. There is nothing to verify and no
temp copy -- unlike `zerofree`, which changes the image and therefore proves it did no harm.

Two honest limitations:

* **APFS only.** Hole punching reclaims space on a filesystem that supports sparse files.
  exFAT and FAT32 -- what an SD card is formatted as -- do not, so a compacted image expands
  back to full size the moment it is copied to the card. compact helps the Mac-side backup
  store, not the card. On a filesystem that cannot punch holes, it says so and changes nothing.
* **Image files only.** A raw device has no host-side allocation to reclaim, so a device is
  refused.
"""

from __future__ import annotations

import fcntl
import os
import struct
import subprocess
from typing import Any

from ..addressing import parse
from ..errors import ImageError, UsageError
from ..render import Output, human_bytes

#: macOS <sys/fcntl.h>. F_PUNCHHOLE deallocates a byte range, which then reads back as zeros.
_F_PUNCHHOLE = 99
#: struct fpunchhole_t { uint32 fp_flags; uint32 reserved; off_t fp_offset; off_t fp_length; }
_PUNCHHOLE_STRUCT = "=IIqq"
#: Scan and punch at this granularity. APFS reclaims whole allocation blocks (4 KiB), so a
#: finer unit would not free more, and page-aligned runs need no separate alignment step.
_PAGE = 4096


def cmd_compact(args: Any, out: Output) -> int:
    addr = parse(args.source)
    if addr.is_device:
        raise UsageError(
            f"{addr.spec}: compact reclaims host-side disk space and cannot punch holes in a "
            f"raw device. It operates on image files.")
    if addr.is_directory:
        raise UsageError(f"{addr.path}: is a host directory, not an image")
    path = addr.path
    if not os.path.isfile(path):
        raise ImageError(f"{path}: not a regular file")

    if args.dry_run:
        punchable = _scan_zero_runs(path, punch=False)
        out.line(f"would punch {human_bytes(punchable)} of zeros "
                 f"(up to that much reclaimable on a sparse-capable filesystem) "
                 f"-- nothing was changed")
        out.data({"path": path, "punchable_bytes": punchable, "dry_run": True})
        return 0

    before = _du_bytes(path)
    punched = _scan_zero_runs(path, punch=True)
    after = _du_bytes(path)
    reclaimed = max(0, before - after)

    out.line(f"compacted {path}")
    out.line(f"punched {human_bytes(punched)} of zeros; "
             f"reclaimed {human_bytes(reclaimed)} on disk")
    if punched > 0 and reclaimed == 0:
        out.line("(no space was reclaimed -- compaction needs APFS; on the exFAT/FAT32 an "
                 "SD card uses, an image cannot be stored sparsely)")
    out.data({
        "path": path,
        "punched_bytes": punched,
        "reclaimed_bytes": reclaimed,
        "du_before_bytes": before,
        "du_after_bytes": after,
        "dry_run": False,
    })
    return 0


def _scan_zero_runs(path: str, *, punch: bool) -> int:
    """Find maximal runs of zero pages and, when `punch`, hole-punch each; return their bytes.

    The file is read in windows for speed but decided a page at a time, so runs are page-aligned
    by construction and need no separate alignment. A trailing partial page at EOF is never
    punched. Only ranges confirmed to be entirely zero are ever punched, which is what makes
    the operation lossless.
    """
    size = os.path.getsize(path)
    zero_page = bytes(_PAGE)
    window = 4 * 1024 * 1024
    total = 0
    flags = os.O_RDWR if punch else os.O_RDONLY
    fd = os.open(path, flags)
    try:
        run_start: int | None = None
        off = 0
        while off < size:
            os.lseek(fd, off, os.SEEK_SET)
            data = os.read(fd, min(window, size - off))
            if not data:
                break
            for i in range(0, len(data), _PAGE):
                page = data[i:i + _PAGE]
                if len(page) == _PAGE and page == zero_page:
                    if run_start is None:
                        run_start = off + i
                elif run_start is not None:
                    total += _punch(fd, run_start, (off + i) - run_start, punch)
                    run_start = None
            off += len(data)
        if run_start is not None:
            total += _punch(fd, run_start, off - run_start, punch)
    finally:
        os.close(fd)
    return total


def _punch(fd: int, offset: int, length: int, do_punch: bool) -> int:
    """Punch a hole over `[offset, offset+length)` (or just measure it when `do_punch` is off)."""
    if length <= 0:
        return 0
    if do_punch:
        arg = struct.pack(_PUNCHHOLE_STRUCT, 0, 0, offset, length)
        try:
            fcntl.fcntl(fd, _F_PUNCHHOLE, arg)
        except OSError as e:
            raise ImageError(
                f"this filesystem does not support hole punching ({e.strerror}); compact "
                f"needs APFS. The image is unchanged."
            ) from e
    return length


def _du_bytes(path: str) -> int:
    """On-disk size in bytes, via `du -k` (KiB), which reflects holes; `os.path.getsize` does not."""
    try:
        out = subprocess.check_output(["du", "-k", path])
        return int(out.split()[0]) * 1024
    except (subprocess.CalledProcessError, ValueError, IndexError):
        return 0


__all__ = ["cmd_compact"]
