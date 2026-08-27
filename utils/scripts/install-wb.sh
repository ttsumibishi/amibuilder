#!/usr/bin/env bash
#
# Install AmigaOS onto a drive made by `amibuilder init`, from the install floppies.
#
# This is the one step of the whole workflow that cannot be automated: the AmigaOS installer is
# interactive, so somebody sits through it once. Everything after that -- capturing the result as a
# base layer, stacking a patch delta and a driver delta on top, restoring any of it -- is what
# amibuilder does. So the least this can do is not require rediscovering the emulator configuration
# and the disk set every time.
#
# Replaces the earlier install-wb-3.2.sh, which hardcoded 3.2 throughout and has been removed.
#
# Usage:
#   utils/scripts/install-wb.sh [options] DRIVE.hdf
#   utils/scripts/install-wb.sh [options] --new-disk DRIVE.hdf
#
#   --version=VER       OS version to install (default: the newest under images/floppy/workbench)
#   --model=NAME        Amiga model to emulate (default A1200). Selects the Modules disk
#   --new-disk          create DRIVE.hdf first, with `amibuilder init`. Refuses an existing file
#   --size=SIZE         with --new-disk, total image size (default 4G)
#   --partition=SPEC    with --new-disk, repeatable; replaces the default layout entirely
#   --rom=PATH|NAME     Kickstart to use, passed through to boot-hdf.sh
#   --dry-run           report everything it would do, launch nothing
#   -h, --help          this text
#
# ## It delegates the launch, deliberately
#
# `boot-hdf.sh` already resolves ROMs and models (including inferring the model from a ROM name),
# curates the 20-entry floppy swap list, attaches drives, and suppresses macOS's window-restore
# requester -- all of it covered by utils/scripts/test-boot-hdf.sh. Writing a second FS-UAE config
# here would mean two config writers drifting apart, so this script's job is only the part
# boot-hdf.sh has no business knowing: which disks a given OS version needs, which Modules disk goes
# with the emulated model, and whether they are all present.
#
# What it passes and why:
#
#   --in-place        an install MUST write to the real drive. boot-hdf.sh clones by default, which
#                     is right for booting and exactly wrong here
#   --turbo-floppy    a dozen 880 KB disks at authentic speed is most of an hour of waiting. Safe
#                     for an installer, which reads disks normally; it is copy-protected games that
#                     turbo breaks, which is why boot-hdf.sh leaves it off by default
#   --no-warp         warp makes the display erratic and kills audio, which is unpleasant when you
#                     are clicking through a GUI installer. Cmd+W toggles it live for the long waits
#   --drive-0-adf     the Install disk, booted
#   --drive-1-adf     the Workbench disk. Two drives means most installer prompts need one swap
#                     rather than two
#   --add-adf         the rest, curated -- see the swap list note below
#
# ## The swap list is curated, not swept
#
# `boot-hdf.sh --floppy-path` would offer the whole directory, and that is the wrong tool here: the
# FS-UAE swap list holds 20 images and a full 3.2 set is 35 disks, so it truncates alphabetically --
# which drops `Workbench3.2.adf` and `Storage3.2.adf` behind 22 `Locale-*` files. Naming the disks
# explicitly, required ones first, is what keeps the list both short enough and correct.
#
# ## Adding a version is a data edit
#
# `disks_for()` below is a per-version manifest, because disk names differ across releases (3.2 uses
# `Workbench3.2.adf`, and earlier sets do not carry the version in the filename at all) and that is
# data, not logic. An unknown version is refused with a message saying exactly what to add, rather
# than guessed at -- a wrong manifest fails halfway through an interactive install, which is the
# most expensive place to fail.
#
# Only 3.2 is populated, because 3.2 is the only set that has been verified against real media.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MEDIA_ROOT="${AMIBUILDER_MEDIA_ROOT:-$REPO/images/floppy/workbench}"
BOOT_HDF="$REPO/utils/scripts/boot-hdf.sh"
AMIBUILDER="${AMIBUILDER_BIN:-$REPO/.venv/bin/amibuilder}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Per-version disk manifests
# ---------------------------------------------------------------------------

