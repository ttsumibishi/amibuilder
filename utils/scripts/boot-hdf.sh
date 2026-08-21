#!/usr/bin/env bash
#
# Boot a hard drive image under FS-UAE, optionally with floppies attached.
#
# **Booting CAN write to the image, so this boots a COPY by default** and leaves the original alone.
#
# Measured 2026-08-20, correcting an earlier claim in this comment: booting a barebones AmigaOS 3.2 to
# Workbench and leaving it idle for a minute writes *nothing at all*. The image came back
# byte-identical, host mtime untouched. So the reflex belief that "AmigaOS restamps everything on the
# way up" is wrong for a bare boot.
#
# The copy still defaults on, for two reasons that survive that correction. Anything you actually *do*
# writes -- saving a preference, moving an icon (which rewrites its .info), anything in WBStartup. And
# the copy is free: `cp -c` asks for an APFS clone, measured at 17 ms for a 4 GiB image, sharing blocks
# until one side is written. Free insurance is worth taking even when the risk turns out to be small.
#
# It matters most for the stock base image, which is deliberately un-booted -- that is what makes
# "what does a first boot change?" answerable at all.
#
# Usage:
#   utils/scripts/boot-hdf.sh [options] DRIVE.hdf
#
#   --in-place              boot the real file; changes persist. Default is a clone.
#   --drive-N-adf=PATH      INSERT this ADF into DFN at boot, for N in 0..3
#   --add-adf=PATH          offer this ADF in the swap list without inserting it. Repeatable.
#   --floppy-path=DIR       offer every floppy in DIR in the swap list, inserting none
#   --extra-hd=PATH         attach another hard drive. Repeatable, up to 3.
#   --no-warp               boot at real speed instead of flat out
#   --rom=PATH|NAME         Kickstart to use. A bare name is looked up in ROMs/
#   --model=NAME            Amiga model (default A1200)
#   --ui                    hand the config to FS-UAE Launcher instead of booting
#
# ## Choosing a ROM
#
# AmigaOS 3.2.3 expects Kickstart 47.115, and the 3.2 update ships one per machine. Drop them in
# `ROMs/` at the repo root (gitignored -- they are licensed Cloanto files) and name one directly:
#
#   utils/scripts/boot-hdf.sh --rom=A1200.47.115 drive.hdf
#
# A bare name is resolved against ROMs/, with or without the .rom suffix; anything containing a
# slash is taken as a path. A wrong name lists what is actually there rather than just failing.
#
# The default is deliberately left alone rather than pointed at the newest ROM found, because
# which ROM booted an image is exactly the kind of thing that should not change silently
# underneath a comparison. Set AMIBUILDER_KICKSTART to change it for a shell.
#
# **The model and the ROM have to agree.** An A4000 Kickstart under `--model A1200` will not boot,
# and the failure looks like a broken image rather than a mismatched pair. When --rom names a ROM
# whose filename starts with a known model and --model was not given, the model is taken from the
# filename and reported.
#
# ## --ui: configure in the Launcher, start it yourself
#
# `--ui` writes the same config, then opens FS-UAE Launcher with it selected and **does not start
# the emulator**. For picking apart a config by hand, or changing memory and chipset before a run.
#
# Note that this cannot be done by passing the config to the Launcher on its command line, which
# is the obvious approach and is wrong: measured 2026-08-20, `fs-uae-launcher <config>` spawns
# `fs-uae` about a second later and boots it. So the config goes into the Launcher's own
# Configurations directory and its Settings.ini is pointed at it, and the Launcher is opened with
# no arguments.
#
# That means --ui writes two files outside this repo, under ~/Documents/FS-UAE. Settings.ini is
# backed up beside itself first, and the config is named `amibuilder-<drive>` so it is obvious
# where it came from. Both are the Launcher's own data directory, which is what it is for.
#
# ## Extra drives are NOT cloned
#
# The boot drive is copied by default; an --extra-hd is attached as-is, deliberately. That makes it a
# two-way channel: whatever the Amiga writes to it is on the host afterwards. It is how you hand files
# to the Amiga -- see utils/scripts/make-transfer-hdf.sh, which builds one from a host directory --
# and how you get results back.
#
# The asymmetry is the point. The boot drive is the thing worth protecting from an accidental write;
# a transfer drive is disposable and rebuildable, and cloning it would silently discard whatever the
# Amiga put there.
#
# ## Warp mode is ON by default
#
# Emulation runs without the frame limiter, which makes booting and installing dramatically faster and
# does NOT affect accuracy -- FS-UAE's own documentation is explicit that it only removes the pauses
# between generating frames.
#
# The costs, both worth knowing rather than discovering. There is no audio while warp is on, and the
# display updates erratically, so it is unpleasant to click through a GUI. And it runs a CPU core flat
# out, so a session left sitting at Workbench will heat the machine and drain the battery.
#
# `Cmd+W` toggles it live, so the sensible pattern is: let it boot fast, then Cmd+W to interact. Pass
# --no-warp when the whole session is interactive.
#
# Two jobs, kept separate: --drive-N-adf is "boot from this" or "have this in the drive", and
# --add-adf / --floppy-path are "make this available to swap to". The install case wants both -- boot
# the Install disk, reach the rest from the F12 menu.
#
# **--add-adf and --floppy-path are mutually exclusive.** Naming disks explicitly and sweeping a
# directory are two different intentions, and combining them mostly produces a list nobody predicted.
# Pick one.
#
# With no floppy option, no floppy drives are configured at all. FS-UAE sizes the drive count from the
# highest floppy_drive_N given, so --drive-3-adf alone emulates four drives with only DF3 loaded.
#
# Anything named with --drive-N-adf is ALSO added to the swap list, so you can put it back after
# swapping away from it. F12 opens the menu to swap.
#
# ## The 20-image ceiling, and why the list cannot be paged across drives
#
# **The swap list holds 20 images maximum** -- floppy_image_0 through floppy_image_19 -- and it is a
# SINGLE GLOBAL LIST, not one per drive. FS-UAE has no per-drive list option: the only name in the
# binary is `floppy_image_%d`, a flat namespace. From the F12 menu you pick which drive to insert a
# listed disk into. So "first 20 on DF0, next 20 on DF1" is not expressible, however reasonable it
# sounds.
#
# What raises the ceiling instead is that **an inserted disk does not have to be in the list**. Four
# drives plus a 20-entry list means up to 24 distinct floppies reachable without quitting.
#
# Beyond that, curate. A directory with more than 20 is truncated alphabetically, with a warning
# naming every disk dropped. Alphabetical is unhelpful but honest -- over
# images/floppy/workbench/3.2 (35 disks) it drops Workbench3.2.adf and Storage3.2.adf, because 22
# Locale-* files sort ahead of them. Anything named with --drive-N-adf is placed FIRST and so always
# survives truncation, which is the intended escape.
#
# Note --floppy-path is NOT FS-UAE's `floppies_dir`, which is only a search path for relative
# filenames and puts nothing in the list. The list has to be enumerated entry by entry. `floppies_dir`
# is set as well, so the F12 file browser opens somewhere useful.
#
# Examples:
#   # just boot the drive
#   utils/scripts/boot-hdf.sh images/hd/base32/base-3.2.hdf
#
#   # boot with a disk in DF0
#   utils/scripts/boot-hdf.sh --drive-0-adf=$WB/Extras3.2.adf drive.hdf
#
#   # install something: nothing inserted, three disks reachable from the F12 menu
#   utils/scripts/boot-hdf.sh --add-adf=$WB/Extras3.2.adf --add-adf=$WB/Fonts.adf \
#       --add-adf=$WB/Classes3.2.adf drive.hdf
#
#   # everything in a directory offered, nothing inserted
#   utils/scripts/boot-hdf.sh --floppy-path=$WB drive.hdf
#
#   # one disk in the drive, the rest of the directory reachable
#   utils/scripts/boot-hdf.sh --drive-0-adf=$WB/Workbench3.2.adf --floppy-path=$WB drive.hdf
#
# The clone lands next to the original as <name>-booted.hdf and is kept, so you can see what the
# boot did:
#
#   amibuilder snap diff images/hd/base32/base-3.2-booted.hdf --parent base-3.2 --label boot-once
#
# (`snap diff` requires --label: it writes a reviewable candidate, not a finished layer.)
#
# Override the ROM with AMIBUILDER_KICKSTART, the model with AMIBUILDER_MODEL, the emulator with
# AMIBUILDER_FSUAE.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL="${AMIBUILDER_MODEL:-A1200}"
MODEL_FROM_ROM=""
ROM="${AMIBUILDER_KICKSTART:-$REPO/source-files-do-not-add-to-git/roms/kicka1200.rom}"
FSUAE="${AMIBUILDER_FSUAE:-/Applications/FS-UAE.app/Contents/MacOS/fs-uae}"
LAUNCHER="${AMIBUILDER_FSUAE_LAUNCHER:-/Applications/FS-UAE Launcher.app}"

