"""VLSIT step `verify` (Phase 5, docs/spec/vlsit-flow.md): simulate every selected test case, mutation-test the RTL, build the
RTM, report, Gate 5.

Code does everything computable: the tool roles `sim` / `run` (+ `mutate`) run the testbench per test case
(`TC_RESULT <id> PASS|FAIL`, SVA violation lines — simlog.py), the mutation score per requirement comes from mutants made
of the RTL (mutation.py), the six-condition matrix is rebuilt from the files (rtm.py). A test case without a result line is a
failure, `N/A` / `pending` mutation is never evidence. `signed_off` is only ever set by `review.sign_req` (you).

The fix loop: failing test cases go to one single-shot stage `verify_triage` (no tools) that says whose bug each is
(RTL / TB / SVA / spec); fixes are dispatched as scoped change requests (`[rtl:<module>]`, `[tb:<tc>]`, `[sva:<module>]`) and
the engine runs again (`meta["loop"]`), bounded by pipeline.max_fix_iterations. The same finding coming back
pipeline.escalate_after_repeats times becomes a question for you instead. Compile errors are routed by file, without an LLM.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from q3tui.eda import tools as T
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.core.project import read_json, write_json
from q3tui.steps.common import Question, assign_question_ids, load_answers, scoped
from q3tui.steps.vlsit import artifacts, mutation, rtm, simenv, simlog
from q3tui.steps.vlsit.base import VlsitStep

from pathlib import Path as _Path

from q3tui.flows import skill_sections as _skill_sections

# the stage system prompts are the named sections of md/skill.md
TEXT = _skill_sections(_Path(__file__).parent / "md" / "skill.md")

SYSTEM = TEXT["SYSTEM"]


class TcVerdict(BaseModel):
    tc_id: str
    verdict: Literal["RTL_BUG", "TB_BUG", "SVA_BUG", "SPEC_ISSUE", "UNSURE"]
    unit: str = Field("", description="RTL / SVA: the module name; TB: the tc_id")
    summary: str
    fix: str = ""


class VerifyTriage(BaseModel):
    verdicts: list[TcVerdict]
    notes: str = ""


INVESTIGATE_SYSTEM = TEXT["INVESTIGATE_SYSTEM"]

ROUTE = {"RTL_BUG": "rtl", "TB_BUG": "tb", "SVA_BUG": "sva"}


def _strip_code(text: str) -> str:
    return re.sub(r"```.*?```", "", text or "", flags=re.S).strip()


class VerifyStep(VlsitStep):
    name = "verify"
    title = "Verification"
    deps = ("sva", "rtl", "tb")
    artifact = "verification_report.json"
    tool_roles = ("sim", "run")

    def max_rounds(self, cfg) -> int:
        """Fix rounds before the loop stops: `options.max_fix_rounds` of the flow file, else pipeline.max_fix_iterations."""
        return int(self.options.get("max_fix_rounds") or cfg.pipeline.max_fix_iterations)

    # -- files ---------------------------------------------------------------------------------

    def inputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        files = []
        for d in (lay.rtl, lay.tb, lay.sva):
            if d.is_dir():
                files += sorted(p for p in d.rglob("*") if p.is_file() and p.suffix not in (".md", ".log"))
        return [lay.artifact("structured_spec.json"), lay.artifact("final_config.json"), lay.artifact("selected_testplan.json"),
                *files, self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        return [p for p in (lay.artifact("verification_report.json"), lay.artifact("rtm.json"), lay.schemas / "RTM.md") if p.is_file()]

    def reset_files(self, engine) -> list[Path]:  # (your sign-offs in schemas/rtm_signoffs.json stay)
        return [*super().reset_files(engine), *self.outputs(engine)]

    def missing_input(self, engine) -> str | None:
        lay = self.lay(engine)
        if not lay.artifact("structured_spec.json").is_file():
            return "the parsed requirements are needed first (schemas/structured_spec.json)"
        if not lay.artifact("selected_testplan.json").is_file():
            return "the test plan is needed first (schemas/selected_testplan.json: tb step)"
        if not lay.rtl.is_dir() or not any(lay.rtl.glob("*.sv")):
            return "the RTL is needed first (src/rtl)"
        return simenv.roles_problem(engine, ("sim", "run"))

    def on_approve(self, engine) -> None:
        now = artifacts.now()
        # the original /autoflow: "a TC FAILs → Gate 5 cannot be signed" — an approval of the gate (also an unattended one)
        # never writes a signature over failing test cases
        failing = [t for t, v in ((rtm.sim_results(engine) or {}).get("tests") or {}).items() if v.get("status") != "pass"]
        report = artifacts.read(engine.project, "verification_report.json") or {}
        if report.get("sim_summary", {}).get("compile_errors"):
            failing = failing or ["(the simulation does not compile)"]
        signed = not failing
        if not signed:
            engine.bus.emit("warning", self.name, message=f"Gate 5 is not signed: {len(failing)} test case(s) fail "
                                                          f"({', '.join(failing[:6])}); fix them (or answer the verify questions) and run again")
        artifacts.set_gate(engine.project, "rtm.json", {"gate_5_approved": signed, "gate_5_at": now if signed else None})
        artifacts.set_gate(engine.project, "verification_report.json", {"gate_5_approved": signed, "approved_at": now if signed else None})
        from q3tui.steps.vlsit.base import rehash_shared

        rehash_shared(engine, ["rtm.json", "verification_report.json"])

    # -- run -----------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        import anyio

        engine, project, cfg = ctx.engine, ctx.project, ctx.cfg
        lay = self.lay(engine)
        ts = T.load_tools(cfg, project.root)
        meta = engine.state.step_meta.setdefault(self.name, {})
        run_id = getattr(engine, "_run_id", None)
        if meta.get("run") != run_id:  # the round bound is per run (a later run starts counting again)
            meta["run"], meta["fix_rounds"] = run_id, 0
        meta["loop"] = False
        plan = rtm.testplan(engine)
        tc_ids = [t["tc_id"] for t in plan]
        if not tc_ids:
            raise StepFailed("schemas/selected_testplan.json has no selected test case")
        with_sva = T.supports(ts, "sim", "sva") and any(lay.sva.glob("*_sva.sv"))
        work = rtm.work_dir(engine, "verify")
        work.mkdir(parents=True, exist_ok=True)

        cached = rtm.sim_results(engine)
        reuse = bool(cached) and not ctx.regenerate and not ctx.feedback and all(t in cached.get("tests", {}) for t in tc_ids) \
            and cached.get("with_sva") == with_sva
        compile_failed: list[str] = []
        if reuse:
            tests = cached["tests"]
            ctx.emit("log", message="verify: the sources are unchanged since the last simulation — results reused")
        else:
            res = await anyio.to_thread.run_sync(lambda: simenv.compile_all(engine, ts, work, with_sva))
            if not res.ok:
                errs = [d for d in res.diagnostics if d.severity == "error"]
                compile_failed = [f"{d.file}:{d.line} {d.message}" if d.file else d.message for d in errs] or [res.summary]
                tests = {}
                ctx.emit("error", message="compile failed: " + "; ".join(compile_failed[:4]))
            else:
                tests = {}
                for tc in tc_ids:
                    out = await anyio.to_thread.run_sync(lambda tc=tc: simenv.run_tc(engine, ts, work, tc))
                    tests[tc] = out.to_dict()
                    ctx.emit("log", message=f"{tc}: {out.status.upper()}" + (f" (SVA: {', '.join(out.sva_violations[:3])})" if out.sva_violations else "")
                             + (f" — {out.reason}" if out.reason else ""))
                write_json(work / "results.json", {"fingerprint": rtm.fingerprint(engine), "with_sva": with_sva, "tests": tests,
                                                   "tool_versions": simenv.tool_versions(engine, ts, ["sim", "run", "lint", "synth"])})
        failing = [tc for tc in tc_ids if tests.get(tc, {}).get("status") != "pass"]

        # mutation testing: needs a passing baseline
        mut = rtm.mutation_results(engine)
        if compile_failed or failing:
            mut = None
        elif not self.options.get("mutation", True):
            mut = mutation.not_run(engine, "mutation testing is turned off (options.mutation: false)")
        elif mut is None:
            per = int(self.options.get("mutants_per_module", 12))
            if "mutate" in ts.roles and T.available(ts, "mutate"):
                ctx.emit("log", message="mutation testing (tool role `mutate`) …")
                mut = await anyio.to_thread.run_sync(lambda: mutation.run_tool(engine, ts, tc_ids, per))
            else:
                ctx.emit("log", message=f"mutation testing (built-in mutator, ≤ {per} mutants per module) …")
                mut = await mutation.run_builtin(engine, ts, tc_ids, per, with_sva, int(self.options.get("mutation_parallel", 4)), ctx.emit)
        else:
            ctx.emit("log", message="mutation results reused (sources unchanged)")

        rtm_doc = rtm.write_rtm(engine)
        report = self._report(engine, ts, rtm_doc, tests, tc_ids, plan, mut, compile_failed)
        artifacts.write(project, "verification_report.json", report)
        rtm.write_rtm(engine)  # (the report now exists: its rtm_summary follows)
        rows = rtm_doc["requirements"]
        sim = report["sim_summary"]
        ctx.emit("log", message=f"verify: {sim['pass']}/{sim['total_tc']} test case(s) pass, {sim['sva_violations']} SVA violation(s); "
                                f"{sum(r['status'] == 'pending' for r in rows)} requirement(s) ready to sign, "
                                f"{sum(r['status'] == 'locked' for r in rows)} locked")
        if reuse and not failing:
            ctx.unchanged = True
        if compile_failed:
            await self._dispatch_compile_errors(ctx, compile_failed)
        elif failing:
            await self._triage(ctx, failing, tests, plan, rtm_doc)
        else:
            meta["fix_rounds"] = 0
            self.save_questions(engine, [q for q in self.questions_list(engine) if q.id in load_answers(self._q_paths(engine)[1])])
            if self.options.get("auto_sign", engine.auto_reviews()):
                self._auto_sign(ctx)

    def _auto_sign(self, ctx: StepContext) -> None:
        """Auto approve: every requirement whose conditions 1–5 hold (the RTM's `pending`) is signed — marked as not reviewed.
        Never over a failing test case (this runs only when everything passes) and never one that is locked."""
        from q3tui.flows.vlsit.sva import review

        engine = ctx.engine
        ready = [r["req_id"] for r in review.rtm_rows(engine) if r["status"] == "pending"]
        for rid in ready:
            review.sign_req(engine, rid, True, "auto-signed (auto approve), not reviewed")
        if ready:
            ctx.emit("warning", message=f"verify: auto approve — signed off {len(ready)} requirement(s) that meet conditions 1–5 "
                                        f"({', '.join(ready[:8])}{' …' if len(ready) > 8 else ''}); nobody reviewed them")

    # -- the report ------------------------------------------------------------------------------

    def _report(self, engine, ts, rtm_doc: dict, tests: dict, tc_ids: list[str], plan: list[dict], mut: dict | None,
                compile_failed: list[str]) -> dict:
        project = engine.project
        spec_meta = rtm.spec_data(engine).get("metadata", {})
        old = (artifacts.read(project, "verification_report.json", {}) or {}).get("metadata", {})
        titles = {t["tc_id"]: t.get("title", "") for t in plan}
        results = []
        for tc in tc_ids:
            t = tests.get(tc)
            results.append({"tc_id": tc, "name": titles.get(tc, ""), "status": t["status"] if t else "skipped",
                            "cycles": (t or {}).get("cycles"), "sva_violations": len((t or {}).get("sva_violations") or []),
                            "log_file": (t or {}).get("log_file", "")})
        count = lambda s: sum(r["status"] == s for r in results)  # noqa: E731
        rows = rtm_doc["requirements"]
        above = sum(isinstance(r["mutation_score"], (int, float)) and r["mutation_score"] >= rtm.THRESHOLD for r in rows)
        below = sum(isinstance(r["mutation_score"], (int, float)) and r["mutation_score"] < rtm.THRESHOLD for r in rows)
        na = sum(r["mutation_score"] == "N/A" for r in rows)
        ms = {"threshold": rtm.THRESHOLD, "total_req_ids": len(rows), "above_threshold": above, "below_threshold": below, "na": na,
              "per_req_id": [{"req_id": r["req_id"], "total": r["mutation_detail"]["total"], "killed": r["mutation_detail"]["killed"],
                              "score": r["mutation_score"] if r["mutation_score"] != "pending" else "N/A",
                              "pass": isinstance(r["mutation_score"], (int, float)) and r["mutation_score"] >= rtm.THRESHOLD} for r in rows]}
        if mut:
            ms.update(total_mutations=mut.get("total_mutations", 0), total_killed=mut.get("total_killed", 0), global_score=mut.get("global_score", 0.0))
        synth = artifacts.read(project, "synth_report.json", {}) or {}
        # synth_report.json as the rtl step writes it: `synthesis` (pass | fail | not run), corners{name: {status, cell_count, area_estimate_um2}}
        state = synth.get("synthesis", synth.get("status"))
        corners = synth.get("corners") or {}
        corner = next(iter(corners), "")
        c = corners.get(corner) or {}
        ss = {"status": state if state in ("pass", "fail") else "not_run",
              "cell_count": c.get("cell_count", synth.get("cell_count")), "area_um2": c.get("area_estimate_um2", synth.get("area_um2")),
              "pdk": synth.get("pdk") or "", "corner": corner or synth.get("corner") or ""}
        data = {
            "metadata": {"ip_name": spec_meta.get("ip_name", ""), "spec_revision": str(spec_meta.get("spec_revision", "")), "phase": 5,
                         "gate_5_approved": bool(old.get("gate_5_approved", False)), "approved_at": old.get("approved_at")},
            "tool_versions": {k: v for k, v in ((read_json(rtm.work_dir(engine, "verify") / "results.json", {}) or {}).get("tool_versions") or {}).items()},
            "sim_summary": {"total_tc": len(results), "pass": count("pass"), "fail": count("fail"), "timeout": count("timeout"),
                            "sva_violations": sum(r["sva_violations"] for r in results), "tc_results": results},
            "mutation_summary": ms,
            "rtm_summary": rtm.rtm_summary(rows),
            "synth_sign_off": ss,
        }
        if compile_failed:
            data["sim_summary"]["compile_errors"] = compile_failed[:20]
        return data

    # -- the fix loop ------------------------------------------------------------------------------

    def _owner(self, engine, path_text: str, message: str = "") -> tuple[str, str] | None:
        """(step, unit) of the file an error names."""
        lay = self.lay(engine)
        p = Path(path_text)
        p = p if p.is_absolute() else engine.project.root / p
        try:
            p = p.resolve()
        except OSError:
            return None
        for d, kind in ((lay.rtl, "rtl"), (lay.sva, "sva"), (lay.tb, "tb")):
            if d.resolve() in p.parents:
                stem = p.stem
                if kind == "sva" and stem.endswith("_bind"):
                    # the bind file is generated: its error (a width mismatch, an unknown port) is in the assertion module it
                    # instantiates — the file that defines the `<x>_sva` module named in the message
                    for name in re.findall(r"\b(\w+_sva)\b", message):
                        for f in sorted(lay.sva.glob("*.sv")):
                            if f.stem != stem and re.search(rf"\bmodule\s+{re.escape(name)}\b", f.read_text(errors="replace")):
                                return "sva", f.stem[: -len("_sva")] if f.stem.endswith("_sva") else f.stem
                return kind, (stem[: -len("_sva")] if kind == "sva" and stem.endswith("_sva") else stem)
        return None

    async def _dispatch_compile_errors(self, ctx: StepContext, errors: list[str]) -> None:
        engine, cfg = ctx.engine, ctx.cfg
        meta = engine.state.step_meta.setdefault(self.name, {})
        step_of = {kind: next((s.name for s in engine.steps if s.kind == f"vlsit_{kind}"), kind) for kind in ("rtl", "tb", "sva")}
        by: dict[tuple[str, str], list[str]] = {}
        for e in errors:
            m = re.match(r"(\S+?):(\d+)\s", e)
            owner = self._owner(engine, m.group(1), e) if m else None
            if owner:
                by.setdefault(owner, []).append(e)
        if not by:
            raise StepFailed("the simulation does not compile: " + "; ".join(errors[:4]))
        rounds = int(meta.get("fix_rounds", 0))
        if rounds >= self.max_rounds(cfg):
            ctx.emit("warning", message=f"compile errors remain after {rounds} fix round(s) (options.max_fix_rounds / pipeline.max_fix_iterations): " + "; ".join(errors[:3]))
            return
        for (kind, unit), msgs in by.items():
            engine.request_change(step_of[kind], scoped(step_of[kind], unit, "From verify: the simulation does not compile — fix: " + "; ".join(msgs[:6])))
            ctx.emit("log", message=f"compile error → {step_of[kind]} ({unit}): {msgs[0]}")
        meta["loop"], meta["fix_rounds"] = True, rounds + 1

    async def _investigate(self, ctx: StepContext, out: VerifyTriage, unsure: list[str], tests: dict, by_tc: dict, reqs: dict,
                           modules: list[dict]) -> VerifyTriage:
        """The test cases triage could not place: a second stage WITH tools (Read / Grep / Glob) that opens the full simulation
        log, the test case, the testbench top, the RTL and the SVA. (The logs are copied out of `.q3tui/`, which no stage
        may read.) Its verdicts replace the UNSURE ones; a verdict that stays UNSURE becomes a question as before."""
        engine, project = ctx.engine, ctx.project
        lay = self.lay(engine)
        logs = project.root / ctx.cfg.project.sim_dir / "vlsit_verify"
        logs.mkdir(parents=True, exist_ok=True)
        items = []
        for tc in unsure:
            res, t = tests.get(tc, {}), by_tc.get(tc, {})
            lf = res.get("log_file")
            copy = logs / f"{tc}.log"
            if lf and (project.root / lf).is_file():
                copy.write_text((project.root / lf).read_text(errors="replace"))
            test_file = t.get("file") or ""
            rq = "; ".join(f"{r}: {reqs[r].get('text', '')[:200]}" for r in t.get("req_ids") or [] if r in reqs)
            items.append(f"- {tc} ({t.get('title', '')}): {res.get('reason', '')}\n  log đầy đủ: {project.rel(copy)}"
                         f"\n  file test case: {test_file or '(xem ' + project.rel(lay.tb) + '/tests/)'}\n  REQ: {rq or '—'}")
        first = {v.tc_id: v for v in out.verdicts if v.tc_id in unsure}
        prompt = ("Các test case sau FAIL nhưng lần phân loại đầu trả UNSURE. Hãy điều tra bằng công cụ:\n\n" + "\n".join(items)
                  + f"\n\nTestbench top: {project.rel(lay.tb)}/tb_top.sv · RTL: {project.rel(lay.rtl)}/ · SVA: {project.rel(lay.sva)}/\n"
                  + "Lý do UNSURE ban đầu: " + "; ".join(f"{k}: {v.summary}" for k, v in first.items())
                  + "\n\nModule RTL và REQ: " + ", ".join(f"{m['name']} ({', '.join(m['req_ids'])})" for m in modules)
                  + "\n\nTrả về đúng một verdict cho mỗi test case ở trên.")
        ctx.emit("log", message=f"investigating {', '.join(unsure)} (triage was unsure) …")
        got: VerifyTriage = (await ctx.llm(Stage(name="verify_investigate", system_prompt=INVESTIGATE_SYSTEM, prompt=prompt, cwd=project.root,
                                                 builtin_tools=["Read", "Grep", "Glob"], output_model=VerifyTriage))).output
        by = {v.tc_id: v for v in got.verdicts if v.tc_id in unsure}
        merged = [by.get(v.tc_id, v) if v.tc_id in unsure else v for v in out.verdicts]
        return VerifyTriage(verdicts=merged, notes=out.notes)

    async def _triage(self, ctx: StepContext, failing: list[str], tests: dict, plan: list[dict], rtm_doc: dict) -> None:
        engine, cfg, project = ctx.engine, ctx.cfg, ctx.project
        meta = engine.state.step_meta.setdefault(self.name, {})
        history: list[dict] = meta.setdefault("history", [])
        rounds = int(meta.get("fix_rounds", 0))
        reqs = {r["req_id"]: r for r in rtm.requirements(engine)}
        by_tc = {t["tc_id"]: t for t in plan}
        props = {p["label"]: p for p in rtm.properties(engine)}
        modules = rtm.modules(engine)
        blocks = []
        for tc in failing:
            t, res = by_tc.get(tc, {}), tests.get(tc, {})
            log = ""
            lf = res.get("log_file")
            if lf and (project.root / lf).is_file():
                # (the covers that hit are noise here: the failing checks and the end of the run are what matters)
                lines = [x for x in (project.root / lf).read_text(errors="replace").splitlines() if not x.startswith("COVER_HIT")]
                log = "\n".join(lines[-25:])
            viol = [f"{v}" + (f" — {props[v]['nl']}" if v in props else "") for v in res.get("sva_violations") or []]
            rq = "\n".join(f"  {r}: {reqs[r].get('text', '')}" for r in t.get("req_ids") or [] if r in reqs)
            blocks.append(f"### {tc} {t.get('title', '')} → {res.get('status', 'skipped').upper()}\n{res.get('reason', '')}\n"
                          f"REQ:\n{rq or '  (không có)'}\nSVA vi phạm: {'; '.join(viol) or 'không'}\nCuối log:\n{log}")
        mods = "\n".join(f"- {m['name']}: {', '.join(m['req_ids']) or '—'}" for m in modules)
        hist = "\n".join(f"- vòng {h['round']}: {h['verdict']} {h['unit']} ({h['tc']}): {h['summary']}" for h in history[-20:])
        answers = load_answers(self._q_paths(engine)[1])
        settled = "\n".join(f"- {q.question} → {answers[q.id]}" for q in self.questions_list(engine) if q.id in answers)
        prompt = (f"Các test case sau FAIL / TIMEOUT:\n\n" + "\n\n".join(blocks) + f"\n\n## Module RTL và REQ của chúng\n{mods}\n"
                  + (f"\n## Đã sửa ở các vòng trước (không lặp lại cùng một sửa)\n{hist}\n" if hist else "")
                  + (f"\n## Quyết định người dùng đã đưa ra (áp dụng)\n{settled}\n" if settled else "")
                  + "\nTrả về đúng một verdict cho mỗi test case (tc_id như trên).")
        out: VerifyTriage = (await ctx.llm(Stage(name="verify_triage", system_prompt=SYSTEM, prompt=prompt, cwd=project.root,
                                                 builtin_tools=[], output_model=VerifyTriage, max_turns=4))).output
        unsure = [v.tc_id for v in out.verdicts if v.verdict == "UNSURE" and v.tc_id in failing]
        if unsure and self.options.get("investigate", True):
            out = await self._investigate(ctx, out, unsure, tests, by_tc, reqs, modules)
        module_names = [m["name"] for m in modules]
        step_of = {kind: next((s.name for s in engine.steps if s.kind == f"vlsit_{kind}"), kind) for kind in ("rtl", "tb", "sva")}
        seen_counts: dict[tuple, int] = {}
        for h in history:
            k = (h["verdict"], h["unit"], h["tc"])
            seen_counts[k] = seen_counts.get(k, 0) + 1
        dispatched, asks = [], []
        for v in out.verdicts:
            if v.tc_id not in failing:
                continue
            unit = v.unit.strip()
            if v.verdict in ("RTL_BUG", "SVA_BUG"):
                unit = next((m for m in module_names if m == unit or m in unit or unit in m and unit), unit)
                if unit not in module_names:  # the module that implements the TC's first requirement
                    tcr = set(by_tc.get(v.tc_id, {}).get("req_ids") or [])
                    unit = next((m["name"] for m in modules if tcr & set(m["req_ids"])), module_names[0] if module_names else unit)
            elif v.verdict == "TB_BUG":
                unit = v.tc_id
            fix = _strip_code(v.fix) or v.summary
            key = (v.verdict, unit, v.tc_id)
            seen = seen_counts.get(key, 0)
            text = f"From verify ({v.tc_id} {v.verdict}): {_strip_code(v.summary)} {fix}"
            if v.verdict in ROUTE and seen < cfg.pipeline.escalate_after_repeats and rounds < self.max_rounds(cfg) and len(fix) >= 8:
                step = step_of[ROUTE[v.verdict]]
                engine.request_change(step, scoped(step, unit, text))
                dispatched.append((v, step, unit))
            elif v.verdict in ROUTE and seen >= cfg.pipeline.escalate_after_repeats:
                asks.append((v, f"{v.tc_id} keeps failing ({v.verdict} in {unit}, {seen + 1}×): {v.summary} How should it be settled?",
                             "Keep the requirement as it is and change the implementation once more, starting from the evidence.", "design_choice"))
            elif v.verdict in ("SPEC_ISSUE", "UNSURE"):
                asks.append((v, f"{v.tc_id}: {v.summary}", v.fix or "Keep the spec as written and treat the failing side as the bug.",
                             "spec_gap" if v.verdict == "SPEC_ISSUE" else "design_choice"))
        for v, step, unit in dispatched:
            history.append({"round": rounds + 1, "verdict": v.verdict, "unit": unit, "tc": v.tc_id, "summary": v.summary})
            ctx.emit("log", message=f"{v.tc_id}: {v.verdict} → {step} ({unit}): {v.summary}")
        old = self.questions_list(engine)
        kept = [q for q in old if q.id in answers]
        # (blocking: a failing test case is not something to approve with a default — the original stops before Gate 5)
        fresh = [Question(id="", question=q, blocking=True, default_assumption=d, kind=k) for _, q, d, k in asks]
        self.save_questions(engine, [*kept, *assign_question_ids(fresh, kept, "VF", taken=set(answers))])
        for v, q, _, _ in asks:
            ctx.emit("warning", message=f"{v.tc_id}: {v.verdict} — a question for you (verify questions): {v.summary}")
        if dispatched:
            meta["loop"], meta["fix_rounds"] = True, rounds + 1
            ctx.emit("log", message=f"fix round {rounds + 1}/{self.max_rounds(cfg)}: updating the steps that got fixes, then verifying again")
        elif failing and rounds >= self.max_rounds(cfg):
            ctx.emit("warning", message=f"reached the fix-round limit ({self.max_rounds(cfg)}); {len(failing)} test case(s) still fail")
