#!/usr/bin/env bash
#
# Build a plain HDF from a directory of host files, to hand files to an Amiga.
#
# The gap this fills: amibuilder can read an image and can compose one from layers, but it has no way
# to put a host file into an image -- `put`/`cp` are Phase 4 and unbuilt. So there is no route from
# "a .lha on my Mac" to "a file on the Amiga". A transfer drive is that route: build one here, attach
# it as a second hard drive with boot-hdf.sh --extra-hd, and copy across from the Amiga side.
#
# Why a hard drive rather than an ADF: an ADF holds 880 KB, and a patch set or a game usually does not.
# A plain HDF can be any size and needs no swapping.
#
# **It is not cloned when attached, so it is a two-way channel.** Whatever the Amiga writes to it is
# there on the host afterwards, which is a convenient way to get files back out.
#
# Usage:
#   utils/scripts/make-transfer-hdf.sh SOURCE_DIR OUTPUT.hdf [options]
#
#   --size SIZE     image size, e.g. 20M or 1G. Default: derived from the source, with slack.
#   --volume NAME   Amiga volume name. Default: Transfer
#   --dostype TYPE  ffs+intl (default) or ffs. intl matters for filenames with accents.
#
# The contents of SOURCE_DIR land at the volume root, so the directory itself is not reproduced as a
# level of nesting.
#
# An existing OUTPUT.hdf is never overwritten, for the same reason `amibuilder init` refuses: the whole
# premise is that the target is new.
#
# Uses amitools' xdftool directly, since that is the only thing here that can write host files into an
# image. The result is validated with `amibuilder check` before being reported as usable.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
XDFTOOL="$REPO/.venv/bin/xdftool"
AMIBUILDER="$REPO/.venv/bin/amibuilder"

VOLUME="Transfer"
DOSTYPE="ffs+intl"
SIZE=""
SRC=""
OUT=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --size=*)     SIZE="${1#*=}" ;;
        --size)       shift; SIZE="${1:-}" ;;
        --volume=*)   VOLUME="${1#*=}" ;;
        --volume)     shift; VOLUME="${1:-}" ;;
        --dostype=*)  DOSTYPE="${1#*=}" ;;
        --dostype)    shift; DOSTYPE="${1:-}" ;;
        -h|--help)    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' \
                          "${BASH_SOURCE[0]}"; exit 0 ;;
        -*)           die "unknown option $1" ;;
        *)            if [ -z "$SRC" ]; then SRC="$1"
                      elif [ -z "$OUT" ]; then OUT="$1"
                      else die "unexpected argument $1"; fi ;;
    esac
    shift
done

[ -n "$SRC" ] && [ -n "$OUT" ] || die "usage: ${BASH_SOURCE[0]##*/} SOURCE_DIR OUTPUT.hdf [options]"
[ -d "$SRC" ] || die "$SRC is not a directory"
[ ! -e "$OUT" ] || die "$OUT already exists, and this never overwrites. Delete it first."
[ -x "$XDFTOOL" ] || die "xdftool not found at $XDFTOOL"

# Anything at the top level of SRC. Refusing an empty source beats producing a blank image that looks
# like it worked.
entries=()
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    entries+=("$entry")
done < <(find "$SRC" -mindepth 1 -maxdepth 1 | sort)
[ "${#entries[@]}" -gt 0 ] || die "$SRC is empty; nothing to transfer"

if [ -z "$SIZE" ]; then
    # Derive a size: content, plus about a kilobyte per file for FFS overhead (measured at roughly
    # 760-810 bytes per file on real installs), plus half again as slack, with a 2 MiB floor. Being
    # generous costs nothing -- the image is sparse until written.
    bytes=$(find "$SRC" -type f -exec stat -f '%z' {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')
    nfiles=$(find "$SRC" -type f | wc -l | tr -d ' ')
    want=$(( (bytes + nfiles * 1024) * 3 / 2 ))
    [ "$want" -ge $((2 * 1024 * 1024)) ] || want=$((2 * 1024 * 1024))
    SIZE="$(( (want + 1024 * 1024 - 1) / (1024 * 1024) ))M"
    printf 'source:          %s bytes in %s file(s)\n' "$bytes" "$nfiles"
fi

printf 'building:        %s (%s, volume %s:, %s)\n' "$OUT" "$SIZE" "$VOLUME" "$DOSTYPE"

# One xdftool invocation: create, format, then a write per top-level entry. Chained with '+' because
# each command in a list operates on the image left open by the previous one.
cmd=("$XDFTOOL" "$OUT" create "size=$SIZE" + format "$VOLUME" "$DOSTYPE")
for entry in "${entries[@]}"; do
    cmd+=(+ write "$entry" "$(basename "$entry")")
done

if ! "${cmd[@]}"; then
    # A partial image looks usable and is not -- same reasoning as `amibuilder init`.
    rm -f "$OUT"
    die "xdftool failed; removed the partial image"
fi

# Validate with the reader, not the writer. xdftool reporting success is not evidence that what it
# wrote is structurally sound.
if [ -x "$AMIBUILDER" ]; then
    if ! "$AMIBUILDER" check "$OUT"; then
        rm -f "$OUT"
        die "the image did not validate; removed it"
    fi
    "$AMIBUILDER" tree "$OUT" || true
fi

printf '\nAttach it with:\n'
printf '  utils/scripts/boot-hdf.sh --extra-hd=%s DRIVE.hdf\n' "$OUT"
printf 'It mounts as %s: and is NOT cloned, so anything the Amiga writes to it lands here.\n' "$VOLUME"