#: Where --rom looks up a bare name. Gitignored: Kickstarts are licensed Cloanto files.
#: Overridable so the test suite can point at a fixture -- a test that read the real ROMs directory
#: would only run on a machine that has licensed ROMs in it.
ROM_DIR="${AMIBUILDER_ROM_DIR:-$REPO/ROMs}"
#: FS-UAE Launcher's data directory. Its own, not ours -- but --ui has to write into it, because
#: handing the config to the Launcher on the command line makes it boot immediately.
FSUAE_BASE="${AMIBUILDER_FSUAE_BASE:-$HOME/Documents/FS-UAE}"

#: FS-UAE supports floppy_image_0 .. floppy_image_19 and no more. One global list, not per drive.
SWAP_LIMIT=20
#: floppy_drive_0 .. floppy_drive_3, per the documented option set.
MAX_DRIVES=4
#: The boot drive takes hard_drive_0, so extras occupy slots 1 upward.
MAX_EXTRA_HD=3

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# FS-UAE is free to resolve a relative path against its own working directory rather than ours, so
# everything written into the config is made absolute. No external command: realpath is not
# guaranteed and this only needs to handle a path that already exists.
abspath() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$PWD/$1" ;; esac; }

#: Models whose Kickstart the 3.2 update ships, keyed by the filename prefix it uses. Used only to
#: infer --model from a --rom name, never to override an explicit --model.
rom_model_for() {
    case "$1" in
        A1200*)  printf 'A1200\n' ;;
        A3000*)  printf 'A3000\n' ;;
        A4000T*) printf 'A4000/040\n' ;;
        A4000*)  printf 'A4000/040\n' ;;
        CDTV*|A500*|A600*|A2000*) printf 'A500\n' ;;
        *)       printf '\n' ;;
    esac
}

