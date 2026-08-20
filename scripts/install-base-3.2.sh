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
# Usage:
#   scripts/install-base-3.2.sh [drive.hdf]
#
# Defaults to drives/base-3.2.hdf. Override the ROM with AMIBUILDER_KICKSTART.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRIVE="${1:-$REPO/drives/base-3.2.hdf}"
MEDIA="$REPO/install-media"
ROM="${AMIBUILDER_KICKSTART:-$REPO/source-files-do-not-add-to-git/roms/kicka1200.rom}"
FSUAE="${AMIBUILDER_FSUAE:-/Applications/FS-UAE.app/Contents/MacOS/fs-uae}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -x "$FSUAE" ] || die "FS-UAE not found at $FSUAE (set AMIBUILDER_FSUAE)"
[ -f "$ROM" ]   || die "Kickstart not found at $ROM (set AMIBUILDER_KICKSTART)"
[ -f "$DRIVE" ] || die "$DRIVE does not exist. Create it first:
  .venv/bin/amibuilder init ${DRIVE#$REPO/} --size 4G \\
      --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest"
[ -d "$MEDIA" ] || die "$MEDIA not found; the install floppies live there"
[ -f "$MEDIA/Install3.2.adf" ] || die "$MEDIA/Install3.2.adf is missing"

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
    printf 'hard_drive_0 = %s\n' "$DRIVE"
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
    # update erratically and is unpleasant to click through. F12+W toggles it when waiting.
    printf 'fullscreen = 0\n'
} > "$CONF"

printf '\nIn the emulator:\n'
printf '  F12          FS-UAE menu -- swap floppies from the list when the installer asks\n'
printf '  F12 then W   toggle warp mode: runs flat out during long waits, no loss of accuracy.\n'
printf '               Turn it off to interact -- it kills audio and the display gets choppy\n'
printf '  F12 then Q   quit\n'
printf '  The drive appears as Workbench:, Work: and Persist: -- install to Workbench:\n'
printf '  Skip HDToolBox entirely; the partitions already exist and are already formatted\n\n'

# -ApplePersistenceIgnoreState suppresses macOS's "reopen windows" requester, which otherwise
# appears after any abnormal quit and stops FS-UAE from starting at all.
exec "$FSUAE" "$CONF" -ApplePersistenceIgnoreState YES
