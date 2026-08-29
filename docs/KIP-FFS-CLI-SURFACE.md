# CLI surface: how you name things, and why it is shaped that way

**Date:** 2026-08-28
**Status:** Decisions recorded, **one deliberately left open pending real use.** Everything
described as shipped is on `main` and covered by tests.

This is the design record for amibuilder's *addressing grammar* and *argument shape* — how a
command names an image, a partition and a path, and why those three are not all expressed the
same way. It exists because that question came up three times in three different forms, each
time looking like a fresh idea, and the answers rest on measurements that are tedious to
re-derive.

**Read this before proposing a change to how specs or destinations are written.** In particular
before proposing a trailing-path form or `--target-*` flags: both were analysed here, and the
reasons they were declined are *not* the obvious ones.

Companion docs: `KIP-FFS-NOTES.md` (verified findings about FFS itself), `KIP-FFS-IDEAS.md`
(option analysis from before anything was built), `KIP-FFS-PLAN.md` (build order and the
per-item backlog, including Phase 6 ergonomics item 3 which this doc expands).

---

## 0. The decisions, at a glance

| Question | Answer | Reason — note it is rarely the obvious one |
|---|---|---|
| Is a path part of the spec? | **No**, always a separate argument | A partition named `Work` and a top-level directory named `Work` would be indistinguishable |
| Is `card.hdf:Work/Utils` *ambiguous*? | **No.** Measured, it parses cleanly | The `/` rule applies only to the selector, which `parse` has already isolated |
| Should we support it anyway? | **No** — rejected 2026-08-28 | It cannot *replace* `--to`, so it would be a second spelling for one destination. CLI surface, not ambiguity |
| `--target-image`/`-partition`/`-path` flags? | **Not recommended** — analysed 2026-08-28 | Trades ambiguity for a combinatorial validity matrix, and multiplies on two-sided commands |
| Is `--to` an inconsistency? | **No, it is forced** | `cp` takes variadic sources, so a second trailing positional cannot be told from another source |
| What about a path typed into a spec? | **Refused with the correct spelling**, shipped 2026-08-28 | It used to be swallowed as a partition *name* and fail layers later |
| `--image` / `AMIBUILDER_IMAGE` default? | **Still deferred** | Needs a *safety* design, not an implementation. See §7 |

---

## 1. The grammar as it actually is

One argument names any of these. This is `amibuilder/addressing.py`, and every command depends
on it:

```
card.hdf                 whole image (plain HDF, or an RDB's only partition)
card.hdf:0               RDB partition by index          -> partition is an int
card.hdf:DH0             by AmigaDOS device name         -> partition is a str
card.hdf:Workbench       by volume name                  -> partition is a str
disk.adf                 floppy image (.adf, .adz, .adf.gz, .ipf, .dms)
/dev/rdisk4              raw device (requires --device)
/dev/rdisk4:0x76:1       MBR primary slot 1, and the RDB inside it
/dev/rdisk4:0x76:1:2     ...then partition 2 of that RDB
./staging                a host directory
```

**How `parse` resolves it, because everything in §2 depends on this.** The path portion is found
**first**, by trying the whole spec and then progressively shorter prefixes split at each `:`,
right to left, and checking each against the filesystem (or the device-node regex). Only once a
path has been found is the remainder handed to `_parse_selector`.

Two consequences that are easy to miss and that decide the rest of this document:

1. **The path portion's own slashes are never in scope for any selector rule.** An image at
   `/Users/dave/images/card.hdf` or a device at `/dev/rdisk4` is isolated before the selector is
   examined at all.
2. **A selector is only parsed when a `:` was actually split off.** A bare `/dev/rdisk4` never
   reaches `_parse_selector`.

Longest-first exists so a file genuinely named `weird:name.hdf` — legal on macOS — wins over
reading `:name.hdf` as a selector. The cost is that resolution is filesystem-dependent: a host
entry literally named `card.hdf:Work` would win for the bare spec while `card.hdf:Work/Utils`
would fall back to splitting at the `:`. That is pathological and documented as a deliberate
trade in `addressing.py`.

`int` vs `str` for the partition is preserved deliberately all the way to resolution: a volume
literally named `0` is legal on an Amiga, however unwise, and collapsing the two would open the
wrong volume.

---

## 2. The trailing-path form — measured, it holds, and it was still declined

**The proposal.** Write `cp ./x card.hdf:Work/Utils/Patches` instead of
`cp ./x card.hdf:Work --to Utils/Patches`, on the claim that "anything after the first `/` is
necessarily a path" so it cannot be confused with a volume selector.

