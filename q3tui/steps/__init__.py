"""Pipeline step registry (order = execution order)."""

from __future__ import annotations

from q3tui.pipeline.base import StepDef

# step kind -> "module:Class" (lazy: the Agent SDK / pyslang imports stay out of the CLI start). Flow files name kinds.
KINDS: dict[str, str] = {
    "spec": "q3tui.steps.spec.step:SpecStep",
    # the VLSIT flow (docs/spec/vlsit-flow.md)
    "vlsit_parse": "q3tui.flows.vlsit.parse.step:ParseStep",
    "vlsit_config": "q3tui.flows.vlsit.config.step:ConfigStep",
    "vlsit_rtl": "q3tui.flows.vlsit.rtl.step:RtlGenStep",
    "vlsit_tb": "q3tui.flows.vlsit.tb.step:TbGenStep",
    "vlsit_sva": "q3tui.flows.vlsit.sva.step:SvaStep",
    "vlsit_verify": "q3tui.flows.vlsit.verify.step:VerifyStep",
    "vlsit_doc": "q3tui.flows.vlsit.doc.step:DocStep",
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

