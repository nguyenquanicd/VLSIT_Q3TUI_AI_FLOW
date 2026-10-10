"""Add-ons: what a flow folder brings to the one Q3TUI (docs/spec/flows.md "Add-ons").

Core (CLI, TUI, assistant, `Ops`, engine) knows no flow by name. A flow folder may hold, next to `FLOW.md` and its steps:

    addon.py     `register(api)`: user actions (`api.action`), step kinds (`api.kind`)
    tools.json   default tool roles (`{"roles": {...}}`): used only by this flow, for a role nothing configures
    panels.py    TUI panels of its step kinds (`register_panel(kind, builder)`); imported when the TUI opens the flow

An *action* is a user action of the flow (change the SDC, …): `fn(ops, **params) -> str`, params all strings. Core exposes
every action the same three ways — the assistant's tools, `q3tui act NAME key=value…`, and the TUI's `/act NAME key=value…` —
so they all go through `Ops.run_action`, like every other user action."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Action:
    name: str
    fn: Callable[..., str]  # fn(ops, **params) -> a message
    help: str
    params: dict[str, str] = field(default_factory=dict)  # name -> description (all strings)
    optional: tuple[str, ...] = ()  # the params that may be left out


class AddonAPI:
    """What `register(api)` of an add-on can use."""

    def __init__(self, flow):
        self.flow = flow
        self.dir = flow.path.parent

    def action(self, name: str, help: str, params: dict[str, str] | None = None, optional: tuple[str, ...] = ()):
        """Decorator: `@api.action("sdc_set", "Change one SDC setting", {"key": "…", "value": "…"})` on `fn(ops, key, value)`."""

        def deco(fn):
            self.flow.actions[name] = Action(name, fn, help, dict(params or {}), tuple(optional))
            return fn

        return deco

    def kind(self, kind: str, target: str) -> None:
        """Register a step kind ("package.module:Class") the flow's steps can name."""
        from q3tui.steps import register_kind

        register_kind(kind, target)


def _load_file(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # (dataclasses, pickling and `typing` look a class's module up by name)
    spec.loader.exec_module(mod)
    return mod


def attach(flow) -> None:
    """Load the add-on files of a flow folder into `flow` (no-op for what the folder does not have)."""
    root = flow.path.parent
    tools = root / "tools.json"
    if tools.is_file():
        flow.tool_defaults = dict(json.loads(tools.read_text()).get("roles") or {})
    if (root / "panels.py").is_file():
        flow.panels_file = root / "panels.py"
    addon = root / "addon.py"
    if addon.is_file():
        mod = _load_file(addon, f"q3tui_addon_{flow.name}")
        register = getattr(mod, "register", None)
        if register is None:
            raise ValueError(f"{addon}: an add-on defines `register(api)`")
        register(AddonAPI(flow))


def load_panels(flow) -> None:
    """Import the flow's `panels.py` (TUI side: it registers its panels on import). Once per flow."""
    f = flow.panels_file
    if f is not None and not getattr(flow, "_panels_loaded", False):
        flow._panels_loaded = True  # type: ignore[attr-defined]
        _load_file(f, f"q3tui_panels_{flow.name}")
