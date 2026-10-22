"""Step `smoke` of the RTL flow: a basic-function testbench from the spec alone, compiled with the RTL and run.

Independence (as the VLSIT `tb`): the writer sees the spec, the requirements and the configuration — the RTL files are denied to it. It works with
a skill (smoke-testbench) and the tools `smoke_compile` / `smoke_run`, so it iterates by itself. Code decides the result: it compiles and runs the
testbench again and reads the `SMOKE_RESULT` lines. A failure goes to a second LLM (skill smoke-triage) that may read everything; code dispatches
its verdicts — an RTL bug to `rtl` as `[rtl:<module>] …`, a testbench bug back to this step, a spec issue / unsure as a blocking question — and the
engine's fix loop runs the chain again, bounded by `options.max_rounds`. Update, not re-run: the testbench is rewritten only when what it was built
from (the spec, the requirements, the parameters, your answers) changed, or a change request names it; a changed RTL only compiles and runs again."""

from __future__ import annotations

from pathlib import Path
import anyio

from q3tui.core.project import sha256_file, sha256_json, write_json
from q3tui.flows import skill_sections
from q3tui.flows.rtl.lib import csr
from q3tui.flows.rtl.lib import skills as skillmod
from q3tui.flows.rtl.lib import smoke
from q3tui.flows.rtl.lib.base import RtlFlowStep
from q3tui.flows.rtl.lib.models import SmokePlan, SmokeTriage, Verdict
from q3tui.flows.rtl.lib.tools import smoke_tools
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import Question, assign_question_ids, load_answers, scoped, spec_block
from q3tui.flows.spec.lib import artifacts

_SECTIONS = skill_sections(Path(__file__).parent / "md" / "skill.md")
WRITER, TRIAGE = _SECTIONS["WRITER"], _SECTIONS["TRIAGE"]


def describe_changes(old: dict, new: dict) -> list[str]:
    out = []
    for rid in sorted(set(new["reqs"]) - set(old["reqs"])):
        out.append(f"new requirement {rid}: {new['reqs'][rid]}")
    for rid in sorted(set(old["reqs"]) - set(new["reqs"])):
        out.append(f"{rid} no longer applies")
    out += [f"{rid} changed: {new['reqs'][rid]} (was: {old['reqs'][rid]})" for rid in sorted(set(old["reqs"]) & set(new["reqs"])) if old["reqs"][rid] != new["reqs"][rid]]
    out += [f"parameter {k} = {new['params'].get(k)} (was {old['params'].get(k)})" for k in sorted(set(old["params"]) | set(new["params"])) if old["params"].get(k) != new["params"].get(k)]
    out += [f"the spec document {k} changed: read it again" for k in sorted(set(old["spec"]) | set(new["spec"])) if old["spec"].get(k) != new["spec"].get(k)]
    if old.get("answers") != new.get("answers"):
        out.append("your answers to its questions changed")
    return out


