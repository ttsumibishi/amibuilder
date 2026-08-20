#!/usr/bin/env bash
#
# Launch FS-UAE to install AmigaOS 3.2 onto a drive made by `amibuilder init`.
#
# This is the one step of the whole workflow that cannot be automated: the AmigaOS installer is
# interactive, so someone has to sit through it once. Everything after that -- capturing the result
# as a base layer, stacking a patch delta and a driver delta on top, restoring any of it -- is what
# amibuilder does.
#
# The install runs from FLOPPIES, not the CD. The AmigaOS 3.2 CD carries the complete floppy set in
# its /ADF directory, and the floppy install is the documented path for a plain A1200; using the CD
# instead would mean getting a CD driver and filesystem working first, for no benefit.
#
# Floppies come from images/floppy/workbench/3.2/ and are NOT in this repository -- they are
# licensed Workbench disks, so supplying them is the user's job. See the README there.
#
# Usage:
#   utils/scripts/install-wb-3.2.sh DRIVE.hdf
#
# The drive is a REQUIRED argument, deliberately. It once defaulted to the drive this was written
# for, which was fine while that drive was blank and became a footgun the moment it held a finished
# install: a bare re-run would have installed straight over it. There is no safe default for an
# argument naming something that is about to be written into.
#
# Override the ROM with AMIBUILDER_KICKSTART, or the emulator with AMIBUILDER_FSUAE.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DRIVE="${1:-}"
MEDIA="$REPO/images/floppy/workbench/3.2"
ROM="${AMIBUILDER_KICKSTART:-$REPO/source-files-do-not-add-to-git/roms/kicka1200.rom}"
FSUAE="${AMIBUILDER_FSUAE:-/Applications/FS-UAE.app/Contents/MacOS/fs-uae}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# FS-UAE is free to resolve a relative path against its own working directory rather than ours, so
# the drive written into the config is made absolute. MEDIA and ROM are already absolute, being built
# from $REPO. No external command: realpath is not guaranteed on every macOS.
abspath() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$PWD/$1" ;; esac; }

[ -n "$DRIVE" ] || die "usage: ${BASH_SOURCE[0]##*/} DRIVE.hdf

Name the drive to install onto. To make a fresh one first:
  .venv/bin/amibuilder init images/hd/mydrive.hdf --size 4G \\
      --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest"
[ -x "$FSUAE" ] || die "FS-UAE not found at $FSUAE (set AMIBUILDER_FSUAE)"
[ -f "$ROM" ]   || die "Kickstart not found at $ROM (set AMIBUILDER_KICKSTART)"
[ -f "$DRIVE" ] || die "$DRIVE does not exist. Create it first:
  .venv/bin/amibuilder init ${DRIVE#$REPO/} --size 4G \\
      --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest"
[ -d "$MEDIA" ] || die "$MEDIA not found; the install floppies live there.
It is gitignored on purpose -- Workbench ADFs are licensed software, so put your own there."

# Only the disks a 3.2 install genuinely needs. Checked up front rather than discovered halfway
# through, and named individually so a missing one is obvious. The optional extras (GlowIcons,
# Backdrops, MMULibs, the other locales) are offered in the swap list if present and never required.
for disk in Install3.2 Workbench3.2 Locale Extras3.2 Fonts Storage3.2 Classes3.2 \
            ModulesA1200_3.2; do
    [ -f "$MEDIA/$disk.adf" ] || die "$MEDIA/$disk.adf is missing.
A 3.2 install needs: Install3.2 Workbench3.2 Locale Extras3.2 Fonts Storage3.2 Classes3.2
ModulesA1200_3.2 (the Modules disk must match the emulated model, which here is A1200)."
done

# The installer WILL write to this drive, which is the entire point -- but say so, because the
# argument is easy to get wrong and a wrong one means installing over something.
printf 'Installing AmigaOS 3.2 onto: %s\n' "$DRIVE"
printf 'Partitions it will offer:\n'
if [ -x "$REPO/.venv/bin/amibuilder" ]; then
    "$REPO/.venv/bin/amibuilder" partitions "$DRIVE" || true
fi
printf '\nThe installer writes to this drive. Ctrl-C now if it is the wrong one.\n'
printf 'Press Return to launch FS-UAE. '
read -r _

CONF="$(mktemp -t amibuilder-install).fs-uae"
trap 'rm -f "$CONF"' EXIT

{
    printf '[config]\n'
    printf 'amiga_model = A1200\n'
    printf 'kickstart_file = %s\n' "$ROM"
    printf 'chip_memory = 2048\n'
    printf 'fast_memory = 8192\n'
    printf 'hard_drive_0 = %s\n' "$(abspath "$DRIVE")"
    # Two drives makes the install far less tedious: DF0 holds the boot/Install disk and the
    # installer reads the rest from DF1, so most prompts need one swap rather than two.
    printf 'floppy_drive_0 = %s/Install3.2.adf\n' "$MEDIA"
    printf 'floppy_drive_1 = %s/Workbench3.2.adf\n' "$MEDIA"
    # The swap list, reachable in-emulator so the ADFs never have to be named again.
    i=0
    for disk in Install3.2 Workbench3.2 Locale Locale-EN Locale-UK Extras3.2 Fonts \
                Storage3.2 Classes3.2 ModulesA1200_3.2 GlowIcons3.2 Backdrops3.2 \
                MMULibs HDSetup3.2 DiskDoctor; do
        if [ -f "$MEDIA/$disk.adf" ]; then
            printf 'floppy_image_%d = %s/%s.adf\n' "$i" "$MEDIA" "$disk"
            i=$((i + 1))
        fi
    done
    # Turbo floppy: every floppy operation completes immediately instead of at authentic 1980s
    # speed. A 3.2 install reads roughly a dozen 880 KB disks, and at the default (100) that is
    # most of an hour of pure waiting.
    #
    # Safe HERE specifically because this is an OS installer, which reads disks normally. Turbo
    # floppy is what breaks copy-protected games -- they time the drive to detect a real disk --
    # so this belongs in this single-purpose script and NOT in a general-purpose launcher.
    printf 'floppy_drive_speed = 0\n'
    printf 'window_width = 960\n'
    printf 'window_height = 720\n'
    # Visible and interactive, unlike the test harness -- there is a human driving this one.
    # Deliberately NOT warp_mode: it removes the frame limiter entirely, which makes the display
    # update erratically and is unpleasant to click through. Cmd+W toggles it during the waits.
    printf 'fullscreen = 0\n'
} > "$CONF"

printf '\nIn the emulator:\n'
printf '  F12          FS-UAE menu -- swap floppies from the list when the installer asks\n'
printf '  Cmd+W        toggle warp mode: runs flat out during long waits, no loss of accuracy.\n'
printf '               It is a CHORD, not F12 then W, and warp is not in the F12 menu at all.\n'
printf '               Look for "Warp mode enabled" on screen. Turn it off to interact --\n'
printf '               it kills audio and makes the display choppy\n'
printf '  Cmd+Q        quit\n'
printf '  The drive appears as Workbench:, Work: and Persist: -- install to Workbench:\n'
printf '  Skip HDToolBox entirely; the partitions already exist and are already formatted\n\n'

# -ApplePersistenceIgnoreState suppresses macOS's "reopen windows" requester, which otherwise
# appears after any abnormal quit and stops FS-UAE from starting at all.
exec "$FSUAE" "$CONF" -ApplePersistenceIgnoreState YES
