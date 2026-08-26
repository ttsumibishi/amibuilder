"""`completion` -- emit a zsh completion script for amibuilder.

**The script is generated from the live argparse parser, never hand-maintained.** With 29
commands, two of which nest their own subcommands, a hand-written completion would be stale
within a week and stale completion is worse than none: it offers flags that no longer exist
and hides ones that do. Walking `build_parser()` means the completion is correct by
construction, and adding a command updates it for free.

Only zsh is generated. It is what the project's own notes asked for, and a bash version is a
different enough dialect that guessing at it would ship something untested.

What can and cannot be completed:

* Command and subcommand names, and every option, with its help text as the description --
  all read from the parser.
* Option values where the parser knows them (`--format`, `--type` and friends complete from
  their `choices`).
* Host paths, for the positionals that name one (an image, a host file, a destination).
* **In-image paths are deliberately not completed.** Completing `card.hdf:Work S/Start...`
  would mean opening and reading the image on every keypress. That is what `amibuilder shell`
  is for -- it holds the volume open and completes in-image paths there.

**This module reads argparse's private attributes** (`_actions`, `_SubParsersAction`,
`_choices_actions`, `_AppendAction`), because argparse offers no public API for introspecting
a parser. That is a deliberate trade-off: the alternative is a second, hand-maintained
description of the CLI, which is the drift this module exists to avoid. The exposure is
contained -- it is all in the four helpers below -- and `test_completion_cli.py` guards it by
generating the script and *running* it in zsh, so a future Python that reshapes these
internals fails the suite rather than silently emitting an empty completion.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..errors import UsageError
from ..render import Output

#: Shells we generate for. Anything else is refused by name rather than guessed at.
SHELLS = ("zsh",)

#: Positional metavars that name a host path, so `_files` is the right completion. `PATH` is
#: absent on purpose: it is a path *inside* the image (see the module docstring).
_PATH_METAVARS = frozenset({
    "SOURCE", "SOURCE_A", "SOURCE_B", "IMAGE", "DEST", "FILE", "TARGET",
})


def _zq(text: str) -> str:
    """Escape a string for use inside a single-quoted zsh word."""
    return text.replace("'", "'\\''")


def _flatten(text: str | None) -> str:
    """One line, no runs of whitespace -- argparse help strings are wrapped in the source."""
    return " ".join((text or "").split())


def _desc(text: str | None) -> str:
    """A help string for an `_arguments` `[description]`.

    Square brackets would close the description early, so they become parentheses.
    """
    return _zq(_flatten(text).replace("[", "(").replace("]", ")"))


def _tag(text: str | None) -> str:
    """A help string for a `_describe` `'name:description'` entry.

    The first colon separates name from description, so colons inside become dashes rather
    than needing escapes.
    """
    flat = _flatten(text).replace("[", "(").replace("]", ")").replace(":", " -")
    return _zq(flat)


def _subaction(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    """The parser's subcommand action, if it has one."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _choice_help(action: argparse._SubParsersAction) -> dict[str, str]:
    """Map each subcommand name to its help text."""
    return {c.dest: (c.help or "") for c in action._choices_actions}


def _value_action(action: argparse.Action) -> str:
    """The zsh completion action for an option's or positional's value."""
    if action.choices:
        return "(" + " ".join(str(c) for c in action.choices) + ")"
    metavar = action.metavar or ""
    if metavar in _PATH_METAVARS:
        return "_files"
    return ""


def _option_specs(parser: argparse.ArgumentParser) -> list[str]:
    """`_arguments` specs for every option on a parser, globals included.

    Globals are inherited onto each subparser by argparse's `parents=`, so they need no
    special handling here -- they simply show up in `_actions` like any other option.
    """
    specs: list[str] = []
    for action in parser._actions:
        if not action.option_strings or isinstance(action, argparse._SubParsersAction):
            continue
        # Mutually exclude an option's own spellings so zsh does not offer -l after --long.
        exclusion = f"({' '.join(action.option_strings)})" if len(action.option_strings) > 1 else ""
        repeat = "*" if isinstance(action, argparse._AppendAction) else ""
        desc = _desc(action.help)
        if action.nargs == 0:
            tail = ""
        else:
            metavar = action.metavar or action.dest.upper()
            tail = f":{metavar}:{_value_action(action)}"
        for opt in action.option_strings:
            specs.append(f"'{exclusion}{repeat}{opt}[{desc}]{tail}'")
    return specs


def _positional_specs(parser: argparse.ArgumentParser) -> list[str]:
    """`_arguments` specs for a parser's positionals, in order."""
    specs: list[str] = []
    index = 0
    for action in parser._actions:
        if action.option_strings or isinstance(action, argparse._SubParsersAction):
            continue
        metavar = action.metavar or action.dest.upper()
        completion = _value_action(action)
        if action.nargs in ("*", "+"):
            # Variadic swallows the rest, so nothing after it can be numbered.
            specs.append(f"'*:{metavar}:{completion}'")
            break
        index += 1
        optional = ":" if action.nargs == "?" else ""
        specs.append(f"'{index}{optional}:{metavar}:{completion}'")
    return specs


