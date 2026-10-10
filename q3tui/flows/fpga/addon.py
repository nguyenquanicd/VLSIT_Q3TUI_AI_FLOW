"""Add-on of the FPGA flow (q3tui/addons.py): the SDC actions. Core exposes them as assistant tools, `q3tui act …` and the TUI's
`/act …`; the SDC tab of the sdc step calls the same ones."""

from __future__ import annotations

from q3tui.flows.fpga.lib import sdc as sdcmod


def _edit(ops, fn) -> str:
    """Load sdc/sdc.json, apply `fn(cfg) -> message`, validate by rendering, save, regenerate the Tcl files."""
    from q3tui.pipeline.engine import EngineError

    step = next((s for s in ops.engine.steps if s.kind == "fpga.sdc"), None)
    if step is None:
        raise EngineError("this flow has no SDC step")
    cfg = sdcmod.load(ops.project)
    if cfg is None:
        raise EngineError("sdc/sdc.json does not exist yet: run the sdc step first")
    msg = fn(cfg)
    ports = sdcmod.top_ports(ops.project, step.filelist(ops.engine), step.top(ops.engine))
    sdcmod.render(cfg, ports)  # validate before anything is written (a ValueError becomes the action's error)
    sdcmod.save(ops.project, cfg)
    sdcmod.write(ops.project, cfg, ports)
    return ops._log(step.name, f"SDC: {msg} (sdc/*.tcl updated; run to re-synthesize)")


def _handoff(ops, ids: str) -> str:
    """Send the proposed PPA fixes to the flow that owns the RTL and the spec, as change requests of its steps."""
    from datetime import datetime

    from q3tui import flows
    from q3tui.flows.fpga.lib.plan import PLAN_JSON, handoff_text
    from q3tui.pipeline.engine import Engine, EngineError

    step = next((s for s in ops.engine.steps if s.kind == "fpga.ppa_optimize"), None)
    if step is None:
        raise EngineError("this flow has no ppa_optimize step")
    plan = step.read(ops.engine, PLAN_JSON, {})
    wanted = {i.strip() for i in ids.split(",") if i.strip()}
    todo = [f for f in plan.get("fixes", []) if not f.get("handed_off") and (not wanted or f["id"] in wanted)]
    if not todo:
        raise EngineError("no proposed fix to hand off (run ppa_optimize first, or they were handed off already)")
    owner = flows.load_flow(str(step.options.get("handoff_flow") or "vlsit"), ops.project.root)
    if owner.name == ops.engine.flow.name:
        raise EngineError("handoff_flow is this flow itself")
    ops.engine.save()  # what this process holds is on disk; the other flow's engine reads and writes the same pipeline.json
    other = Engine(ops.project, flow=owner)
    sent = []
    for f in todo:
        target, text = handoff_text(f, plan.get("part") or "", str(step.options.get("goal") or ""))
        if target not in other.by_name:
            raise EngineError(f"flow '{owner.name}' has no step '{target}' to hand {f['id']} to")
        other.request_change(target, text)
        f["handed_off"] = {"flow": owner.name, "step": target, "at": datetime.now().isoformat(timespec="seconds")}
        sent.append(f"{f['id']} → {owner.name}:{target}")
    step.write(ops.engine, PLAN_JSON, plan)
    ops.engine.reload()  # the change requests are in the state now
    rec = ops.engine.state.steps.get(step.name)
    if rec is not None:  # (the plan records the hand-off: not a user edit of the step's output)
        rec.outputs = ops.engine._hash_files(step.outputs(ops.engine))
        ops.engine.save()
    return ops._log(step.name, f"PPA: handed off {', '.join(sent)}; run flow '{owner.name}' to apply them (q3tui --flow {owner.name} run)")


def register(api) -> None:
    @api.action("ppa_handoff", "Hand the PPA fixes ppa_optimize proposed to the flow that owns the RTL and the spec: an RTL fix becomes a "
                "scoped change request for its rtl step, a spec fix a change request for its spec step. That flow then updates the "
                "RTL (run it), and this flow's synthesis runs again on the new RTL.",
                {"ids": "fix ids separated by commas (F1,F2); empty: every fix not handed off yet"}, optional=("ids",))
    def ppa_handoff(ops, ids: str = "") -> str:
        return _handoff(ops, ids)

    @api.action("sdc_show", "Show the SDC settings (clocks and frequencies, I/O delay budget, resets, exceptions, clock groups, "
                "environment) as `key | setting | value` lines.")
    def sdc_show(ops) -> str:
        cfg = sdcmod.load(ops.project)
        return "\n".join(f"{k} | {n} | {v}" for k, n, v in sdcmod.rows(cfg)) if cfg else "no sdc/sdc.json yet: run the sdc step"

    @api.action("sdc_set", "Change one SDC setting by its key from sdc_show (clock.i_clk.freq_mhz, clock.i_clk.period_ns, "
                "io.input_pct, io.output_pct, resets, env.load_pf, …); the Tcl files are regenerated at once. Empty value clears an "
                "optional one.", {"key": "a key from sdc_show", "value": "the new value"})
    def sdc_set(ops, key: str, value: str) -> str:
        return _edit(ops, lambda cfg: sdcmod.apply_edit(cfg, key, value))

    @api.action("sdc_add", "Add to the SDC: what=clock (text 'name port period_ns'), exception (text 'false_path from=A to=B', "
                "'multicycle from=A to=B value=2', 'max_delay … value=ns') or group (text 'asynchronous clkA | clkB').",
                {"what": "clock | exception | group", "text": "what to add"})
    def sdc_add(ops, what: str, text: str) -> str:
        return _edit(ops, lambda cfg: sdcmod.add_item(cfg, what, text))

    @api.action("sdc_replace", "Replace an SDC exception (exception.N) or clock group (group.N) by new text in the form sdc_add takes "
                "('false_path from=A to=B', 'asynchronous clkA | clkB').", {"key": "exception.N or group.N from sdc_show", "text": "the new text"})
    def sdc_replace(ops, key: str, text: str) -> str:
        return _edit(ops, lambda cfg: sdcmod.replace_item(cfg, key, text))

    @api.action("sdc_remove", "Remove an SDC exception (exception.N), clock group (group.N) or clock (clock.<name>) by its key from sdc_show.",
                {"key": "a key from sdc_show"})
    def sdc_remove(ops, key: str) -> str:
        return _edit(ops, lambda cfg: sdcmod.remove_item(cfg, key))