#: Disks a given version needs, on stdout, one per line, REQUIRED ones first and the Modules disk
#: for $2 among them. Empty output means "no manifest for this version".
#: The `#optional` suffix marks a disk that is offered when present and never required.
disks_for() {
    local version="$1" model="$2"
    case "$version" in
        3.2|3.2.*)
            printf '%s\n' \
                "Install3.2" \
                "Workbench3.2" \
                "Locale" \
                "Extras3.2" \
                "Fonts" \
                "Storage3.2" \
                "Classes3.2" \
                "Modules$(modules_suffix "$model")_3.2" \
                "GlowIcons3.2#optional" \
                "Backdrops3.2#optional" \
                "MMULibs#optional" \
                "HDSetup3.2#optional" \
                "DiskDoctor#optional" \
                "Locale-EN#optional" \
                "Locale-UK#optional"
            ;;
        *) : ;;   # no manifest -- caller reports it
    esac
}

#: The Modules disk carries the model's ROM modules, so it MUST match the emulated machine. The
#: names are not FS-UAE's model strings: the set ships A4000D and A4000T where FS-UAE has one
#: A4000/040, so the mapping is explicit rather than a lowercase of the model.
modules_suffix() {
    case "$1" in
        A500|A500+)   printf 'A500\n' ;;
        A600)         printf 'A600\n' ;;
        A1200)        printf 'A1200\n' ;;
        A2000)        printf 'A2000\n' ;;
        A3000)        printf 'A3000\n' ;;
        A4000/040|A4000)  printf 'A4000D\n' ;;
        A4000T)       printf 'A4000T\n' ;;
        CD32)         printf 'CD32\n' ;;
        *)            printf '\n' ;;
    esac
}

#: Versions with a manifest, for error messages. Kept beside disks_for so they cannot drift.
KNOWN_VERSIONS="3.2"

# ---------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------

#: Highest version directory under MEDIA_ROOT. Sorted numerically per dot-component rather than
#: lexically, so 3.10 would rank above 3.2 -- and without `sort -V`, which is GNU-only and this is
#: a macOS script (notes: GNU/BSD divergence).
newest_version() {
    [ -d "$MEDIA_ROOT" ] || return 0
    local found
    found=$(find "$MEDIA_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sed 's|.*/||')
    [ -n "$found" ] || return 0
    printf '%s\n' "$found" | awk '
        {
            n = split($0, part, ".")
            key = ""
            for (i = 1; i <= 4; i++) key = key sprintf("%06d", (i <= n ? part[i] + 0 : 0))
            print key "\t" $0
        }' | sort | tail -1 | cut -f2
}

list_versions() {
    [ -d "$MEDIA_ROOT" ] || return 0
    local found
    found=$(find "$MEDIA_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sed 's|.*/||' | sort)
    [ -n "$found" ] || return 0
    printf '\nVersion directories present under %s:\n' "$MEDIA_ROOT" >&2
    printf '%s\n' "$found" | sed 's|^|  |' >&2
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

VERSION=""
MODEL="${AMIBUILDER_MODEL:-A1200}"
NEW_DISK=0
SIZE="4G"
PARTITIONS=()
ROM_ARG=""
DRY_RUN=0
DRIVE=""

need_value() { [ $# -ge 2 ] && [ -n "${2:-}" ] || die "$1 needs a value"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --version=*)    VERSION="${1#*=}" ;;
        --version)      need_value "$1" "${2:-}"; VERSION="$2"; shift ;;
        --model=*)      MODEL="${1#*=}" ;;
        --model)        need_value "$1" "${2:-}"; MODEL="$2"; shift ;;
        --new-disk)     NEW_DISK=1 ;;
        --size=*)       SIZE="${1#*=}" ;;
        --size)         need_value "$1" "${2:-}"; SIZE="$2"; shift ;;
        --partition=*)  PARTITIONS+=("${1#*=}") ;;
        --partition)    need_value "$1" "${2:-}"; PARTITIONS+=("$2"); shift ;;
        --rom=*)        ROM_ARG="${1#*=}" ;;
        --rom)          need_value "$1" "${2:-}"; ROM_ARG="$2"; shift ;;
        -n|--dry-run)   DRY_RUN=1 ;;
        -h|--help)      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' \
                            "${BASH_SOURCE[0]}"; exit 0 ;;
        -*)             die "unknown option $1" ;;
        *)              [ -z "$DRIVE" ] || die "one drive at a time"
                        DRIVE="$1" ;;
    esac
    shift
done