list_roms() {
    [ -d "$ROM_DIR" ] || return 0
    local found
    found=$(find "$ROM_DIR" -maxdepth 1 -type f \( -iname '*.rom' \) 2>/dev/null | sort)
    [ -n "$found" ] || return 0
    printf '\nAvailable in ROMs/:\n' >&2
    printf '%s\n' "$found" | sed 's|.*/|  |' >&2
}

# A bare name is looked up in ROMs/, with or without the suffix; anything with a slash is a path.
# Returns the resolved path on stdout, or dies naming what is available.
resolve_rom() {
    local want="$1" candidate
    case "$want" in
        */*) printf '%s\n' "$want"; return 0 ;;
    esac
    for candidate in "$ROM_DIR/$want" "$ROM_DIR/$want.rom" "$ROM_DIR/$want.ROM"; do
        if [ -f "$candidate" ]; then printf '%s\n' "$candidate"; return 0; fi
    done
    # Not in ROMs/ and not obviously a path: it may still be a bare filename in the cwd.
    if [ -f "$want" ]; then printf '%s\n' "$want"; return 0; fi
    printf 'error: --rom %s: no such ROM in %s\n' "$want" "$ROM_DIR" >&2
    list_roms
    exit 2
}

IN_PLACE=0
WARP=1
UI=0
MODEL_GIVEN=0
ROM_GIVEN=""
DRIVE=""
FLOPPY_PATH=""
# One slot per emulated drive. Indexed rather than four named variables so adding a fifth, if FS-UAE
# ever grows one, is a constant change.
DRIVE_ADF=("" "" "" "")
# --add-adf, in the order given: the caller chose that order, so it is preserved rather than sorted.
ADD_ADF=()
# --extra-hd, filling hard_drive_1 upward in the order given.
EXTRA_HD=()

# Both --opt=value and --opt value are accepted; the first is what the request asked for and the
# second is what fingers type anyway.
need_value() { [ $# -ge 2 ] && [ -n "${2:-}" ] || die "$1 needs a value"; }

# --drive-2-adf -> 2. The case patterns below already restrict N to 0..3, so this only ever sees a
# digit in range.
drive_index() { local n="${1#--drive-}"; printf '%s\n' "${n%%-adf*}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --in-place)          IN_PLACE=1 ;;
        --no-warp)           WARP=0 ;;
        --warp)              WARP=1 ;;
        --ui)                UI=1 ;;
        --rom=*)             ROM="$(resolve_rom "${1#*=}")"; ROM_GIVEN="${1#*=}" ;;
        --rom)               need_value "$1" "${2:-}"
                             ROM="$(resolve_rom "$2")"; ROM_GIVEN="$2"; shift ;;
        --model=*)           MODEL="${1#*=}"; MODEL_GIVEN=1 ;;
        --model)             need_value "$1" "${2:-}"; MODEL="$2"; MODEL_GIVEN=1; shift ;;
        --drive-[0-3]-adf=*) DRIVE_ADF[$(drive_index "$1")]="${1#*=}" ;;
        --drive-[0-3]-adf)   need_value "$1" "${2:-}"
                             DRIVE_ADF[$(drive_index "$1")]="$2"; shift ;;
        # Named so the failure says what is wrong rather than "unknown option".
        --drive-*-adf|--drive-*-adf=*)
                             die "only drives 0-$((MAX_DRIVES - 1)) exist: $1" ;;
        --add-adf=*)         ADD_ADF+=("${1#*=}") ;;
        --add-adf)           need_value "$1" "${2:-}"; ADD_ADF+=("$2"); shift ;;
        --extra-hd=*)        EXTRA_HD+=("${1#*=}") ;;
        --extra-hd)          need_value "$1" "${2:-}"; EXTRA_HD+=("$2"); shift ;;
        --floppy-path=*)     FLOPPY_PATH="${1#*=}" ;;
        --floppy-path)       need_value "$1" "${2:-}"; FLOPPY_PATH="$2"; shift ;;
        # Prints the whole header comment, however long it grows: every '#' line after the shebang,
        # stopping at the first line that is not one. A hardcoded line range silently truncated the
        # help the first time this comment was edited.
        -h|--help)           awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' \
                                 "${BASH_SOURCE[0]}"; exit 0 ;;
        -*)                  printf 'error: unknown option %s\n' "$1" >&2; exit 2 ;;
        *)                   [ -z "$DRIVE" ] || { printf 'error: one drive at a time\n' >&2; exit 2; }
                             DRIVE="$1" ;;
    esac
    shift
done

if [ -z "$DRIVE" ]; then
    printf 'error: name the image to boot.\n\nusage: %s [options] DRIVE.hdf\n' \
        "${BASH_SOURCE[0]##*/}" >&2
    printf '       --help for the options\n' >&2
    # Easier than making the user go and look.
    if [ -d "$REPO/images/hd" ]; then
        found=$(find "$REPO/images/hd" -maxdepth 3 -name '*.hdf' 2>/dev/null | sort)
        if [ -n "$found" ]; then
            printf '\nAvailable under images/hd:\n' >&2
            printf '%s\n' "$found" | sed "s|^$REPO/|  |" >&2
        fi
    fi
    exit 2
