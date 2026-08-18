"""Capture an image, compose it back, and prove the result matches.

This is the test the whole project rests on. Everything else checks a piece; this checks that a
drive can go into the layer store and come back out with its contents, its metadata, its partition
layout and its DosEnvec intact -- because a composed drive that differs from its source in some
unnoticed way is worse than no tool at all.
"""

from __future__ import annotations

import pytest

from amibuilder.addressing import parse
from amibuilder.cli import main
from amibuilder.image import DOS_ENV_FIELDS, ImageKind, open_container
from amibuilder.layers import capture as C
from amibuilder.layers import compose as CP
from amibuilder.layers import manifest as M
from amibuilder.layers import store as S
from amibuilder.layers import targets as T
from amibuilder.layers import drive as D
from helpers import images

#: A tree with the awkward cases: nested directories, an empty directory, a file needing several
#: data blocks, an empty file, and metadata worth losing.
TREE = {
    "S/Startup-Sequence": b"C:SetPatch QUIET\nC:Version >NIL:\n",
    "S/Shell-Startup": b'Prompt "%N.%S> "\n',
    "C/List": bytes(range(256)) * 8,
    "C/Dir": bytes(700),
    "Devs/DOSDrivers/CD0": b"FileSystem = L:CDFileSystem\n",
    "Libs/thing.library": bytes(9_000),
    "Tools/Calculator.info": bytes(1_200),
    "Empty/keeper": b"",
}


def two_partition_source(path: str) -> str:
    images.make_rdb_hdf(
        path,
        size="32Mi",
        partitions=[
            images.Partition(size="12MiB", dos_type="ffs+intl", bootable=True,
                             volume="Workbench"),
            images.Partition(dos_type="ffs+intl", volume="Work"),
        ],
    )
    images.write_files(path, TREE, part=0)
    images.write_files(path, {"Games/readme": b"second partition content"}, part=1)
    return path


def snapshot(path: str) -> dict:
    """Everything about an image that composition is supposed to reproduce."""
    with open_container(parse(path)) as container:
        out: dict = {
            "kind": container.kind.value,
            "geometry": container.geometry.as_dict(),
            "partitions": [],
            "volumes": {},
        }
        for part in container.partitions(probe_volumes=True):
            out["partitions"].append({
                "index": part.index,
                "device": part.device_name,
                "volume": part.volume_name,
                "dos_type": part.dos_type.raw,
                "bootable": part.bootable,
                "automount": part.automount,
                "low_cyl": part.low_cyl,
                "high_cyl": part.high_cyl,
                "dos_env": dict(part.dos_env),
            })
            if part.volume_name is None:
                continue
            with container.open_volume(part.index) as vol:
                entries = {}
                data = {}
                for _dirpath, dirs, files in vol.walk():
                    for entry in list(dirs) + list(files):
                        entries[entry.path] = {
                            "is_dir": entry.is_dir,
                            "size": entry.size,
                            "protect": entry.protect_str,
                            "comment": entry.comment,
                            "ts": (entry.mod_secs, entry.mod_ticks),
                        }
                        if not entry.is_dir:
                            data[entry.path] = vol.read_file(entry.path)
                out["volumes"][part.volume_name] = {"entries": entries, "data": data}
        return out


@pytest.fixture
def composed(workdir, tmp_path):
    """Capture a two-partition RDB, compose it straight back out, return both snapshots."""
    source = two_partition_source(str(workdir / "source.hdf"))
    store = S.Store(str(tmp_path / "store"))
    store.init()

    with open_container(parse(source)) as container:
        drive_record = D.capture(container)
        result = C.capture_container(container, store.blobs)
    assert result.warnings == [], f"capture warned: {result.warnings}"

    layer = store.write_layer(
        entries=result.entries, kind=S.KIND_BASE, label="base", drive=drive_record
    )
    store.set_ref("base", layer.id)

    target = str(workdir / "composed.hdf")
    plan = CP.build_plan(store, ["base"])
    assert plan.is_writable, f"plan refused: {[p.message for p in plan.blocking]}"
    write = T.write_rdb(plan, store.blobs, target)

    return {
        "source": source,
        "target": target,
        "before": snapshot(source),
        "after": snapshot(target),
        "write": write,
        "store": store,
        "layer": layer,
    }


# ---------------------------------------------------------------------------
# The drive itself
# ---------------------------------------------------------------------------


def test_composed_image_is_an_rdb(composed):
    assert composed["after"]["kind"] == ImageKind.RDB.value


def test_geometry_is_reproduced(composed):
    before, after = composed["before"]["geometry"], composed["after"]["geometry"]
    for field in ("block_size", "cylinders", "heads", "sectors", "num_blocks", "total_bytes"):
        assert after[field] == before[field], f"{field} differs"


