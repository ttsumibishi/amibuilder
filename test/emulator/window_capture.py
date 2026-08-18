"""Capture a specific window by title, without bringing it to the front.

FS-UAE cannot run headless, so an emulator run always puts a real window on the user's
desktop. `screencapture -l <windowID>` captures that window's own buffer, which means:

* it works while the window is occluded or behind the IDE
* it does not steal focus or raise the window
* it captures only the emulator, not the whole desktop

Getting a CGWindowID needs Quartz; macOS ships no CLI that lists them. `pyobjc-framework-
Quartz` is therefore an optional test-only dependency, and everything here degrades to a
clear message when it is absent rather than failing an unrelated test.

Note on titles: FS-UAE names its window after the emulated model, so an A1200 run gives
"FS-UAE Amiga 1200" or similar. Matching is a case-insensitive substring against both the
window title and the owning application name, so plain "fs-uae" is a reliable match
regardless of model.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

#: Downscale width for captures that are going to be read back. A Retina capture is far
#: larger than anything needs to be, and an Amiga display is 720x568 at most anyway.
READABLE_WIDTH = 1200


class WindowCaptureUnavailable(RuntimeError):
    """Quartz is not installed, or no matching window exists."""


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    title: str
    owner: str
    width: int
    height: int
    on_screen: bool

    def describe(self) -> str:
        return (
            f"id={self.window_id} owner={self.owner!r} title={self.title!r} "
            f"{self.width}x{self.height}"
        )


def _quartz():
    try:
        import Quartz  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise WindowCaptureUnavailable(
            "pyobjc-framework-Quartz is not installed, so window IDs cannot be looked up. "
            "Install it with:  VIRTUAL_ENV=.venv uv pip install pyobjc-framework-Quartz"
        ) from e
    return Quartz


def list_windows(on_screen_only: bool = True) -> list[WindowInfo]:
    """Every window Quartz will tell us about.

    `on_screen_only=False` also finds minimised windows, which is worth having: a
    minimised emulator is exactly the case where a whole-screen capture shows nothing.
    """
    Quartz = _quartz()
    options = Quartz.kCGWindowListExcludeDesktopElements
    options |= (
        Quartz.kCGWindowListOptionOnScreenOnly if on_screen_only
        else Quartz.kCGWindowListOptionAll
    )
    raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []

    out: list[WindowInfo] = []
    for w in raw:
        bounds = w.get("kCGWindowBounds") or {}
        out.append(
            WindowInfo(
                window_id=int(w.get("kCGWindowNumber", 0)),
                title=str(w.get("kCGWindowName", "") or ""),
                owner=str(w.get("kCGWindowOwnerName", "") or ""),
                width=int(bounds.get("Width", 0) or 0),
                height=int(bounds.get("Height", 0) or 0),
                on_screen=bool(w.get("kCGWindowIsOnscreen", False)),
            )
        )
    return out


def find_window(match: str, *, min_width: int = 64) -> WindowInfo | None:
    """The best window whose title or owner contains `match`, case-insensitively.

    Preference order matters more than it looks. An app owns several windows, and FS-UAE in
    particular owns blank 500x500 placeholder surfaces alongside the real emulator window.
    Picking merely the largest match returns one of those blanks, which captures as a white
    rectangle and looks exactly like a broken capture.

    So: a window with a non-empty *title* always beats an untitled one, and only then is
    area used to break ties. FS-UAE titles its real window after the model -- "FS-UAE ·
    Amiga 1200" -- so a title match is a reliable discriminator.
    """
    needle = match.lower()
    candidates = [
        w for w in list_windows(on_screen_only=False)
        if (needle in w.title.lower() or needle in w.owner.lower())
        and w.width >= min_width
    ]
    if not candidates:
        return None
    # Titled windows first, then by area.
    return max(candidates, key=lambda w: (bool(w.title.strip()), w.width * w.height))


def wait_for_window(match: str, timeout: float = 20.0, poll: float = 0.5) -> WindowInfo:
    """Block until a matching window appears. Raises on timeout.

    An emulator takes a moment to create its window, so capturing immediately after launch
    finds nothing.
    """
    deadline = time.time() + timeout
    last: list[str] = []
    while time.time() < deadline:
        found = find_window(match)
        if found is not None:
            return found
        time.sleep(poll)
    try:
        last = [w.describe() for w in list_windows(on_screen_only=False)][:12]
    except WindowCaptureUnavailable:
        pass
    raise WindowCaptureUnavailable(
        f"no window matching {match!r} appeared within {timeout:.0f}s. "
        f"Windows seen: {last}"
    )


def capture_window(window_id: int, out_path: str | Path) -> Path:
    """Capture one window to a PNG by its CGWindowID.

    `-x` suppresses the shutter sound, `-o` omits the drop shadow so the image is exactly
    the window content.

    Nothing may be chained after `screencapture` in a shell line -- it silently prevents
    the rest of the line from running -- so this always invokes it on its own.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    proc = subprocess.run(
        ["screencapture", "-x", "-o", "-l", str(window_id), str(out)],
        capture_output=True, text=True, timeout=30,
    )
    if not out.exists() or out.stat().st_size == 0:
        raise WindowCaptureUnavailable(
            f"screencapture produced nothing for window {window_id} "
            f"(exit {proc.returncode}): {proc.stdout.strip()} {proc.stderr.strip()}. "
            "Screen Recording permission is the usual cause."
        )
    return out


