#!/usr/bin/env bash
#
# Behaviour matrix for install-wb.sh. Run it after touching that script:
#
#   utils/scripts/test-install-wb.sh
#
# Hermetic, the same way test-boot-hdf.sh is. Real floppies live under images/, which is gitignored
# because it holds licensed software, so a test depending on them would not run for anyone else.
# install-wb.sh never parses an ADF -- it tests existence and builds filenames -- so empty files
# named *.adf exercise every path faithfully.
#
# The launch itself is checked through --dry-run, which prints the boot-hdf.sh command line it would
# exec. That is the actual contract between the two scripts, so asserting on it is asserting the
# thing that matters: which disk lands in DF0, that the install writes in place, that turbo floppy is
# on and warp off, and that the Modules disk follows the model.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/install-wb.sh"
[ -x "$SCRIPT" ] || { printf 'error: %s is not executable\n' "$SCRIPT" >&2; exit 1; }

WORK="$(mktemp -d -t install-wb-test)"
trap 'rm -rf "$WORK"' EXIT

MEDIA_ROOT="$WORK/media"
DRIVE="$WORK/drive.hdf"
: > "$DRIVE"

# A complete 3.2 set, and a 3.1 directory with no manifest, and a 3.2.1 to exercise version sorting.
mkdir -p "$MEDIA_ROOT/3.2" "$MEDIA_ROOT/3.1"
for name in Install3.2 Workbench3.2 Locale Extras3.2 Fonts Storage3.2 Classes3.2 \
            ModulesA1200_3.2 ModulesA500_3.2 ModulesA4000D_3.2 ModulesCD32_3.2 \
            GlowIcons3.2 Backdrops3.2 MMULibs HDSetup3.2 DiskDoctor Locale-EN Locale-UK \
            Locale-DE Locale-FR; do
    : > "$MEDIA_ROOT/3.2/$name.adf"
done
: > "$MEDIA_ROOT/3.1/Workbench.adf"

export AMIBUILDER_MEDIA_ROOT="$MEDIA_ROOT"

# A stub emulator and a fixture ROM, so nothing here can start FS-UAE for real. This is not
# belt-and-braces: a `refuse` case that stops refusing would fall through to the launch, and the
# emulator is installed on the machine this runs on. A test suite must not be able to open a window.
STUB="$WORK/stub-fsuae.sh"
printf '#!/bin/sh\nexit 0\n' > "$STUB"
chmod +x "$STUB"
: > "$WORK/fake.rom"
export AMIBUILDER_FSUAE="$STUB"
export AMIBUILDER_KICKSTART="$WORK/fake.rom"

fails=0
count=0

# stdin from /dev/null throughout. install-wb.sh only prompts on a TTY, but a test suite that can
# HANG instead of failing is worse than one that fails: this suite did exactly that once, when a
# mutation removed the missing-disk check and the script sailed on to its confirmation prompt.
run_dry() { bash "$SCRIPT" --dry-run "$@" < /dev/null 2>&1; }

# --- assertion helpers -----------------------------------------------------

says() {  # label needle -- then args
    local label="$1" needle="$2"; shift 2
    count=$((count + 1))
    local out
    out="$(run_dry "$@")"
    if printf '%s' "$out" | grep -qF -- "$needle"; then
        printf 'PASS  %-46s %s\n' "$label" "$needle"
    else
        printf 'FAIL  %-46s want %s\n' "$label" "$needle"
        printf '      got: %s\n' "$(printf '%s' "$out" | tr '\n' '|' | cut -c1-220)"
        fails=$((fails + 1))
    fi
}

says_not() {  # label needle -- then args
    local label="$1" needle="$2"; shift 2
    count=$((count + 1))
    local out
    out="$(run_dry "$@")"
    if printf '%s' "$out" | grep -qF -- "$needle"; then
        printf 'FAIL  %-46s must NOT contain %s\n' "$label" "$needle"
        fails=$((fails + 1))
    else
        printf 'PASS  %-46s no %s\n' "$label" "$needle"
    fi
}