**The claim is true.** Measured across 35 spec shapes in two probe runs against the real
`parse`. Nothing that can legally appear in a selector can contain `/`: an AmigaDOS volume name
and an RDB device name cannot, because `/` is AmigaDOS's own path separator; a partition index
cannot; and an MBR selector is only hex digits, decimal digits and colons. So the first `/` in a
selector is unambiguously the volume↔path boundary.

**The worry recorded in the plan was wrong.** It said "a device path alone probably breaks it".
It does not, for the reason in §1: `/dev/rdisk4` has no `:`, so `_parse_selector` never runs, and
`/dev/rdisk4:0x76:1` isolates the device as the *path* and `0x76:1` as the selector, which
contains no slash. Verified for the awkward cases too — a volume name containing a space
(`My Work/Utils`) and an image filename containing a colon
(`weird:name.hdf:Work/Utils`) both isolate correctly, as does an image whose own path has
slashes (`sub/dir/card2.hdf:Work/Utils`).

### ⚠️ It was NOT declined for ambiguity. Do not record it that way.

This matters because "rejected as ambiguous" is a false premise someone could act on later: it
would tell a future reader the grammar cannot support the notation, when it demonstrably can. The
actual chain has three links, and only the middle one involves ambiguity:

1. Is the form unambiguous? **Yes.**
2. Can it therefore *replace* `--to`? **No.** `card.hdf/Utils`, with no colon, fails — candidate
   generation splits only at `:`. And it has to stay that way: extending the shortening to `/`
   would make **host directories** ambiguous, because `./staging` and `./staging/Utils` are both
   valid directory specs, so `./staging/Utils` could not be distinguished from "`./staging` plus
   path `Utils`". *That* is where ambiguity bites — one step upstream of the decision. So for a
   plain single-volume HDF, where `--to Utils` works today without naming a volume, the new form
   would require one.
3. Therefore it is **additive**, not a replacement → two spellings for one destination → declined
   on CLI surface area, which is the same objection that declined the `put` alias.

Had it been able to replace `--to` outright, the recommendation would have been to build it.
Anyone revisiting this should argue about surface area, not re-derive the parsing.

**One spelling is left free by this decision**, should it ever be wanted: `card.hdf:/Utils` (no
partition named, path only) currently parses as the nonsense `partition='/Utils'`, so it is
available to mean "the default volume, at this path".

---

## 3. Why `--to` exists at all — and how narrow the inconsistency really is

The shape that feels convoluted is that **image and partition are joined positionally while the
path is a flag**: `cp ./x card.hdf:Work --to Utils`. That is worth understanding before
proposing to fix it, because it is forced rather than chosen.

`cp` takes **variadic sources**: `cp a b c card.hdf:Work --to Utils`. With N sources and the
destination image as the last positional, a *second* trailing positional could not be
distinguished from another source file. Unix `cp` has exactly the same constraint and solves it
the same way — by having only one destination slot. So `--to` is not somebody being inconsistent;
it is the only place left to put a path.

**And it is confined to two commands.** Measured by walking the live argparse parser on
2026-08-28:

| | Count | Commands |
|---|---|---|
| Take a volume-relative PATH **positionally** | 12 | `cat` `comment` `du` `find` `get` `hexdump` `ls` `mkdir` `protect` `rm` `touch` `tree` |
| Use **`--to`** instead | 2 | `cp` (`files[+] image`), `inject` (`source dest`) |
| No in-volume path at all | 17 | `check` `compact` `completion` `compose` `diff` `doctor` `format` `info` `init` `partitions` `recipe` `relabel` `shell` `snap` `sync` `version` `zerofree` |

So the surface is already about as consistent as the grammar allows, and the exception has a
structural cause. **If `--to` still grates after real use, the smallest honest fix is to rename
it** — `--into` reads more like a destination — which touches one flag on two commands and
changes no grammar.

---

## 4. The `--target-image` / `--target-partition` / `--target-path` proposal

**Idea:** replace the compact spec with three explicit flags, to remove ambiguity entirely and be
easier for a caller to understand.

**Assessment: not recommended.** It is a reasonable instinct and it does kill one real error
class, but on balance it makes the logic more complex rather than less. Four reasons:

