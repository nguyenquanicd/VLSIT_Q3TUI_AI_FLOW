"""FPGA step `timing_util_report`: utilization, timing (WNS / TNS / Fmax), the worst path and what is unconstrained, from the
Vivado reports, judged against the targets (`schemas/ppa_report.json`); the gate is the review of the numbers.
Setup slack is judged at every stage; hold slack only after implementation (`options.fpga.implement` of fpga_synth).

Is the result the design? Targets alone pass a design the tool trimmed away (lib/judge.py): code gathers what the tool removed and the
memories the RTL declares; when that points at removed logic (`options.judge: auto`; `always`, `off`), one read-only LLM stage judges the
result against the design, and an implausible one fails the report (`verdict.plausibility`) with its causes and whose fix each is.

`options.targets`: `{max_util_pct: {lut: 70, ff: 70, bram_tile: 80, dsp: 80}, min_slack_ns: 0.0}` (timing must meet the SDC
by default: WNS >= `min_slack_ns`, 0 unless set)."""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.flows.fpga.lib import oss, quartus, templates, vivado
from q3tui.flows import skill_sections
from q3tui.flows.fpga.lib.base import PPA_JSON, SYNTH_JSON, FpgaStep
from q3tui.steps.common import Question, StepQuestions, assign_question_ids, load_answers

JUDGE = skill_sections(Path(__file__).parent / "md" / "skill.md")["JUDGE"]