def test_image_is_the_same_size(composed):
    import os

    assert os.path.getsize(composed["target"]) == os.path.getsize(composed["source"])


def test_every_partition_is_reproduced(composed):
    before = composed["before"]["partitions"]
    after = composed["after"]["partitions"]
    assert len(after) == len(before)
    for b, a in zip(before, after):
        for field in ("index", "device", "volume", "dos_type", "bootable", "automount",
                      "low_cyl", "high_cyl"):
            assert a[field] == b[field], f"partition {b['device']} {field}: {a[field]} != {b[field]}"


def test_the_dosenvec_is_reproduced_field_for_field(composed):
    """The reason a base layer records it at all: a guessed mask corrupts data on real hardware."""
    for b, a in zip(composed["before"]["partitions"], composed["after"]["partitions"]):
        for field in DOS_ENV_FIELDS:
            assert a["dos_env"][field] == b["dos_env"][field], (
                f"partition {b['device']} dos_env.{field}: "
                f"{a['dos_env'][field]} != {b['dos_env'][field]}"
            )


def test_the_mask_and_max_transfer_survive(composed):
    """Called out separately because these two are the documented corruption footgun."""
    for b, a in zip(composed["before"]["partitions"], composed["after"]["partitions"]):
        assert a["dos_env"]["mask"] == b["dos_env"]["mask"]
        assert a["dos_env"]["max_transfer"] == b["dos_env"]["max_transfer"]


def test_the_bootable_flag_survives(composed):
    bootable = [p["device"] for p in composed["after"]["partitions"] if p["bootable"]]
    assert bootable == ["DH0"]


def test_composed_image_passes_check(composed, capsys):
    code = main(["check", composed["target"]])
    output = capsys.readouterr().out
    assert code == 0, f"check found problems:\n{output}"


# ---------------------------------------------------------------------------
# The contents
# ---------------------------------------------------------------------------


def test_both_volumes_are_present(composed):
    assert set(composed["after"]["volumes"]) == set(composed["before"]["volumes"])
    assert set(composed["after"]["volumes"]) == {"Workbench", "Work"}


def test_every_path_is_present(composed):
    for volume, before in composed["before"]["volumes"].items():
        after = composed["after"]["volumes"][volume]
        missing = set(before["entries"]) - set(after["entries"])
        extra = set(after["entries"]) - set(before["entries"])
        assert not missing, f"{volume}: missing {sorted(missing)}"
        assert not extra, f"{volume}: unexpected {sorted(extra)}"


def test_every_file_is_byte_identical(composed):
    for volume, before in composed["before"]["volumes"].items():
        after = composed["after"]["volumes"][volume]
        for path, expected in before["data"].items():
            assert after["data"][path] == expected, f"{volume}:{path} content differs"


def test_a_multi_block_file_survives(composed):
    """9,000 bytes needs several data blocks, so this exercises more than the header block."""
    data = composed["after"]["volumes"]["Workbench"]["data"]
    assert len(data["Libs/thing.library"]) == 9_000


def test_the_empty_file_survives(composed):
    assert composed["after"]["volumes"]["Workbench"]["data"]["Empty/keeper"] == b""


def test_nested_directories_survive(composed):
    entries = composed["after"]["volumes"]["Workbench"]["entries"]
    assert entries["Devs/DOSDrivers"]["is_dir"]
    assert "Devs/DOSDrivers/CD0" in entries


def test_protection_bits_survive(composed):
    for volume, before in composed["before"]["volumes"].items():
        after = composed["after"]["volumes"][volume]
        for path, meta in before["entries"].items():
            assert after["entries"][path]["protect"] == meta["protect"], f"{volume}:{path}"


def test_comments_survive(composed):
    for volume, before in composed["before"]["volumes"].items():
        after = composed["after"]["volumes"][volume]
        for path, meta in before["entries"].items():
            assert after["entries"][path]["comment"] == meta["comment"], f"{volume}:{path}"


def test_timestamps_survive_to_the_tick(composed):
    """The point of storing the raw triple. An hour's drift here would mean the epoch leaked in."""
    for volume, before in composed["before"]["volumes"].items():
        after = composed["after"]["volumes"][volume]
        for path, meta in before["entries"].items():
            assert after["entries"][path]["ts"] == meta["ts"], (
                f"{volume}:{path} timestamp {after['entries'][path]['ts']} != {meta['ts']}"
            )


def test_file_sizes_survive(composed):
    for volume, before in composed["before"]["volumes"].items():
        after = composed["after"]["volumes"][volume]
        for path, meta in before["entries"].items():
            assert after["entries"][path]["size"] == meta["size"], f"{volume}:{path}"