**It relocates the parsing instead of removing it.** Three flags cover image + partition + path,
but the grammar has more axes than three: a raw device carries different guard rails, an MBR type
and slot live inside that device, a partition lives inside *that* slot's RDB, an ADF has no
partitions at all, a host directory has none either, and a layer ref is a fourth kind. So the
real set is closer to `--target-image`, `--target-device`, `--target-dir`, `--target-mbr-slot`,
`--target-partition`, `--target-path` — and then the *combinations* need validating by hand: slot
requires device, image excludes device, partition excludes dir, partition excludes ADF. Today
that is one `parse()`, one error type and one test file, shared by all 32 commands.
**Ambiguity would be traded for combinatorial validity, which is more states, not fewer** — and
"flag B requires flag A" is precisely what argparse cannot express for you, so every rule becomes
hand-written and hand-tested.

**Two-sided commands multiply it.** `sync`, `diff` and `inject` each name two sources, and `diff`
accepts any kind on either side. `sync card.hdf:Work ./backup` becomes six flags; `diff` would
carry ten to twelve, with the reader tracking which prefix belongs to which side.

**It works against the problem that started the ergonomics backlog**, which was literally "the
image spec has to be retyped on every command". `amibuilder ls card.hdf:Work Utils` is 33
characters; the flag form is 80 — on the command run most often.

**The consistency it would buy is narrower than it feels**, per the survey in §3: 12 commands
already take the path positionally and only 2 use `--to`.

**Where the instinct is right.** It would eliminate the one error class that was real — a path
conflated with a partition — because `--target-partition Work --target-path Utils` cannot be
misread. But the error message in §5 buys the same protection for about fifteen lines. There is
also a legitimate niche in **scripting**, where explicit beats terse and quoting is a hazard; if
that bites, the cheaper answers are the `--json` output that already exists, or explicit flags on
the *one* command where they pay rather than uniformly across 31.

---

## 5. What shipped instead: the path-in-a-spec refusal

Committed 2026-08-28 (`f5e7ff3`). `_parse_selector` refuses any `/` in a selector:

```console
$ amibuilder ls card.hdf:Work/Utils
amibuilder: card.hdf:Work/Utils: ':' selects a partition, it does not open a path -- a path
inside the image is a separate argument. Write 'card.hdf:Work Utils' instead. (cp and inject
take their destination path as '--to PATH', because their positional slots hold the sources.)
```

Exit 2, as an addressing error.

**The defect it fixes was inconsistency, not just a bad message.** Before, the same mistake was
reported two different ways depending on a detail the user was not thinking about:

| you typed | before |
|---|---|
| `card.hdf:Work/Utils` | `partition='Work/Utils'` — accepted, then failed layers later as a missing partition |
| `card.hdf:/Utils` | `partition='/Utils'` — same |
| `/dev/rdisk4:0x76:1:2/Utils` | `partition='2/Utils'` — same |
| `/dev/rdisk4:0x76:1/Utils` | clean syntax error |
| `card.hdf:0x76:1/Utils` | clean syntax error |

The swallowing cases were the worse ones: "no such partition `Work/Utils`" sends the reader to
inspect their RDB rather than their command line. The check is therefore placed **ahead of the
MBR branch**, so all shapes report identically.

**The suggestion is derived, not canned**, which is what makes it worth having: split at the
*first* `/` so a deep path stays one path; drop the colon entirely when no partition was named
(`card.hdf:/Utils` → `card.hdf Utils`); trim stray slashes so it can be pasted as-is.

**It earns a good message because it is the spelling people reach for** — this package's own
`commands/inject.py` docstring writes "drop a game ADF into `card.hdf:Work/Games`" when
describing where files land.

7 tests; 6 mutations, all killed. One initially survived: `Work/` cannot exercise the slash trim,
because nothing follows the slash, so the doubled (`//Utils`) and mid-path (`Work//Utils`) cases
were added to pin it.

**Known two-step on a plain HDF.** `card.hdf:Work/Utils` suggests `card.hdf:Work Utils`, which
then correctly reports that a plain image has no partition table — the truly correct spelling
being `card.hdf Utils`. `parse` is purely syntactic plus an existence check and deliberately does
not open the image, so it cannot know which it is. Coupling parsing to image inspection would be
the worse trade.

---

## 6. Measured facts worth not re-deriving

All from 2026-08-28 unless noted.

- **`parse` never opens an image.** It is regex plus `os.path.exists` plus `os.stat`. An empty
  file called `card.hdf` parses exactly like a real one, which is why the probes in §8 need no
  fixtures. It also means no parse-time error can depend on the image's contents or kind.
