#!/usr/bin/env bash
#
# Behaviour matrix for boot-hdf.sh. Run it after touching that script:
#
#   utils/scripts/test-boot-hdf.sh
#
# Exists because a one-character regression very nearly shipped: the line that adds a discovered
# floppy to the swap list was commented out, which silently reduced --floppy-path to a no-op. The
# script still exited 0 and still launched, so nothing short of counting the generated config lines
# would have noticed.
#
# Hermetic on purpose. Real floppies and drive images live under images/, which is gitignored because
# it holds licensed software, so a test depending on them would not run for anyone else. boot-hdf.sh
# never parses these files -- it tests existence and reads filename extensions -- so empty files named
# *.adf and *.hdf exercise every path faithfully. FS-UAE is replaced by a stub that captures the
# generated configuration, which is the thing actually under test.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/boot-hdf.sh"
[ -x "$SCRIPT" ] || { printf 'error: %s is not executable\n' "$SCRIPT" >&2; exit 1; }

WORK="$(mktemp -d -t boot-hdf-test)"
trap 'rm -rf "$WORK"' EXIT

FLOPPIES="$WORK/floppies"
CONF="$WORK/captured.conf"
mkdir -p "$FLOPPIES" "$WORK/empty"

# 35 floppies, matching the real 3.2 set closely enough to exercise the 20-image ceiling and the
# alphabetical truncation that goes with it.
for name in Install3.2 Workbench3.2 Extras3.2 Fonts Classes3.2 Storage3.2 Locale \
            ModulesA1200_3.2 ModulesA2000_3.2 ModulesA3000_3.2 ModulesA4000D_3.2 \
            ModulesA4000T_3.2 ModulesA500_3.2 ModulesA600_3.2 ModulesCD32_3.2 \
            GlowIcons3.2 Backdrops3.2 MMULibs HDSetup3.2 DiskDoctor \
            Locale-DE Locale-DK Locale-EN Locale-ES Locale-FR Locale-GR Locale-IT \
            Locale-NL Locale-NO Locale-PL Locale-PT Locale-RU Locale-SE Locale-TR Locale-UK; do
    : > "$FLOPPIES/$name.adf"
done
# Uppercase, which Amiga media often arrives as, and which -iname is there to catch.
: > "$FLOPPIES/UPPERCASE.ADF"

DRIVE="$WORK/drive.hdf"
: > "$DRIVE"
ROM="$WORK/fake.rom"
: > "$ROM"

STUB="$WORK/stub-fsuae.sh"
cat > "$STUB" <<'STUBEOF'
#!/bin/sh
cp "$1" "$CAPTURE_TO" 2>/dev/null
exit 0
STUBEOF
chmod +x "$STUB"

export AMIBUILDER_FSUAE="$STUB"
export AMIBUILDER_KICKSTART="$ROM"
export CAPTURE_TO="$CONF"

M="$FLOPPIES"
fails=0
count=0

# `grep -c` PRINTS the count and EXITS 1 when the count is zero, so `|| echo 0` would emit a second
# zero and break the comparison. Ignore the status; an empty result means the file is missing.
count_lines() {
    local n
    n=$(grep -c "$1" "$CONF" 2>/dev/null || true)
    [ -n "$n" ] || n=0
    printf '%s\n' "$n"
}

check() {  # label want_images want_drives -- then args, drive appended
    local label="$1" want_img="$2" want_drv="$3"; shift 3
    count=$((count + 1))
    rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
    "$SCRIPT" "$@" "$DRIVE" >/dev/null 2>/dev/null
    local rc=$? img drv
    img=$(count_lines '^floppy_image_')
    drv=$(count_lines '^floppy_drive_')
    if [ "$rc" -eq 0 ] && [ "$img" -eq "$want_img" ] && [ "$drv" -eq "$want_drv" ]; then
        printf 'PASS  %-42s images=%-2s drives=%s\n' "$label" "$img" "$drv"
    else
        printf 'FAIL  %-42s images=%s (want %s) drives=%s (want %s) rc=%s\n' \
            "$label" "$img" "$want_img" "$drv" "$want_drv" "$rc"
        fails=$((fails + 1))
    fi
}

