"""CLI tests for `version`."""

import json
import sys

import pytest

from amibuilder.cli import main
from amibuilder.commands import version as versioncmd


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


def test_version_reports_each_component(run):
    code, out, err = run("version")
    assert code == 0, err
    for label in ("amibuilder", "amitools", "Python", "Platform"):
        assert label in out


def test_version_json_shape(run):
    code, out, _ = run("version", "--json")
    assert code == 0
    payload = json.loads(out)
    assert set(payload) == {"amibuilder", "amitools", "python",
                            "python_implementation", "platform"}
    assert payload["platform"] == sys.platform
    assert payload["python"].startswith(f"{sys.version_info.major}.{sys.version_info.minor}")


def test_version_says_so_when_amitools_is_absent(run, monkeypatch):
    monkeypatch.setattr(versioncmd, "_amitools_version", lambda: None)
    code, out, _ = run("version")
    assert code == 0  # `version` reports, it does not judge -- that is doctor's job
    assert "not installed" in out


def test_version_flag_still_prints_just_the_tool_version(run):
    """The argparse `--version` flag is a different, terser path; it must keep working."""
    with pytest.raises(SystemExit) as excinfo:
        run("--version")
    assert excinfo.value.code == 0
