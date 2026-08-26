"""CLI tests for `completion`.

The generated script is checked **behaviourally**, not by substring: it is syntax-checked with
`zsh -n`, and then actually executed in zsh with the completion builtins stubbed, so the real
`case` statements, `_c=(...)` arrays and every `_arguments` spec run. A quoting fault (help
text here contains apostrophes, colons and parentheses) shows up as a runtime error rather
than passing unnoticed.

The expected command list is derived from `build_parser()` rather than hardcoded. A hardcoded
list would silently stop covering any command added later, which is exactly the drift the
generated completion exists to prevent.
"""

import shutil
import subprocess

import pytest

from amibuilder.cli import build_parser, main
from amibuilder.commands.completion import _subaction

zsh_required = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")


@pytest.fixture
def run(capsys):
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return _run


def parser_commands() -> dict[str, dict]:
    """{command: {subcommands}} straight from the live parser."""
    parser, _ = build_parser()
    action = _subaction(parser)
    assert action is not None
    out = {}
    for name, sub in action.choices.items():
        inner = _subaction(sub)
        out[name] = set(inner.choices) if inner else set()
    return out


@pytest.fixture
def script(run) -> str:
    code, out, err = run("completion")
    assert code == 0, err
    return out


def test_default_shell_is_zsh(script):
    assert script.startswith("#compdef amibuilder")


def test_explicit_zsh_matches_the_default(run, script):
    code, out, _ = run("completion", "zsh")
    assert code == 0
    assert out == script


def test_unsupported_shell_is_refused(run):
    code, _, err = run("completion", "bash")
    assert code == 2
    assert "bash" in err and "zsh" in err


def test_json_is_refused(run):
    code, _, err = run("completion", "--json")
    assert code == 2
    assert "cannot be represented as JSON" in err


def test_every_command_and_subcommand_is_offered(script):
    """Derived from the parser, so adding a command cannot leave this test vacuous."""
    commands = parser_commands()
    assert len(commands) >= 29, "sanity: the parser should expose the full command set"
    for name, subs in commands.items():
        assert f"'{name}:" in script, f"{name} missing from the completion"
        for sub in subs:
            assert f"'{sub}:" in script, f"{name} {sub} missing from the completion"


def test_nested_commands_get_their_own_function(script):
    for name, subs in parser_commands().items():
        if subs:
            assert f"_amibuilder_{name}()" in script


@zsh_required
def test_generated_script_is_valid_zsh(script, tmp_path):
    path = tmp_path / "_amibuilder"
    path.write_text(script)
    proc = subprocess.run(["zsh", "-n", str(path)], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr


#: Drives the generated script with zsh's completion builtins stubbed, printing whatever
#: reaches `_describe` so the test can compare it against the parser.
_DRIVER = r"""
emulate -L zsh
typeset -g MOCK_STATE=""
_arguments() {
  local a
  for a in "$@"; do
    [[ $a == -* && $a != *'['* ]] && continue
    print -r -- "SPEC|$a"
  done
  state="$MOCK_STATE"
  return 0
}
_describe() {
  local arrname="${@[-1]}"
  local -a arr
  eval "arr=(\"\${${arrname}[@]}\")"
  local e
  for e in "${arr[@]}"; do print -r -- "ITEM|$e"; done
  return 0
}
_files() { print -r -- "FILES"; }
compdef() { : }

source SCRIPT_PATH
print -r -- "SOURCED_OK"

MOCK_STATE=command
words=(amibuilder)
_amibuilder
print -r -- "RC|$?"

MOCK_STATE=args
words=(zerofree)
_amibuilder
"""


@zsh_required
def test_generated_script_executes_and_emits_the_real_command_list(script, tmp_path):
    """Run the generated functions for real -- the strongest check available offline."""
    comp = tmp_path / "_amibuilder"
    comp.write_text(script)
    driver = tmp_path / "drive.zsh"
    driver.write_text(_DRIVER.replace("SCRIPT_PATH", str(comp)))

    proc = subprocess.run(["zsh", "-f", str(driver)], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "SOURCED_OK" in proc.stdout
    # A quoting fault surfaces here rather than silently completing nothing.
    assert "bad substitution" not in proc.stderr
    assert "parse error" not in proc.stderr

    offered = {line.split("|", 1)[1].split(":", 1)[0]
               for line in proc.stdout.splitlines() if line.startswith("ITEM|")}
    assert set(parser_commands()) <= offered

    # ...and the per-command branch really produces that command's own options.
    specs = [line for line in proc.stdout.splitlines() if line.startswith("SPEC|")]
    assert any("--compact[" in s for s in specs), "zerofree's own options should be offered"
    assert any("1:IMAGE:_files" in s for s in specs), "an image positional completes files"


@zsh_required
def test_help_text_with_an_apostrophe_survives_quoting(script, tmp_path):
    """`info`'s help contains an apostrophe -- the classic generated-script quoting break."""
    assert "image'\\''s" in script, "apostrophes must be escaped for a single-quoted zsh word"
    path = tmp_path / "_amibuilder"
    path.write_text(script)
    proc = subprocess.run(["zsh", "-n", str(path)], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
