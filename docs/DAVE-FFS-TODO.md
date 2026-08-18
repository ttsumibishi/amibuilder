# Dave's TODO — the Phase 2 measurement

**For:** when you get home. **Time needed:** about 15 minutes of your attention, plus some
waiting.

Everything in Phase 2 is built and tested, but **all of it is fixture-scale**. Nothing has
touched a real AmigaOS install, so there is still no measured base-layer size, no diff size, and
no compression ratio for real Amiga content. That is the number that decides how much of the
rest of this project is worth building, so it is worth getting rather than guessing.

There are two jobs. **Job A needs no emulator** and is the more valuable of the two.

---

## Before you start

```bash
cd ~/cooooode/amiga
git checkout main            # Phase 3 work is on the 'phase3' branch; measure from main
```

Measuring from `main` is deliberate — it keeps the numbers attributable to the reviewed Phase 2
code rather than to whatever Phase 3 is mid-way through.

**Two safety facts, so you know what you are agreeing to:**

- **Capture only ever reads.** There is no write path in `snap` at all — Phase 3 is where
  writing gets built. Your images cannot be modified by any command below.
- **Raw devices are refused unless you pass `--device`.** None of the commands below pass it, so
  even a typo pointing at `/dev/rdisk4` gets refused rather than read.

The store goes in `/tmp` so nothing accumulates in your home directory. Rough space to have
free: **a bit more than the compressed size of your install** — likely a few hundred MB, but
that is exactly what we are measuring, so leave a couple of GB spare to be safe.

---

## Job A — capture a real install as a base layer

**No emulator. Read-only. This is the important one.**

Pick one real HDF — ideally your Workbench/boot image, since that is the one you would most want
to restore to stock.

```bash
# 1. What are we comparing against? Note both numbers.
ls -l /path/to/your.hdf                 # apparent size (the 4 GB figure)
du -h  /path/to/your.hdf                # actual size on disk (sparse, may be much smaller)

# 2. Sanity check that amibuilder understands the image at all.
.venv/bin/amibuilder info /path/to/your.hdf
.venv/bin/amibuilder partitions /path/to/your.hdf

# 3. The capture. -v prints each file as it goes, so you can see it working.
time .venv/bin/amibuilder snap create /path/to/your.hdf \
     --label base-os-3.2.3 \
     --store /tmp/measure -v

# 4. The numbers.
.venv/bin/amibuilder snap ls --store /tmp/measure
du -sh /tmp/measure
.venv/bin/amibuilder snap show base-os-3.2.3 --store /tmp/measure
```

**This may take a while, and how long is itself a result.** Compression is stdlib `lzma`, which
is slow — roughly 1–3 MB/s. A 300 MB install could take five to ten minutes. If it is
unbearable, that is real evidence for switching the codec to zstd, which is a ten-line change by
design. So note the `time` output either way; do not just wait it out and forget it.

### What to send me from Job A

1. The `snap ls` line — entries, files, stored size.
2. `du -sh /tmp/measure` — the actual store size.
3. The source image's apparent size and `du` size, for the comparison.
4. The `time` output.
5. **Any warnings the capture printed.** Two kinds matter:
   - `link skipped` — amitools cannot represent AmigaDOS links, so if your install has any, I
     need to know. This is the one genuine gap in capture.
   - `not captured` — a partition that would not mount, e.g. PFS3 or SFS.
6. The `drive` block from `snap show`. I specifically want the **`mask` and `maxtr` values**.
   Those are the ones that silently corrupt data on the wrong controller, and having yours
   captured from a drive that already works is what stops Phase 3 guessing them.

---

## Job B — the diff

Two routes. **Route 1 is better and needs no emulator**, so try it first.

### Route 1 — two backups you already have (preferred)

If you have two snapshots of the same drive from different points in time — anything where you
installed or changed something between them — that gives a real-world diff, which is better
evidence than anything I could stage in an emulator.

```bash
# Capture the OLDER one as the base.
.venv/bin/amibuilder snap create /path/to/older.hdf --label before --store /tmp/measure2

# Diff the NEWER one against it. This writes a candidate; it commits nothing.
.venv/bin/amibuilder snap diff /path/to/newer.hdf \
     --parent before --label after --store /tmp/measure2

# Look at what it thinks changed.
.venv/bin/amibuilder snap review after --explain --store /tmp/measure2
```

### Route 2 — install something in FS-UAE

Only if Route 1 is not possible. Capture a base, boot the image in FS-UAE, install one piece of
software, shut down cleanly (**important** — AmigaDOS caches writes, see G16), then diff.

```bash
.venv/bin/amibuilder snap create /path/to/your.hdf --label base --store /tmp/measure2
# ... install something in FS-UAE, shut down cleanly ...
.venv/bin/amibuilder snap diff /path/to/your.hdf --parent base --label thing --store /tmp/measure2
.venv/bin/amibuilder snap review thing --explain --store /tmp/measure2
```

### What to send me from Job B

1. The `snap diff` summary — the change count and the `by_reason` breakdown.
2. **The full `snap review --explain` output, or at least a good chunk of it.** This is the part
   I most want to see, and the reason is worth stating plainly: **the diff is the single thing in
   this design most likely to disappoint.** Between two captures the Amiga boots and runs, so it
   writes preferences, `Disk.info` files, `Devs/system-configuration`, and restamps directories.
   The default exclusion list is a guess I made in advance and the design says outright that the
   first few real diffs will reveal noise nobody predicted.
   
   So if the diff contains 400 entries when you expected 40, **that is a useful result, not a
   failure** — it tells me exactly what to add to the exclusion list. Do not tidy it up before
   sending it.
3. The diff layer's stored size from `snap ls`, against the source image size. **This is the
   headline number for the whole project**: a small diff against a 4 GB image is the thing that
   makes many snapshots cheap.

---

## Cleaning up

```bash
rm -rf /tmp/measure /tmp/measure2
```

Nothing else to undo. No config was changed, nothing was written to your images, and the stores
are entirely self-contained.

---

## If something goes wrong

Not a problem — a failed capture is a result too, and it cannot have damaged anything.

- **It refuses the image.** Send me the exact message from `amibuilder info`. It may be a
  container shape I have not handled.
- **It crashes with a traceback.** Send the whole thing. Any traceback is a bug in amibuilder by
  definition — expected failures are supposed to be one-line messages.
- **It is impossibly slow or fills the disk.** Ctrl-C is safe; the store is just files under
  `/tmp/measure` and `rm -rf` clears it. Tell me how far it got.
- **A partition will not mount.** Expected for PFS3 or SFS. Capture continues and warns, which
  is by design — one unreadable volume must not make a four-partition drive uncapturable.

---

## Why this matters, in one line

Right now the honest claim is "the mechanism works on synthetic data." After these two jobs it
becomes "a base layer for a real install is *N* MB and a software install adds *M* MB against a
4 GB image" — and that either justifies the whole layered approach or tells us to rethink it.