# ---------------------------------------------------------------------------
# Re-capturing the composed image
# ---------------------------------------------------------------------------


def test_recapturing_the_composed_image_yields_the_same_manifest(composed):
    """The strongest statement available: capture is idempotent across a composition.

    If the composed drive were different in any way capture can see, the manifest bytes would
    differ and so would the layer ID.
    """
    store = composed["store"]
    with open_container(parse(composed["target"])) as container:
        again = C.capture_container(container, store.blobs)
    assert again.warnings == [], f"re-capture warned: {again.warnings}"

    original = store.read_manifest(composed["layer"].id)
    assert M.canonical_bytes(again.entries) == M.canonical_bytes(original)


def test_recapturing_produces_the_same_layer_id(composed):
    store = composed["store"]
    with open_container(parse(composed["target"])) as container:
        drive_record = D.capture(container)
        again = C.capture_container(container, store.blobs)

    relayer = store.write_layer(
        entries=again.entries, kind=S.KIND_BASE, label="recaptured", drive=drive_record
    )
    assert relayer.id == composed["layer"].id


def test_diffing_the_composed_image_against_its_source_layer_is_empty(composed):
    """The user-facing form of the same claim: `snap diff` should find nothing to record."""
    store = composed["store"]
    with open_container(parse(composed["target"])) as container:
        again = C.capture_container(container, store.blobs)
    result = C.diff(store.read_manifest(composed["layer"].id), again.entries)
    assert result.is_empty, f"unexpected differences: {[c.as_dict() for c in result.changes]}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_write_result_counts_both_volumes(composed):
    write = composed["write"]
    assert sorted(write.volumes) == ["Work", "Workbench"]
    assert write.files == sum(
        1 for v in composed["before"]["volumes"].values() for _ in v["data"]
    )


def test_write_reports_no_warnings_for_a_faithful_reproduction(composed):
    """Any warning here means something about the source was not reproduced exactly."""
    assert composed["write"].warnings == [], composed["write"].warnings


def flat_stack(store: S.Store, workdir, files: dict[str, bytes] | None = None) -> S.Store:
    """A captured single-volume (plain) HDF, registered as ref 'flat'."""
    source = images.make_plain_hdf(str(workdir / "flat.hdf"), size="20Mi", volume="Flat")
    if files:
        images.write_files(source, files)
    with open_container(parse(source)) as container:
        record = D.capture(container)
        result = C.capture_container(container, store.blobs)
    layer = store.write_layer(
        entries=result.entries, kind=S.KIND_BASE, label="flat", drive=record
    )
    store.set_ref("flat", layer.id)
    return store


def test_rdb_refuses_a_stack_with_no_partition_table(tmp_path, workdir):
    """A plain HDF source has no layout to reproduce, so the refusal must name the way out."""
    store = S.Store(str(tmp_path / "store"))
    store.init()
    flat_stack(store, workdir, {"hello": b"content"})

    plan = CP.build_plan(store, ["flat"])
    assert plan.is_writable, "the plain stack itself should compose fine"
    with pytest.raises(Exception, match="no partition table"):
        T.write_rdb(plan, store.blobs, str(workdir / "out.hdf"))


def test_rdb_refusal_does_not_depend_on_the_plan_being_writable(tmp_path, workdir):
    """The format mismatch is structural, so it must be reported even for an unwritable plan.

    Otherwise asking for `--format rdb` on a plain stack reports "unresolved problems", sending
    the user to look at a problem list that has nothing to do with why it refused.
    """
    store = S.Store(str(tmp_path / "store"))
    store.init()
    flat_stack(store, workdir)  # no files -> nothing to compose -> unwritable plan

    plan = CP.build_plan(store, ["flat"])
    assert not plan.is_writable
    with pytest.raises(Exception, match="no partition table"):
        T.write_rdb(plan, store.blobs, str(workdir / "out.hdf"))


def test_an_empty_single_volume_capture_has_no_volume_to_compose(tmp_path, workdir):
    """Pins a known gap: a plain HDF records no volume name, only its partitions would.

    A single-volume drive record carries `partitions: []`, so the volume name reaches the plan
    only through the manifest entries. Capture an empty volume and there is nothing to name it
    with, so the plan covers nothing. Harmless for a drive with files on it -- which is every
    real backup -- but it means `capture an empty formatted volume, compose it back` does not
    work. Recorded here so the behaviour is deliberate rather than a surprise; fixing it means
    adding the volume name to the drive record, which changes layer IDs.
    """
    store = S.Store(str(tmp_path / "store"))
    store.init()
    flat_stack(store, workdir)

    plan = CP.build_plan(store, ["flat"])
    assert plan.volumes == []
    assert not plan.is_writable
    assert any("no volumes" in p.message for p in plan.blocking)
