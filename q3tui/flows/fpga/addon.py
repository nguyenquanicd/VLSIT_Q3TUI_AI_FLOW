"""Add-on of the FPGA flow (q3tui/addons.py): the SDC actions. Core exposes them as assistant tools, `q3tui act …` and the TUI's
`/act …`; the SDC tab of the sdc step calls the same ones."""

from __future__ import annotations

from q3tui.flows.fpga.lib import hints
from q3tui.flows.fpga.lib import sdc as sdcmod


def _edit(ops, fn) -> str:
    """Load sdc/sdc.json, apply `fn(cfg) -> message`, validate by rendering, save, regenerate the Tcl files."""
    from q3tui.pipeline.engine import EngineError

    step = next((s for s in ops.engine.steps if s.kind.endswith(".sdc")), None)  # (the ASIC flow shares this step)
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
    notes = [n for c in cfg.clocks if (n := hints.asic_note(ops.project, c.name))]
    return ops._log(step.name, f"SDC: {msg} (sdc/*.tcl updated; run to re-synthesize)" + (f" · {'; '.join(f'{c.name}: {n}' for c in cfg.clocks if (n := hints.asic_note(ops.project, c.name)))}" if notes else ""))


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
    from q3tui.flows.fpga.lib import templates

    # the synthesis template and device (Settings → fpga_synth), then the PPA step's options
    api.setting("fpga_synth", "template", "FPGA template", "choice", "The family and tool: Vivado for Xilinx 7-series, Yosys + nextpnr (open source) for Lattice ECP5 / iCE40.",
                templates.choices())
    api.setting("fpga_synth", "part", "Device (part)", "text", "Empty: the template's default. Vivado: a part name (xc7a100tcsg324-1); ECP5: 25k, 45k, 85k, um-85k; iCE40: hx8k, up5k, lp8k.",
                nullable=True)
    api.setting("fpga_synth", "package", "Package (open-source templates)", "text", "Empty: the device's default (ECP5 CABGA381, iCE40 hx8k ct256). More IOs than the package has stops nextpnr.",
                nullable=True)
    api.setting("fpga_synth", "speed", "Speed grade (ECP5)", "text", "6, 7 or 8; empty: 6.", nullable=True)
    api.setting("fpga_synth", "implement", "Place and route (Vivado)", "bool",
                "Add opt / place / route after synthesis: numbers closer to the board, and hold slack is judged. Slower.")
    api.setting("fpga_synth", "frontend", "Verilog frontend (open source)", "choice", "verilog: Yosys read_verilog -sv; slang: the yosys-slang plugin (fuller SystemVerilog).",
                (("verilog", "verilog — read_verilog -sv"), ("slang", "slang — yosys-slang plugin")), nullable=True)
    api.setting("fpga_synth", "synth_args", "Extra synthesis arguments", "lines",
                "One per line, added to the synthesis command (Vivado synth_design: -flatten_hierarchy full; Yosys synth_ecp5 / synth_ice40: -abc9).")
    # the ASIC library: the ASIC flow's own option, here too (the estimate and the SDC tab use it; one choice for both flows)
    api.setting("asic_synth", "library", "ASIC library (.db)", "text",
                "The standard-cell library of the ASIC flow (a .db; one per corner in a PDK folder): the quick Fmax estimate and the ASIC synthesis use it. "
                "Also in the clock dialog of the SDC tab.", default=hints.asic_default_library())
    # the PPA step's options in the Settings dialog (its tab) and for the assistant; saved to pipeline.step_options in q3tui.yaml
    api.setting("ppa_optimize", "max_iterations", "Max PPA rounds", "int",
                "Optimization rounds (proposals) per set of constraints before the step stops and asks you. A new clock starts afresh.")
    api.setting("ppa_optimize", "when", "Optimize when", "choice", "fail: only when the report misses a target; always: also to improve a passing result.",
                (("fail", "fail — only when a target is missed"), ("always", "always — also to improve a passing result")))
    api.setting("ppa_optimize", "goal", "Goal", "text", "What to improve, in words (\"fewer LUTs\", \"reach 250 MHz\"); empty = the missed targets.",
                nullable=True)
    api.setting("ppa_optimize", "handoff_flow", "Hand fixes to flow", "text", "The flow that owns the RTL and the spec (default vlsit).")
    api.setting("ppa_optimize", "verify_after", "Verify after PPA", "bool",
                "Targets met on RTL the optimization changed: check its function in that flow (tb → verify).")

    @api.action("ppa_handoff", "Hand the PPA fixes ppa_optimize proposed to the flow that owns the RTL and the spec: an RTL fix becomes a "
                "scoped change request for its rtl step, a spec fix a change request for its spec step. That flow then updates the "
                "RTL (run it), and this flow's synthesis runs again on the new RTL.",
                {"ids": "fix ids separated by commas (F1,F2); empty: every fix not handed off yet"}, optional=("ids",))
    def ppa_handoff(ops, ids: str = "") -> str:
        return _handoff(ops, ids)

    register_sdc(api)
    from q3tui.flows.asic.addon import register_estimate  # (what the ASIC library allows, asked where the frequency is typed)

    register_estimate(api)


def register_sdc(api) -> None:
    """The SDC actions (the ASIC flow registers the same ones: both work on `sdc/`)."""

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
