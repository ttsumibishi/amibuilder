# AmigaOS 3.2 install floppies

Put your own `.adf` files here. They are not committed — Workbench disks are licensed software.

If you have the AmigaOS 3.2 CD, the complete floppy set is already on it under `/ADF`, which is
easier than hunting for individual disks:

```sh
hdiutil attach -readonly -nobrowse -mountpoint /tmp/os32cd /path/to/amigaos32.iso
cp /tmp/os32cd/ADF/*.adf images/floppy/workbench/3.2/
hdiutil detach /tmp/os32cd
```

## Required

`utils/scripts/install-wb-3.2.sh` refuses to start without these eight, and names the missing one:

| Disk | |
|---|---|
| `Install3.2.adf` | boots, and runs the installer |
| `Workbench3.2.adf` | the OS itself |
| `Locale.adf` | |
| `Extras3.2.adf` | |
| `Fonts.adf` | |
| `Storage3.2.adf` | |
| `Classes3.2.adf` | |
| `ModulesA1200_3.2.adf` | **must match the emulated model.** The set includes A500, A600, A2000, A3000, A4000D, A4000T and CD32 variants; the script emulates an A1200 |

## Optional

Offered in the emulator's swap list when present, never required: `GlowIcons3.2`, `Backdrops3.2`,
`MMULibs`, `HDSetup3.2`, `DiskDoctor`, and the per-language `Locale-*` disks.

Worth knowing that a *barebones* install — no GlowIcons, no CPU libraries — is about 5.8 MiB across
812 files. Extras are worth skipping if the point is a minimal base layer to stack deltas onto.

## The install itself

Not automatable: the AmigaOS installer is interactive, so somebody sits through it once. Two things
that make it far less tedious, both already set or documented by the script:

- **Turbo floppy** (`floppy_drive_speed = 0`) makes floppy operations immediate instead of authentic
  1980s speed. Without it a dozen 880 KB disks is most of an hour.
- **`Cmd+W` toggles warp mode** during long waits. It is a chord, `Mod` is `Cmd` on macOS, and warp
  is *not* in the F12 menu. FS-UAE prints `Warp mode enabled` when it takes.

Skip HDToolBox when offered — `amibuilder init` already created and formatted the partitions.