fi

# --ui hands off to the Launcher and never runs the emulator, so it does not need the emulator
# binary -- but it does need the Launcher.
if [ "$UI" -eq 1 ]; then
    [ -d "$LAUNCHER" ] || die "FS-UAE Launcher not found at $LAUNCHER (set AMIBUILDER_FSUAE_LAUNCHER)"
else
    [ -x "$FSUAE" ] || die "FS-UAE not found at $FSUAE (set AMIBUILDER_FSUAE)"
fi

if [ ! -f "$ROM" ]; then
    printf 'error: Kickstart not found at %s\n' "$ROM" >&2
    printf '       Name one with --rom, or set AMIBUILDER_KICKSTART.\n' >&2
    list_roms
    exit 2
fi
[ -f "$DRIVE" ] || die "$DRIVE does not exist"

# A ROM and a model that disagree do not boot, and the symptom looks like a corrupt image rather
# than a mismatched pair. When --rom names a ROM whose filename identifies a machine and --model
# was not given, follow the ROM -- and say so, because a silently changed model is worse than a
# wrong one.
if [ -n "$ROM_GIVEN" ] && [ "$MODEL_GIVEN" -eq 0 ]; then
    inferred="$(rom_model_for "$(basename "$ROM")")"
    if [ -n "$inferred" ] && [ "$inferred" != "$MODEL" ]; then
        MODEL="$inferred"
        MODEL_FROM_ROM="$(basename "$ROM")"
    fi