refuse() {  # label -- then args; the drive is NOT appended, so cases can omit it
    local label="$1"; shift
    count=$((count + 1))
    rm -f "${DRIVE%.hdf}-booted.hdf"
    if "$SCRIPT" "$@" >/dev/null 2>&1; then
        printf 'FAIL  %-42s should have been refused\n' "$label"
        fails=$((fails + 1))
    else
        printf 'PASS  %-42s refused\n' "$label"
    fi
}

first_image_is() {  # label expected-basename -- then args
    local label="$1" want="$2"; shift 2
    count=$((count + 1))
    rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
    "$SCRIPT" "$@" "$DRIVE" >/dev/null 2>/dev/null
    local got
    got=$(grep '^floppy_image_0 ' "$CONF" 2>/dev/null | sed 's|.*/||')
    if [ "$got" = "$want" ]; then
        printf 'PASS  %-42s floppy_image_0=%s\n' "$label" "$got"
    else
        printf 'FAIL  %-42s floppy_image_0=%s (want %s)\n' "$label" "${got:-<none>}" "$want"
        fails=$((fails + 1))
    fi
}

printf -- '--- floppy configuration ---\n'
check "no floppy options"                     0  0
check "one inserted"                          1  1 --drive-0-adf=$M/Fonts.adf
check "four inserted"                         4  4 --drive-0-adf=$M/Fonts.adf \
                                                   --drive-1-adf=$M/Extras3.2.adf \
                                                   --drive-2-adf=$M/Classes3.2.adf \
                                                   --drive-3-adf=$M/Locale.adf
check "sparse: only DF3 given"                1  1 --drive-3-adf=$M/Fonts.adf
check "add-adf, three disks"                  3  0 --add-adf=$M/Extras3.2.adf \
                                                   --add-adf=$M/Fonts.adf \
                                                   --add-adf=$M/Classes3.2.adf
check "add-adf, space-separated form"         1  0 --add-adf $M/Fonts.adf
check "drive-0-adf, space-separated form"     1  1 --drive-0-adf $M/Fonts.adf
check "inserted plus add-adf"                 3  1 --drive-0-adf=$M/Install3.2.adf \
                                                   --add-adf=$M/Workbench3.2.adf \
                                                   --add-adf=$M/Extras3.2.adf
check "dedup: inserted disk also in add-adf"  1  1 --drive-0-adf=$M/Fonts.adf \
                                                   --add-adf=$M/Fonts.adf
check "floppy-path finds 36, caps at 20"     20  0 --floppy-path=$M
check "floppy-path plus inserted"            20  1 --drive-0-adf=$M/Workbench3.2.adf \
                                                   --floppy-path=$M
check "floppy-path on an empty directory"     0  0 --floppy-path=$WORK/empty

printf -- '\n--- warp mode ---\n'

config_says() {  # label expected-config-line -- then args
    local label="$1" want="$2"; shift 2
    count=$((count + 1))
    rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
    "$SCRIPT" "$@" "$DRIVE" >/dev/null 2>/dev/null
    if grep -qxF "$want" "$CONF" 2>/dev/null; then
        printf 'PASS  %-42s %s\n' "$label" "$want"
    else
        printf 'FAIL  %-42s want %s, got: %s\n' "$label" "$want" \
            "$(grep '^warp_mode' "$CONF" 2>/dev/null || echo '<absent>')"
        fails=$((fails + 1))
    fi
}

# On by default is the whole point; asserting the explicit 0 too means a future refactor cannot
# quietly drop the line and leave FS-UAE's own default deciding.
config_says "warp is on by default"      'warp_mode = 1'
config_says "--no-warp turns it off"     'warp_mode = 0' --no-warp
config_says "--warp is explicit on"      'warp_mode = 1' --warp
config_says "last flag wins: on->off"    'warp_mode = 0' --warp --no-warp
config_says "last flag wins: off->on"    'warp_mode = 1' --no-warp --warp