class Step(RtlFlowStep):
    name = "smoke"
    title = "Smoke test"
    deps = ("style_lint",)
    tool_roles = ("sim", "run")

    def _files(self, engine) -> dict[str, Path]:
        d = self.smoke_dir(engine)
        return {"tb": d / smoke.TB_FILE, "plan": d / "smoke_plan.md", "plan_json": d / "smoke_plan.json", "report": d / "smoke_report.json", "filelist": d / "filelist.f"}

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "final_config.json"), *self.design_files(engine),
                self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        return [f for k, f in self._files(engine).items() if f.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        p = engine.project
        if not self.design_files(engine):
            return f"the RTL is needed first ({p.rel(self.rtl_dir(engine))}/: step rtl)"
        if not artifacts.path(p, "structured_spec.json").is_file() or not artifacts.path(p, "final_config.json").is_file():
            return "the parsed spec and the configuration are needed first (steps parse and config)"
        waiting = [s for s in ("rtl", "rules", "csr") if engine.state.feedback.get(s)]
        if waiting:  # fixes were handed back: the chain runs them first, the smoke test then looks at the result
            return f"waiting for the fixes handed to {', '.join(waiting)} (the chain runs them next)"
        return self.tool_problem(engine, "sim", "run")

    def max_rounds(self, cfg) -> int:
        return int(self.options.get("max_rounds") or cfg.pipeline.max_fix_iterations)

    def _snapshot(self, engine) -> dict:
        p = engine.project
        spec = artifacts.read(p, "structured_spec.json") or {}
        config = artifacts.read(p, "final_config.json") or {}
        return {"spec": {p.rel(f): sha256_file(f) for f in p.spec_documents()},
                "reqs": {r["req_id"]: r.get("text", "") for r in spec.get("requirements", []) if r.get("req_id")},
                "params": {k: str((v or {}).get("value")) for k, v in (config.get("parameters") or {}).items()},
                "answers": load_answers(self._q_paths(engine)[1]), "top": (spec.get("metadata") or {}).get("top_module")}

    # -- the writer -----------------------------------------------------------------------------------------------

    def _writer_prompt(self, engine, snap: dict, tb: Path, old: dict | None, feedback: list[str], index: str) -> str:
        block, _ = spec_block(engine.project)
        parts = ["# Task: write the smoke testbench", f"Write it to `{engine.project.rel(tb)}`. The design under test is the module `{snap['top'] or '(see the spec)'}`.", "",
                 "## Specification", block, "", "## Requirements", "\n".join(f"- {k}: {v}" for k, v in snap["reqs"].items()) or "(none)", "",
                 "## Configuration (parameter values to instantiate with)", "\n".join(f"- {k} = {v}" for k, v in snap["params"].items()) or "(none)"]
        if old is not None and tb.is_file():
            ch = describe_changes(old, snap)
            parts += ["", "## Update: the testbench exists and was written from earlier inputs. Change only what these require", *map("- {}".format, ch or ["(nothing new in the inputs)"]),
                      "", "Current testbench:", "```systemverilog", tb.read_text(), "```"]
        if feedback:
            parts += ["", "## Change requests", *[f"- {f}" for f in feedback]]
        return "\n".join([*parts, "", index])

    def _deny(self, engine) -> list[Path]:
        """Independence: every RTL file (and what is made of it: style files, the synthesis script) is denied to the writer. The folder itself
        cannot be: the smoke folder lives under it."""
        p = engine.project
        rtl = self.rtl_dir(engine)
        files = [f for f in sorted(rtl.iterdir()) if f.is_file()] if rtl.is_dir() else []
        return [*files, self.style_dir(engine), rtl / "csr", p.model_dir, p.build_dir, p.src_dir / "sva", p.tb_dir]

    async def _write_tb(self, ctx: StepContext, snap: dict, old: dict | None, feedback: list[str]) -> SmokePlan:
        engine, project = ctx.engine, ctx.project
        f = self._files(engine)
        ts, work = self.tools(engine), self.workdir(engine)
        skills = skillmod.discover(project.config_dir, engine.flow.path, ["smoke-testbench", *(["apb-csr-tool"] if csr.read_provided(self.rtl_dir(engine)) else [])])
        f["tb"].parent.mkdir(parents=True, exist_ok=True)
        stage = Stage(name="smoke_tb", system_prompt=WRITER, prompt=self._writer_prompt(engine, snap, f["tb"], old, feedback, skillmod.index(skills, project.root)),
                      cwd=project.root, builtin_tools=["Read", "Grep", "Glob", "Write", "Edit"],
                      sdk_tools=smoke_tools(ts, project.root, work, lambda: self.design_files(engine), f["tb"]), add_dirs=skillmod.add_dirs(skills),
                      output_model=SmokePlan, deny_dirs=self._deny(engine), write_dirs=[f["tb"].parent], write_files=[f["tb"]])
        plan: SmokePlan = (await ctx.llm(stage)).output
        if not f["tb"].is_file():
            raise StepFailed(f"the writer did not write {project.rel(f['tb'])}")
        f["plan"].write_text("# Smoke test plan\n\n" + (plan.summary + "\n\n" if plan.summary else "")
                             + "| id | behaviour | REQ |\n|---|---|---|\n" + "".join(f"| {b.id} | {b.description} | {', '.join(b.req_ids)} |\n" for b in plan.behaviours))
        write_json(f["plan_json"], plan.model_dump(exclude={"questions"}))
        return plan

    # -- run ------------------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        engine, project, cfg = ctx.engine, ctx.project, ctx.cfg
        f = self._files(engine)
        meta = engine.state.step_meta.setdefault(self.name, {})
        # the fix rounds are counted until the smoke test passes (or you regenerate it, or answer its questions: new information), not per
        # run: a limit that every `q3tui run` starts over can never be reached, and the loop would go on for ever
        answered = len(load_answers(self._q_paths(engine)[1]))
        if ctx.regenerate or "rounds" not in meta or meta.get("answered") != answered:
            meta["rounds"] = 0
        meta["answered"] = answered
        meta["loop"] = False
        snap = self._snapshot(engine)
        if not snap["reqs"] and not project.spec_documents():
            raise StepFailed("no requirements and no spec to write the smoke test from")
        answers = load_answers(self._q_paths(engine)[1])
        questions: list[Question] = [q for q in self.questions_list(engine) if q.id in answers]
        wrote = False
        if ctx.regenerate or not f["tb"].is_file() or meta.get("snapshot") != snap or ctx.feedback:
            plan = await self._write_tb(ctx, snap, None if ctx.regenerate else meta.get("snapshot"), list(ctx.feedback))
            meta["snapshot"], wrote = snap, True
            questions += assign_question_ids(plan.questions, questions, "SM", taken=set(answers))
        self.save_questions(engine, questions)
        ts, work = self.tools(engine), self.workdir(engine)
        rtl = self.design_files(engine)
        res = await anyio.to_thread.run_sync(lambda: smoke.compile_tb(ts, project.root, work, rtl, f["tb"]))
        errors = smoke.compile_errors(res)
        out = smoke.Outcome("fail", {}, "does not compile") if errors else await anyio.to_thread.run_sync(lambda: smoke.run_tb(ts, project.root, work))
        report = {"tool": {"sim": ts.roles["sim"].cmd.split()[0], "run": ts.roles["run"].cmd.split()[0]}, "rtl_files": [p.name for p in rtl],
                  "compile": {"ok": not errors, "errors": errors[:20]}, "run": out.to_dict(), "status": out.status if not errors else "compile_error",
                  "rounds": meta["rounds"], "triage": []}
        ctx.unchanged = not wrote and out.status == "pass"
        if out.status == "pass" and not errors:
            meta["rounds"] = 0
            write_json(f["report"], report)
            ctx.emit("log", message=f"smoke: PASS ({len(out.results)} behaviour(s))")
            return
        ctx.emit("warning", message="smoke: " + (f"compile errors: {errors[0]}" if errors else smoke.show_outcome(out).splitlines()[0]))
        await self._triage(ctx, snap, report, errors, out)

    # -- failure → whose fault ------------------------------------------------------------------------------------

    async def _triage(self, ctx: StepContext, snap: dict, report: dict, errors: list[str], out: smoke.Outcome) -> None:
        engine, project, cfg = ctx.engine, ctx.project, ctx.cfg
        f = self._files(engine)
        meta = engine.state.step_meta.setdefault(self.name, {})
        rounds, limit = int(meta["rounds"]), self.max_rounds(cfg)
        summary = ("compile errors:\n" + "\n".join(errors[:20])) if errors else smoke.show_outcome(out)
        if rounds >= limit:
            write_json(f["report"], report)
            raise StepFailed(f"smoke: still failing after {rounds} fix round(s) (options.max_rounds / pipeline.max_fix_iterations):\n{summary}")
        skills = skillmod.discover(project.config_dir, engine.flow.path, ["smoke-triage"])
        block, _ = spec_block(project)
        rtl = self.design_files(engine)
        prompt = "\n".join(["# Task: triage the failing smoke test", "", "## Specification", block, "", "## Requirements",
                            "\n".join(f"- {k}: {v}" for k, v in snap["reqs"].items()), "", "## Smoke plan", f["plan"].read_text() if f["plan"].is_file() else "(none)", "",
                            f"## Files (read them): testbench {project.rel(f['tb'])}; RTL " + ", ".join(project.rel(p) for p in rtl),
                            "", "## Already handed back in earlier rounds (do not ask for the same again: say if it did not work, and why)",
                            "\n".join(f"- {h['key']}" for h in meta.get("history", [])[-10:]) or "(nothing yet)",
                            "", "## Result of the run (ground truth)", summary, f"Log: {project.rel(Path(out.log)) if out.log else '(compile log in the work folder)'}",
                            "", skillmod.index(skills, project.root)])
        stage = Stage(name="smoke_triage", system_prompt=TRIAGE, prompt=prompt, cwd=project.root, builtin_tools=["Read", "Grep", "Glob"],
                      add_dirs=skillmod.add_dirs(skills), output_model=SmokeTriage, deny_dirs=[project.model_dir, project.src_dir / "tb", project.src_dir / "sva"])
        verdicts: list[Verdict] = (await ctx.llm(stage)).output.verdicts
        modules = {p.stem for p in rtl}
        asks: list[Question] = []
        dispatched = 0
        by_module: dict[str, list[Verdict]] = {}  # (one change request per module: behaviours with one cause arrive together)
        history: list[dict] = meta.setdefault("history", [])
        seen: dict[str, int] = {}
        for h in history:
            seen[h["key"]] = seen.get(h["key"], 0) + 1
        for v in verdicts:
            report["triage"].append(v.model_dump())
            unit = v.module if v.module in modules else next(iter(sorted(modules)), "")
            key = f"{v.verdict}|{unit}|{v.id}"
            if v.verdict in ("rtl_bug", "tb_bug") and v.request.strip() and seen.get(key, 0) >= cfg.pipeline.escalate_after_repeats:
                # the same failure keeps coming back after the fix was handed over: not a thing another round settles (a spec that disagrees with a rule,
                # a name the generator will not use): ask, as `verify` does
                asks.append(Question(id="", blocking=True, kind="design_choice", question=(
                    f"Smoke test {v.id} keeps failing ({v.verdict.replace('_', ' ')}{' in ' + unit if unit else ''}, {seen[key] + 1} times): {v.summary} How should it be settled?"),
                    default_assumption="Keep the spec as the reference and change the implementation once more, starting from the evidence."))
            elif v.verdict == "rtl_bug" and v.request.strip():
                by_module.setdefault(unit, []).append(v)
                history.append({"key": key, "round": rounds + 1})
            elif v.verdict == "tb_bug" and v.request.strip():
                history.append({"key": key, "round": rounds + 1})
                engine.request_change(self.name, f"From the smoke triage ({v.id}): {v.summary} {v.request}")
                ctx.emit("log", message=f"smoke {v.id}: testbench bug → smoke: {v.summary[:100]}")
                dispatched += 1
            else:
                asks.append(Question(id="", question=f"Smoke test {v.id}: {v.summary}", blocking=True, kind="spec_gap" if v.verdict == "spec_issue" else "design_choice",
                                     default_assumption=v.request or "Keep the spec as written and treat the failing side as the bug."))
        for unit, vs in by_module.items():
            engine.request_change("rtl", scoped("rtl", unit, f"From the smoke test ({', '.join(v.id for v in vs)}): " + " | ".join(f"{v.summary} {v.request}" for v in vs)))
            ctx.emit("log", message=f"smoke {', '.join(v.id for v in vs)}: RTL bug → rtl ({unit}): {vs[0].summary[:100]}")
            dispatched += 1
        answers = load_answers(self._q_paths(engine)[1])
        kept = [q for q in self.questions_list(engine) if q.id in answers]
        self.save_questions(engine, [*kept, *assign_question_ids(asks, kept, "SM", taken=set(answers))])
        write_json(f["report"], report)
        if dispatched:
            meta["loop"], meta["rounds"] = True, rounds + 1
        elif not asks:
            raise StepFailed(f"smoke: the triage gave no verdict to act on:\n{summary}")
        else:  # nothing to hand over, only questions: the test is failing and stays failing until you answer — never "done"
            ids = ", ".join(q["id"] for q in engine.questions() if q["step"] == self.name and q["status"] == "open")
            raise StepFailed(f"smoke: the test is failing and needs your decision on {ids or 'the open questions'} (answer them, then run again):\n{summary}")