# The drive is required, deliberately. The script this replaces once defaulted to the drive it was
# written for, which was fine while that drive was blank and became a footgun the moment it held a
# finished install: a bare re-run would have installed straight over it.
[ -n "$DRIVE" ] || die "name the drive to install onto.

  utils/scripts/install-wb.sh DRIVE.hdf              # install onto an existing drive
  utils/scripts/install-wb.sh --new-disk DRIVE.hdf   # create it first, then install

Run with --help for the full option list."

[ -x "$BOOT_HDF" ] || die "$BOOT_HDF is missing or not executable; it does the launching"

# ---------------------------------------------------------------------------
# Resolve the version and its disk set
# ---------------------------------------------------------------------------

if [ -z "$VERSION" ]; then
    VERSION="$(newest_version)"
    [ -n "$VERSION" ] || { printf 'error: no version directories under %s\n' "$MEDIA_ROOT" >&2
        printf '       Put the install floppies in %s/<version>/, e.g. %s/3.2/\n' \
            "$MEDIA_ROOT" "$MEDIA_ROOT" >&2
        printf '       They are gitignored on purpose: Workbench ADFs are licensed software.\n' >&2
        exit 1; }
    VERSION_FROM=" (newest found)"
else
    VERSION_FROM=""
fi

MEDIA="$MEDIA_ROOT/$VERSION"
[ -d "$MEDIA" ] || { printf 'error: no floppies for version %s at %s\n' "$VERSION" "$MEDIA" >&2
    list_versions; exit 1; }

MODULES="$(modules_suffix "$MODEL")"
[ -n "$MODULES" ] || die "no Modules disk is known for model $MODEL.
The 3.2 set ships A500, A600, A1200, A2000, A3000, A4000D, A4000T and CD32 variants; pass one of
those machines with --model (FS-UAE spells the A4000 'A4000/040')."

MANIFEST="$(disks_for "$VERSION" "$MODEL")"
[ -n "$MANIFEST" ] || die "no disk manifest for version $VERSION (known: $KNOWN_VERSIONS).

Disk names differ across releases, so the set is data rather than logic -- guessing would fail
halfway through an interactive install. Add a case for $VERSION to disks_for() in
${BASH_SOURCE[0]##*/}, listing the required disks first and marking optional ones '#optional'."

# Split the manifest into required and optional, keeping manifest order: boot-hdf.sh places
# --drive-N-adf disks first in the swap list, and the rest follow in the order given, so a required
# disk can never be the one truncated away.
REQUIRED=()
OPTIONAL=()
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$entry" in
        *"#optional") OPTIONAL+=("${entry%\#optional}") ;;
        *)            REQUIRED+=("$entry") ;;
    esac
done <<EOF
$MANIFEST
EOF

# ---------------------------------------------------------------------------
# Check the media before touching anything
# ---------------------------------------------------------------------------

MISSING=()
for disk in "${REQUIRED[@]}"; do
    [ -f "$MEDIA/$disk.adf" ] || MISSING+=("$disk")
done

if [ "${#MISSING[@]}" -gt 0 ]; then
    printf 'error: %d required disk(s) missing from %s:\n' "${#MISSING[@]}" "$MEDIA" >&2
    for disk in "${MISSING[@]}"; do printf '  %s.adf\n' "$disk" >&2; done
    printf '\nAn AmigaOS %s install onto an %s needs all of:\n' "$VERSION" "$MODEL" >&2
    for disk in "${REQUIRED[@]}"; do printf '  %s.adf\n' "$disk" >&2; done
    printf '\nThe Modules disk must match the emulated model (%s here, so Modules%s_%s).\n' \
        "$MODEL" "$MODULES" "$VERSION" >&2
    printf 'If you have the AmigaOS %s CD the whole set is in its /ADF directory; see\n' "$VERSION" >&2
    printf '%s/README.md.\n' "$MEDIA" >&2
    exit 1
fi

PRESENT_OPTIONAL=()
for disk in ${OPTIONAL[@]+"${OPTIONAL[@]}"}; do
    [ -f "$MEDIA/$disk.adf" ] && PRESENT_OPTIONAL+=("$disk")
done

# ---------------------------------------------------------------------------
# The drive
# ---------------------------------------------------------------------------

