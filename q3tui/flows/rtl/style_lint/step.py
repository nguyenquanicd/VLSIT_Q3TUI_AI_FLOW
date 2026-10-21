"""Step `style_lint` of the RTL flow: Verible lint, judged by an LLM, fixes handed back to their owners.

Code: runs Verible through the role `verible_lint` (the findings are ground truth), carries earlier waivers over (update, not re-run),
dispatches what the judge decided as change requests — a violation to `rtl` (`[rtl:<module>] …`: that module only), a flag problem to `rules` —
and sets the engine's fix loop, bounded by `options.max_rounds`. LLM (skill `lint-triage`): per finding violation / flag problem / acceptable.
No findings: no LLM at all. The RTL belongs to `rtl`, the flags to `rules`: this step edits neither."""

from __future__ import annotations

from pathlib import Path

import anyio

from q3tui.core.project import sha256_json, write_json
from q3tui.flows import skill_sections
from q3tui.flows.rtl.lib import skills as skillmod
from q3tui.flows.rtl.lib import verible
from q3tui.flows.rtl.lib.base import RtlFlowStep
from q3tui.flows.rtl.lib.models import Fix, FlagProblem, StyleVerdict
from q3tui.flows.rtl.lib.tools import verible_tools
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import Question, assign_question_ids, load_answers, scoped

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["SYSTEM"]
SKILLS = ["lint-triage"]


def _waiver_key(module: str, rule: str, text: str) -> str:
    return f"{module}|{rule}|{text.strip()}"


