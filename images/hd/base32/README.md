# Stock AmigaOS 3.2 hard drive image

`base-3.2.hdf` — the base of the layer stack. Not committed; it contains licensed software.

## What it is

Created by `amibuilder init` and installed onto in FS-UAE, with **no HDToolBox step**: the
partitions already existed and were already formatted.

```sh
.venv/bin/amibuilder init images/hd/base32/base-3.2.hdf --size 4G \
    --partition Workbench=1G,bootable --partition Work=2G --partition Persist=rest
utils/scripts/install-wb-3.2.sh images/hd/base32/base-3.2.hdf
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

That boots a **clone** and leaves this image alone, which is the point: AmigaOS rewrites
`Env-Archive`, restamps directories and touches plenty else on the way up, so a boot is a
destructive edit even if you touch nothing. One careless boot would permanently spend the
never-been-booted property described above. The clone is an APFS clone — instant, and free until
written to — so there is no reason to skip it. `--in-place` overrides when you do mean to keep the
changes.

The clone is kept at `base-3.2-booted.hdf`, which makes the first boot itself measurable:

```sh
.venv/bin/amibuilder snap diff images/hd/base32/base-3.2-booted.hdf --parent base-3.2
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