fi

# Naming disks and sweeping a directory are different intentions; together they mostly produce a list
# nobody predicted, and the 20-entry ceiling makes the result depend on which won the race.
if [ ${#ADD_ADF[@]} -gt 0 ] && [ -n "$FLOPPY_PATH" ]; then
    die "--add-adf and --floppy-path do the same job two different ways, so only one is allowed.
Use --add-adf to name disks, or --floppy-path to offer a whole directory.
--drive-N-adf combines with either."
fi

[ -z "$FLOPPY_PATH" ] || [ -d "$FLOPPY_PATH" ] || die "--floppy-path: $FLOPPY_PATH is not a directory"

slot=0
while [ "$slot" -lt "$MAX_DRIVES" ]; do
    adf="${DRIVE_ADF[$slot]}"
    [ -z "$adf" ] || [ -f "$adf" ] || die "--drive-$slot-adf: $adf does not exist"
    slot=$((slot + 1))
done

for adf in ${ADD_ADF[@]+"${ADD_ADF[@]}"}; do
    [ -f "$adf" ] || die "--add-adf: $adf does not exist"
done

if [ "${#EXTRA_HD[@]}" -gt "$MAX_EXTRA_HD" ]; then
    die "at most $MAX_EXTRA_HD extra drive(s); the boot drive occupies hard_drive_0"
fi
for hd in ${EXTRA_HD[@]+"${EXTRA_HD[@]}"}; do
    # A directory is legal here -- FS-UAE mounts one as a volume -- so this accepts either.
    [ -e "$hd" ] || die "--extra-hd: $hd does not exist"
    # Attaching the boot drive twice would have the Amiga mount two volumes with the same name, which
    # AmigaDOS resolves unpredictably. Cheaper to refuse than to explain later.
    [ "$(abspath "$hd")" != "$(abspath "$DRIVE")" ] \
        || die "--extra-hd names the boot drive; that would mount the same volumes twice"
done

# ---------------------------------------------------------------------------
# The swap list
#
# Order: inserted disks first so their position is predictable and they survive truncation, then
# --add-adf in the order given, then a --floppy-path directory alphabetically. Duplicates are skipped,
# so naming a disk that is also in the directory lists it once.
#
# bash 3.2 is what macOS ships, so the empty-array expansions below are guarded -- "${arr[@]}" on an
# empty array is an unbound-variable error there under `set -u`.
# ---------------------------------------------------------------------------
SWAP=()

swap_has() {
    local want="$1" have
    for have in ${SWAP[@]+"${SWAP[@]}"}; do
        [ "$have" = "$want" ] && return 0
    done
    return 1
}

swap_add() {
    local abs
    abs="$(abspath "$1")"
    swap_has "$abs" || SWAP+=("$abs")
}

# Inserted disks first, in drive order, so they always survive truncation.
slot=0
INSERTED=0
while [ "$slot" -lt "$MAX_DRIVES" ]; do
    if [ -n "${DRIVE_ADF[$slot]}" ]; then
        swap_add "${DRIVE_ADF[$slot]}"
        INSERTED=$((INSERTED + 1))
    fi
    slot=$((slot + 1))
done

# Then anything named with --add-adf, in the order given. Deduplicated against the inserted disks, so
# naming one that is also in a drive lists it once.
for adf in ${ADD_ADF[@]+"${ADD_ADF[@]}"}; do
    swap_add "$adf"
done

if [ -n "$FLOPPY_PATH" ]; then
    # find|sort rather than a glob: no nullglob to worry about, and -iname catches the uppercase
    # .ADF that Amiga media so often arrives as.
    while IFS= read -r found_adf; do
        [ -n "$found_adf" ] || continue
        swap_add "$found_adf"
    done < <(find "$FLOPPY_PATH" -maxdepth 1 -type f \
                  \( -iname '*.adf' -o -iname '*.adz' \) 2>/dev/null | sort)
fi

TOTAL=${#SWAP[@]}
if [ "$TOTAL" -gt "$SWAP_LIMIT" ]; then
    printf 'warning: %d floppies but FS-UAE allows only %d in the swap list.\n' \
        "$TOTAL" "$SWAP_LIMIT" >&2
    printf '         Keeping the first %d. Dropped:\n' "$SWAP_LIMIT" >&2
    index=$SWAP_LIMIT
    while [ "$index" -lt "$TOTAL" ]; do
        printf '           %s\n' "$(basename "${SWAP[$index]}")" >&2
        index=$((index + 1))
    done
    printf '         The list is one global list of %d, not one per drive, so it cannot be paged\n' \
        "$SWAP_LIMIT" >&2
    printf '         across DF0-DF%d. What does raise the ceiling: an inserted disk need not be\n' \
        "$((MAX_DRIVES - 1))" >&2
    printf '         in the list, so --drive-0-adf..--drive-%d-adf reach %d more, and anything\n' \
        "$((MAX_DRIVES - 1))" "$MAX_DRIVES" >&2
    printf '         named that way is placed first and always survives truncation. --add-adf\n' >&2
    printf '         names disks for the list explicitly, in your own order.\n' >&2
fi

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
# --ui copies the config somewhere permanent before exiting, so the temp file goes either way.
trap 'rm -f "$CONF"' EXIT

{
    printf '[config]\n'
    printf 'amiga_model = %s\n' "$MODEL"
    printf 'kickstart_file = %s\n' "$ROM"
    printf 'chip_memory = 2048\n'
    printf 'fast_memory = 8192\n'
    printf 'hard_drive_0 = %s\n' "$(abspath "$TARGET")"

    # Attached as-is, never cloned: see the header. Slot 1 upward, since slot 0 is the boot drive.
    hd_slot=1
    for hd in ${EXTRA_HD[@]+"${EXTRA_HD[@]}"}; do
        printf 'hard_drive_%d = %s\n' "$hd_slot" "$(abspath "$hd")"
        hd_slot=$((hd_slot + 1))
    done

    # FS-UAE decides how many drives to emulate from the highest floppy_drive_N configured, so
    # writing these only when asked for is what keeps a driveless boot driveless.
    slot=0
    while [ "$slot" -lt "$MAX_DRIVES" ]; do
        if [ -n "${DRIVE_ADF[$slot]}" ]; then
            printf 'floppy_drive_%d = %s\n' "$slot" "$(abspath "${DRIVE_ADF[$slot]}")"
        fi
        slot=$((slot + 1))
    done

    if [ -n "$FLOPPY_PATH" ]; then
        # Only a search path for relative names -- it populates nothing. Set so the F12 browser
        # opens in the right place.
        printf 'floppies_dir = %s\n' "$(abspath "$FLOPPY_PATH")"
    fi

    index=0
    for entry in ${SWAP[@]+"${SWAP[@]}"}; do
        [ "$index" -lt "$SWAP_LIMIT" ] || break
        printf 'floppy_image_%d = %s\n' "$index" "$entry"
        index=$((index + 1))
    done

    printf 'window_width = 960\n'
    printf 'window_height = 720\n'
    # Visible, unlike the test harness -- there is a human driving this one.
    printf 'fullscreen = 0\n'
    # Written in both states rather than relying on FS-UAE's default, so the generated config states
    # the intent and a test can assert on it. Cmd+W toggles it live whatever is set here.
    printf 'warp_mode = %d\n' "$WARP"
} > "$CONF"

slot=0
while [ "$slot" -lt "$MAX_DRIVES" ]; do
    if [ -n "${DRIVE_ADF[$slot]}" ]; then
        printf 'DF%d:             %s\n' "$slot" "$(basename "${DRIVE_ADF[$slot]}")"
    fi
    slot=$((slot + 1))
done
if [ "$TOTAL" -gt 0 ]; then
    shown=$TOTAL
    [ "$shown" -le "$SWAP_LIMIT" ] || shown=$SWAP_LIMIT
    printf 'swap list:       %d floppy(ies)' "$shown"
    # The reachable count is what actually matters, and it is not the list length: an inserted disk
    # outside the list still counts, and one inside it is not counted twice.
    if [ "$TOTAL" -gt "$SWAP_LIMIT" ]; then
        printf ' of %d found (%d dropped)' "$TOTAL" "$((TOTAL - SWAP_LIMIT))"
    fi
    printf '\n'
fi

hd_slot=1
for hd in ${EXTRA_HD[@]+"${EXTRA_HD[@]}"}; do
    printf 'extra drive %d:   %s  (attached as-is, NOT cloned -- writes land on the host)\n' \
        "$hd_slot" "$hd"
    hd_slot=$((hd_slot + 1))
done

printf 'model:           %s' "$MODEL"
if [ -n "$MODEL_FROM_ROM" ]; then
    printf '  (from the ROM name %s; --model overrides)' "$MODEL_FROM_ROM"
fi
printf '\n'
printf 'kickstart:       %s\n' "$(basename "$ROM")"

if [ "$WARP" -eq 1 ]; then
    printf 'warp mode:       ON -- no audio, choppy display, one CPU core flat out\n'
else
    printf 'warp mode:       off (--no-warp)\n'
fi

printf '\n  F12    FS-UAE menu'
[ "$TOTAL" -gt 0 ] && printf ' -- swap floppies from the list here'
printf '\n'
if [ "$WARP" -eq 1 ]; then
    printf '  Cmd+W  turn warp OFF before clicking around -- it is on now\n'
else
    printf '  Cmd+W  turn warp on for the long waits\n'
fi
printf '         (a chord, not in the F12 menu; watch for "Warp mode enabled/disabled")\n'
printf '  Cmd+Q  quit\n\n'

# ---------------------------------------------------------------------------
# --ui: hand off to FS-UAE Launcher without starting the emulator
#
# Deliberately NOT `fs-uae-launcher <config>`, which is the obvious approach and does the one thing
# this option exists to avoid: measured 2026-08-20, it spawns `fs-uae` about a second later and
# boots. So the config is installed into the Launcher's own Configurations directory, Settings.ini
# is pointed at it, and the Launcher is opened with no arguments -- which loads and pre-selects it
# and starts nothing.
# ---------------------------------------------------------------------------
if [ "$UI" -eq 1 ]; then
    CONF_DIR="$FSUAE_BASE/Configurations"
    SETTINGS="$FSUAE_BASE/Data/Settings.ini"
    [ -d "$CONF_DIR" ] || die "no FS-UAE Configurations directory at $CONF_DIR.
Run FS-UAE Launcher once so it creates its data directory, or set AMIBUILDER_FSUAE_BASE."

    # Named after the drive so it is obvious in the Launcher's list where it came from, and so a
    # second run on the same drive replaces its own entry rather than accumulating.
    base="$(basename "$TARGET")"
    CONF_NAME="amibuilder-${base%.hdf}"
    INSTALLED="$CONF_DIR/$CONF_NAME.fs-uae"
    cp "$CONF" "$INSTALLED" || die "could not write $INSTALLED"

    if [ -f "$SETTINGS" ]; then
        # Backed up because the next step edits it, and because of the side effect below. Timestamped
        # rather than a single .bak so an earlier good copy is never overwritten by a later bad one.
        BACKUP="$SETTINGS.amibuilder-backup-$(date +%Y%m%d-%H%M%S)"
        cp -p "$SETTINGS" "$BACKUP" || die "could not back up $SETTINGS"

        # Only these two keys. The Launcher reads config_path and loads that file, so rewriting the
        # inline [config] block as well would be redundant -- verified 2026-08-20 by pointing it at a
        # config whose model differed from [config] and watching the Launcher follow the file.
        if grep -q '^config_path' "$SETTINGS"; then
            sed -i '' \
                -e "s|^config_name = .*|config_name = $CONF_NAME|" \
                -e "s|^config_path = .*|config_path = $INSTALLED|" \
                "$SETTINGS"
        else
            printf 'config_name = %s\nconfig_path = %s\n' "$CONF_NAME" "$INSTALLED" >> "$SETTINGS"
        fi
    else
        BACKUP=""
        mkdir -p "$(dirname "$SETTINGS")"
        printf '[settings]\nconfig_name = %s\nconfig_path = %s\n' \
            "$CONF_NAME" "$INSTALLED" > "$SETTINGS"
    fi

    printf 'Handing off to FS-UAE Launcher. The emulator is NOT started.\n\n'
    printf 'config:          %s\n' "$INSTALLED"
    printf 'selected as:     %s\n' "$CONF_NAME"
    [ -z "$BACKUP" ] || printf 'settings backup: %s\n' "$BACKUP"
    printf '\n'
    # Worth stating plainly: it is the Launcher's behaviour, it is not reversible from here, and it
    # surprised us once already.
    printf 'Note: once a NAMED config is selected, FS-UAE Launcher discards the unnamed working\n'
    printf '      config it keeps inline in Settings.ini when it quits. If you had settings there\n'
    printf '      you cared about, they are in the backup above.\n\n'
    printf 'Press Start in the Launcher when ready.\n'

    open -a "$LAUNCHER" || die "could not open $LAUNCHER"
    exit 0
fi

# Suppresses macOS's "reopen windows" requester, which otherwise appears after any abnormal quit and
# stops FS-UAE from starting at all.
exec "$FSUAE" "$CONF" -ApplePersistenceIgnoreState YES
