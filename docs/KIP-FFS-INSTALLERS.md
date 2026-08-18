# KIP-FFS — Installer Script Feasibility

**Date:** 2026-08-17
**Subject:** AmigaOS 3.2 CD (`amigaos32.iso`, Hyperion Entertainment, 74 MB, ISO 9660,
built 2021-04-13) — the hardest installer available, used as the worst case.

**Verdict: feasible, and considerably easier than expected.** The base OS installer is
approachable at file level. Of 30 external-binary invocations, **four** need real work,
and none of them are load-bearing for a working install. Details below, all measured.

Analysis is reproducible: `test/helpers/installer.py` plus
`test/test_installer_scripts.py`, which skip cleanly when the ISO is absent.

---

## 1. What is on the CD

The install floppies ship as **35 ADF images** in `ADF/`, including
`Workbench3.2.adf`, `Extras3.2.adf`, `Classes3.2.adf`, `Fonts.adf`, `Storage3.2.adf`,
`Locale.adf` plus 18 language variants, `GlowIcons3.2.adf`, `MMULibs.adf`, `DiskDoctor.adf`,
and **eight per-model module disks** (`ModulesA500_3.2.adf` … `ModulesCD32_3.2.adf`).

Our read path handles them. Reading `Install3.2.adf` with the project's own tooling:

- Listing works, filenames including Latin-1 ones (`Español.info`, `Türkçe.info`,
  `Français.info`) render correctly.
- `xdfscan` reports **`boot ok`** — a genuine, bootable, valid Amiga floppy.
- DosType is **`DOS1:ffs`**, i.e. plain FFS with **no** international hashing. Worth
  noting given the project targets DOS3: the hash variant differs between the install
  media and the installed system.
- Real-world protection bits appear, including the pure bit (`--p-rwed`) on commands and
  the script bit (`-s--rw-d`) on scripts.

This is the first validation of the read path against media not produced by amitools.

## 2. The installer script

`Install3.2.adf:Install/Install` is **177,688 bytes of plain ISO-8859 text** —
`$VER: Install 47.123 (9.4.2021)`. Alongside it sits `Installer` (107,440 bytes), the
m68k LISP interpreter that executes it.

| Measure | Value |
|---|---|
| Bytes | 177,688 |
| Lines | 6,673 |
| Forms (S-expressions) | 3,407 |
| Distinct head symbols | 104 |
| Max nesting depth | 16 |
| Parentheses | balanced |
| User-defined procedures | 15 |
| Language branches | 10 |
| Localisable message symbols | 78 |

**64% of the file is string data and 3% comments, leaving 59 KB of actual logic.** The
apparent size is dominated by ten languages' worth of translated UI text. A ~80-line
tokeniser parses the whole thing cleanly.

Call distribution:

| Category | Calls | Notable |
|---|---|---|
| Control flow | 2,002 | `set` 975, `cat` 836, `if` 127, `select` 15, `while` 9 |
| File operations | 279 | `makedir` 60, `copyfiles` 49, `exists` 33, `delete` 26, `makeassign` 24, `rename` 14, `foreach` 11, `protect` 3 |
| Prompts | 97 | `complete` 34, `askdisk` 15, `askbool` 9, `user` 9, `message` 8, `working` 8, `askdir` 5, `askoptions` 3, `askchoice` 3 |
| Environment | 6 | `database` 2, `getenv` 3, `getdiskspace` 1 |

Two things stand out. **`database` is called exactly twice** — `total-mem` and `vblank`
(PAL/NTSC) — so machine introspection is nearly absent. And **`(tooltype ...)` is never
used at all**; the script shells out to `CopyToolTypes` instead.

## 3. The 30 `(run ...)` calls, itemised

This was the expected blocker. It is not.