# Two needles, BOTH required. Exists because a single loose needle can be satisfied by boot-hdf.sh
# refusing downstream rather than by install-wb.sh's own check -- which is a different thing, and
# mutation-testing caught exactly that. Pairing the disk/drive name with install-wb.sh's own framing
# pins the refusal to this script.
refuse_both() {  # label needle1 needle2 -- then args
    local label="$1" one="$2" two="$3"; shift 3
    count=$((count + 1))
    local out rc
    out="$(bash "$SCRIPT" "$@" < /dev/null 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        printf 'FAIL  %-46s exited 0, should have refused\n' "$label"
        fails=$((fails + 1))
    elif printf '%s' "$out" | grep -qF -- "$one" && printf '%s' "$out" | grep -qF -- "$two"; then
        printf 'PASS  %-46s refused: %s + %s\n' "$label" "$one" "$two"
    else
        printf 'FAIL  %-46s want both %s and %s\n' "$label" "$one" "$two"
        printf '      got: %s\n' "$(printf '%s' "$out" | tr '\n' '|' | cut -c1-220)"
        fails=$((fails + 1))
    fi
}

refuse() {  # label needle -- then args
    local label="$1" needle="$2"; shift 2
    count=$((count + 1))
    local out rc
    out="$(bash "$SCRIPT" "$@" < /dev/null 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        printf 'FAIL  %-46s exited 0, should have refused\n' "$label"
        fails=$((fails + 1))
    elif printf '%s' "$out" | grep -qF -- "$needle"; then
        printf 'PASS  %-46s refused: %s\n' "$label" "$needle"
    else
        printf 'FAIL  %-46s refused but not for %s\n' "$label" "$needle"
        printf '      got: %s\n' "$(printf '%s' "$out" | tr '\n' '|' | cut -c1-220)"
        fails=$((fails + 1))
    fi
}

printf -- '--- the boot-hdf.sh contract ---\n'

# An install must write to the real drive, not boot-hdf.sh's default clone. This is the single most
# important line in the whole handover: without it the install lands in a throwaway copy.
says "installs in place, not into a clone" "--in-place" "$DRIVE"
says "turbo floppy on: a dozen disks at 1980s speed is an hour" "--turbo-floppy" "$DRIVE"
says "warp off: it makes a GUI installer unpleasant" "--no-warp" "$DRIVE"
says "Install disk boots in DF0"    "--drive-0-adf=$MEDIA_ROOT/3.2/Install3.2.adf" "$DRIVE"
says "Workbench disk sits in DF1"   "--drive-1-adf=$MEDIA_ROOT/3.2/Workbench3.2.adf" "$DRIVE"
says "the model is passed through"  "--model=A1200" "$DRIVE"
says "nothing is launched on a dry run" "nothing was launched" "$DRIVE"

# --floppy-path would sweep all 35 disks and truncate alphabetically, dropping Workbench3.2 behind
# 22 Locale-* files. Curation is the whole point, so its absence is load-bearing.
says_not "does not sweep the directory" "--floppy-path" "$DRIVE"

printf -- '\n--- the Modules disk follows the model ---\n'

says "A1200 by default"      "ModulesA1200_3.2" "$DRIVE"
says "A500 selects its own"  "ModulesA500_3.2"  --model=A500 "$DRIVE"
says "CD32 selects its own"  "ModulesCD32_3.2"  --model=CD32 "$DRIVE"
# FS-UAE spells the A4000 'A4000/040' while the disk set ships A4000D -- the mapping is not a
# lowercase of the model, which is exactly why it is an explicit table.
says "A4000/040 maps to the A4000D disk" "ModulesA4000D_3.2" --model=A4000/040 "$DRIVE"
refuse "an unknown model is refused"     "no Modules disk is known" --model=A9000 "$DRIVE"

printf -- '\n--- version discovery ---\n'

says "picks the newest version present" "AmigaOS 3.2 (newest found)" "$DRIVE"
says "--version is honoured"            "AmigaOS 3.2"  --version=3.2 "$DRIVE"
refuse "a version with no media"        "no floppies for version" --version=9.9 "$DRIVE"
refuse "a version with no manifest"     "no disk manifest for version 3.1" --version=3.1 "$DRIVE"

