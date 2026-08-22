"""A host directory presented as if it were a mounted Amiga volume.

`snap create` and `snap diff` capture a drive by walking a `volume.Volume` -- reading each
entry's name, metadata and contents. A host directory can be captured the same way if it is
wrapped in something that offers the same three things (`name`, `walk`, `read_file`), which is
all `capture.capture_volume` actually touches. That wrapper is `DirectoryVolume`, and it is
duck-typed rather than a `Volume` subclass on purpose: a `Volume` is inseparably an amitools
filesystem, and a host directory is not one.

The layout it reads is exactly the one `targets.write_directory` writes, which is what makes a
round trip work: `compose --format dir` produces a tree, FS-UAE boots it, and `snap create`
captures the result straight back into a layer with no HDF in between.

* **Names are `%XX`-unescaped** (`uaem.unescape_name`), so a host file `foo%2abar` is captured
  as the Amiga name `foo*bar` it stood for.
* **Metadata comes from the `.uaem` sidecar** when one is present, and falls back to the host
  file's own mtime with default (`----rwed`) protection when it is not -- so a directory the
  user assembled by hand, with no sidecars, is still capturable.
* **`.uaem` files are skipped as content.** They are metadata for their neighbour, not files in
  their own right. The one ambiguity this creates -- a genuine Amiga file literally named
  `something.uaem` -- is the same one amitools' own convention has, and is documented rather
  than worked around, because such a name essentially never occurs.
* **Host symlinks are reported as links and skipped**, matching how the image path treats Amiga
  links (`capture` cannot reproduce a link target), so a stray symlink can neither be silently
  followed into junk nor send the walk into a loop.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from .. import timestamps
from ..errors import ImageError, UsageError
from ..volume import Entry
from . import manifest as M
from . import uaem


class DirectoryVolume:
    """A read-only, `Volume`-shaped view of a host directory.

    Offers just the surface `capture.capture_volume` needs: `name`, `walk()` yielding
    `(dir_path, subdirs, files)` of `volume.Entry`, and `read_file(path)`. Metadata-read
    problems that do not stop the capture (a corrupt sidecar, an unreadable subdirectory) are
    collected in `warnings` for the caller to surface, the same way `CaptureResult` carries the
    warnings from an image capture.
    """

    def __init__(self, root: str, name: str | None = None):
        if not os.path.isdir(root):
            raise ImageError(f"{root}: not a directory")
        self.root = os.path.normpath(root)
        self._name = self._resolve_name(name)
        self.warnings: list[str] = []

    def _resolve_name(self, override: str | None) -> str:
        """The volume name to record. Explicit override wins; otherwise the directory's own
        basename, unescaped, which is what `compose --format dir` used to name the subdirectory.

        A host directory carries no Amiga volume name of its own, so this is a choice rather
        than a fact -- which is exactly why `--volume` exists to state it.
        """
        if override:
            name = override
        else:
            name = uaem.unescape_name(os.path.basename(os.path.abspath(self.root)))
        if not name:
            raise UsageError(
                f"cannot tell what volume {self.root} should be captured as; "
                "pass --volume NAME to name it"
            )
        if ":" in name or "/" in name:
            raise UsageError(
                f"volume name {name!r} contains ':' or '/', which an AmigaDOS volume name "
                "cannot; pass --volume NAME to give a valid one"
            )
        return name

    @property
    def name(self) -> str:
        return self._name

    # -- host-path mapping ---------------------------------------------------
    def _host(self, rel: str) -> str:
        """The on-disk path for a volume-relative path, re-escaping each component.

        The inverse of how `walk` builds a relative path, so a name that was unescaped for the
        manifest is escaped again to find the file it came from.
        """
        parts = [uaem.escape_name(p) for p in rel.split("/") if p]
        return os.path.join(self.root, *parts) if parts else self.root

    # -- entry construction --------------------------------------------------
    def _entry(self, dirent: os.DirEntry[str], rel: str) -> Entry:
        """Build a `volume.Entry` for a real (non-symlink) directory entry.

        Symlinks are filtered out in `walk` before they get here, so `dirent` is always a plain
        file or directory and its own stat is the right one.
        """
        is_dir = dirent.is_dir(follow_symlinks=False)
        try:
            st = dirent.stat(follow_symlinks=False)
            mtime = st.st_mtime
            size = 0 if is_dir else int(st.st_size)
        except OSError as e:
            self.warnings.append(f"{self._name}:{rel}: cannot stat ({e}); recorded with no size")
            mtime, size = 0.0, 0

        protect = M.DEFAULT_PROTECT
        secs, ticks = timestamps.from_unix(mtime) if mtime else (0, 0)
        comment = ""

        host = self._host(rel)
        try:
            parsed = uaem.read_sidecar(host)
        except uaem.SidecarError as e:
            self.warnings.append(
                f"{self._name}:{rel}: ignoring a corrupt {uaem.UAEM_SUFFIX} sidecar "
                f"({e}); using the host file's own timestamp instead"
            )
            parsed = None
        if parsed is not None:
            protect, secs, ticks, comment = parsed

        return Entry(
            name=uaem.unescape_name(dirent.name),
            path=rel,
            is_dir=is_dir,
            size=size,
            protect_str=protect,
            comment=comment,
            mod_secs=secs,
            mod_ticks=ticks,
        )

    # -- walk ----------------------------------------------------------------
    def walk(self, path: str = "") -> Iterator[tuple[str, list[Entry], list[Entry]]]:
        """Depth-first walk yielding `(dir_path, subdirs, files)`, matching `Volume.walk`.

        Subdirs are real directories, to be recursed into; files are the plain files.
        `.uaem` sidecars are omitted -- they describe their neighbour, they are not entries.
        A symlink is skipped with a warning rather than followed: `capture` cannot reproduce a
        link, following one could capture unrelated content, and a directory symlink could send
        the walk into a loop. That warning is the signal that something was left out.
        """
        stack = [path.strip("/")]
        while stack:
            current = stack.pop()
            host_dir = self._host(current)
            try:
                raw = list(os.scandir(host_dir))
            except OSError as e:
                self.warnings.append(f"{self._name}:{current}: cannot list ({e}); skipped")
                continue

            dirs: list[Entry] = []
            files: list[Entry] = []
            for dirent in sorted(raw, key=lambda d: d.name.lower()):
                if dirent.name.lower().endswith(uaem.UAEM_SUFFIX):
                    continue
                child_rel = f"{current}/{uaem.unescape_name(dirent.name)}" if current \
                    else uaem.unescape_name(dirent.name)
                if dirent.is_symlink():
                    self.warnings.append(
                        f"{self._name}:{child_rel}: symlink skipped -- links are not captured"
                    )
                    continue
                entry = self._entry(dirent, child_rel)
                (dirs if entry.is_dir else files).append(entry)

            yield current, dirs, files
            for d in reversed(dirs):
                stack.append(d.path)

    # -- reading -------------------------------------------------------------
    def read_file(self, path: str) -> bytes:
        host = self._host(path)
        try:
            with open(host, "rb") as fh:
                return fh.read()
        except OSError as e:
            raise ImageError(f"{self._name}:{path}: cannot read ({e})") from e


__all__ = ["DirectoryVolume"]