- **A device is recognised by shape as well as by `stat`**, so a busy or privileged device still
  parses as a device and the guard rails engage, instead of reporting "no such file". `/dev/rdisk99`
  parses fine and touches nothing.
- **`card.hdf:0x76:1` works on a plain *file*, not only a device.** The MBR selector is not
  device-only, which is useful for testing a card image without a card.
- **`capture.capture_volume` never records the volume root as an entry**, so a volume-relative
  `rel` is never empty. `sync` relies on this: an empty `rel` would put a `.uaem` sidecar beside
  the backup directory rather than inside it.
- **`card.hdf:Work/../Escape` used to yield `partition='Work/../Escape'`.** It is now refused, but
  if a path portion is ever accepted in a spec it will need traversal validation of its own.
- **Nothing in the package or the tests constructs a slash-bearing partition name.**
  `address.partition` only ever reaches `open_volume`/`resolve_partition` — `image.py:602`,
  `compare.py:61`, `format.py:167`, `zerofree.py:165`, `inspect.py:243`. That is what made §5 safe
  to change.
- **`shell`'s `mv` is a copy followed by a delete**, because amitools has no node-level rename. So
  it needs room for both copies briefly, and its failure mode is a duplicate rather than a loss.
  Files only; directories are refused. Six tests, including metadata preservation.

---

## 7. Revisit after real use — and what evidence would change each answer

The whole point of writing this down is that the decisions above were made from measurement and
reasoning, **not from having used the tool to install software on an Amiga**. That is the evidence
that is missing, and it is the evidence that should be allowed to overturn any of it.

**What to pay attention to while using it:**

- **Does `--to` actually trip you up, or just look odd?** Those need different fixes. If you
  *mistype* it — passing the path positionally to `cp` — that is a real defect and argues for
  either the trailing-path form after all, or at minimum a better error when `cp` gets an extra
  positional. If it merely reads awkwardly, §3's rename to `--into` is the proportionate answer.
- **Do you reach for `card.hdf:Work/Utils` more than once?** The refusal now catches it, so this
  is measurable rather than a matter of opinion — every occurrence is an exit-2 you will remember.
  Several a day would be real evidence that the surface-area objection is outweighed.
- **Does retyping the spec dominate, or is it the path?** If the spec, the answer is `shell`
  (which already holds the image open and makes everything a bare path) or the deferred
  `--image` default — not a change to the grammar.
- **Does anything break on a *real* card that parsed fine here?** Especially the `0x76` device
  forms, which have never been exercised against real hardware.

**The one still open: `--image` / `AMIBUILDER_IMAGE`.** Deferred pending a safety design, not an
implementation, and that distinction is the whole of it. An environment variable that silently
decides which image a command operates on is a footgun on a tool that deletes things: a stale
`AMIBUILDER_IMAGE` turns `amibuilder rm S/Startup-Sequence` into a command that destroys
something on a drive you were not thinking about — the same class of mistake the device guard
rails exist to prevent. If it is built, it should at minimum **print the image it resolved** and
probably **refuse to apply the default to the destructive commands** (`rm`, `format`, `zerofree`,
`sync --delete`) so those always name their target explicitly. Note the case that actually bites
— interactive use — is already served by `shell`, so what remains is scripting convenience, which
is exactly where an invisible default is most dangerous.

---

## 8. Reproducing the measurements

The probes were throwaway scripts in `/tmp` and are not committed; they are a few minutes to
rebuild and the useful output is already in §2, §3 and §5. If addressing questions keep coming
up, promoting them to `utils/scripts/` would be reasonable.

**Spec-shape table.** Import `amibuilder.addressing.parse`, create an empty file named
`card.hdf` and a directory in a `tempfile.TemporaryDirectory` (no real image needed, per §6), then
print `path` / `is_device` / `is_directory` / `mbr_type` / `mbr_slot` / `partition` for each spec
in a list. Use `/dev/rdisk99` for the device shapes so no real card is named — `parse` only
regex-matches and stats, so nothing is opened or written.

**Command-shape survey.** Walk `amibuilder.cli.build_parser()` — it returns
`(parser, handlers)` — find the action whose `choices` is a dict, and for each subparser split
`_actions` into positionals (`not a.option_strings`) and options. Group by whether `--to` is
present and whether a positional is named `path`/`paths`. This reads argparse privates on purpose,
the same way `commands/completion.py` does, so the answer cannot drift from the real CLI.
