# Kiro crew handoff — amibuilder

**Written:** 2026-10-04 by the session that shipped `.uaem` sync metadata, the addressing
refusal, `mv` and the doc auditor.
**For:** a brand-new session with no memory of this project.
**Status of this document:** every fact below was re-read from the repo on 2026-10-04, not
recalled. Where something is inferred rather than measured, it says so.

> ⚠️ **Re-verify before you trust anything here.** Five weeks passed between the last
> substantial work (2026-08-29) and this handoff (2026-10-04), and a document is stale the
> moment it is written. The commands under [Verify in 60 seconds](#verify-in-60-seconds)
> re-establish the state cheaply. **Run them first.** This project has repeatedly been bitten
> by stale assumptions — a stale git ref produced a confidently wrong "this does not exist
> anywhere" claim, and a stale injected date put wrong stamps in three documents.

---

## 1. What this project is

A CLI that reads and writes Amiga disk images (HDF, RDB, ADF) **at the file level without
mounting them**, plus a Docker-style layer store on top. Written in Python over
[amitools](https://github.com/cnvogelg/amitools).

The motivating problem, in Dave's words: he has several Amigas with SD-card readers in place of
SCSI drives, holding multi-gigabyte HDF files that are mostly empty. Backing up by copying whole
images is slow, wears the cards, and eats disk. So: capture a stock AmigaOS install once as a
base layer, record each subsequent change as a diff layer holding only what changed, then
compose whichever stack you want into whatever the target machine reads.

**The headline measurement:** installing SysInfo 4.4 onto a 4 GiB AmigaOS 3.2 drive captures as
a **46.7 KiB** layer — 89,830× smaller than the image. Composed back, real AmigaOS boots it,
verified under FS-UAE with AmigaDOS's own tools. `STATISTICS.md` has the method.

Read `README.md` for the user-facing picture and `USAGE.md` for the command surface. Do not
re-derive either from the code; they are current (the auditor proves the examples run).

---

## 2. Verify in 60 seconds

```bash
cd /Users/davcha/cooooode/amiga
date '+%Y-%m-%d %H:%M %Z'                      # the injected session date goes stale; trust this
git -P log --oneline -5 && git status --short
git -P rev-parse HEAD origin/main              # these should match
.venv/bin/python -m amibuilder version
.venv/bin/python utils/scripts/audit-docs.py   # docs vs the real CLI; expect exit 0
uvx ruff check amibuilder test                 # expect "All checks passed!"
ps -Ao pid,etime,command | grep -i fs-uae | grep -v grep   # expect nothing
```

### State as of 2026-10-04

| | |
|---|---|
| Branch | `main`, clean except untracked `.kiro/settings/` |
| HEAD | `f782225` "Prettify the README.md" — **Dave's own commit, 2026-10-03** |
| Remote | `origin` = `http://192.168.1.71:3069/lmdracos/amibuilder.git` (self-hosted Gitea on the home LAN) |
| HEAD == origin/main | yes, `0 0` ahead/behind |
| Versions | amibuilder 0.1.0 · amitools 0.8.1 · Python 3.12.12 CPython · darwin |
| Commands | **32** |
| Tests | **1713** non-emulator · 63 emulator-marked · **1776** total · 97 in `test_emulator.py` |
| Last full gate | 1713 passed, 63 deselected, 23 min 10 s — **2026-08-29 at `9338b3b`** |
| ruff | clean |
| `audit-docs.py` | clean (87 invocations across the 5 root `.md` files — **including this one**, so the examples below cannot rot silently) |

**On the gate figure:** the only commit after `9338b3b` is `f782225`, which touches `README.md`
only. So the code is byte-identical to the last verified green run and 1713 should still hold —
**that is an inference, not a measurement.** Re-run before relying on it:

```bash
.venv/bin/python -m pytest -p no:cacheprovider -q -m "not emulator" --timeout=300
```

Expect ~23 minutes. Run it in the **foreground** with a generous timeout (see §4).

---

## 3. Where everything is

### Code

```
amibuilder/
  addressing.py   the spec grammar every command depends on -- READ §8 BEFORE TOUCHING
  cli.py          one big build_parser(); `completion` and `audit-docs.py` introspect it
  volume.py       the Volume abstraction over amitools; normalise(), check_name(), blocks_for()
  image.py        containers, partition resolution, open_addressed_volume()
  blocks.py mbr.py device.py timestamps.py render.py errors.py
  commands/       one module per command group (see below)
  layers/         the snapshot store: manifest, blobs, capture, compose, targets, hostdir, uaem
```

Command modules worth knowing by name:

| Module | Holds |
|---|---|
| `write.py` | `cp`, `mkdir`, `rm`, **`mv`** |
| `meta.py` | `touch`, `protect`, `comment`, `relabel` |
| `browse.py` `inspect.py` `extract.py` | the read commands (`ls`/`tree`/`find`/`du`/`cat`/`hexdump`, `info`/`partitions`/`check`, `get`) |
| `transfer.py` | shared host↔image and in-image copy primitives, incl. `copy_in_image` |
| `sync.py` `inject.py` `compare.py` | `sync`, `inject`, `diff` |
| `snap.py` `recipe.py` `compose.py` | the layer commands |
| `shell.py` | the interactive REPL (has its own `mv`, `cp`, `put`, `get`, `rm`) |
| `init.py` `format.py` `zerofree.py` `compact.py` | image creation and space reclamation |
| `doctor.py` `version.py` `completion.py` | the Meta bucket |

### Docs — and which kind each one is

**Root: user-facing. Every example must run.** `audit-docs.py` enforces this.

| File | What it is |
|---|---|
| `README.md` | Entry point, features, roadmap. **Dave edits this himself** — he reformatted it on 2026-10-03 |
| `USAGE.md` | The command reference. Addressing grammar, every command, the test-running section |
| `FAQ.md` | Task-shaped answers ("how do I back up a card", "do I need ROMs to run the tests") |
| `STATISTICS.md` | Measured claims, and a mutation-testing section listing vacuous guards found |

**`docs/`: working notes. Aspirational in places — by design.** Auditing them reports intent
as breakage, which is why `audit-docs.py` skips them unless you pass `--all`.

| File | What it is |
|---|---|
| `KIP-FFS-PLAN.md` | **The master document.** §0 is the live resume point: phase status, a reverse-chronological session log, loose ends, and the Phase 6 ergonomics backlog with every decision and its reasoning. Start here |
| `KIP-FFS-CLI-SURFACE.md` | The addressing grammar and argument shape, and why they are that way. **Read before proposing any change to how specs or destinations are written** |
| `KIP-FFS-NOTES.md` | Verified findings about FFS itself, G-numbered (G1, G15, G24, G30…). Cited all over the code |
| `KIP-FFS-LAYERS.md` | The layer-model design. §11/§12 record where the implementation departed from the design |
| `KIP-FFS-IDEAS.md` | Option analysis from before anything was built. Explicitly pre-decision |
| `KIP-FFS-INSTALLERS.md` | Analysis of the Commodore `Installer` script language against the real AmigaOS 3.2 installer |
| `KIP-FFS-STATS.md` | Raw measurements, incl. the test-count history table |

### Tooling in `utils/scripts/`

Not documented in README/USAGE by convention — these are maintainer tools. The plan and their
own docstrings are the documentation.

| Script | Purpose |
|---|---|
| `audit-docs.py` | Validates every documented `amibuilder` command line against the live parser. Exit 1 on drift, so it can gate. Default scope is every `*.md` at the repo root — which now includes this handoff. `--all` adds `docs/`, where 5 findings are expected and correct (three sketched commands in IDEAS, two `compose` options LAYERS' own header flags as unimplemented) |
| `boot-hdf.sh` | Boots an HDF under FS-UAE. `--turbo-floppy` is opt-in (it breaks copy-protected games) |
| `install-wb.sh` | Reproducible AmigaOS install from licensed media; delegates launching to `boot-hdf.sh` |
| `make-transfer-hdf.sh` | Predates the write commands; builds a transfer drive |
| `test-boot-hdf.sh` / `test-install-wb.sh` | 72 and 38 hermetic checks for the two shell scripts |
| `mutate-*.py` (5) | Mutation harnesses: `rm`, `write` (cp/mkdir), `shell`, `boot-hdf`, `install-wb` |

### Directories that are not yours to write

`source-files-do-not-add-to-git/`, `images/`, `ADFs/`, `ROMs/` hold licensed media and real disk
images. **Treat all four as read-only.** Build fixtures from scratch — `test/helpers/images.py`
exists for exactly that, and no test depends on a real image.

---

## 4. How to work here

Two steering files load automatically and you must follow them. **Do not duplicate their rules
into new documents; read them.**

- **`.kiro/steering/amibuilder-operations.md`** — tracked in this repo, so you get it from a
  clone. Covers FS-UAE process hygiene (strays are invisible because the window is hidden by
  default), why `window_hidden = 1` must stay, the window-restore requester, why
  `video_driver = none` must never be used as a test trigger, screen capture, and never testing
  against real images or `/dev/*`.
- **Dave's global steering** (`davcha/*` in his home directory) — scratch files go in
  `~/kip-scratch/<topic>/` and **never** `/tmp` (a reboot has destroyed the only copy of real
  work); truth-serum (say "I don't know" rather than guess); the CLI-surface and
  commit-discipline conventions.

The operational facts that bite hardest here, worth repeating because they cost time every time
they are forgotten:

- **One logical command per `execute_bash` call, on one line.** Heredocs hang. Multi-line
  commands with trailing `\` collapse and swallow the next line as an argument.
- **Terminal output is unreliable.** Redirect to a file under `~/kip-scratch/` and read it with
  the file tool. Direct stdout intermittently returns empty. Piping to `tail` masks exit codes.
- **Parallel tool calls do not run in the order you list them.** Writing `.commitmsg` and
  running `git commit -F .commitmsg` in one turn is a coin flip. It worked three times and
  failed once during this session. Sequence them.
- **Run the test suite in the foreground** with a generous timeout, split into
  `-m "not emulator"` then `test/test_emulator.py`. Background runs have stalled and left stale
  terminals that interfere with later runs.
- **`git -P`** for anything that paginates.

### Git workflow for this repo specifically

**This is a personal project on a self-hosted Gitea, not an Amazon repo.** Commit to `main` and
push directly — **no CR, no code review**. That is a deliberate exception to Dave's global
CR rules, which apply to Amazon work.

- Commit message via `fs_write .commitmsg` → `git commit -q -F .commitmsg` → `rm -f .commitmsg`
  (as its own command, not parallel with the write).
- `git add` **explicit files only**. Never `-A` or `.`.
- Separate commits per concern: `feat(x)` / `tooling:` / `docs:`. Dave likes this.
- **Full non-emulator gate green before pushing** anything that touches code.
- The Gitea host is on the **home LAN**. If Dave is elsewhere it is unreachable; commit locally
  and push later. Check with `nc -z -G 4 192.168.1.71 3069` — and **do not chain the push behind
  that check with `&&`**, because if `nc` fails the redirect never truncates and you will read a
  stale result file and report a push that did not happen. That happened in this session.
- **Always verify a push independently:** compare local `HEAD`, the fetched `origin/main`, and a
  direct `git ls-remote origin main`.

### Testing discipline this project holds itself to

- **Mutation-test every load-bearing guard.** A guard that stays green under its own mutation is
  decoration. `STATISTICS.md` has a running list of vacuous guards found this way, and the
  pattern to copy is `utils/scripts/mutate-write-guards.py`: exact source anchors, assert the
  anchor matched exactly once, require the named test to go **red**, restore and verify
  byte-identical with `filecmp.cmp(..., shallow=False)`.
- **Aim the mutation at the right property.** During this session a mutation survived because
  the test targeted convergence when the code was about timestamp *fidelity* — the code was
  fine and the comment above it was wrong. Another two survived because the guards only improved
  an error *message*, and the tests asserted generic wording that the underlying primitive
  produced anyway.
- **Add a degenerate/empty-input case** for anything computing over a collection.
- **Run the documented examples.** It has caught real doc bugs more than once.

---

## 5. Where the work stands

Phases 1–6 are complete. 32 commands. The project does everything it set out to do **except
write to real hardware**.

| Phase | State |
|---|---|
| 1 — read-only inspection | ✅ |
| 2 — layer capture | ✅ measured against a real AmigaOS 3.2 install |
| 3 — composition | ✅ all targets; verification on by default |
| 4 — additive writes | ✅ `cp` `mkdir` `rm` verified on real AmigaOS; `format`; `touch`/`protect`/`comment`/`relabel`; 4b `inject` |
| 5 — space reclamation | ✅ `zerofree` + `compact`, content-preservation verify on by default |
| 6 — shell + quality of life | ✅ `shell`, `diff`, `doctor`, `version`, `completion` |
| 7 — destructive writes | ✅ everything named shipped (`rm`, `sync --delete`, `mv`). Only the in-place-partial-card-write idea is untouched |
| Real hardware | ❌ **the remaining work** |

### What this session did (2026-08-26 → 08-29)

Six pieces, all on `main` and pushed:

1. **`.uaem` sidecar metadata for host↔image `sync`** (`5750cbc`, `d64a8aa`). On by default,
   `--no-metadata` to opt out. Protection bits and comments now survive a backup *and* a
   restore. The design hazard is the interesting part — see §8.
2. **A docs currency pass** (`bf07fd3`). Found a copy-pasteable example that exited 2, a
   self-contradicting status header, and a six-session-stale measurement.
3. **Ergonomics backlog #3 answered** (`29f9241`, `11c25f8`). The trailing-path form
   (`cp ./x card.hdf:Work/Utils`) measured across 35 spec shapes: **unambiguous**, and declined
   anyway on CLI surface area. The plan's recorded worry was wrong.
4. **The path-in-a-spec refusal** (`f5e7ff3`, `4ea28b2`). `card.hdf:Work/Utils` used to parse as
   a partition *named* `Work/Utils` and fail layers later; now it tells you to write
   `card.hdf:Work Utils`.
5. **`docs/KIP-FFS-CLI-SURFACE.md`** (`df80865`). Consolidated the addressing/argument decisions
   so they stop being re-derived.
6. **Top-level `mv`** (`9338b3b`) and **`utils/scripts/audit-docs.py`** (`e004fdf`), plus docs
   (`8b090cf`).

Then Dave reformatted `README.md` himself (`f782225`) and adopted `audit-docs.py` into a new
Contributing section — so he is using it.

---

## 6. Open items

### A. The remaining real work: hardware

**ZuluSCSI first, then the PiStorm/Emu68 `0x76` device write target.** The only item on the
README roadmap, and the last mile of the original goal — today you compose to an HDF and get it
onto the card yourself.

- Feasibility is **already proven** (`KIP-FFS-NOTES.md` §9.3): parsing the MBR for `0x76`
  entries and presenting the byte range to amitools as a file-like object needs no amitools
  changes. The read path works and is tested (`test_mbr_slice.py`, and the `amiga_card` fixture
  builds a PiStorm-shaped card).
- `compose` has **no `--target-partition`**, and there is no device write path. Verified against
  the parser, not assumed.
- **This is the highest-risk work in the project and wants its own design pass.** A wrong device
  write destroys a PiStorm card's Emu68 partition unrecoverably. The guard rails
  (`--device`, confirmation, the file-only line that `zerofree`/`compact`/`inject`/`sync` hold)
  exist precisely for this, and must not be widened to make a test pass.
- Dave's stated gate: *"it waits until there is a real card to test it on."* Do not start
  building without asking whether that card exists yet.

### B. Loose ends (from `KIP-FFS-PLAN.md` §0)

1. **An unexplained intermittent emulator failure.**
   `test_real_amigaos_boots_and_reports_back` once reported `emulator-exited` at 15.1 s with
   **exit code 0** and a complete clean shutdown log — FS-UAE quit itself mid-run. Seen once,
   not reproduced across four subsequent runs. The outcome reporting identifies it correctly;
   the cause is unknown.
2. **`get --preserve-times` has never been checked against a real Amiga.** It maps the Amiga
   triple to host local time, which is self-consistent. The *opposite* direction is verified:
   `cp --preserve-times` writes a host mtime of `1999-07-14 15:09:26` and AmigaDOS renders
   `14-Jul-99 15:09:26`. Both directions use the same interpretation, so that is strong
   circumstantial evidence — but the host-side leg is unproven. The emulator harness exists to
   close it. **Small, self-contained, and relevant the moment Dave actually uses `get`.**

### C. Deferred, with a reason

**`--image` / `AMIBUILDER_IMAGE` default** (ergonomics backlog #1). Blocked on a **safety
design**, not on effort. An environment variable that silently decides which image a command
operates on is a footgun on a tool that deletes things: a stale `AMIBUILDER_IMAGE` turns
`amibuilder rm S/Startup-Sequence` into a command that destroys something on a drive the user
was not thinking about — the same class of mistake the device guard rails exist to prevent. If
built, it should at minimum **print the image it resolved** and probably **refuse to apply the
default to the destructive commands** (`rm`, `format`, `zerofree`, `sync --delete`). Note the
case that actually bites (interactive) is already served by `shell`, so what remains is
scripting convenience — exactly where an invisible default is most dangerous.

### D. Decided against — do not re-propose without new evidence

All three keep getting reinvented because they are intuitive. Reasoning is in
`KIP-FFS-CLI-SURFACE.md`; the one-line versions:

| Proposal | Verdict |
|---|---|
| A `put` alias with the read commands' argument order | ❌ Two orders for one operation is *more* to learn, and every doc page has to pick a side |
| Trailing-path destination (`cp ./x card.hdf:Work/Utils`) | ❌ **Not for ambiguity** — it is provably unambiguous. It cannot *replace* `--to`, so it would be a second spelling for one destination |
| `--target-image` / `--target-partition` / `--target-path` flags | ❌ Trades ambiguity for a combinatorial validity matrix, multiplies on two-sided commands (`sync`/`diff`/`inject`), and takes the commonest invocation from 33 characters to 80 |

⚠️ **Do not record the trailing-path decision as "rejected because ambiguous."** That is a false
premise someone could act on: it would tell a future reader the grammar cannot support the
notation, when it demonstrably can. `KIP-FFS-CLI-SURFACE.md` §2 carries an explicit warning
about this.

### E. Maintenance debt

| Item | Detail |
|---|---|
| **`mv` mutations are not committed** | The 11 mutations that validate `mv`'s guards lived in a throwaway script. `utils/scripts/mutate-write-guards.py` is the established home and already covers `cp`/`mkdir` in exactly this pattern. **Fold them in** — otherwise the guards are unprotected against future refactors. Highest-value item in this table |
| **Test counts live in ~8 places** | `README.md` (now **two**: a badge `1776 · 63` and a table row `1713`), `USAGE.md` ×3, `FAQ.md`, `KIP-FFS-PLAN.md` ×2, `KIP-FFS-STATS.md` ×2. The badge is new from Dave's prettify. ⚠️ `KIP-FFS-NOTES.md` contains the string `1759` in an unrelated block calculation — **do not** bulk-replace |
| **`KIP-FFS-STATS.md` §8 emulator row** | Dated **2026-08-20** and never re-run, while the non-emulator row is current. Deliberately labelled as such rather than silently refreshed, because that half needs a Kickstart ROM |
| **The STATS history table has an acknowledged gap** | It jumps 1235 → 1689, swallowing six sessions. Flagged in the table itself. Backfilling would mean inventing counts nobody measured — decided against |
| **Stale local branch `phase3`** | Long merged. `git branch -d phase3` once you have checked it holds nothing |
| **`.kiro/settings/` is untracked and not gitignored** | Local CLI config plus a lock file. Either gitignore it or leave it; it shows up in every `git status` |
| **`mv` is files-only** | A directory move would copy the whole subtree. If wanted, `inject`'s `_plan`/`_preflight`/`_write_one`/`_restamp_dirs` already does a recursive metadata-preserving tree copy — but `_plan` reads inject-specific `args` fields, so it needs a small refactor first |
| **`audit-docs.py` is not wired into anything** | It is a manual step in README's Contributing section. Could be a `PostFileSave` hook on `*.md`, or run in the gate |

### F. Open questions needing Dave, not code

From `KIP-FFS-PLAN.md` §7. Q1 (headless emulator) and Q5 (tool name — `amibuilder`) are
answered. These are not, and they need him to inspect his own hardware:

- **Q2 — which AmigaOS versions across the machines?** Decides whether one base layer serves
  several machines, and whether any partition needs a filesystem binary embedded in the RDB
  (notes G17).
- **Q3 — do any drives use PFS3 or SFS?** Those are invisible to file-level tooling and need the
  byte-exact path instead.
- **Q4 — are the existing images RDB or plain?** Determines how much migration the existing
  collection needs.

Also unassembled: the **golden corpus** (`KIP-FFS-PLAN.md` §6) — reduced copies of real images
covering RDB multi-partition, plain HDF, DOS1/DOS3/OFS, a PiStorm card, PFS3, links, and
name-length edge cases. Every fixture is currently synthetic, which is a strength for
reproducibility and a gap for realism.

---

## 7. Recommended next steps

In the order I would take them, with reasoning. **Dave's own stated next move is to use the tool
for a while**, which outranks all of this.

1. **Fold the `mv` mutations into `utils/scripts/mutate-write-guards.py`.** 30 minutes, finishes
   work already done, and leaves `mv`'s six refusals protected. The eleven mutations and their
   target tests are listed in `KIP-FFS-PLAN.md`'s 2026-08-29 session row and
   `KIP-FFS-STATS.md`'s history row.
2. **Close loose end B2 (`get --preserve-times` on real AmigaOS).** Small, the harness exists,
   and it turns circumstantial evidence into proof on a path Dave will use.
3. **Then stop and ask.** The next substantial thing is hardware, and it should not be started
   at the tail of a session. Confirm the card exists, then design the write path and its guard
   rails before writing code.

Things to watch for while Dave uses it, from `KIP-FFS-CLI-SURFACE.md` §7 — this is the evidence
that should be allowed to overturn the §D decisions:

- Does `--to` actually trip him up, or just read oddly? Those need different fixes. A *mistype*
  argues for the trailing-path form after all; merely reading awkwardly argues for renaming it
  to `--into` (one flag, two commands, no grammar change).
- Does he reach for `card.hdf:Work/Utils`? Now measurable — each occurrence is an exit 2 he will
  remember.
- Does retyping the spec dominate, or is it the path? If the spec, the answer is `shell` or the
  deferred `--image`, not a grammar change.

---

## 8. Gotchas and hard-won facts

Measured, not assumed. Re-deriving these costs hours.

### Addressing

- **`parse` isolates the path portion first**, by splitting at `:` longest-first and checking
  filesystem existence, *before* the selector is examined. That is why a device path's own
  slashes and an image path's own slashes are never in scope for any selector rule. Everything
  in `KIP-FFS-CLI-SURFACE.md` depends on this.
- **A selector is only parsed when a `:` was actually split off**, so a bare `/dev/rdisk4` never
  reaches `_parse_selector`.
- **`parse` never opens an image.** Regex + `os.path.exists` + `os.stat`. An empty file called
  `card.hdf` parses exactly like a real one — which makes probing it trivially cheap, and means
  no parse-time error can depend on the image's kind. (Hence the known two-step: a path typed
  into a plain-HDF spec suggests `card.hdf:Work Utils`, which then correctly reports that a
  plain image has no partition table.)
- **A device is recognised by shape as well as by `stat`**, so a busy or privileged device still
  parses as a device and the guard rails engage. `/dev/rdisk99` parses fine and touches nothing —
  **use that for probes**, never a real `rdisk`.
- **`card.hdf:0x76:1` works on a plain file**, not only a device.

### Filesystem and amitools

- **amitools has no node-level rename** — only `ADFSVolume.relabel`, which renames the volume.
  Hence `mv` is copy-then-delete. A real rename means hash-chain surgery: unlink from the
  parent's chain by relinking its predecessor, rewrite the name and hash in the header block,
  splice into the new bucket, fix every checksum. **Not built, and the risk Phase 7 warned about.**
- **FFS compares names case-insensitively but preserves case.** So `List` and `list` are the same
  entry, which makes a case-only rename impossible via copy-then-delete — the copy collides with
  its own source. `mv` refuses it with that explanation.
- **`capture.capture_volume` never records the volume root as an entry**, so a volume-relative
  `rel` is never empty. `sync` relies on this: an empty `rel` would put a `.uaem` sidecar
  *beside* the backup directory instead of inside it.
- **Two amitools quirks are worked around at the block level:** `change_meta_info` skips a zero
  protect mask, and `change_comment` crashes on any comment (`len()` on a `FileName`).
- **amitools' timestamp epoch is built with `time.mktime`**, so it carries the reading host's
  January UTC offset and lands an hour out under DST. Everything here uses
  `amibuilder.timestamps` (pure subtraction, no timezone step) and passes `update_ts=False`.
  `layers/uaem.py` parses sidecars itself rather than calling amitools for the same reason.

### `sync` and `.uaem`

- **The load-bearing asymmetry: a missing sidecar means "no opinion", not "default".** A folder
  with no `.uaem` files reports the default `----rwed` for everything, because that is all
  `DirectoryVolume` can infer from a plain file. Reading that as a *statement* would make a
  restore **strip** the card's protection bits — on a real Workbench 3.2 install that is 742 of
  882 entries (84%), including the pure bit on all 83 commands in `C/`, which silently breaks
  `Resident`. `_states_metadata` probes for the sidecar file itself. **Do not "simplify" this.**
- **Sidecars are written unconditionally** per touched entry, not only when informative. That is
  what keeps absence unambiguous, and it avoids needing a removal path when metadata returns to
  the default. It also makes a synced folder the same shape as `compose --format dir` output.
- An empty `protect` on a `SyncItem` is the marker for "the source stated nothing".

### Whole-or-nothing writes

Every write into an image is **pre-flighted whole** — names, capacity, collisions — before a
single block is written, so a refusal leaves the image untouched rather than half-updated.
`KIP-FFS-NOTES.md` G1 is why: filenames over 30 characters simply cannot exist on classic FFS, so
a naive copy loop aborts partway. `sync._preflight_image`, `write._preflight` and
`inject._preflight` are the three implementations; reuse one rather than writing a fourth.

### Environment

- **`/tmp` does not resolve consistently between the edit tools and `execute_bash`**, and a
  reboot takes it. Use `~/kip-scratch/<topic>/`.
- **`uvx ruff check amibuilder test`** is the lint command. `utils/scripts/*.py` are **not** in
  scope, and `mutate-boot-hdf-guards.py` has 3 pre-existing findings — leave them.
- **83 IDE warnings on `test/test_sync_cli.py`** are "redefining name from outer scope" for
  pytest fixtures, across the whole file. Deliberate, not in ruff's selected set. Likewise the
  four "protected member `_actions`" warnings in `audit-docs.py` and `completion.py` — reading
  argparse privates is the point, and both docstrings say so.
- **`brazil`/CR tooling does not apply here.** This is not an Amazon package.

---

## 9. Conventions Dave cares about

Observed over this session, worth matching:

- **Curated, not blanket.** When a sweep is possible, classify each case rather than bulk-apply.
  A blind `sed` over "every mention of X" has been explicitly rejected here.
- **Preserve intentional patterns and the "why" comments.** The codebase carries a lot of
  reasoning in comments. If a comment explains why something looks odd, it stays.
- **Correct him when he is wrong**, and flag design decisions rather than quietly making them.
  He asked specifically for this. He has also corrected *me* productively more than once —
  including on whether sshd keepalive applied to an SSM session, and on whether a measured
  behaviour was "physics" or a single broken box.
- **Per-feature commits**, thorough commit messages that record the reasoning and the rejected
  alternatives, docs updated in the same breath.
- **Flag what you could not verify.** Explicitly: he values "I don't know" over a confident
  guess. The habit that has paid off most here is distinguishing *measured* from *inferred* in
  the same sentence.
- He writes long, reasoned docs and wants them thorough — *"Be thorough. Super thorough. Explain
  everything. Over explain if you have to."* That is why these documents look the way they do.

---

## 10. One-paragraph version, if you read nothing else

amibuilder is feature-complete for image files: 32 commands, 1713 non-emulator tests green as of
2026-08-29, everything pushed to `main` on a self-hosted Gitea with no code review. The one
remaining capability is writing to real hardware — ZuluSCSI then the PiStorm `0x76` target — and
it is deliberately unstarted because it is the highest-risk work and needs a real card to test
against. The best immediate task is folding the `mv` mutation tests into
`utils/scripts/mutate-write-guards.py`, because that finishes work already done and leaves a
shipped command's guards protected. Before changing anything about how commands name images,
partitions or paths, read `docs/KIP-FFS-CLI-SURFACE.md` — three intuitive redesigns have already
been analysed and declined there, and the reasons are not the obvious ones.