if [ "$NEW_DISK" -eq 1 ]; then
    # No --force is passed and the file is not pre-created, so `init` refuses an existing image
    # itself rather than this script second-guessing it.
    [ -x "$AMIBUILDER" ] || die "$AMIBUILDER not found; --new-disk needs it to create the drive"
    if [ "${#PARTITIONS[@]}" -eq 0 ]; then
        PARTITIONS=("Workbench=1G,bootable" "Work=2G" "Persist=rest")
    fi
    INIT_ARGS=("$DRIVE" "--size" "$SIZE")
    for spec in "${PARTITIONS[@]}"; do INIT_ARGS+=("--partition" "$spec"); done

    printf 'Creating %s (%s)\n' "$DRIVE" "$SIZE"
    for spec in "${PARTITIONS[@]}"; do printf '  partition %s\n' "$spec"; done
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'would run: %s init %s\n' "${AMIBUILDER##*/}" "${INIT_ARGS[*]}"
    else
        "$AMIBUILDER" init "${INIT_ARGS[@]}"
    fi
elif [ "${#PARTITIONS[@]}" -gt 0 ] || [ "$SIZE" != "4G" ]; then
    die "--size and --partition only mean anything with --new-disk"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    [ -f "$DRIVE" ] || die "$DRIVE does not exist. Create it first:

  utils/scripts/install-wb.sh --new-disk $DRIVE

or by hand:
  ${AMIBUILDER##*/} init $DRIVE --size 4G \\
      --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest"
fi

# ---------------------------------------------------------------------------
# Report, then hand over to boot-hdf.sh
# ---------------------------------------------------------------------------

BOOT_ARGS=(--in-place --turbo-floppy --no-warp
           "--drive-0-adf=$MEDIA/${REQUIRED[0]}.adf")
# The second required disk is the OS itself, which belongs in DF1 so most prompts need one swap.
[ "${#REQUIRED[@]}" -lt 2 ] || BOOT_ARGS+=("--drive-1-adf=$MEDIA/${REQUIRED[1]}.adf")
for disk in "${REQUIRED[@]:2}"; do BOOT_ARGS+=("--add-adf=$MEDIA/$disk.adf"); done
for disk in ${PRESENT_OPTIONAL[@]+"${PRESENT_OPTIONAL[@]}"}; do
    BOOT_ARGS+=("--add-adf=$MEDIA/$disk.adf")
done
BOOT_ARGS+=("--model=$MODEL")
[ -z "$ROM_ARG" ] || BOOT_ARGS+=("--rom=$ROM_ARG")

printf '\nAmigaOS %s%s onto %s\n' "$VERSION" "$VERSION_FROM" "$DRIVE"
printf 'model:           %s  (Modules%s_%s)\n' "$MODEL" "$MODULES" "$VERSION"
printf 'media:           %s\n' "$MEDIA"
printf 'DF0:             %s.adf\n' "${REQUIRED[0]}"
[ "${#REQUIRED[@]}" -lt 2 ] || printf 'DF1:             %s.adf\n' "${REQUIRED[1]}"
printf 'swap list:       %d disk(s) -- %d required, %d optional present\n' \
    "$(( ${#REQUIRED[@]} + ${#PRESENT_OPTIONAL[@]} ))" "${#REQUIRED[@]}" "${#PRESENT_OPTIONAL[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
    printf '\nwould run: %s %s %s\n' "${BOOT_HDF##*/}" "${BOOT_ARGS[*]}" "$DRIVE"
    printf 'nothing was launched\n'
    exit 0
fi

printf '\nIn the emulator:\n'
printf '  F12          FS-UAE menu -- swap floppies from the list when the installer asks\n'
printf '  Cmd+W        toggle warp mode during the long waits. It is a CHORD, and warp is not\n'
printf '               in the F12 menu. Look for "Warp mode enabled" on screen\n'
printf '  Cmd+Q        quit\n'
printf '  Install to the bootable partition (Workbench: in the default layout)\n'
printf '  Skip HDToolBox entirely -- the partitions already exist and are already formatted\n'
printf '\nThe installer WRITES to this drive. Ctrl-C now if it is the wrong one.\n'
# Only prompt when there is somebody to answer. Without a TTY a `read` blocks forever, which turns a
# script that should have failed into one that hangs -- the same reason the device guard rails require
# --yes off a terminal. Use --dry-run to see the plan without launching.
if [ -t 0 ]; then
    printf 'Press Return to launch FS-UAE. '
    read -r _
else
    printf '(no terminal on stdin, so not waiting for confirmation)\n'
fi

exec "$BOOT_HDF" "${BOOT_ARGS[@]}" "$DRIVE"