printf -- '\n--- ordering ---\n'
# The whole point of placing inserted disks first: over-limit directories drop alphabetically, so a
# disk you named must not be a casualty.
first_image_is "inserted disk survives truncation" "Workbench3.2.adf" \
    --drive-0-adf=$M/Workbench3.2.adf --floppy-path=$M
first_image_is "add-adf keeps the given order" "Fonts.adf" \
    --add-adf=$M/Fonts.adf --add-adf=$M/Extras3.2.adf

printf -- '\n--- refusals ---\n'
refuse "add-adf together with floppy-path" --add-adf=$M/Fonts.adf --floppy-path=$M "$DRIVE"
refuse "drive index out of range"          --drive-4-adf=$M/Fonts.adf "$DRIVE"
refuse "add-adf naming a missing file"     --add-adf=$WORK/nope.adf "$DRIVE"
refuse "drive-0-adf naming a missing file" --drive-0-adf=$WORK/nope.adf "$DRIVE"
refuse "floppy-path naming a missing dir"  --floppy-path=$WORK/nodir "$DRIVE"
refuse "unknown option"                    --nonsense "$DRIVE"
refuse "two drives named"                  "$DRIVE" "$DRIVE"
refuse "no drive named"
refuse "missing drive image"               "$WORK/absent.hdf"

printf -- '\n--- the copy-before-boot default ---\n'
count=$((count + 1))
rm -f "${DRIVE%.hdf}-booted.hdf"
printf 'original' > "$DRIVE"
"$SCRIPT" "$DRIVE" >/dev/null 2>/dev/null
if [ -f "${DRIVE%.hdf}-booted.hdf" ] && [ "$(cat "$DRIVE")" = "original" ]; then
    printf 'PASS  %-42s clone made, original intact\n' "default boots a copy"
else
    printf 'FAIL  %-42s\n' "default boots a copy"
    fails=$((fails + 1))
fi

count=$((count + 1))
rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
"$SCRIPT" --in-place "$DRIVE" >/dev/null 2>/dev/null
if [ ! -f "${DRIVE%.hdf}-booted.hdf" ] && grep -q "hard_drive_0 = $DRIVE\$" "$CONF" 2>/dev/null; then
    printf 'PASS  %-42s no clone, config names the original\n' "--in-place uses the real file"
else
    printf 'FAIL  %-42s\n' "--in-place uses the real file"
    fails=$((fails + 1))
fi

# A RELATIVE input is the only way to test this. An earlier version of this check passed an absolute
# path -- mktemp -d always yields one -- so abspath was a no-op and the assertion could not fail.
# Mutation testing caught it: deleting the abspath call left the suite green.
count=$((count + 1))
rm -f "$CONF" "$WORK/relative-booted.hdf"
: > "$WORK/relative.hdf"
( cd "$WORK" && "$SCRIPT" --in-place relative.hdf ) >/dev/null 2>/dev/null
if grep -q '^hard_drive_0 = /' "$CONF" 2>/dev/null; then
    printf 'PASS  %-42s relative input became absolute\n' "config paths are absolute"
else
    printf 'FAIL  %-42s got: %s\n' "config paths are absolute" \
        "$(grep '^hard_drive_0' "$CONF" 2>/dev/null)"
    fails=$((fails + 1))
fi

# Same for a floppy, which travels through a different call site.
count=$((count + 1))
rm -f "$CONF"
( cd "$WORK" && "$SCRIPT" --in-place --drive-0-adf=floppies/Fonts.adf relative.hdf ) \
    >/dev/null 2>/dev/null
if grep -q '^floppy_drive_0 = /' "$CONF" 2>/dev/null; then
    printf 'PASS  %-42s relative input became absolute\n' "floppy paths are absolute"
else
    printf 'FAIL  %-42s got: %s\n' "floppy paths are absolute" \
        "$(grep '^floppy_drive_0' "$CONF" 2>/dev/null)"
    fails=$((fails + 1))
fi

printf '\n%d checks, ' "$count"
if [ "$fails" -eq 0 ]; then printf 'all passed\n'; else printf '%d FAILED\n' "$fails"; fi
exit $(( fails > 0 ? 1 : 0 ))