def downscale(src: str | Path, dest: str | Path, width: int = READABLE_WIDTH) -> Path:
    """Shrink and re-encode as JPEG, so the result is small enough to read back."""
    src, dest = Path(src), Path(dest)
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "70",
         "--resampleWidth", str(width), str(src), "--out", str(dest)],
        capture_output=True, text=True, timeout=60,
    )
    if not dest.exists():
        raise WindowCaptureUnavailable(f"sips failed to write {dest}")
    return dest


def snapshot(match: str, out_dir: str | Path, name: str = "window",
             timeout: float = 20.0) -> tuple[Path, Path, WindowInfo]:
    """Find, capture and downscale in one call.

    Returns (png, jpg, window_info).
    """
    info = wait_for_window(match, timeout=timeout)
    out_dir = Path(out_dir)
    png = capture_window(info.window_id, out_dir / f"{name}.png")
    jpg = downscale(png, out_dir / f"{name}.jpg")
    return png, jpg, info


# ---------------------------------------------------------------------------
# CLI, so this is usable by hand or from a subagent
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Capture a window by title substring, without raising it."
    )
    p.add_argument("match", nargs="?", default="fs-uae",
                   help="case-insensitive substring of the window title or app name")
    p.add_argument("-o", "--out-dir", default="/tmp/window-capture")
    p.add_argument("-n", "--name", default="window")
    p.add_argument("-t", "--timeout", type=float, default=20.0)
    p.add_argument("--list", action="store_true", help="list windows and exit")
    p.add_argument("--repeat", type=int, default=1,
                   help="take N captures, so a boot can be watched frame by frame")
    p.add_argument("--interval", type=float, default=5.0)
    args = p.parse_args(argv)

    try:
        if args.list:
            for w in sorted(list_windows(on_screen_only=False),
                            key=lambda x: -(x.width * x.height)):
                if w.width >= 64:
                    print(w.describe())
            return 0

        for i in range(args.repeat):
            name = args.name if args.repeat == 1 else f"{args.name}-{i:02d}"
            png, jpg, info = snapshot(args.match, args.out_dir, name, args.timeout)
            print(f"{info.describe()}\n  png: {png}\n  jpg: {jpg}")
            if i + 1 < args.repeat:
                time.sleep(args.interval)
    except WindowCaptureUnavailable as e:
        print(f"window_capture: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