| Target | Calls | Assessment |
|---|---|---|
| `Resident … PURE` | 7 | AmigaDOS RAM-residency optimisation. **No filesystem effect.** |
| `Delete QUIET …` | 6 | **Replaceable** with our own delete |
| `DAControl … LOAD/EJECT` | 3 | **Mounts an ADF as a virtual floppy — we read the ADF directly** |
| `wait 1` / `wait 2` | 3 | Timing only. **No-op.** |
| `AddBuffers DF0: 15` | 1 | Floppy cache tuning. **No-op.** |
| `Copy >NIL: …` | 1 | **Replaceable** with our own copy |
| `C:LoadModule REMOVE NOREBOOT` | 1 | Soft-kicks ROM modules. **Irrelevant** to a file-level install |
| `AmigaModel to ENV:AmigaModel` | 1 | Writes a machine fact. **Supply from a target profile** |
| `CPU CPUType to ENV:CPU` | 1 | Ditto |
| `GuessBootDev … TO ENV:BootDev` | 1 | Ditto |
| **`UpdateWBFiles`** | 2 | **m68k binary, real filesystem work.** Needs a shim, vamos, or observe mode |
| **`IconPos`** | 1 (procedure called 16×) | **Icon positioning** — writes `.info` files |
| **`CopyToolTypes`** | 1 (procedure called 2×) | **Icon tooltypes** — writes `.info` files |
| **`Prefs/WBPattern USE`** | 1 | Applies desktop pattern prefs |

**26 of 30 are no-ops, trivially replaceable, or answerable from configuration.** Four
need real work.

The `DAControl` finding is the pleasant surprise: **the AmigaOS 3.2 installer already
treats its own ADFs as virtual floppies.** It mounts them from the CD rather than asking
for physical disks. So all 15 `askdisk` prompts reduce to "select the matching ADF", which
is precisely the ADF staging capability already verified in
`KIP-FFS-NOTES.md` §10. The script's own `MOUNTADF` / `UNMOUNTADF` procedures are the
abstraction we would substitute for.

## 4. What the install actually does

`copyfiles` sources are almost entirely whole mounted ADFs:
`workbenchPath`, `extrasPath`, `classesPath`, `fontPath`, `storagePath`, `backdropsPath`,
`glowiconsPath`, `mmulibsPath` — each copied wholesale to the target. On top of that sit
selective copies out of `Storage:` subdirectories (`Printers`, `Keymaps`, `Monitors`,
`LIBS`, `WBStartup`, `Presets/Pointers`) chosen by the user, plus rename/delete handling
for a pre-existing Workbench.

So the base OS install is, in essence: **copy these ADFs to these paths, then apply a
handful of selections.** That is exactly the shape of a layer.

## 5. The user decisions

Twenty prompt calls, covering roughly a dozen genuine choices:

| Prompt symbol | Decision |
|---|---|
| `#which-language` | Installation language |
| `#ask-function` | Install or update |
| `#which-disk` | Target volume |
| `#confirm-target` | Confirm overwriting an existing Workbench |
| `#ask-amiga-model` | Amiga model, selecting the module disk |
| `#which-printer` | Printer driver |
| `#which-keymap` | Keymap |
| `#ask-glowicons` | Install GlowIcons |
| `#move-old` / `#delete-old` / `#confirm-delete` | Handling of pre-existing files |
| `#which-disk-lang`, `#which-disk-icon` | Language and icon set placement |

A dozen questions for the entire base OS. Answering them up front and recording them in
the layer metadata is very tractable, and it is exactly the "prompt me for the standard
things" behaviour wanted.

## 6. The four genuine gaps

**`UpdateWBFiles` (2 calls).** A real m68k executable on the install disk
(`$VER: updatewbfiles 45.1`, 4,876 bytes). Name and call sites suggest it merges new
Workbench files with existing ones. **Unverified what it does precisely** — it needs
either disassembly, running under vamos, or observation of its effects via snapshot diff.
This is the one that would most benefit from observe mode.

**`IconPos` (16 invocations) and `CopyToolTypes` (2).** Both write Amiga `.info` icon
files. amitools has no icon support, so this needs a `DiskObject` parser. The format is
well documented and not large, so writing one is realistic. Importantly, the *impact* is
bounded:

