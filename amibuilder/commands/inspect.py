"""`info`, `partitions` and `check` -- what is this image, and is it sound?"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import blocks, timestamps
from ..errors import ImageError, UnsupportedError
from ..image import Container, ImageKind
from ..render import Output, Table, human_bytes
from . import opened_container

# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def cmd_info(args: Any, out: Output) -> int:
    with opened_container(args) as c:
        data: dict[str, Any] = c.as_dict()

        out.field("Image", c.address.path)
        out.field("Kind", _kind_label(c))
        if c.address.is_device:
            out.field("Device", "yes (raw)")
        out.field("Size", f"{human_bytes(c.size_bytes)}  ({c.size_bytes} bytes)")

        g = c.geometry
        if g.cyls:
            out.field(
                "Geometry",
                f"{g.cyls} cyl x {g.heads} head x {g.sectors} sec, "
                f"{g.num_blocks} blocks of {g.block_size}",
            )
        else:
            out.field("Blocks", f"{g.num_blocks} of {g.block_size}")

        if c.address.has_mbr_selector:
            out.field("MBR slice", f"slot {c.address.mbr_slot} at byte "
                                   f"{c.slice_offset} ({human_bytes(c.size_bytes)})")

        if c.kind is ImageKind.MBR:
            _report_mbr(c, out, data)
            out.data(data)
            return 0

        if c.kind is ImageKind.RDB:
            parts = c.partitions()
            data["partitions"] = [p.as_dict() for p in parts]
            out.field("Partitions", len(parts))
            out.heading("Partitions:")
            out.table(_partition_table(parts))
            out.data(data)
            return 0

        # Flat container: report the single volume, or say clearly why there is none.
        if c.boot_dos_type is not None:
            out.field("DosType", c.boot_dos_type.describe())
        try:
            with c.open_addressed_volume() as vol:
                info = vol.info()
                data["volume"] = info.as_dict()
                _report_volume(info, out)
        except UnsupportedError as e:
            data["volume"] = None
            data["volume_error"] = str(e)
            out.heading(f"No readable filesystem: {str(e).split(': ', 1)[-1]}")
        out.data(data)
    return 0


def _kind_label(c: Container) -> str:
    labels = {
        ImageKind.RDB: "RDB whole-disk image (RigidDiskBlock + partitions)",
        ImageKind.PLAIN_HDF: "plain HDF (single volume, no partition table)",
        ImageKind.ADF: "ADF floppy image",
        ImageKind.MBR: "MBR-partitioned container",
        ImageKind.DIRECTORY: "host directory",
    }
    return labels.get(c.kind, c.kind.value)


def _report_mbr(c: Container, out: Output, data: dict[str, Any]) -> None:
    parts = c.mbr_partitions()
    data["mbr_partitions"] = [
        {
            "slot": p.index,
            "type": f"0x{p.ptype:02x}",
            "type_name": p.type_name,
            "start_lba": p.start_lba,
            "sectors": p.sector_count,
            "bytes": p.byte_length,
            "is_amiga": p.is_amiga,
        }
        for p in parts
    ]
    out.heading("MBR primary partitions:")
    t = Table(["slot", "type", "name", "start LBA", "size", "amiga"],
              align=["r", "l", "l", "r", "r", "l"], indent="  ")
    for p in parts:
        t.add(p.index, f"0x{p.ptype:02x}", p.type_name, p.start_lba,
              human_bytes(p.byte_length), "yes" if p.is_amiga else "-")
    out.table(t)

    amiga = [p for p in parts if p.is_amiga]
    out.line()
    if amiga:
        out.line("This container holds no filesystem of its own. Address an Amiga slot:")
        for p in amiga:
            out.line(f"    amibuilder info {c.address.path}:0x76:{p.index}")
    else:
        out.line("No type 0x76 slots -- nothing here for amibuilder to read.")


def _report_volume(info: Any, out: Output) -> None:
    out.heading("Volume:")
    out.field("  Name", info.name)
    out.field("  DosType", info.dos_type.describe())
    out.field("  Block size", info.block_size)
    out.field("  Total", f"{human_bytes(info.total_bytes)}  ({info.total_blocks} blocks)")
    out.field("  Used", f"{human_bytes(info.used_bytes)}  ({info.used_blocks} blocks, "
                        f"{info.percent_used:.2f}%)")
    out.field("  Free", f"{human_bytes(info.free_bytes)}  ({info.free_blocks} blocks)")
    out.field("  Root block", info.root_block)
    if info.mod_secs:
        out.field("  Modified", timestamps.format(info.mod_secs))
    if info.create_secs:
        out.field("  Created", timestamps.format(info.create_secs))


# ---------------------------------------------------------------------------
# partitions
# ---------------------------------------------------------------------------


def _partition_table(parts: list[Any]) -> Table:
    t = Table(
        ["#", "device", "volume", "dostype", "cyls", "size", "flags"],
        align=["r", "l", "l", "l", "r", "r", "l"],
        indent="  ",
    )
    for p in parts:
        flags = []
        if p.bootable:
            flags.append(f"boot({p.boot_pri})")
        if not p.automount:
            flags.append("nomount")
        if not p.dos_type.supported:
            flags.append("UNSUPPORTED")
        vol = p.volume_name if p.volume_name is not None else f"<{p.volume_error or '?'}>"
        t.add(
            p.index, p.device_name, vol, p.dos_type.describe(),
            f"{p.low_cyl}-{p.high_cyl}", human_bytes(p.num_bytes),
            " ".join(flags) or "-",
        )
    return t


def cmd_partitions(args: Any, out: Output) -> int:
    with opened_container(args) as c:
        if c.kind is ImageKind.MBR:
            data = c.as_dict()
            _report_mbr(c, out, data)
            out.data(data)
            return 0

        if c.kind is not ImageKind.RDB:
            out.line(
                f"{c.address.path}: {_kind_label(c)} -- no partition table. "
                f"It holds a single volume."
            )
            out.data({**c.as_dict(), "partitions": []})
            return 0

        parts = c.partitions(probe_volumes=not args.no_probe)
        out.table(_partition_table(parts))
        if args.verbose:
            out.heading("Details:")
            for p in parts:
                out.line(f"  #{p.index} {p.device_name}")
                out.field("    reserved", p.reserved, width=20)
                out.field("    block size", p.block_size, width=20)
                out.field("    mask", f"0x{p.mask:08x}", width=20)
                out.field("    max transfer", f"0x{p.max_transfer:08x}", width=20)
                out.field("    buffers", p.num_buffer, width=20)
                out.field("    blocks", p.num_blocks, width=20)
        out.data({**c.as_dict(), "partitions": [p.as_dict() for p in parts]})
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

#: A bitmap-allocation error, which is the shape the dircache false positive takes.
_BITMAP_RE = re.compile(
    r"Invalid bitmap allocation .*?blks \[(\d+)\.\.\.(\d+)\].*?"
    r"got=([0-9a-fA-F]+)\s+expect=([0-9a-fA-F]+)"
)

CLASS_REAL = "error"
CLASS_LIMITATION = "validator-limitation"


@dataclass
class Finding:
    level: str
    message: str
    block: int | None = None
    classification: str = CLASS_REAL
    explanation: str = ""
    blocks_involved: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "level": self.level,
            "message": self.message,
            "classification": self.classification,
        }
        if self.block is not None and self.block >= 0:
            d["block"] = self.block
        if self.explanation:
            d["explanation"] = self.explanation
        if self.blocks_involved:
            d["blocks"] = self.blocks_involved
        return d


def cmd_check(args: Any, out: Output) -> int:
    with opened_container(args) as c:
        if c.kind is ImageKind.MBR:
            raise ImageError(
                f"{c.address.path}: MBR container -- check a specific Amiga slot, "
                f"e.g. '{c.address.path}:0x76:1'"
            )

        targets: list[int | str | None]
        if c.kind is ImageKind.RDB and c.address.partition is None:
            targets = [p.index for p in c.partitions(probe_volumes=False)]
        else:
            targets = [c.address.partition]

        results = []
        worst = 0
        for target in targets:
            res = _check_one(c, target, args)
            results.append(res)
            worst = max(worst, res["exit"])
            _report_check(res, out, verbose=args.verbose)

        out.data({**c.as_dict(), "checks": results})
    return worst


def _check_one(c: Container, target: int | str | None, args: Any) -> dict[str, Any]:
    from amitools.fs.validate.Validator import Validator

    label = f"{c.address.path}" + (f":{target}" if target is not None else "")

    try:
        vol = c.open_volume(target)
    except UnsupportedError as e:
        return {
            "target": label, "skipped": True, "reason": str(e),
            "errors": 0, "warnings": 0, "limitations": 0, "findings": [], "exit": 0,
        }

    with vol:
        volinfo = vol.info()
        # The validator takes a block device rather than a mounted filesystem.
        v = Validator(vol.blkdev, min_level=0 if args.verbose else 1)

        # The five steps, in order. Omitting scan_files() leaves data blocks unaccounted
        # for and produces thousands of bogus bitmap errors (notes section 5.5).
        boot_dos, bootable = v.scan_boot()
        root_ok = False
        if boot_dos:
            root_ok = bool(v.scan_root())
            if root_ok:
                v.scan_dir_tree()
                v.scan_files()
                v.scan_bitmap()

        findings = _classify(v, c, volinfo)

    real_errors = [f for f in findings if f.level == "error" and f.classification == CLASS_REAL]
    limitations = [f for f in findings if f.classification == CLASS_LIMITATION]
    warnings = [f for f in findings if f.level == "warning"]

    exit_code = 0
    if not boot_dos or not root_ok:
        exit_code = 6
    elif real_errors:
        exit_code = 6

    return {
        "target": label,
        "skipped": False,
        "volume": volinfo.name,
        "dos_type": volinfo.dos_type.label,
        "boot_is_dos": bool(boot_dos),
        "bootable": bool(bootable),
        "root_valid": root_ok,
        "errors": len(real_errors),
        "warnings": len(warnings),
        "limitations": len(limitations),
        "findings": [f.as_dict() for f in findings],
        "exit": exit_code,
    }


def _classify(validator: Any, c: Container, volinfo: Any) -> list[Finding]:
    """Turn validator log entries into findings, separating known validator gaps.

    The dircache case is *classified*, not suppressed. amitools' validator has no
    dircache support at all, so every dircache block on a DOS4/DOS5 volume produces a
    bitmap error (notes section 5.5). Blanket-ignoring bitmap errors on such volumes
    would also hide genuine corruption, so each discrepant block is read and identified
    independently: only an error whose every discrepant block is a checksum-valid
    dircache block is reclassified.
    """
    log = validator.log
    levels = {log.DEBUG: "debug", log.INFO: "info", log.WARN: "warning", log.ERROR: "error"}

    findings: list[Finding] = []
    stream = None
    try:
        for entry in log.entries:
            level = levels.get(entry.level, "info")
            if level in ("debug", "info"):
                continue
            message = entry.msg
            blk = entry.blk_num if entry.blk_num is not None and entry.blk_num >= 0 else None
            finding = Finding(level=level, message=message, block=blk)

            m = _BITMAP_RE.search(message)
            if m and level == "error":
                if stream is None:
                    stream = c.stream()
                finding = _classify_bitmap(finding, m, stream, volinfo)
            findings.append(finding)
    finally:
        if stream is not None:
            stream.close()
    return findings


def _classify_bitmap(finding: Finding, m: re.Match, stream: Any, volinfo: Any) -> Finding:
    first, last = int(m.group(1)), int(m.group(2))
    got, expect = int(m.group(3), 16), int(m.group(4), 16)

    # A set bit means FREE. Bits the validator expects free but the bitmap has allocated
    # are the ones to explain; bit i of the mask maps to block first+i, LSB first.
    discrepant = expect & ~got
    blocks_involved = [
        first + i for i in range(last - first + 1) if discrepant & (1 << i)
    ]
    finding.blocks_involved = blocks_involved
    if not blocks_involved:
        return finding

    dircache_blocks = []
    for num in blocks_involved:
        try:
            blk = blocks.read_block(stream, num, volinfo.block_size)
        except EOFError:
            return finding
        ident = blocks.identify_block(blk, num)
        if ident.kind == "dircache" and ident.checksum_ok:
            dircache_blocks.append(num)

    if len(dircache_blocks) == len(blocks_involved):
        finding.classification = CLASS_LIMITATION
        finding.explanation = (
            f"all {len(dircache_blocks)} block(s) are valid dircache blocks. amitools' "
            f"validator has no dircache support, so it treats them as unallocated. The "
            f"volume is correct; the validator is incomplete."
        )
    return finding


def _report_check(res: dict[str, Any], out: Output, verbose: bool) -> None:
    if res["skipped"]:
        out.line(f"{res['target']}: SKIPPED -- {res['reason']}")
        return

    bits = [f"{res['target']}: {res['volume']!r} {res['dos_type']}"]
    out.line(" ".join(bits))

    if not res["boot_is_dos"]:
        out.line("  boot block is not AmigaDOS -- nothing further checked")
        return
    if not res["root_valid"]:
        out.line("  ERROR: root block invalid -- filesystem unreadable")
        return

    for f in res["findings"]:
        if f["classification"] == CLASS_LIMITATION:
            continue
        where = f" @{f['block']}" if "block" in f else ""
        out.line(f"  {f['level'].upper()}{where}: {f['message']}")

    lim = [f for f in res["findings"] if f["classification"] == CLASS_LIMITATION]
    if lim:
        n_blocks = sum(len(f.get("blocks", [])) for f in lim)
        out.line(
            f"  note: {len(lim)} bitmap finding(s) covering {n_blocks} dircache block(s) "
            f"are amitools validator limitations, not corruption"
        )
        if verbose:
            for f in lim:
                out.line(f"    - {f['message']}")
                out.line(f"      {f['explanation']}")

    verdict = "ok" if res["exit"] == 0 else "PROBLEMS FOUND"
    extra = []
    if res["errors"]:
        extra.append(f"{res['errors']} error(s)")
    if res["warnings"]:
        extra.append(f"{res['warnings']} warning(s)")
    if res["limitations"]:
        extra.append(f"{res['limitations']} validator limitation(s)")
    if res["bootable"]:
        extra.append("bootable")
    out.line(f"  {verdict}" + (f" -- {', '.join(extra)}" if extra else ""))
