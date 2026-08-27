# Stock AmigaOS 3.2 hard drive image

`base-3.2.hdf` — the base of the layer stack. Not committed; it contains licensed software.

## What it is

Created by `amibuilder init` and installed onto in FS-UAE, with **no HDToolBox step**: the
partitions already existed and were already formatted.

```sh
.venv/bin/amibuilder init images/hd/base32/base-3.2.hdf --size 4G \
    --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest
utils/scripts/install-wb.sh images/hd/base32/base-3.2.hdf
```

Or in one step, since that partition layout is `--new-disk`'s default:

```sh
utils/scripts/install-wb.sh --new-disk images/hd/base32/base-3.2.hdf
```

| | |
|---|---|
| Image | 4 GiB, RDB, sparse — **about 7.6 MB on disk** |
| `Workbench:` | 1 Gi, bootable, `DOS\3` FFS+intl — 5.8 MiB across 812 files |
| `Work:` | 2 Gi, `DOS\3` — empty |
| `Persist:` | 1023.9 Mi, `DOS\3` — empty |

## Deliberately barebones

Base 3.2 only: **no GlowIcons, no CPU libraries, no extras.** That is the point. It is the floor
that patch and driver deltas stack onto, so anything installed here is something every later image
inherits and cannot easily drop.

**It has also never been booted.** The installer was quit rather than allowed to restart, so nothing
a first boot might rewrite — `Env-Archive` and the like — has been touched. A base layer wants to be
the installer's output, not the output of the installer plus one boot.

## Booting it — use the script, and mind that booting writes

```sh
utils/scripts/boot-hdf.sh images/hd/base32/base-3.2.hdf
```

That boots a **clone** and leaves this image alone. `--in-place` overrides when you do mean to keep
the changes.

**Measured 2026-08-20: a bare boot writes nothing.** Booting this image to Workbench and leaving it
idle for a minute returned it **byte-identical** — `cmp` clean, host mtime untouched. The common
belief that AmigaOS restamps everything on the way up is simply not true for a boot where you do not
touch anything.

The copy still defaults on anyway, because it is free (`cp -c` clones a 4 GiB sparse image in ~17 ms,
sharing blocks until written) and because anything you actually *do* will write: saving a preference,
moving an icon, which rewrites its `.info`, or anything in `WBStartup`.

The clone is kept at `base-3.2-booted.hdf`, which is how the above was measured:

```sh
.venv/bin/amibuilder snap diff images/hd/base32/base-3.2-booted.hdf \
    --parent base-3.2 --label boot-once
cmp images/hd/base32/base-3.2.hdf images/hd/base32/base-3.2-booted.hdf
```

## Restoring it

The image is the *input* to a layer, not the way it is stored. Once captured, put it back with:

```sh
.venv/bin/amibuilder snap create images/hd/base32/base-3.2.hdf --label base-3.2
.venv/bin/amibuilder compose --stack base-3.2 --into card.hdf --format rdb
```

`Persist:` is the volume that should survive an OS reinstall, and it wants the `preserve` policy —
which is inferred as `merge` today, so it needs `--policy Persist=preserve` until recorded intent
exists. See Phase 4 of `docs/KIP-FFS-PLAN.md`.
