# images/

Disk and floppy images. **Nothing here is committed except these README files.**

That is a licensing line, not a size one: Workbench ADFs and Kickstart ROMs are licensed software,
and hard drive images made from them contain it. `.gitignore` blanket-excludes `images/**` with an
exception only for `README.md`, so the layout is discoverable in the repository while the contents
stay on your machine. Supplying the media is your job.

```
images/
  floppy/
    workbench/
      3.2/          AmigaOS 3.2 install floppies (.adf)
  hd/
    base32/         a stock AmigaOS 3.2 hard drive image
```

The `floppy/workbench/<version>/` shape is deliberate: 3.1 and 3.2.1 can sit alongside 3.2, which is
what a version-aware installer script would select between. See "utils/scripts/install-wb.sh" under
Phase 6 of `docs/KIP-FFS-PLAN.md`.

## Why keep whole images at all, when the point of the tool is not to?

Because a layer store needs somewhere to start. `amibuilder snap create` turns an image here into a
content-addressed base layer, after which snapshots cost their content rather than the drive's
capacity — but the first one has to be captured from something. These images are the input to that,
not the way work is stored.

Note they cost far less than their size suggests. A 4 GiB image holding a stock 3.2 install occupies
about 7.6 MB on APFS, because it is sparse. `du -h` tells the truth here and `ls -l` does not.
