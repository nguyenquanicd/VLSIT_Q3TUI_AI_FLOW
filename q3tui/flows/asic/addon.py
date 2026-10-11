"""Add-on of the ASIC flow (q3tui/addons.py): the SDC actions (the FPGA flow's: both work on `sdc/`), the library and target
settings of the synthesis, and `asic_handoff`: the ASIC port findings to the flow that owns the RTL."""

from __future__ import annotations

from q3tui.flows.fpga import addon as fpga_addon


def _handoff(ops, ids: str) -> str:
    """Send the ASIC port findings to the flow that owns the RTL, as scoped change requests of its rtl step."""
    from datetime import datetime

    from q3tui import flows
    from q3tui.flows.asic.lib import port
    from q3tui.flows.asic.lib.base import PORT_JSON
    from q3tui.pipeline.engine import Engine, EngineError

    step = next((s for s in ops.engine.steps if s.kind == "asic.asic_port"), None)
    if step is None:
        raise EngineError("this flow has no asic_port step")
    data = step.read(ops.engine, PORT_JSON, {}) or {}
    wanted = {i.strip() for i in ids.split(",") if i.strip()}
    todo = [f for f in data.get("findings", []) if not f.get("handed_off") and (not wanted or f["id"] in wanted)]
    if not todo:
        raise EngineError("no finding to hand off (run asic_port first, or they were handed off already)")
    owner = flows.load_flow(str(step.options.get("handoff_flow") or "vlsit"), ops.project.root)
    if owner.name == ops.engine.flow.name:
        raise EngineError("handoff_flow is this flow itself")
    ops.engine.save()
    other = Engine(ops.project, flow=owner)
    if "rtl" not in other.by_name:
        raise EngineError(f"flow '{owner.name}' has no step 'rtl' to hand the findings to")
    for f in todo:
        other.request_change("rtl", port.handoff_text(f))
        f["handed_off"] = {"flow": owner.name, "step": "rtl", "at": datetime.now().isoformat(timespec="seconds")}
    step.write(ops.engine, PORT_JSON, data)
    ops.engine.reload()
    rec = ops.engine.state.steps.get(step.name)
    if rec is not None:  # (the hand-off is recorded in the findings: not a user edit of the step's output)
        rec.outputs = ops.engine._hash_files(step.outputs(ops.engine))
        ops.engine.save()
    return ops._log(step.name, f"ASIC port: handed off {', '.join(f['id'] for f in todo)} → {owner.name}:rtl; run flow '{owner.name}' to apply them "
                               f"(q3tui --flow {owner.name} run)")


def register_estimate(api) -> None:
    """The quick Fmax estimate (the FPGA flow registers it too: it is asked where the frequency is typed)."""

    @api.action("asic_estimate", "Estimate, in about a minute, the clock frequency the ASIC library allows for this RTL: Design Compiler at low effort "
                "with only the clocks constrained (register-to-register, no I/O). The result is shown next to each clock in the SDC tab and in sdc_set's "
                "answer; the full asic_synth is the exact number.")
    def asic_estimate(ops) -> str:
        from q3tui.flows.asic.lib import estimate

        return estimate.run(ops)


def register(api) -> None:
    api.setting("asic_synth", "library", "Library (.db)", "text",
                "The standard-cell library Design Compiler maps to: the .db file (a list in q3tui.yaml for several). Formality reads the same.")
    api.setting("asic_synth", "compile", "Compile command", "text", "compile_ultra, compile_ultra -gate_clock, compile -map_effort high, …")
    api.setting("asic_synth", "min_slack_ns", "Min setup slack (ns)", "float", "The review fails when the worst slack is below this.")
    api.setting("asic_synth", "max_area_um2", "Max area (µm²)", "float", "The review fails above this area; empty: no budget.", nullable=True)
    api.setting("asic_port", "min_mem_words", "Smallest memory worth a finding (words)", "int",
                "Unpacked arrays of at least this many words are reported as memories without a macro.")
    api.setting("asic_port", "handoff_flow", "Hand findings to flow", "text", "The flow that owns the RTL (default vlsit).")

    @api.action("asic_handoff", "Hand the ASIC port findings (FPGA primitives, memories without a macro, power-up values, …) to the flow that "
                "owns the RTL as scoped change requests for its rtl step. That flow then updates the RTL (run it); this flow's checks "
                "run again on the new RTL.",
                {"ids": "finding ids separated by commas (P1,P2); empty: every finding not handed off yet"}, optional=("ids",))
    def asic_handoff(ops, ids: str = "") -> str:
        return _handoff(ops, ids)

    register_estimate(api)
    fpga_addon.register_sdc(api)