# 3.2.1 sorts above 3.2 numerically per component, which a lexical sort would also get right --
# but 3.10 would not, so the padding is what is really being checked here.
mkdir -p "$MEDIA_ROOT/3.10"
says "3.10 outranks 3.2 (numeric, not lexical)" "no disk manifest for version 3.10" "$DRIVE"
rmdir "$MEDIA_ROOT/3.10"

printf -- '\n--- missing media is reported before anything else happens ---\n'

mv "$MEDIA_ROOT/3.2/Fonts.adf" "$MEDIA_ROOT/3.2/Fonts.adf.hidden"
# Both needles: boot-hdf.sh would also reject a missing ADF and name it, so "Fonts.adf" alone does
# not prove install-wb.sh checked anything. Pairing it with this script's own report does.
refuse_both "a missing required disk names it" "Fonts.adf" "required disk(s) missing" "$DRIVE"
refuse "and lists the whole required set"  "install onto an" "$DRIVE"
mv "$MEDIA_ROOT/3.2/Fonts.adf.hidden" "$MEDIA_ROOT/3.2/Fonts.adf"

# The Modules disk is required like any other, and the message has to point at the model, because
# "ModulesA600_3.2.adf is missing" is baffling without knowing why that disk was wanted.
refuse "a missing Modules disk explains the model" "must match the emulated model" \
    --model=A600 "$DRIVE"

printf -- '\n--- optional disks ---\n'

says "present optional disks are offered" "GlowIcons3.2.adf" "$DRIVE"
mv "$MEDIA_ROOT/3.2/GlowIcons3.2.adf" "$MEDIA_ROOT/3.2/GlowIcons3.2.adf.hidden"
says_not "an absent optional disk is skipped" "GlowIcons3.2.adf" "$DRIVE"
says "and the install still proceeds"     "--in-place" "$DRIVE"
mv "$MEDIA_ROOT/3.2/GlowIcons3.2.adf.hidden" "$MEDIA_ROOT/3.2/GlowIcons3.2.adf"

printf -- '\n--- the drive ---\n'

refuse "no drive named"            "name the drive to install onto"
refuse "two drives"                "one drive at a time" "$DRIVE" "$WORK/other.hdf"
# Both needles again: boot-hdf.sh also refuses a missing drive, so "does not exist" alone would pass
# on its refusal rather than this script's. The --new-disk suggestion is only in install-wb.sh.
refuse_both "a missing drive says how to make one" "does not exist" "--new-disk" \
    "$WORK/absent.hdf"
refuse "--size without --new-disk" "only mean anything with --new-disk" --size=8G "$DRIVE"
refuse "--partition without --new-disk" "only mean anything with --new-disk" \
    --partition=A=1G "$DRIVE"

printf -- '\n--- --new-disk ---\n'

says "default layout is the 4G three-partition one" "Workbench=1G,bootable" \
    --new-disk "$WORK/fresh.hdf"
says "and it says the size"        "(4G)"           --new-disk "$WORK/fresh.hdf"
says "--size overrides"            "(8G)"           --new-disk --size=8G "$WORK/fresh.hdf"
says "--partition replaces the layout" "partition Solo=rest" \
    --new-disk --partition=Solo=rest "$WORK/fresh.hdf"
says_not "--partition really replaces, not appends" "Workbench=1G,bootable" \
    --new-disk --partition=Solo=rest "$WORK/fresh.hdf"
# `init` refuses an existing image itself; this script must not pre-create the file or pass --force,
# so an existing drive with --new-disk has to reach init and be refused there rather than silently
# overwritten. On a dry run nothing is created, so this only asserts the command that would run.
says "--new-disk does not pass --force" "init" --new-disk "$WORK/fresh.hdf"
says_not "--new-disk never forces"      "--force" --new-disk "$WORK/fresh.hdf"

printf -- '\n--- unknown options ---\n'

refuse "an unknown option is named"  "unknown option --wat" --wat "$DRIVE"
refuse "--version needs a value"     "needs a value" --version

printf '\n%d checks' "$count"
if [ "$fails" -eq 0 ]; then
    printf ', all passed\n'
    exit 0
fi
printf ', %d FAILED\n' "$fails"
exit 1
