"""The `recipe` command family: named, ordered layer stacks.

A recipe stores what the user typed — ref names stay ref names — so it follows a moving label
rather than freezing to whatever that label pointed at on the day it was written. Resolution
happens at compose time, where an unknown entry can be reported usefully.
"""

from __future__ import annotations

from typing import Any

from .. import render
from ..errors import UsageError
from ..layers import store as S

SHORT = 12


def _store(args: Any) -> S.Store:
    return S.Store(getattr(args, "store", None))


def cmd_new(args: Any, out: render.Output) -> int:
    """Record an ordered list of layers under a name."""
    store = _store(args)
    layers = [spec.strip() for spec in args.layers.split(",") if spec.strip()]
    if not layers:
        raise UsageError("--layers needs at least one layer, comma-separated")
    recipe = store.write_recipe(args.name, layers, description=args.description or "")

    if out.as_json:
        out.data(recipe)
        return 0
    out.line(f"recipe '{args.name}' -> {' + '.join(layers)}")
    return 0


def cmd_ls(args: Any, out: render.Output) -> int:
    store = _store(args)
    names = list(store.iter_recipes())
    if out.as_json:
        out.data([store.read_recipe(name) for name in names])
        return 0
    if not names:
        out.line(f"no recipes in {store.root}")
        return 0
    table = render.Table(headers=["recipe", "layers", "description"])
    for name in names:
        recipe = store.read_recipe(name)
        table.add(name, " + ".join(recipe.get("layers") or []), recipe.get("description", ""))
    out.table(table)
    return 0


def cmd_show(args: Any, out: render.Output) -> int:
    """Show a recipe, resolving each entry so a stale reference is visible."""
    store = _store(args)
    recipe = store.read_recipe(args.name)

    resolved: list[dict[str, Any]] = []
    for spec in recipe.get("layers") or []:
        try:
            layer_id = store.resolve(spec)
            layer = store.read_layer(layer_id)
            resolved.append(
                {"spec": spec, "id": layer_id, "kind": layer.kind,
                 "label": layer.label, "error": None}
            )
        except Exception as exc:  # reported per entry rather than failing the listing
            resolved.append({"spec": spec, "id": None, "error": str(exc)})

    if out.as_json:
        out.data(dict(recipe, resolved=resolved))
        return 0

    out.field("recipe", recipe.get("name", args.name))
    if recipe.get("description"):
        out.field("description", recipe["description"])
    out.field("created", recipe.get("created", "-"))
    out.heading("layers")
    for position, item in enumerate(resolved, start=1):
        if item.get("error"):
            out.line(f"  {position}. {item['spec']}  -- UNRESOLVED: {item['error']}")
        else:
            out.line(f"  {position}. {item['spec']:<20} {item['id'][:SHORT]}  {item['kind']}")
    if any(item.get("error") for item in resolved):
        return 3
    return 0


def cmd_rm(args: Any, out: render.Output) -> int:
    store = _store(args)
    removed = store.delete_recipe(args.name)
    if out.as_json:
        out.data({"removed": removed, "name": args.name})
        return 0 if removed else 1
    if not removed:
        out.line(f"no recipe named '{args.name}'")
        return 1
    out.line(f"removed recipe '{args.name}'")
    return 0


SUBCOMMANDS = {
    "new": cmd_new,
    "ls": cmd_ls,
    "show": cmd_show,
    "rm": cmd_rm,
}


def cmd_recipe(args: Any, out: render.Output) -> int:
    action = getattr(args, "recipe_command", None)
    if not action:
        raise UsageError("recipe needs a subcommand: " + ", ".join(sorted(SUBCOMMANDS)))
    return SUBCOMMANDS[action](args, out)
