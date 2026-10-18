"""Add-on of the Sanity flow (q3tui/addons.py): the SDC actions (the FPGA flow's: both work on `sdc/`), and `sanity_handoff`: the review's RTL fixes and
spec questions to the flow that owns them, as the ASIC flow's `asic_handoff` does."""

from __future__ import annotations

from q3tui.flows.fpga import addon as fpga_addon


def _handoff(ops, ids: str) -> str:
    """Send the `fix_rtl` / `spec` decisions to the owner flow, as change requests of its steps (rtl: scoped to the module; spec)."""
    from datetime import datetime

    from q3tui import flows
    from q3tui.flows.sanity.lib import waivers
    from q3tui.flows.sanity.lib.base import report_json
    from q3tui.flows.sanity.lib.review import WaiverStep
    from q3tui.pipeline.engine import Engine, EngineError

    steps = [s for s in ops.engine.steps if isinstance(s, WaiverStep)]
    if not steps:
        raise EngineError("this flow has no waiver step")
    wanted = {i.strip().upper() for i in ids.split(",") if i.strip()}  # "V1,V3" or "cdc:V1" (each lane numbers its violations from V1)
    ops.engine.save()
    others: dict[str, Engine] = {}  # one engine per owner flow: each review hands off to its own handoff_flow
    sent = []
    for step in steps:
        name = str(step.options.get("handoff_flow") or "vlsit")
        if name == ops.engine.flow.name:
            raise EngineError("handoff_flow is this flow itself")
        if name not in others:
            others[name] = Engine(ops.project, flow=flows.load_flow(name, ops.project.root))
        other = others[name]
        plan = step.plan(ops.engine)
        by_id = {v["id"]: v for v in (step.read(ops.engine, report_json(step.role), {}) or {}).get("violations", [])}
        for d in plan.get("items", []):
            tag, plain = f"{step.role}:{d['id']}".upper(), d["id"].upper()
            if d["action"] not in ("fix_rtl", "spec") or d.get("handed_off") or (wanted and not ({tag, plain} & wanted)):
                continue
            target, text = waivers.handoff_text(step.role, d, by_id[d["id"]])
            if target not in other.by_name:
                raise EngineError(f"flow '{name}' has no step '{target}' to hand {tag} to")
            other.request_change(target, text)
            d["handed_off"] = {"flow": name, "step": target, "at": datetime.now().isoformat(timespec="seconds")}
            sent.append(f"{step.role}:{d['id']} → {name}:{target}")
        step.write(ops.engine, f"{step.role}_waivers.json", plan)
        from q3tui.flows.sanity.lib import table

        table.write(step.table_files(ops.engine), step.role, step.read(ops.engine, report_json(step.role), {}) or {}, plan)
    if not sent:
        raise EngineError("no fix to hand off (run a waiver step first, or they were handed off already)")
    ops.engine.reload()
    for step in steps:  # (the hand-off is recorded in the output: not a user edit of the step's output)
        rec = ops.engine.state.steps.get(step.name)
        if rec is not None:
            rec.outputs = ops.engine._hash_files(step.outputs(ops.engine))
    ops.engine.save()
    return ops._log(steps[0].name, f"Sanity: handed off {', '.join(sent)}; run the flow(s) {', '.join(f'{n!r}' for n in others)} to apply them "
                                   f"({'; '.join(f'q3tui --flow {n} run' for n in others)})")


def register(api) -> None:
    @api.action("sanity_handoff", "Hand the CDC / RDC fixes the waiver review proposed to the flow that owns the RTL and the spec: an RTL fix becomes a "
                "scoped change request for its rtl step, a spec question a change request for its spec step. That flow then updates the RTL (run it), "
                "and this flow's analysis runs again on the new RTL. SDC requests are not handed off: the SDC is yours.",
                {"ids": "violation ids separated by commas (V1,V2 or cdc:V1); empty: every fix not handed off yet"}, optional=("ids",))
    def sanity_handoff(ops, ids: str = "") -> str:
        return _handoff(ops, ids)

    api.setting("cdc", "extra_tcl", "CDC extra Tcl", "text", "Lines for the CDC script after the SDC (configure_cdc_*, set_app_var, …).")
    api.setting("rdc", "extra_tcl", "RDC extra Tcl", "text", "Lines for the RDC script after the SDC.")
    api.setting("cdc_waivers", "handoff_flow", "Hand CDC fixes to flow", "text", "The flow that owns the RTL and the spec (default vlsit).")
    api.setting("rdc_waivers", "handoff_flow", "Hand RDC fixes to flow", "text", "The flow that owns the RTL and the spec (default vlsit).")
    fpga_addon.register_sdc(api)