class Step(RtlFlowStep):
    name = "style_lint"
    title = "Style lint (Verible)"
    deps = ("format",)
    tool_roles = ("verible_lint",)

    def _report(self, engine) -> Path:
        return self.style_dir(engine) / "style_report.json"

    def _flags_file(self, engine) -> Path:
        return self.style_dir(engine) / verible.LINT_FLAGS

    def inputs(self, engine) -> list[Path]:
        f = self._flags_file(engine)
        return [*self.rtl_files(engine), *([f] if f.is_file() else []), *self.rule_paths(engine), self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        r = self._report(engine)
        return [r] if r.is_file() else []

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        if not self.rtl_files(engine):
            return f"the RTL is needed first ({engine.project.rel(self.rtl_dir(engine))}/: step rtl)"
        if not self._flags_file(engine).is_file():
            return "the Verible flags are needed first (step rules: src/rtl/style_lint/verible.flags)"
        return self.tool_problem(engine, "verible_lint")

    def max_rounds(self, cfg) -> int:
        return int(self.options.get("max_rounds") or cfg.pipeline.max_fix_iterations)

    async def run(self, ctx: StepContext) -> None:
        engine, project, cfg = ctx.engine, ctx.project, ctx.cfg
        ts = self.tools(engine)
        work = self.workdir(engine)
        files = self.rtl_files(engine)
        flags = verible.read_flags(self._flags_file(engine))
        meta = engine.state.step_meta.setdefault(self.name, {})
        run_id = getattr(engine, "_run_id", None)
        if meta.get("run") != run_id or "rounds" not in meta:  # the round bound is per run
            meta["run"], meta["rounds"] = run_id, 0
        meta["loop"] = False
        res = await anyio.to_thread.run_sync(lambda: verible.run_lint(ts, work, files, flags))
        verible.check(res, "verible_lint")
        found = verible.findings(res.diagnostics, project.root)
        syntax = verible.syntax_errors(res.diagnostics)
        waived = set(meta.get("waived") or [])
        todo = [f for f in found if _waiver_key(f.module, f.rule, f.text) not in waived]
        report = {"rtl_files": [p.name for p in files], "flags": flags, "findings": len(found), "waived_findings": len(found) - len(todo),
                  "syntax_errors": syntax, "rounds": meta["rounds"], "status": "clean"}
        if not todo and not syntax:
            write_json(self._report(engine), {**report, "verdict": {"summary": "no findings"}})
            self.save_questions(engine, [q for q in self.questions_list(engine) if q.id in load_answers(self._q_paths(engine)[1])])
            ctx.emit("log", message=f"style_lint: clean ({len(found)} finding(s), all accepted earlier)" if found else "style_lint: clean (no findings)")
            ctx.unchanged = True
            return
        rounds = int(meta["rounds"])
        limit = self.max_rounds(cfg)
        if syntax:  # not a style matter: the generator fixes the file
            await self._dispatch(ctx, rounds, limit, [Fix(module=Path(e.split(":")[0]).stem, request=f"fix the syntax error: {e}") for e in syntax], [])
            write_json(self._report(engine), {**report, "status": "syntax errors handed back"})
            return
        skills = skillmod.discover(project.config_dir, engine.flow.path, SKILLS)
        stage = Stage(name="style_triage", system_prompt=SYSTEM, prompt=self._prompt(ctx, todo, flags, skillmod.index(skills, project.root)),
                      cwd=project.root, builtin_tools=["Read", "Grep", "Glob"],
                      sdk_tools=verible_tools(ts, work, project.root, lambda: self.rtl_files(engine), self._flags_file(engine)),
                      add_dirs=skillmod.add_dirs(skills), output_model=StyleVerdict,
                      deny_dirs=[project.tb_dir, project.model_dir, project.build_dir, project.src_dir / "tb", project.src_dir / "sva"])
        verdict: StyleVerdict = (await ctx.llm(stage)).output
        modules = {f.stem for f in files}
        fixes = [x for x in verdict.fixes if x.module in modules and x.request.strip()]
        unknown = [x.module for x in verdict.fixes if x.module not in modules]
        if unknown:
            ctx.emit("warning", message=f"style_lint: the judge named unknown module(s) {', '.join(sorted(set(unknown)))}: ignored")
        for w in verdict.waived:  # accepted: remembered, so a later run does not judge them again
            for f in todo:
                if f.module == w.module and f.rule == w.rule and (not w.line_text or f.text.strip() == w.line_text.strip()):
                    waived.add(_waiver_key(f.module, f.rule, f.text))
        meta["waived"] = sorted(waived)
        left = [f for f in todo if _waiver_key(f.module, f.rule, f.text) not in waived]
        covered = {x.module for x in fixes} | {p.rule for p in verdict.flag_problems}
        unresolved = [f for f in left if f.module not in covered and f.rule not in covered]
        old = self.questions_list(engine)
        kept = [q for q in old if q.id in load_answers(self._q_paths(engine)[1])]
        self.save_questions(engine, [*kept, *assign_question_ids(verdict.questions, kept, "SL", taken=set(load_answers(self._q_paths(engine)[1])))])
        await self._dispatch(ctx, rounds, limit, fixes, verdict.flag_problems)
        status = "fixes handed back" if (fixes or verdict.flag_problems) else ("accepted" if not left else "unresolved")
        write_json(self._report(engine), {**report, "rounds": meta["rounds"], "status": status,
                                          "verdict": verdict.model_dump(exclude={"questions"}), "unresolved": [f.to_dict() for f in unresolved]})
        if unresolved and not (fixes or verdict.flag_problems):
            raise StepFailed(f"style_lint: {len(unresolved)} finding(s) neither fixed, waived nor handed back by the judge: "
                             + "; ".join(f"{f.module}:{f.line} [{f.rule}]" for f in unresolved[:6]) + f" (see {engine.project.rel(self._report(engine))})")

    async def _dispatch(self, ctx: StepContext, rounds: int, limit: int, fixes: list[Fix], flag_problems: list[FlagProblem]) -> None:
        engine = ctx.engine
        meta = engine.state.step_meta.setdefault(self.name, {})
        if not (fixes or flag_problems):
            return
        if rounds >= limit:
            raise StepFailed(f"style_lint: findings remain after {rounds} fix round(s) (options.max_rounds / pipeline.max_fix_iterations): "
                             + "; ".join(f"{x.module}: {x.request[:80]}" for x in fixes[:4]) + (f"; flags: {flag_problems[0].problem[:80]}" if flag_problems else ""))
        for x in fixes:
            engine.request_change("rtl", scoped("rtl", x.module, f"From style_lint (Verible): {x.request}"))
            ctx.emit("log", message=f"style fix → rtl ({x.module}): {x.request[:120]}")
        for p in flag_problems:
            engine.request_change("rules", f"From style_lint: the flag for `{p.rule}` is wrong — {p.problem}" + (f" Suggestion: {p.suggestion}" if p.suggestion else ""))
            ctx.emit("log", message=f"flag problem → rules ({p.rule}): {p.problem[:120]}")
        meta["loop"], meta["rounds"] = True, rounds + 1

    def _prompt(self, ctx: StepContext, todo: list, flags: list[str], index: str) -> str:
        engine = ctx.engine
        parts = ["# Task: judge these Verible findings", "", "## The rules (the first file wins where two conflict)", self.rule_text(engine), "",
                 "## The flags Verible ran with", "\n".join(flags) or "(none)", "",
                 f"## Findings (Verible, ground truth; RTL in {engine.project.rel(self.rtl_dir(engine))}/)", verible.show_findings(todo, 200)]
        if ctx.feedback:
            parts += ["", "## Notes from the user", *[f"- {f}" for f in ctx.feedback]]
        return "\n".join([*parts, "", index])