class Step(StepQuestions, FpgaStep):
    name = "timing_util_report"
    title = "Timing and utilization report"
    deps = ("fpga_synth",)
    hashed_options = ("targets", "judge")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        rep = self.reports_dir(engine)
        # (the report states the clocks of sdc.json: a changed clock is a new report, even when the tool printed the same text)
        return [self.artifact(engine, SYNTH_JSON), sdcmod.config_path(engine.project),
                *(rep / n for n in ("utilization.rpt", "utilization_hier.rpt", "timing.rpt", "check_timing.rpt", "paths.rpt", "pnr.rpt", "yosys_stat.rpt"))]

    def outputs(self, engine) -> list[Path]:
        p = self.artifact(engine, PPA_JSON)
        return [p] if p.is_file() else []

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *(p for p in self._q_paths(engine) if p.is_file())]

    def missing_input(self, engine) -> str | None:
        synth = self.read(engine, SYNTH_JSON, None)
        if not synth:
            return "the synthesis is needed first (step fpga_synth)"
        if synth.get("status", "pass") != "pass":
            return f"the synthesis failed: {str(synth.get('error') or '')[:300]}" + (" — handed to fpga_port, then it runs again" if synth.get("handed_to") else "")
        return None

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        rep = self.reports_dir(engine)

        def text(name: str) -> str:
            f = rep / name
            return f.read_text(errors="replace") if f.is_file() else ""

        synth = self.read(engine, SYNTH_JSON, {})
        tpl = templates.TEMPLATES.get(synth.get("template") or "") or templates.get(None)
        backend = synth.get("backend") or "vivado"
        if backend == "yosys":  # nextpnr's reports (written by fpga_synth in the same names)
            util, timing = oss.parse_utilization(text("pnr.rpt"), text("yosys_stat.rpt"), tpl), oss.parse_timing(text("timing.rpt"))
        elif backend == "quartus":  # the summary, the per-entity table, the timing script's own lines and the Fmax summary
            util, timing = quartus.parse_utilization(text("utilization.rpt"), text("utilization_hier.rpt")), quartus.parse_timing(text("timing.rpt"), text("clocks.rpt"))
        else:
            util, timing = vivado.parse_utilization(text("utilization.rpt")), vivado.parse_timing(text("timing.rpt"))
        if not util or "wns_ns" not in timing:
            if re.search(r"^\s*NA(\s+NA)+\s*$", text("timing.rpt"), re.M):  # the summary is all NA: no clock reached anything
                raise StepFailed("Vivado reported no timing (WNS NA): no clock constraint matched the design — check the clock / reset "
                                 "ports of the SDC against the top module (the critical warnings are in schemas/fpga_synth.json)")
            if backend == "quartus" and "wns_ns" not in timing:
                raise StepFailed("Quartus' Timing Analyzer found no setup path: no clock constraint matched the design — check the clock ports of "
                                 "the SDC against the top module (build/syn/fpga/reports/synth.log)")
            raise StepFailed("could not read the reports in build/syn/fpga/reports/ (utilization.rpt / timing.rpt)")
        sdc = sdcmod.load(project)
        clocks = [{"name": c.name, "period_ns": c.period_ns, "freq_mhz": c.freq_mhz} for c in (sdc.clocks if sdc else [])]
        if clocks and backend == "vivado":  # (nextpnr and Quartus give their own Fmax)
            # the worst setup slack limits the clock of the (first) domain: period - WNS is the period it could run at
            period = clocks[0]["period_ns"]
            timing["fmax_mhz"] = round(1000.0 / (period - timing["wns_ns"]), 1) if period - timing["wns_ns"] > 0 else None
        targets = self.options.get("targets") or {}
        min_slack = float(targets.get("min_slack_ns", 0.0))
        over = {k: {"used_pct": util[k]["pct"], "max_pct": float(v)} for k, v in (targets.get("max_util_pct") or {}).items()
                if k in util and util[k].get("pct") is not None and util[k]["pct"] > float(v)}
        implemented = bool(synth.get("implemented"))
        verdict = {"timing": "pass" if timing["wns_ns"] >= min_slack else "fail",
                   # hold needs placement and routing (before them the tool has no skew / route delay): judged only then
                   "hold": ("n/a" if "whs_ns" not in timing else ("pass" if timing["whs_ns"] >= 0 else "fail")) if implemented else "not judged (post-synthesis)",
                   "utilization": ("fail" if over else "pass") if targets.get("max_util_pct") else "n/a",
                   "over_budget": over}
        await self._judge(ctx, synth, util, timing, verdict)
        verdict["overall"] = "pass" if "fail" not in (verdict["timing"], verdict["hold"], verdict["utilization"], verdict["plausibility"]) else "fail"
        data = {"part": synth.get("part"), "template": synth.get("template"), "top": synth.get("top"), "tool": synth.get("tool"), "stage": "implemented" if implemented else "post-synthesis",
                "clocks": clocks, "utilization": util, "timing": timing,
                "critical_path": quartus.critical_path(text("timing.rpt")) if backend == "quartus" else (oss if backend == "yosys" else vivado).critical_path(text("paths.rpt")),
                "unconstrained": vivado.unconstrained(text("check_timing.rpt")) if backend == "vivado" else {},
                "targets": targets, "verdict": verdict}
        self.write(engine, PPA_JSON, data)
        lut, ff = util.get("lut", {}), util.get("ff", {})
        ctx.emit("log", message=f"PPA ({data['stage']}): {lut.get('used')} {lut.get('label') or 'LUT'}, {ff.get('used')} {ff.get('label') or 'FF'}, WNS {timing['wns_ns']} ns"
                                + (f", Fmax ≈ {timing['fmax_mhz']} MHz" if timing.get("fmax_mhz") else "") + f" → {verdict['overall']}")

    async def _judge(self, ctx: StepContext, synth: dict, util: dict, timing: dict, verdict: dict) -> None:
        """verdict.plausibility (pass / fail / not judged / n/a) and verdict.judgment: is the result the design (lib/judge.py)."""
        from q3tui.flows.fpga.lib import judge
        from q3tui.llm.runtime import Stage
        from q3tui.steps.common import spec_block

        engine, project = ctx.engine, ctx.project
        mode = str(self.options.get("judge") or "auto")
        log = project.root / synth["log"] if synth.get("log") else None
        ev = judge.gather(log.read_text(errors="replace") if log and log.is_file() else "", self.rtl_files(engine))
        why = judge.suspicious(ev, util)
        if mode == "off":
            verdict["plausibility"] = "n/a"
            return
        if mode == "auto" and not why:
            verdict["plausibility"] = "pass"
            verdict["judgment"] = {"summary": "nothing points at removed logic (no storage removed, the RTL's memories are there)", "by": "code"}
            return
        hier = self.reports_dir(engine) / "utilization_hier.rpt"
        block, _ = spec_block(project)
        spec_text = "\n".join(p.read_text(errors="replace") for p in project.spec_documents() if p.suffix in (".md", ".txt"))
        from q3tui.flows.fpga.lib import sdc as sdcmod

        cfg = sdcmod.load(project)
        stage = Stage(name="ppa_judge", system_prompt=JUDGE, cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], output_model=judge.SynthJudgment,
                      prompt=judge.prompt(part=str(synth.get("part")), top=str(synth.get("top")), tool=str(synth.get("tool")),
                                          clocks=[{"name": c.name, "period_ns": c.period_ns} for c in (cfg.clocks if cfg else [])], util=util, timing=timing,
                                          ev=ev, why=why, hier=hier.read_text(errors="replace") if hier.is_file() else "", spec=block,
                                          targets=judge.target_lines(spec_text), rtl=[project.rel(f) for f in self.rtl_files(engine)]))
        try:
            j: judge.SynthJudgment = (await ctx.llm(stage)).output
        except StepFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - no credentials, a stage that failed: the numbers stand, said so
            verdict["plausibility"] = "not judged"
            verdict["judgment"] = {"summary": "could not judge: " + (str(exc).splitlines()[0] if str(exc) else type(exc).__name__), "why": why}
            ctx.emit("warning", message=f"the result looks trimmed ({'; '.join(why)}) and could not be judged: check it yourself")
            return
        verdict["plausibility"] = "pass" if j.plausible else "fail"
        verdict["judgment"] = {**j.model_dump(), "why": why, "by": "llm"}
        handed = not j.plausible and self._hand_to_port(ctx, j)
        verdict["judgment"]["handed_to"] = "fpga_port" if handed else None
        self._ask(engine, j, ask=not handed)
        if not j.plausible:
            ctx.emit("warning", message=f"the synthesis result is not the design{' → fpga_port' if handed else ''}: {j.summary}"
                                        + "".join(f" | {c.owner}{':' + c.module if c.module else ''}: {c.what}" for c in j.causes[:3]))

    def _hand_to_port(self, ctx: StepContext, j) -> bool:
        """A result that is not the design goes to fpga_port (the step that makes the RTL ready for the part) with the judgment; the fix loop
        runs it, the synthesis and this report again. False (asked here instead): no fpga_port in the flow, or out of rounds."""
        engine = ctx.engine
        meta = engine.state.step_meta.setdefault(self.name, {})
        run_id = getattr(engine, "_run_id", None)
        if meta.get("run") != run_id or "rounds" not in meta:
            meta["run"], meta["rounds"] = run_id, 0
        mx = self.options.get("max_rounds")
        limit = int(mx) if mx is not None else ctx.cfg.pipeline.max_fix_iterations
        if "fpga_port" not in engine.by_name or meta["rounds"] >= limit:
            return False
        causes = "; ".join(f"({c.owner}{':' + c.module if c.module else ''}) {c.what} — evidence: {c.evidence}" + (f" — {c.request}" if c.request else "")
                           for c in j.causes[:6])
        ways = " | ".join(o.label for o in j.options[:4])
        engine.request_change("fpga_port", f"From timing_util_report: the synthesis result is not the design. {j.summary}"
                                           + (f" Expected: {j.expected}." if j.expected else "") + (f" Causes: {causes}." if causes else "")
                                           + (f" Ways the judge sees: {ways}." if ways else ""))
        meta["loop"], meta["rounds"] = True, meta["rounds"] + 1
        return True

    def _ask(self, engine, j, ask: bool = True) -> None:
        """An implausible result is a decision for you: the judge's ways to a real result are the question's pick list (its recommendation
        first, the default). Questions answered before stay (ppa_optimize reads them as decisions)."""
        answers = load_answers(self._q_paths(engine)[1])
        kept = [q for q in self.questions_list(engine) if q.id in answers]
        asks = []
        if not j.plausible and ask:
            causes = " ".join(f"({c.owner}{':' + c.module if c.module else ''}) {c.what}" for c in j.causes[:4])
            asks.append(Question(id="", blocking=True, kind="design_choice",
                                 question=f"The synthesis result is not the design: {j.summary} {causes} {j.question or 'How should it be settled?'}".strip(),
                                 default_assumption=j.options[0].label if j.options else "", options=j.options[:4]))
        self.save_questions(engine, [*kept, *assign_question_ids(asks, kept, "PPA", taken=set(answers))])
        self.write_questions_file(engine)
