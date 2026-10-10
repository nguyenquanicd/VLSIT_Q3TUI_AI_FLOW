"""FPGA step `ppa_optimize`: proposes RTL / spec fixes for a missed PPA target (`schemas/ppa_plan.json`).

Deterministic first: the report says whether a target is missed; only then one read-only LLM stage looks at the RTL and the worst
paths and proposes fixes (the PpaPlan). It changes no file. The gate is your review of the proposals; the `ppa_handoff` action (the
panel's `h`, `q3tui act ppa_handoff`, the assistant) hands them to the flow that owns the RTL and the spec — as scoped change requests
(`[rtl:<module>] …`) for its `rtl` step, or a change request for its `spec` step. When that flow has updated the RTL, this
flow's synthesis, report and this step are stale and run again; `options.max_iterations` bounds the rounds."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections
from q3tui.flows.fpga.lib.base import PPA_JSON, FpgaStep
from q3tui.flows.fpga.lib.plan import PLAN_JSON, PpaPlan
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["SYSTEM"]


class Step(FpgaStep):
    name = "ppa_optimize"
    title = "PPA optimization"
    deps = ("timing_util_report",)

    def inputs(self, engine) -> list[Path]:
        return [self.artifact(engine, PPA_JSON), *self.rtl_files(engine), *engine.project.spec_documents()]

    def outputs(self, engine) -> list[Path]:
        p = self.artifact(engine, PLAN_JSON)
        return [p] if p.is_file() else []

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        if not self.artifact(engine, PPA_JSON).is_file():
            return "the report is needed first (step timing_util_report)"
        return None

    def missed(self, ppa: dict) -> list[str]:
        """What the report misses, in words (empty: every target is met)."""
        v, t = ppa.get("verdict", {}), ppa.get("timing", {})
        out = []
        if v.get("timing") == "fail":
            out.append(f"setup slack WNS {t.get('wns_ns')} ns is below the target {ppa.get('targets', {}).get('min_slack_ns', 0)} ns")
        if v.get("hold") == "fail":
            out.append(f"hold slack WHS {t.get('whs_ns')} ns is negative")
        for k, o in (v.get("over_budget") or {}).items():
            out.append(f"{k.upper()} uses {o['used_pct']}% of the part, the budget is {o['max_pct']}%")
        return out

    def _prompt(self, engine, ppa: dict, missed: list[str], history: list[dict], modules: list[str]) -> str:
        goal = str(self.options.get("goal") or "")
        lines = ["## Targets missed" if missed else "## Goal (nothing missed: improve the result)",
                 *(f"- {m}" for m in missed or [goal or "improve PPA"]),
                 *([f"\nUser goal: {goal}"] if goal and missed else []),
                 "\n## Report (ground truth)", f"Part {ppa.get('part')} ({ppa.get('stage')}), top {ppa.get('top')}, clocks "
                 + ", ".join(f"{c['name']} {c['freq_mhz']:g} MHz" for c in ppa.get("clocks", [])),
                 "Utilization: " + ", ".join(f"{k} {u['used']}/{u['available']} ({u['pct']}%)" for k, u in ppa.get("utilization", {}).items()
                                             if k in ("lut", "ff", "bram_tile", "dsp", "lut_ram")),
                 "Timing: " + ", ".join(f"{k} {v}" for k, v in ppa.get("timing", {}).items()),
                 "Worst path: " + ", ".join(f"{k} {v}" for k, v in (ppa.get("critical_path") or {}).items()),
                 "Unconstrained: " + (str(ppa.get("unconstrained") or "none")),
                 "\n## RTL modules (read src/rtl/<module>.sv)", ", ".join(modules),
                 "\nThe reports are in fpga/reports/ (paths.rpt has the worst paths in full: read it)."]
        if history:
            lines += ["\n## Already tried in earlier rounds (do not repeat)"]
            for h in history:
                lines.append(f"- round {h['iteration']}: WNS {h.get('wns_ns')} ns, LUT {h.get('lut')}, FF {h.get('ff')}; fixes: "
                             + "; ".join(f"{f['target']}:{f.get('module') or '-'} {f['change']}" for f in h.get("fixes", [])))
        return "\n".join(lines)

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        ppa = self.read(engine, PPA_JSON, {})
        prev = self.read(engine, PLAN_JSON, {}) or {}
        history = list(prev.get("history", []))
        t, util = ppa.get("timing", {}), ppa.get("utilization", {})
        snap = {"wns_ns": t.get("wns_ns"), "lut": util.get("lut", {}).get("used"), "ff": util.get("ff", {}).get("used")}
        # what the last round proposed is now tried: it joins the history, with the result it led to
        if prev.get("status") == "proposed" and prev.get("fixes"):
            history.append({"iteration": prev["iteration"], "wns_ns": prev.get("before", {}).get("wns_ns"),
                            "lut": prev.get("before", {}).get("lut"), "ff": prev.get("before", {}).get("ff"),
                            "fixes": [f for f in prev["fixes"] if f.get("handed_off")], "after": snap})
            history = [h for h in history if h["fixes"]]
        missed = self.missed(ppa)
        when = str(self.options.get("when") or "fail")
        base = {"part": ppa.get("part"), "before": snap, "missed": missed, "history": history, "iteration": len(history) + 1}
        if not missed and when != "always":
            self.write(engine, PLAN_JSON, {**base, "status": "met", "analysis": "every target is met", "fixes": []})
            ctx.emit("log", message="PPA targets met: nothing to optimize")
            return
        limit = int(self.options.get("max_iterations") or 3)
        if len(history) >= limit:
            self.write(engine, PLAN_JSON, {**base, "status": "limit", "analysis": f"{len(history)} rounds were tried (options.max_iterations)", "fixes": []})
            raise StepFailed(f"PPA: {len(history)} optimization round(s) done, targets still missed ({'; '.join(missed)}). "
                             "Raise options.max_iterations to go on, or change the clock target / the spec.")
        modules = sorted(f.stem for f in self.rtl_files(engine))
        stage = Stage(name="ppa_optimize", system_prompt=SYSTEM, prompt=self._prompt(engine, ppa, missed, history, modules),
                      cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], output_model=PpaPlan,
                      deny_dirs=[project.src_dir / "tb", project.src_dir / "sva", project.tb_dir, project.model_dir])
        out: PpaPlan = (await ctx.llm(stage)).output
        fixes, dropped = [], []
        for i, f in enumerate(out.fixes[:5], 1):
            if f.target == "rtl" and f.module not in modules:
                dropped.append(f"fix {i}: unknown module '{f.module}'")
                continue
            fixes.append({"id": f"F{len(history) * 5 + i}", **f.model_dump(), "handed_off": None})
        self.write(engine, PLAN_JSON, {**base, "status": "proposed" if fixes else "no_fix", "analysis": out.analysis, "fixes": fixes,
                                       "no_fix_reason": out.no_fix_reason, "dropped": dropped})
        ctx.emit("log", message=f"PPA round {base['iteration']}: {len(fixes)} fix(es) proposed"
                                + (f" ({'; '.join(dropped)} dropped)" if dropped else "")
                                + ("; review them, then hand them off (ppa_handoff)" if fixes else f" — {out.no_fix_reason or 'nothing helps'}"))

    # -- after the gate ---------------------------------------------------------------------------------------------

    def _pending(self, engine) -> list[dict]:
        return [f for f in (self.read(engine, PLAN_JSON, {}) or {}).get("fixes", []) if not f.get("handed_off")]

    def follow_up(self, engine):
        """Human gate, fixes proposed: offer to hand them off and move to the flow that owns the RTL."""
        pending = self._pending(engine)
        if not pending:
            return None
        owner = str(self.options.get("handoff_flow") or "vlsit")
        ids = ", ".join(f["id"] for f in pending)
        return {"title": f"Hand {len(pending)} PPA fix(es) to '{owner}' and switch to it?",
                "detail": f"{ids}: each becomes a scoped change request of the {owner} step that owns it (rtl / spec); run it there to "
                          f"apply them, then come back and run {engine.flow.name} again. No keeps them proposed (hand off later with h).",
                "action": "ppa_handoff", "switch": owner}

    def _self_approving(self, engine) -> str | None:
        """"auto_answer" (the project's switch or this gate's mode), "auto", or None: a person decides."""
        mode = engine.gate_mode(self.name)
        if engine.project.cfg.pipeline.auto_answer or mode == "auto_answer":
            return "auto_answer"
        return "auto" if mode == "auto" else None

    def _rtl_fixes(self, engine) -> list[str]:
        """The ids of the pending RTL fixes: the only ones a self-approving gate hands off (a spec change is yours to accept)."""
        return [f["id"] for f in self._pending(engine) if f.get("target") == "rtl"]

    def on_approve(self, engine) -> None:
        """A gate that approves itself in mode `auto` cannot ask: the RTL fixes go to the owner flow at once (`auto_answer` does it in
        `unattended`, then also runs that flow's rtl step)."""
        ids = self._rtl_fixes(engine)
        if self._self_approving(engine) != "auto" or not ids:
            return
        from q3tui.core.ops import Ops
        from q3tui.flows.fpga.addon import _handoff

        try:
            _handoff(Ops(engine), ",".join(ids))
        except Exception as exc:  # noqa: BLE001 - the approval stands; the fixes stay proposed
            engine.bus.emit("warning", self.name, message=f"PPA: hand-off failed ({exc}); fixes stay proposed")

    async def unattended(self, engine) -> bool:
        """Auto-answer: hand the RTL fixes to the owner flow, run its step (rtl), and run this flow again on the new RTL — the engine
        loops (sdc, synthesis and the report are stale now) until the targets are met or `max_iterations` rounds are used."""
        ids = self._rtl_fixes(engine)
        left = [f["id"] for f in self._pending(engine) if f.get("target") != "rtl"]
        if left:
            engine.bus.emit("warning", self.name, message=f"PPA: {', '.join(left)} need a change of the spec: not handed off "
                                                         "unattended (hand off with ppa_handoff, or accept it in the spec)")
        if not ids:
            return False
        from q3tui import flows
        from q3tui.core.ops import Ops
        from q3tui.flows.fpga.addon import _handoff
        from q3tui.pipeline.engine import Engine

        try:
            _handoff(Ops(engine), ",".join(ids))
            plan = self.read(engine, PLAN_JSON, {}) or {}
            steps = sorted({f["handed_off"]["step"] for f in plan.get("fixes", []) if f["id"] in ids and f.get("handed_off")})
            owner = flows.load_flow(str(self.options.get("handoff_flow") or "vlsit"), engine.project.root)
            for name in steps:
                engine.bus.emit("log", self.name, message=f"PPA: running {owner.name}:{name} for the handed-off fixes…")
                other = Engine(engine.project, engine.bus, flow=owner)
                outcome = await other.run(only=name, yes=True)
                engine.reload()  # (that run wrote the same pipeline.json, and dropped the run lock)
                engine._take_lock()
                if outcome != "complete":
                    engine.bus.emit("warning", self.name, message=f"PPA: {owner.name}:{name} ended '{outcome}': not going on; "
                                                                  "fix that, then run this flow again")
                    return False
        except Exception as exc:  # noqa: BLE001 - reported; the run ends here with the fixes handed off
            engine.bus.emit("warning", self.name, message=f"PPA: the unattended hand-off stopped ({exc})")
            return False
        return True
