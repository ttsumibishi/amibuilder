#!/usr/bin/env bash
#
# Boot a hard drive image under FS-UAE. Nothing clever -- no floppies, no install media, just the
# drive and a Kickstart.
#
# **Booting writes to the image.** AmigaOS rewrites Env-Archive, datestamps directories and touches
# a fair amount else on the way up, so a boot is a destructive edit even if you touch nothing. This
# script therefore boots a COPY by default and leaves the original alone. That is not paranoia: the
# stock base image is deliberately un-booted, which is what makes "what does a first boot change?"
# answerable, and one careless boot would spend that permanently.
#
# The copy is an APFS clone, so it is instant and occupies no extra space until written to -- a 4 GiB
# image clones in about 20 ms. There is no reason to skip it.
#
# Usage:
#   utils/scripts/boot-hdf.sh DRIVE.hdf              boot a clone, original untouched
#   utils/scripts/boot-hdf.sh --in-place DRIVE.hdf    boot the real thing, changes persist
#
# The clone lands next to the original as <name>-booted.hdf and is kept, so you can see what the
# boot did:
#
#   utils/scripts/boot-hdf.sh images/hd/base32/base-3.2.hdf
#   .venv/bin/amibuilder snap create images/hd/base32/base-3.2.hdf --label base-3.2
#   .venv/bin/amibuilder snap diff images/hd/base32/base-3.2-booted.hdf --parent base-3.2
#
# Override the ROM with AMIBUILDER_KICKSTART, the model with AMIBUILDER_MODEL, the emulator with
# AMIBUILDER_FSUAE.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL="${AMIBUILDER_MODEL:-A1200}"
ROM="${AMIBUILDER_KICKSTART:-$REPO/source-files-do-not-add-to-git/roms/kicka1200.rom}"
FSUAE="${AMIBUILDER_FSUAE:-/Applications/FS-UAE.app/Contents/MacOS/fs-uae}"

IN_PLACE=0
DRIVE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --in-place) IN_PLACE=1 ;;
        -h|--help)  sed -n '3,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)         printf 'error: unknown option %s\n' "$1" >&2; exit 2 ;;
        *)          [ -z "$DRIVE" ] || { printf 'error: one drive at a time\n' >&2; exit 2; }
                    DRIVE="$1" ;;
    esac
    shift
done

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# FS-UAE is free to resolve a relative path against its own working directory rather than ours, so
# everything written into the config is made absolute. No external command: realpath is not
# guaranteed and this only needs to handle a path that already exists.
abspath() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$PWD/$1" ;; esac; }

if [ -z "$DRIVE" ]; then
    printf 'error: name the image to boot.\n\nusage: %s [--in-place] DRIVE.hdf\n' \
        "${BASH_SOURCE[0]##*/}" >&2
    # Easier than making the user go and look.
    if [ -d "$REPO/images/hd" ]; then
        found=$(find "$REPO/images/hd" -name '*.hdf' -maxdepth 3 2>/dev/null | sort)
        if [ -n "$found" ]; then
            printf '\nAvailable under images/hd:\n' >&2
            printf '%s\n' "$found" | sed "s|^$REPO/|  |" >&2
        fi
    fi
    exit 2
fi

[ -x "$FSUAE" ] || die "FS-UAE not found at $FSUAE (set AMIBUILDER_FSUAE)"
[ -f "$ROM" ]   || die "Kickstart not found at $ROM (set AMIBUILDER_KICKSTART)"
[ -f "$DRIVE" ] || die "$DRIVE does not exist"

if [ "$IN_PLACE" -eq 1 ]; then
    TARGET="$DRIVE"
    printf 'Booting IN PLACE: %s\n' "$TARGET"
    printf 'Changes the Amiga makes are permanent. Ctrl-C now if that is not what you want.\n'
else
    TARGET="${DRIVE%.hdf}-booted.hdf"
    # -c asks for an APFS clone: instant, and shares blocks until one side is written to. Falls back
    # to a plain copy on a filesystem that cannot clone, which is slower but still correct.
    cp -c "$DRIVE" "$TARGET" 2>/dev/null || cp "$DRIVE" "$TARGET" \
        || die "could not copy $DRIVE to $TARGET"
    printf 'Booting a copy:  %s\n' "$TARGET"
    printf 'Original left untouched: %s\n' "$DRIVE"
fi

if [ -x "$REPO/.venv/bin/amibuilder" ]; then
    "$REPO/.venv/bin/amibuilder" partitions "$TARGET" || true
fi

CONF="$(mktemp -t amibuilder-boot).fs-uae"
trap 'rm -f "$CONF"' EXIT

{
    printf '[config]\n'
    printf 'amiga_model = %s\n' "$MODEL"
    printf 'kickstart_file = %s\n' "$ROM"
    printf 'chip_memory = 2048\n'
    printf 'fast_memory = 8192\n'
    printf 'hard_drive_0 = %s\n' "$(abspath "$TARGET")"
    printf 'window_width = 960\n'
    printf 'window_height = 720\n'
    printf 'fullscreen = 0\n'
} > "$CONF"

printf '\n  F12    FS-UAE menu\n'
printf '  Cmd+W  toggle warp mode (a chord, not in the F12 menu; look for "Warp mode enabled")\n'
printf '  Cmd+Q  quit\n\n'

# Suppresses macOS's "reopen windows" requester, which otherwise appears after any abnormal quit and
# stops FS-UAE from starting at all.
exec "$FSUAE" "$CONF" -ApplePersistenceIgnoreState YES
