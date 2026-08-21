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

printf -- '\n--- extra hard drives ---\n'

: > "$WORK/transfer.hdf"
: > "$WORK/second.hdf"
: > "$WORK/third.hdf"
: > "$WORK/fourth.hdf"

hd_lines() {  # label expected-count -- then args
    local label="$1" want="$2"; shift 2
    count=$((count + 1))
    rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
    "$SCRIPT" "$@" "$DRIVE" >/dev/null 2>/dev/null
    local got
    got=$(grep -c '^hard_drive_[1-9]' "$CONF" 2>/dev/null || true); [ -n "$got" ] || got=0
    if [ "$got" -eq "$want" ]; then
        printf 'PASS  %-42s extra drives=%s\n' "$label" "$got"
    else
        printf 'FAIL  %-42s extra drives=%s (want %s)\n' "$label" "$got" "$want"
        fails=$((fails + 1))
    fi
}

hd_lines "none by default"          0
hd_lines "one extra drive"          1 --extra-hd=$WORK/transfer.hdf
hd_lines "three extra drives"       3 --extra-hd=$WORK/transfer.hdf \
                                      --extra-hd=$WORK/second.hdf \
                                      --extra-hd=$WORK/third.hdf
hd_lines "--extra-hd space-separated form" 1 --extra-hd $WORK/transfer.hdf

# Slot 0 is the boot drive, so extras must start at 1 -- overwriting slot 0 would silently replace the
# drive under test with the transfer drive.
count=$((count + 1))
rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
"$SCRIPT" --extra-hd=$WORK/transfer.hdf "$DRIVE" >/dev/null 2>/dev/null
if grep -q '^hard_drive_1 = ' "$CONF" 2>/dev/null \
   && [ "$(grep -c '^hard_drive_0 = ' "$CONF")" -eq 1 ]; then
    printf 'PASS  %-42s extras start at slot 1\n' "boot drive keeps slot 0"
else
    printf 'FAIL  %-42s\n' "boot drive keeps slot 0"
    fails=$((fails + 1))
fi

# Not cloned, deliberately: it is a two-way channel, and cloning would discard whatever the Amiga
# wrote to it.
count=$((count + 1))
printf 'payload' > "$WORK/transfer.hdf"
rm -f "$CONF" "$WORK/transfer-booted.hdf"
"$SCRIPT" --extra-hd=$WORK/transfer.hdf "$DRIVE" >/dev/null 2>/dev/null
if [ ! -e "$WORK/transfer-booted.hdf" ] \
   && grep -q "^hard_drive_1 = $WORK/transfer.hdf\$" "$CONF" 2>/dev/null; then
    printf 'PASS  %-42s no clone, config names the original\n' "extra drive is not cloned"
else
    printf 'FAIL  %-42s\n' "extra drive is not cloned"
    fails=$((fails + 1))
fi

refuse "four extra drives"          --extra-hd=$WORK/transfer.hdf --extra-hd=$WORK/second.hdf \
                                    --extra-hd=$WORK/third.hdf --extra-hd=$WORK/fourth.hdf "$DRIVE"
refuse "extra drive missing"        --extra-hd=$WORK/absent.hdf "$DRIVE"
refuse "extra drive is the boot drive" --extra-hd="$DRIVE" "$DRIVE"

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

printf -- '\n--- ROM and model selection ---\n'

# A fixture ROM directory, so these tests do not need the real one. ROMs are licensed Cloanto files
# and gitignored, so a test reading $REPO/ROMs would only run on a machine that has them.
FAKE_ROMS="$WORK/roms"
mkdir -p "$FAKE_ROMS"
for r in A1200.47.115 A3000.47.115 A4000.47.115 A4000T.47.115 CDTVA500A600A2000.47.115; do
    : > "$FAKE_ROMS/$r.rom"
done
export AMIBUILDER_ROM_DIR="$FAKE_ROMS"