def _arguments_call(indent: str, specs: list[str], stateful: bool = False) -> list[str]:
    """Render an `_arguments` invocation, one spec per continued line."""
    if not specs:
        return [f"{indent}_files"]
    flags = "-s -C" if stateful else "-s"
    lines = [f"{indent}_arguments {flags} \\"]
    for spec in specs[:-1]:
        lines.append(f"{indent}  {spec} \\")
    lines.append(f"{indent}  {specs[-1]}")
    return lines


def _describe_block(indent: str, title: str, entries: list[tuple[str, str]]) -> list[str]:
    """Render a `_describe` over a local array of `name:description` pairs."""
    lines = [f"{indent}local -a _c", f"{indent}_c=("]
    for name, help_text in entries:
        lines.append(f"{indent}  '{name}:{_tag(help_text)}'")
    lines.append(f"{indent})")
    lines.append(f"{indent}_describe -t commands '{_zq(title)}' _c")
    return lines


def _leaf_case(indent: str, name: str, parser: argparse.ArgumentParser) -> list[str]:
    """A `case` branch completing one command that has no subcommands of its own."""
    specs = _option_specs(parser) + _positional_specs(parser)
    lines = [f"{indent}{name})"]
    lines += _arguments_call(indent + "  ", specs)
    lines.append(f"{indent}  ;;")
    return lines


def _nested_function(func: str, title: str, parser: argparse.ArgumentParser) -> list[str]:
    """A completion function for a command that nests subcommands (`snap`, `recipe`)."""
    action = _subaction(parser)
    assert action is not None
    helps = _choice_help(action)

    lines = [
        f"{func}() {{",
        "  local context state state_descr line",
        "  typeset -A opt_args",
    ]
    lines += _arguments_call(
        "  ", [*_option_specs(parser), "'1: :->sub'", "'*:: :->args'"], stateful=True)
    lines += [
        "  case $state in",
        "    sub)",
    ]
    lines += _describe_block("      ", title,
                             [(n, helps.get(n, "")) for n in action.choices])
    lines += [
        "      ;;",
        "    args)",
        "      case $words[1] in",
    ]
    for sub_name, sub_parser in action.choices.items():
        lines += _leaf_case("        ", sub_name, sub_parser)
    lines += [
        "      esac",
        "      ;;",
        "  esac",
        "}",
        "",
    ]
    return lines


def _zsh_script(parser: argparse.ArgumentParser, version: str) -> str:
    """Generate the whole zsh completion script from a parser."""
    action = _subaction(parser)
    if action is None:  # pragma: no cover - the real parser always has subcommands
        raise UsageError("the parser exposes no subcommands to complete")
    helps = _choice_help(action)
    prog = parser.prog

    nested: dict[str, str] = {}
    for name, sub in action.choices.items():
        if _subaction(sub) is not None:
            nested[name] = f"_{prog}_{name}"

    lines = [
        f"#compdef {prog}",
        "",
        f"# zsh completion for {prog} {version}, generated by `{prog} completion zsh`.",
        "# Do not edit: regenerate after upgrading, so it cannot drift from the real CLI.",
        "",
    ]

    for name, func in nested.items():
        lines += _nested_function(func, f"{prog} {name} subcommand", action.choices[name])

    lines += [
        f"_{prog}() {{",
        "  local context state state_descr line",
        "  typeset -A opt_args",
    ]
    lines += _arguments_call(
        "  ", [*_option_specs(parser), "'1: :->command'", "'*:: :->args'"], stateful=True)
    lines += [
        "  case $state in",
        "    command)",
    ]
    lines += _describe_block("      ", f"{prog} command",
                             [(n, helps.get(n, "")) for n in action.choices])
    lines += [
        "      ;;",
        "    args)",
        "      case $words[1] in",
    ]
    for name, sub in action.choices.items():
        if name in nested:
            lines += [f"        {name})", f"          {nested[name]}", "          ;;"]
        else:
            lines += _leaf_case("        ", name, sub)
    lines += [
        "      esac",
        "      ;;",
        "  esac",
        "}",
        "",
        "# Works both autoloaded from fpath and sourced directly.",
        f'if [ "$funcstack[1]" = "_{prog}" ]; then',
        f'  _{prog} "$@"',
        "else",
        f"  compdef _{prog} {prog}",
        "fi",
    ]
    return "\n".join(lines) + "\n"


def cmd_completion(args: Any, out: Output) -> int:
    if out.as_json:
        raise UsageError(
            "completion writes a shell script, which cannot be represented as JSON. "
            "Redirect it to a file on your fpath instead."
        )
    shell = args.shell or "zsh"
    if shell not in SHELLS:
        raise UsageError(
            f"{shell}: no completion is generated for that shell. "
            f"Supported: {', '.join(SHELLS)}."
        )

    # Imported here rather than at module scope: cli imports this module, so a top-level
    # import would be circular.
    from ..cli import build_parser
    from ..layers.store import tool_version

    parser, _ = build_parser()
    out.lines(_zsh_script(parser, tool_version()).splitlines())
    return 0


__all__ = ["SHELLS", "cmd_completion"]
