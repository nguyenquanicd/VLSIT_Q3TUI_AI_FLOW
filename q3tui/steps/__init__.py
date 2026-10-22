"""Pipeline step registry (order = execution order)."""

from __future__ import annotations

from q3tui.pipeline.base import StepDef

# step kind -> "module:Class" (lazy: the Agent SDK / pyslang imports stay out of the CLI start). Flow files name kinds.
KINDS: dict[str, str] = {
    "spec": "q3tui.steps.spec.step:SpecStep",
    # the spec flow (docs/spec/spec-flow.md)
    "vlsit_parse": "q3tui.flows.spec.parse.step:ParseStep",
    "vlsit_config": "q3tui.flows.spec.config.step:ConfigStep",
    # the dv flow (docs/spec/dv-flow.md)
    "dv_tb": "q3tui.flows.dv.tb.step:TbGenStep",
}


def register_kind(kind: str, target: str) -> None:
    """Make a step kind available to flow files ("package.module:Class"); for extensions and tests."""
    KINDS[kind] = target


def make_step(kind: str) -> StepDef:
    import importlib

    if kind not in KINDS:
        raise KeyError(f"unknown step kind '{kind}' (kinds: {', '.join(sorted(KINDS))})")
    module, _, cls = KINDS[kind].rpartition(":")
    if module.endswith(".py"):  # a flow folder's own step.py (flows: `<step>/step.py`)
        import importlib.util

        spec = importlib.util.spec_from_file_location(f"q3tui_flow_step_{abs(hash(module))}", module)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(module)
    step = getattr(mod, cls)()
    step.kind = kind
    return step