config_line() {  # label key expected-value -- then args
    local label="$1" key="$2" want="$3"; shift 3
    count=$((count + 1))
    rm -f "$CONF" "${DRIVE%.hdf}-booted.hdf"
    "$SCRIPT" "$@" "$DRIVE" >/dev/null 2>/dev/null
    local got
    got=$(grep "^$key = " "$CONF" 2>/dev/null | sed "s|^$key = ||")
    if [ "$got" = "$want" ]; then
        printf 'PASS  %-42s %s=%s\n' "$label" "$key" "$got"
    else
        printf 'FAIL  %-42s %s=%s (want %s)\n' "$label" "$key" "${got:-<none>}" "$want"
        fails=$((fails + 1))
    fi
}

# A bare name is the point of the feature: nobody wants to type the whole path to a ROM they
# already put in the ROMs directory.
config_line "bare ROM name resolves in ROMs/" kickstart_file "$FAKE_ROMS/A1200.47.115.rom" \
    --rom=A1200.47.115
config_line "bare name with .rom suffix"      kickstart_file "$FAKE_ROMS/A1200.47.115.rom" \
    --rom=A1200.47.115.rom
config_line "a path is used as given"         kickstart_file "$ROM" --rom="$ROM"
config_line "--rom space-separated form"       kickstart_file "$FAKE_ROMS/A3000.47.115.rom" \
    --rom A3000.47.115
config_line "default when --rom is absent"    kickstart_file "$ROM"

# The model and the ROM have to agree or nothing boots, and the symptom looks like a corrupt image.
config_line "model defaults to A1200"         amiga_model "A1200"
config_line "--model is honoured"             amiga_model "A4000/040" --model=A4000/040
config_line "model follows an A3000 ROM"      amiga_model "A3000"     --rom=A3000.47.115
config_line "A4000T ROM maps to A4000/040"    amiga_model "A4000/040" --rom=A4000T.47.115
config_line "CDTV/A500 ROM maps to A500"      amiga_model "A500" --rom=CDTVA500A600A2000.47.115
# Inference must never beat an explicit choice, whichever order they are given in.
config_line "--model beats ROM inference"     amiga_model "A1200" --rom=A4000.47.115 --model=A1200
config_line "order does not matter"           amiga_model "A1200" --model=A1200 --rom=A4000.47.115
# An A1200 ROM under the default A1200 model must not report an inference that did not happen.
config_line "matching ROM leaves model alone" amiga_model "A1200" --rom=A1200.47.115

# Asserting the MESSAGE, not just the refusal. A bad --rom name is refused twice over -- once by the
# ROMs-directory lookup and again by the generic "kickstart not found" check -- so a test that only
# looked at the exit code could not tell which fired, and would pass with the lookup removed. The
# distinction is worth keeping: only the lookup message says *where* it searched, which is the one
# useful fact when a bare name did not resolve.
refuse_saying() {  # label expected-substring -- then args
    local label="$1" want="$2"; shift 2
    count=$((count + 1))
    rm -f "${DRIVE%.hdf}-booted.hdf"
    local out
    out=$("$SCRIPT" "$@" 2>&1)
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        printf 'FAIL  %-42s should have been refused\n' "$label"
        fails=$((fails + 1))
    elif printf '%s' "$out" | grep -qF "$want"; then
        printf 'PASS  %-42s refused: %s\n' "$label" "$want"
    else
        printf 'FAIL  %-42s refused but did not say %s\n' "$label" "$want"
        fails=$((fails + 1))
    fi
}

refuse_saying "unknown ROM name" "no such ROM in" --rom=NoSuchRom "$DRIVE"
refuse_saying "unknown ROM lists what exists" "A1200.47.115.rom" --rom=NoSuchRom "$DRIVE"
refuse "--rom without a value" --rom

printf -- '\n--- --ui hands off to the Launcher ---\n'

# `open` is stubbed rather than letting the real one run: this whole option exists to NOT start
# things, and a test that launched a GUI would be neither hermetic nor welcome.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/open" <<'OPENEOF'
#!/bin/sh
printf '%s\n' "$*" >> "$OPEN_LOG"
exit 0
OPENEOF
chmod +x "$WORK/bin/open"
export PATH="$WORK/bin:$PATH"
export OPEN_LOG="$WORK/open.log"