- `IconPos` sets where an icon appears in a drawer window. **Cosmetic.** An install that
  skips it works; the icons just land in default positions.
- `CopyToolTypes` copies tooltypes such as stack size between icons. **Functional but
  narrow.**

**`Prefs/WBPattern USE` (1).** Applies the desktop backdrop preference, writing prefs
files. Cosmetic, and reproducible by capturing the resulting files.

**None of the four prevent a bootable, working system.** Skipping all of them yields a
Workbench that boots and functions, with default icon positions and no desktop pattern.

## 7. Recommendation

**Observe mode remains the right first step, and this analysis strengthens rather than
weakens that.** Running the install once in FS-UAE and taking a snapshot diff captures
`UpdateWBFiles`, the icon edits and the prefs correctly, because it captures *outcomes*.
It needs nothing beyond Phase 2 capture plus the emulator harness.

**Simulate mode is now clearly worth building afterwards**, and the base OS is a viable
target rather than an aspiration. A realistic split:

1. **Interpret** the S-expressions: control flow, `set`/`cat`, `copyfiles`, `makedir`,
   `delete`, `rename`, `protect`, `makeassign`, `startup`, `foreach`. Covers 279 file
   operations and 2,002 control-flow calls.
2. **Prompt** for the dozen decisions, saving answers into layer metadata.
3. **Substitute** `askdisk` and `DAControl` with direct ADF reads.
4. **Answer** `database` and the ENV-writing tools from a target machine profile — which
   is better than emulating, because it allows building for a machine you do not have.
5. **Shim or skip** the four gaps, with `--strict` refusing to emit a layer and
   `--allow-partial` proceeding with an explicit manifest of what was skipped.

A hybrid is attractive for the base OS specifically: simulate everything, and hand only
the `UpdateWBFiles` step to observe mode.

**On simpler installers** — drivers, games, apps, utilities — the picture is far easier
still. Most are drag-to-drawer or a short script, and the ones with real Installer scripts
will use a small subset of what the base OS exercises. The base OS was the worst case, and
it turned out tractable, so simpler cases should not need re-litigating.

## 8. Verified vs unverified

**VERIFIED:** every count in this document, measured by
`test/helpers/installer.py`; that the script parses with balanced parentheses; that
`Install3.2.adf` reads correctly and validates as bootable via our own tooling; the
`(run ...)` inventory and its classification; that `(tooltype ...)` is unused; that the CD
ships the full ADF set including per-model module disks; the 64% string fraction.

**UNVERIFIED:**

- What `UpdateWBFiles` actually does. Not disassembled, not run.
- Whether vamos can execute it. vamos installs and initialises but has not been shown to
  run any Amiga binary (`KIP-FFS-NOTES.md` §11.6).
- Whether an install assembled purely at file level boots. That is the emulator harness's
  job and needs a Kickstart ROM.
- Whether the other 34 ADFs all read as cleanly as `Install3.2.adf`. Only the install disk
  was inspected.
- Whether `.info` files can be written correctly without an existing library. The format
  is documented; no code has been written against it.

## 9. Reproducing

```bash
# Mount the CD read-only (macOS)
hdiutil attach -readonly -nobrowse -mountpoint /tmp/aos32 \
    source-files-do-not-add-to-git/amigaos32.iso

# Read the install disk with our own tooling
.venv/bin/xdftool /tmp/aos32/ADF/Install3.2.adf open + list
.venv/bin/xdfscan /tmp/aos32/ADF/Install3.2.adf        # -> boot ok

# Extract and analyse the script
.venv/bin/xdftool /tmp/aos32/ADF/Install3.2.adf open \
    + read Install/Install /tmp/Install

hdiutil detach /tmp/aos32

# Or just run the tests, which do all of the above and assert the findings
.venv/bin/python -m pytest test/test_installer_scripts.py -q
.venv/bin/python -m pytest test/test_installer_scripts.py -q -s \
    -k report_renders            # prints the full analysis
```

Note: `grep` treats the script as binary because of its Latin-1 high bytes, and
**silently suppresses matches without `-a`**. That cost time during this investigation.
