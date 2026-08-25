"""CLI tests for `doctor`, the environment self-check.

The filesystem-dependent checks (hole-punching, sparse store) are exercised on both the
real host and via monkeypatching `_probe_sparse`, so the warn paths are covered without
needing an actual exFAT/FAT volume. amitools and Python version outcomes are driven by
monkeypatching their readers, so the fail/warn/pass classifications and the exit-code
wiring are all tested deterministically.
"""

import json
import sys
from collections import namedtuple

import pytest

from amibuilder.cli import main
from amibuilder.commands import doctor


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


def _by_name(payload: dict) -> dict:
    return {c["name"]: c for c in payload["checks"]}


ALL_CHECKS = ("amibuilder", "python", "amitools", "hole-punching",
              "sparse store", "readline", "device guard")


def test_doctor_human_reports_every_check(run):
    code, out, err = run("doctor")
    assert code == 0, err
    for name in ALL_CHECKS:
        assert name in out
    assert "all checks passed." in out


def test_doctor_json_shape(run):
    code, out, _ = run("doctor", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    checks = _by_name(payload)
    assert set(checks) >= set(ALL_CHECKS)
    for c in payload["checks"]:
        assert {"name", "status", "detail"} <= set(c)
    assert set(payload["summary"]) == {"ok", "warn", "fail", "info"}
    assert payload["summary"]["fail"] == 0


def test_doctor_store_probe_targets_the_given_directory(run, workdir):
    code, out, _ = run("doctor", "--store", str(workdir), "--json")
    assert code == 0
    store = _by_name(json.loads(out))["sparse store"]
    assert store["store"] == str(workdir)


def test_doctor_store_nonexistent_is_a_usage_error(run, workdir):
    code, _, err = run("doctor", "--store", str(workdir / "nope"))
    assert code == 2
    assert "not a directory" in err


def test_doctor_missing_amitools_fails(run, monkeypatch):
    monkeypatch.setattr(doctor, "_amitools_version", lambda: None)
    code, out, _ = run("doctor", "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert _by_name(payload)["amitools"]["status"] == "fail"
    assert payload["summary"]["fail"] == 1


def test_doctor_amitools_below_floor_fails(run, monkeypatch):
    monkeypatch.setattr(doctor, "_amitools_version", lambda: "0.7.9")
    code, out, _ = run("doctor", "--json")
    assert code == 1
    amitools = _by_name(json.loads(out))["amitools"]
    assert amitools["status"] == "fail"
    assert "below" in amitools["detail"]


def test_doctor_amitools_newer_only_warns(run, monkeypatch):
    monkeypatch.setattr(doctor, "_amitools_version", lambda: "0.9.0")
    code, out, _ = run("doctor", "--json")
    assert code == 0  # a warning must never fail the exit code
    payload = json.loads(out)
    assert payload["ok"] is True
    assert _by_name(payload)["amitools"]["status"] == "warn"
    assert payload["summary"]["warn"] >= 1


def test_doctor_amitools_on_the_floor_passes(run, monkeypatch):
    monkeypatch.setattr(doctor, "_amitools_version", lambda: "0.8.1")
    code, out, _ = run("doctor", "--json")
    assert code == 0
    assert _by_name(json.loads(out))["amitools"]["status"] == "ok"


def test_doctor_python_below_floor_fails(run, monkeypatch):
    version_info = namedtuple("version_info", "major minor micro releaselevel serial")
    monkeypatch.setattr(sys, "version_info", version_info(3, 9, 7, "final", 0))
    code, out, _ = run("doctor", "--json")
    assert code == 1
    py = _by_name(json.loads(out))["python"]
    assert py["status"] == "fail"
    assert "3.9" in py["detail"]


def test_doctor_store_warns_when_filesystem_wont_reclaim(run, monkeypatch):
    monkeypatch.setattr(doctor, "_probe_sparse",
                        lambda d: {"supported": True, "reclaimed": False, "reason": ""})
    code, out, _ = run("doctor", "--json")
    assert code == 0
    payload = json.loads(out)
    store = _by_name(payload)["sparse store"]
    assert store["status"] == "warn"
    assert "compressed" in store["detail"]


def test_doctor_warns_when_hole_punch_unsupported(run, monkeypatch):
    monkeypatch.setattr(doctor, "_probe_sparse",
                        lambda d: {"supported": False, "reclaimed": False,
                                   "reason": "F_PUNCHHOLE not supported here (x)"})
    code, out, _ = run("doctor", "--json")
    assert code == 0
    assert _by_name(json.loads(out))["hole-punching"]["status"] == "warn"