FAKE_BASE="$WORK/fsuae-base"
mkdir -p "$FAKE_BASE/Configurations" "$FAKE_BASE/Data"
FAKE_SETTINGS="$FAKE_BASE/Data/Settings.ini"
export AMIBUILDER_FSUAE_BASE="$FAKE_BASE"
export AMIBUILDER_FSUAE_LAUNCHER="$WORK/FakeLauncher.app"
mkdir -p "$AMIBUILDER_FSUAE_LAUNCHER"

ui_run() {  # resets state, then runs --ui with the given args
    rm -f "$CONF" "$OPEN_LOG" "${DRIVE%.hdf}-booted.hdf"
    rm -f "$FAKE_BASE/Configurations"/*.fs-uae 2>/dev/null
    printf '[settings]\nconfig_name = Unnamed Configuration\nconfig_path = %s/Unnamed.fs-uae\n' \
        "$FAKE_BASE/Configurations" > "$FAKE_SETTINGS"
    "$SCRIPT" --ui "$@" "$DRIVE" >/dev/null 2>&1
}

ok() {  # label condition-already-evaluated
    count=$((count + 1))
    if [ "$1" = "0" ]; then
        printf 'PASS  %-42s %s\n' "$2" "$3"
    else
        printf 'FAIL  %-42s %s\n' "$2" "$3"
        fails=$((fails + 1))
    fi
}

ui_run
INSTALLED="$FAKE_BASE/Configurations/amibuilder-drive-booted.fs-uae"
[ -f "$INSTALLED" ]; ok $? "config installed for the Launcher" "amibuilder-drive-booted.fs-uae"

# The decisive one. --ui must not run the emulator; the stub would have written $CONF if it had.
[ ! -f "$CONF" ]; ok $? "the emulator is NOT started" "FS-UAE stub never invoked"

grep -q 'FakeLauncher.app' "$OPEN_LOG" 2>/dev/null
ok $? "the Launcher is opened" "open -a ...FakeLauncher.app"

# Passing the config on the Launcher's command line would make it boot immediately -- measured
# 2026-08-20 -- so the handoff must go through Settings.ini instead.
grep -q '\.fs-uae' "$OPEN_LOG" 2>/dev/null
if [ $? -eq 0 ]; then ok 1 "no config on the Launcher's argv" "a config path was passed"
else ok 0 "no config on the Launcher's argv" "opened with no config argument"; fi

grep -q "^config_path = $INSTALLED\$" "$FAKE_SETTINGS" 2>/dev/null
ok $? "Settings.ini points at the config" "config_path rewritten"

grep -q '^config_name = amibuilder-drive-booted$' "$FAKE_SETTINGS" 2>/dev/null
ok $? "Settings.ini names the config" "config_name rewritten"

ls "$FAKE_SETTINGS".amibuilder-backup-* >/dev/null 2>&1
ok $? "Settings.ini is backed up first" "timestamped backup written"

# The config the Launcher gets must be the same one the emulator would have had, or --ui silently
# configures something different from what it says.
ui_run --rom=A4000.47.115 --extra-hd="$WORK/transfer.hdf"
grep -q "^kickstart_file = $FAKE_ROMS/A4000.47.115.rom\$" "$INSTALLED" 2>/dev/null
ok $? "--ui config carries the ROM" "kickstart_file present"
grep -q '^hard_drive_1 = ' "$INSTALLED" 2>/dev/null
ok $? "--ui config carries extra drives" "hard_drive_1 present"

# A missing Configurations directory means the Launcher has never run. Writing one blindly would
# leave a config the Launcher may never index, so refuse and say why.
rm -rf "$FAKE_BASE/Configurations"
"$SCRIPT" --ui "$DRIVE" >/dev/null 2>&1
ok $([ $? -ne 0 ] && echo 0 || echo 1) "no Configurations dir is refused" "refused"
mkdir -p "$FAKE_BASE/Configurations"

unset AMIBUILDER_FSUAE_BASE AMIBUILDER_FSUAE_LAUNCHER AMIBUILDER_ROM_DIR

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
