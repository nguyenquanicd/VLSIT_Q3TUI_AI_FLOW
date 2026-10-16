"""VLSIT step `rtl` (phase 3a, /rtl_generator): structured spec + final config → src/rtl/<module>.sv, filelist.f,
synth_report.json.

Deterministic first: the module plan, the order (packages, leaves, the top), the rule checks (rules_check.py), the
`// REQ-xxx` trace tags, lint (tool role `lint`, 0 warnings by default) and synthesis (role `synth`, Design Compiler / Yosys script written
by code) are code. The LLM writes one module per stage (only its own file), with the checks as ground truth; what
still fails goes to a fresh small session with the problems and the current file. Changes propagate as updates: a
module is rewritten only when what it was built from changed (per-module snapshot in `step_meta`), or when a scoped
change request `[rtl:<module>] …` names it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.core.project import read_json, sha256_json, write_json
from q3tui.steps.common import (Question, assign_question_ids, drop_settled, load_answers, render_questions, settled_block,
                                    split_feedback)
from q3tui.steps.vlsit import artifacts, rules_check, synth
from q3tui.flows.vlsit.rtl import task as rtl_prompts
from q3tui.steps.vlsit.base import VlsitStep, rules_text
from q3tui.flows.vlsit.rtl.plan import PlanModule, RtlPlan, compile_order, dependencies, levels, plan_from_spec


class VModuleResult(BaseModel):
    summary: str = Field("", description="How the module implements its function (state, FSM, pipelining), briefly")
    requirements: list[str] = Field(default_factory=list, description="REQ ids this module's logic implements")
    questions: list[Question] = Field(default_factory=list, description=(
        "Questions found while implementing (usually none), each marked with where its answer belongs: spec_gap, "
        "req_gap (the requirement is wrong or missing), or design_choice"))


def describe_changes(old: dict, new: dict) -> list[str]:
    """What a module was built from, before and after (the update prompt names only this)."""
    out = []
    if old.get("description") != new.get("description"):
        out.append(f"the module's description is now: {new.get('description')}")
    a, b = old.get("reqs", {}), new.get("reqs", {})
    for rid in sorted(set(b) - set(a)):
        out.append(f"new requirement {rid}: {b[rid]}")
    for rid in sorted(set(a) - set(b)):
        out.append(f"{rid} no longer applies to this module: remove its logic and tags")
    for rid in sorted(set(a) & set(b)):
        if a[rid] != b[rid]:
            out.append(f"{rid} changed: {b[rid]} (was: {a[rid]})")
    pa, pb = old.get("params", {}), new.get("params", {})
    for k in sorted(set(pa) | set(pb)):
        if pa.get(k) != pb.get(k):
            out.append(f"parameter {k} = {pb.get(k)} (was {pa.get(k)})")
    for c in sorted(set(old.get("children", {})) | set(new.get("children", {}))):
        if old.get("children", {}).get(c) != new.get("children", {}).get(c):
            out.append(f"the file of child/package `{c}` changed: read it again (ports, types)")
    if old.get("answers") != new.get("answers"):
        out.append("your answers to its questions changed (see below)")
    if old.get("rules") != new.get("rules"):
        out.append("the RTL rules changed: re-check the file against them")
    return out


@dataclass
class _Run:
    ctx: StepContext
    plan: RtlPlan
    prof: rules_check.Profile
    rules: str
    rules_key: str
    tools: object
    reqs: dict[str, str]
    params: dict[str, str]
    spec_files: list[str]
    lay: object
    answers: dict[str, str]
    owner: dict[str, str]
    settled: str
    feedback_general: list[str]
    feedback_unit: dict[str, list[str]]
    meta: dict
    results: dict[str, VModuleResult] = field(default_factory=dict)
    lint_state: dict[str, dict] = field(default_factory=dict)  # module -> {status, warnings}
    wrote: set[str] = field(default_factory=set)

    def by_name(self) -> dict[str, PlanModule]:
        return {m.name: m for m in self.plan.modules}

    def file(self, module: str) -> Path:
        return self.lay.rtl / f"{module}.sv"


class RtlGenStep(VlsitStep):
    name = "rtl"
    title = "RTL (rules, lint, synthesis)"
    deps = ("config",)
    artifact = "synth_report.json"
    tool_roles = ("lint", "synth")
    checks_version = 1

    # -- files -------------------------------------------------------------------------------------

    def rule_files(self) -> list[str]:
        return list(self.options.get("rules") or [])

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "final_config.json"),
                self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        files = []
        if lay.rtl.is_dir():
            files += sorted(lay.rtl.glob("*.sv")) + sorted(lay.rtl.glob("*.ys"))
            files += [f for f in (lay.rtl / "filelist.f",) if f.is_file()]
        files += [f for f in (lay.artifact("synth_report.json"), lay.artifact("rtl_plan.json"), self._q_paths(engine)[2]) if f.is_file()]
        return files

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        p = engine.project
        if not artifacts.path(p, "structured_spec.json").is_file():
            return "the parsed spec is needed first (step parse: schemas/structured_spec.json)"
        if not artifacts.path(p, "final_config.json").is_file():
            return "the configuration is needed first (step config: schemas/final_config.json)"
        from q3tui.eda.base import ToolUnavailable
        from q3tui.eda.tools import available, binary_of, load_tools

        try:
            tools = load_tools(p.cfg, p.root)
        except ToolUnavailable as exc:
            return str(exc)
        if "lint" not in tools.roles:
            return "no tool for role 'lint': add roles.lint (command, e.g. vcs +lint=all …) to tools.json or tools.roles — `q3tui tools init` writes a tools.json to start from"
        if not available(tools, "lint"):
            return f"the lint tool '{binary_of(tools.roles['lint'])}' (role 'lint') was not found (check tools.json, modules / setup_script)"
        return None

    # -- questions: owner module per question -------------------------------------------------------

    def _owner(self, engine) -> dict[str, str]:
        return {q["id"]: q.get("module") or "" for q in read_json(self._q_paths(engine)[0], default=[]) or []}

    # -- plan ---------------------------------------------------------------------------------------

    async def _plan(self, ctx: StepContext, spec: dict, reqs: dict[str, str], spec_files: list[str]) -> RtlPlan:
        plan = plan_from_spec(spec)
        if plan is not None:
            return plan
        lay = self.lay(ctx.engine)
        key = sha256_json(reqs)
        saved = artifacts.read(ctx.project, "rtl_plan.json")
        if saved and saved.get("inputs") == key and not ctx.regenerate:
            return RtlPlan.model_validate(saved["plan"])
        ctx.emit("log", message="the spec names no RTL modules: planning them")
        res = await ctx.llm(Stage(
            name="rtl_plan", system_prompt=rtl_prompts.PLAN_SYSTEM, prompt=rtl_prompts.plan_prompt(
                list(reqs.items()), spec_files, (spec.get("metadata") or {}).get("top_module")),
            cwd=ctx.project.root, builtin_tools=["Read", "Grep", "Glob"], output_model=RtlPlan,
            deny_dirs=[ctx.project.model_dir, ctx.project.tb_dir, lay.tb, lay.sva], max_turns=ctx.cfg.rtl.max_turns))
        plan: RtlPlan = res.output
        write_json(artifacts.path(ctx.project, "rtl_plan.json"), {"inputs": key, "plan": plan.model_dump()})
        return plan

    # -- checks (code) --------------------------------------------------------------------------------

    def _lint_filelist(self, run: _Run, module: str) -> Path:
        deps = [d for d in dependencies(run.plan, module) if run.file(d).is_file()]
        files = [run.file(d) for d in deps] + [run.file(module)]
        work = run.lay.sim / "rtl"
        work.mkdir(parents=True, exist_ok=True)
        f = work / f"lint_{module}.f"
        f.write_text("\n".join(str(x.resolve()) for x in files) + "\n")
        return f

    def _lint(self, run: _Run, module: str, top: str | None = None) -> tuple[list[str], int, bool]:
        """(problems of this module, its warnings, lint ran)"""
        from q3tui.eda.tools import run_role

        pm = run.by_name().get(module)
        if pm is not None and pm.is_package:
            return [], 0, False  # (a package is linted with the modules that import it)
        fl = self._lint_filelist(run, module)
        res = run_role(run.tools, "lint", run.lay.sim / "rtl", {"filelist": fl, "top": top or module,
                                                                "files": [l for l in fl.read_text().split()],
                                                                "workdir": run.lay.sim / "rtl", "project": run.ctx.project.root},
                       log_name=f"lint_{module}.log")
        from q3tui.eda.tools import check_broken

        check_broken(res, "lint")  # a tool that fails without an error of its own is not a reason to rewrite the module
        mine = [d for d in res.diagnostics if d.file is None or Path(d.file).name == f"{module}.sv"]
        errors = [d for d in mine if d.severity == "error"]
        warns = [d for d in mine if d.severity == "warning"]
        limit = int(self.options.get("lint_max_warnings", 0))
        problems = [f"lint error {d.code}{f' line {d.line}' if d.line else ''}: {d.message}" for d in errors]
        if len(warns) > limit:
            problems += [f"lint warning {d.code}{f' line {d.line}' if d.line else ''}: {d.message}" for d in warns]
        if not res.ok and not errors and not problems:  # the tool failed without a diagnostic of this file
            others = [d for d in res.diagnostics if d.severity == "error"]
            if others or res.returncode not in (0,):
                problems.append(f"lint tool failed (rc={res.returncode}): " + "; ".join(f"{d.file}: {d.message}" for d in others[:3]))
        return problems, len(warns), True

    def _check_module(self, run: _Run, pm: PlanModule) -> list[str]:
        path = run.file(pm.name)
        if not path.is_file() or not path.read_text().strip():
            return [f"{path.name} was not written"]
        text = path.read_text()
        problems = [f"rule {v}" for v in rules_check.check_source(text, pm.name, run.prof, pm.is_package)]
        missing = sorted(set(pm.req_ids) - rules_check.req_tags(text))
        if missing:
            problems.append(f"REQ trace tags missing: {', '.join(missing)} (tag each with a `// REQ-xxx` comment where it is implemented)")
        lint_problems, warnings, ran = self._lint(run, pm.name)
        problems += lint_problems
        run.lint_state[pm.name] = {"status": "not run" if not ran else ("fail" if lint_problems else "pass"), "warnings": warnings}
        return problems

    # -- LLM ------------------------------------------------------------------------------------------

    def _stage(self, run: _Run, pm: PlanModule, prompt: str) -> Stage:
        ctx, project, lay = run.ctx, run.ctx.project, run.lay
        path = run.file(pm.name)

        def tools():
            import anyio
            from claude_agent_sdk import tool

            @tool("check", "Run Q3TUI's checks on the module of this task (RTL rules, REQ trace tags, lint). "
                           "Returns the problems, or 'clean'.", {})
            async def check_tool(args: dict) -> dict:
                problems = await anyio.to_thread.run_sync(lambda: self._check_module(run, pm))
                text = "clean" if not problems else "\n".join(f"- {p}" for p in problems)
                return {"content": [{"type": "text", "text": text}]}

            return [check_tool]

        return Stage(name=f"rtl_{pm.name}", system_prompt=rtl_prompts.system(run.rules), prompt=prompt, cwd=project.root,
                     builtin_tools=["Read", "Grep", "Glob", "Write", "Edit"], sdk_tools=tools(), output_model=VModuleResult,
                     deny_dirs=[project.model_dir, project.tb_dir, lay.tb, lay.sva],  # independence: never the testbench / SVA
                     write_dirs=[lay.rtl], write_files=[path], max_turns=ctx.cfg.rtl.max_turns)

    def _snapshot(self, run: _Run, pm: PlanModule) -> dict:
        kids = {}
        for c in dependencies(run.plan, pm.name):
            if c in pm.instances or c.endswith("_pkg"):
                f = run.file(c)
                kids[c] = sha256_json(f.read_text()) if f.is_file() else ""
        return {"description": pm.description, "reqs": {r: run.reqs.get(r, "") for r in pm.req_ids}, "params": run.params,
                "children": kids, "answers": {k: v for k, v in run.answers.items() if run.owner.get(k) == pm.name},
                "rules": run.rules_key}

    async def _implement(self, run: _Run, pm: PlanModule, snap: dict, old: dict | None, problems: str) -> None:
        ctx, project = run.ctx, run.ctx.project
        path = run.file(pm.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        feedback = [*run.feedback_general, *run.feedback_unit.get(pm.name, [])]
        settled = run.settled
        if old is not None and path.is_file() and not ctx.regenerate:
            prompt = rtl_prompts.update_prompt(module=pm.name, path=project.rel(path), changes=describe_changes(old, snap),
                                               answers=snap["answers"], feedback=feedback, settled=settled, problems=problems)
        else:
            prompt = rtl_prompts.module_prompt(
                module=pm.name, path=project.rel(path), description=pm.description,
                reqs=[(r, run.reqs.get(r, "")) for r in pm.req_ids], params=run.params,
                children=[(c, project.rel(run.file(c))) for c in pm.instances if c in run.by_name()],
                packages=[project.rel(run.file(m.name)) for m in run.plan.modules if m.is_package and m.name != pm.name],
                spec_files=run.spec_files, answers=snap["answers"], settled=settled, feedback=feedback, problems=problems,
                existing=path.is_file() and bool(path.read_text().strip()), top=pm.name == run.plan.top)
        ctx.emit("log", message=f"implementing {pm.name} …")
        res = await ctx.llm(self._stage(run, pm, prompt))
        run.results[pm.name] = res.output
        run.wrote.add(pm.name)

    async def _fix(self, run: _Run, pm: PlanModule, problems: list[str]) -> None:
        """A fresh small session: the problems and the current file (never the long session of the first write)."""
        ctx, project = run.ctx, run.ctx.project
        path = run.file(pm.name)
        res = await ctx.llm(self._stage(run, pm, rtl_prompts.fix_prompt(
            module=pm.name, path=project.rel(path), problems=rtl_prompts.diagnostics_text(problems),
            code=path.read_text() if path.is_file() else "", reqs=pm.req_ids)))
        if res.output is not None:
            prev = run.results.get(pm.name)
            run.results[pm.name] = res.output if prev is None else res.output.model_copy(
                update={"requirements": res.output.requirements or prev.requirements,
                        "questions": [*prev.questions, *res.output.questions]})
        run.wrote.add(pm.name)

    async def _work(self, run: _Run, pm: PlanModule, todo: bool) -> list[str]:
        """Write (when `todo`) and check one module, with fix rounds; returns what is still wrong."""
        import anyio

        ctx = run.ctx
        snap = self._snapshot(run, pm)
        mm = run.meta["modules"].get(pm.name) or {}
        if not todo and not (mm.get("hash") == sha256_json(snap)):
            todo = True
        problems: list[str] = []
        if not todo:
            problems = await anyio.to_thread.run_sync(lambda: self._check_module(run, pm))
            if not problems:
                ctx.emit("log", message=f"{pm.name}: up to date")
                return []
            ctx.emit("warning", message=f"{pm.name}: {len(problems)} problem(s) in the existing file")
        else:
            await self._implement(run, pm, snap, mm.get("built_from") if mm else None, "")
        for attempt in range(ctx.cfg.rtl.max_fix_attempts + 1):
            problems = await anyio.to_thread.run_sync(lambda: self._check_module(run, pm))
            if not problems:
                ctx.emit("log", message=f"{pm.name}: clean")
                break
            if attempt == ctx.cfg.rtl.max_fix_attempts:
                ctx.emit("warning", message=f"{pm.name}: still {len(problems)} problem(s) after {attempt} fix round(s)")
                break
            ctx.emit("warning", message=f"{pm.name}: {len(problems)} problem(s); fixing …")
            await self._fix(run, pm, problems)
        res = run.results.get(pm.name)
        prev = mm or {}
        run.meta["modules"][pm.name] = {
            "hash": sha256_json(snap), "built_from": snap,
            "summary": res.summary if res else prev.get("summary", ""),
            "requirements": res.requirements if res else prev.get("requirements", [])}
        return problems

    # -- run --------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        import anyio

        from q3tui.eda.base import ToolUnavailable
        from q3tui.eda.tools import load_tools

        engine, project = ctx.engine, ctx.project
        lay = self.lay(engine)
        spec = artifacts.read(project, "structured_spec.json")
        config = artifacts.read(project, "final_config.json")
        if not spec or not config:
            raise StepFailed("schemas/structured_spec.json and schemas/final_config.json are needed (steps parse and config)")
        try:
            tools = load_tools(ctx.cfg, project.root)
        except ToolUnavailable as exc:
            raise StepFailed(str(exc)) from exc
        rule_files = self.rule_files()
        rules = rules_text(rule_files, project.root)
        prof = rules_check.profile_for(rule_files, self.options.get("naming"))
        reqs = {r["req_id"]: r.get("text", "") for r in spec.get("requirements", []) if r.get("req_id")}
        params = {k: str((v or {}).get("value")) for k, v in (config.get("parameters") or {}).items()}
        spec_files = [project.rel(p) for p in project.spec_documents()]
        plan = await self._plan(ctx, spec, reqs, spec_files)
        unknown = sorted({r for m in plan.modules for r in m.req_ids} - set(reqs))
        if unknown:
            ctx.emit("warning", message=f"the plan assigns requirement id(s) the spec does not have: {', '.join(unknown)}")
        try:
            lv = levels(plan)
        except ValueError as exc:
            raise StepFailed(str(exc)) from exc
        q_json, answers_path, q_md = self._q_paths(engine)
        by_unit, general = split_feedback(ctx.feedback, self.name)
        meta = engine.state.step_meta.setdefault(self.name, {})
        meta.setdefault("modules", {})
        lay.rtl.mkdir(parents=True, exist_ok=True)
        run = _Run(ctx, plan, prof, rules, sha256_json([rules, prof.__dict__]), tools, reqs, params, spec_files, lay,
                   load_answers(answers_path), self._owner(engine), settled_block(engine.settled_questions(self.name)), general,
                   by_unit, meta)
        names = {m.name for m in plan.modules}
        for gone in sorted(set(meta["modules"]) - names):  # modules the plan no longer has: their files go (backed up)
            meta["modules"].pop(gone)
            f = lay.rtl / f"{gone}.sv"
            if f.is_file():
                project.backup([f])
                f.unlink()
                ctx.emit("log", message=f"{gone}: no longer in the plan — file removed (backed up)")
        for unit in set(by_unit) - names:
            ctx.emit("warning", message=f"change request for `{unit}`: no such module in the plan (modules: {', '.join(sorted(names))})")
        limiter = anyio.CapacityLimiter(max(1, ctx.cfg.rtl.parallel))
        leftover: dict[str, list[str]] = {}

        async def one(pm: PlanModule) -> None:
            async with limiter:
                todo = ctx.regenerate or bool(general) or pm.name in by_unit
                bad = await self._work(run, pm, todo)
                if bad:
                    leftover[pm.name] = bad

        for level in lv:  # children before parents: a parent reads its children's files
            async with anyio.create_task_group() as tg:
                for pm in level:
                    tg.start_soon(one, pm)

        # the whole design: filelist, lint at the top, cross-module problems go back to the module they are in
        order = compile_order(plan)
        fl = lay.rtl / "filelist.f"
        fl.write_text("\n".join(project.rel(lay.rtl / f"{n}.sv") for n in order if (lay.rtl / f"{n}.sv").is_file()) + "\n")
        by_name = run.by_name()
        for _ in range(max(1, ctx.cfg.pipeline.max_fix_iterations)):
            per = await anyio.to_thread.run_sync(lambda: self._full_lint(run))
            per = {m: p for m, p in per.items() if m in by_name}
            if not per:
                break
            ctx.emit("warning", message=f"lint of the whole design: problems in {', '.join(per)}; fixing …")
            async with anyio.create_task_group() as tg:
                for m, probs in per.items():
                    async def fix(m=m, probs=probs):
                        async with limiter:
                            await self._fix(run, by_name[m], probs)
                            leftover.pop(m, None)
                            after = await anyio.to_thread.run_sync(lambda: self._check_module(run, by_name[m]))
                            if after:
                                leftover[m] = after
                    tg.start_soon(fix)
        else:
            per = await anyio.to_thread.run_sync(lambda: self._full_lint(run))
            for m, probs in per.items():
                if m in by_name:
                    leftover.setdefault(m, []).extend(probs)

        # synthesis
        report = await self._synthesize(run, order, leftover, limiter)
        self._write_report(run, order, report)
        self._write_questions(ctx, run, q_json, answers_path, q_md)
        bad = {m: p for m, p in leftover.items() if p}
        if bad:
            text = "\n".join(f"- {m}: " + "; ".join(p[:3]) + (f" (+{len(p) - 3} more)" if len(p) > 3 else "") for m, p in bad.items())
            raise StepFailed("RTL checks still fail after the fix rounds:\n" + text)
        ctx.unchanged = not run.wrote and report.get("synthesis") != "fail"
        tags = report["req_ids_tagged"]
        ctx.emit("log", message=f"RTL: {len(order)} module(s), lint {report['lint']['status']}, synthesis {report['synthesis']}, "
                                f"{tags} REQ tag(s)")

    def _full_lint(self, run: _Run) -> dict[str, list[str]]:
        """Lint of the whole design at its top: diagnostics grouped by the module whose file they are in."""
        from q3tui.eda.tools import run_role

        top, lay = run.plan.top, run.lay
        fl = lay.rtl / "filelist.f"
        work = lay.sim / "rtl"
        work.mkdir(parents=True, exist_ok=True)
        absfl = work / "filelist_abs.f"
        files = [str((run.ctx.project.root / ln).resolve()) for ln in fl.read_text().split()]
        absfl.write_text("\n".join(files) + "\n")
        res = run_role(run.tools, "lint", work, {"filelist": absfl, "top": top, "files": files, "workdir": work,
                                                 "project": run.ctx.project.root}, log_name="lint_design.log")
        from q3tui.eda.tools import check_broken

        check_broken(res, "lint")
        limit = int(self.options.get("lint_max_warnings", 0))
        per: dict[str, list[str]] = {}
        warns = [d for d in res.diagnostics if d.severity == "warning"]
        for d in res.diagnostics:
            if d.severity == "error" or (d.severity == "warning" and len(warns) > limit):
                mod = Path(d.file).stem if d.file else top
                per.setdefault(mod, []).append(f"design lint {d.severity} {d.code}{f' line {d.line}' if d.line else ''}: {d.message}")
        if not res.ok and not per and res.returncode not in (0, None):
            per.setdefault(top, []).append(f"design lint failed (rc={res.returncode}); see {run.ctx.project.rel(Path(res.log_path))}")
        run.lint_state["__design__"] = {"status": "fail" if per else "pass", "warnings": len(warns)}
        return per

    async def _synthesize(self, run: _Run, order: list[str], leftover: dict, limiter) -> dict:
        import anyio

        from q3tui.eda.tools import available, run_role

        ctx, lay, project = run.ctx, run.lay, run.ctx.project
        top = run.plan.top
        report: dict = {"tool": "not run", "pdk": self.options.get("pdk"), "top_module": top, "synthesis": "not run", "corners": {}}
        if "synth" not in run.tools.roles or not available(run.tools, "synth"):
            why = "no tool for role 'synth'" if "synth" not in run.tools.roles else "the synth tool was not found"
            ctx.emit("warning", message=f"synthesis not run: {why} (recorded as \"not run\", not as passed)")
            report["note"] = f"synthesis not run: {why}"
            return report
        files = [(lay.rtl / f"{n}.sv").resolve() for n in order if (lay.rtl / f"{n}.sv").is_file()]
        backend = synth.backend_for(run.tools.roles["synth"].parse, self.options)
        if backend == "dc" and not any(c["liberty"] for c in synth.corners(self.options)):
            why = ("Design Compiler needs a target library: set options.synth.corners: [{name, library: <.db>}] on the rtl step "
                   "of the flow file")
            ctx.emit("warning", message=f"synthesis not run: {why} (recorded as \"not run\", not as passed)")
            report["note"] = f"synthesis not run: {why}"
            return report
        design_hash = sha256_json({"files": [f.read_text() for f in files], "opt": self.options.get("synth"), "top": top})
        meta = run.meta
        old = artifacts.read(project, "synth_report.json")
        if old and meta.get("synth_hash") == design_hash and old.get("synthesis") in ("pass", "fail") and not run.wrote:
            return old
        work = lay.sim / "rtl"
        work.mkdir(parents=True, exist_ok=True)
        by_name = run.by_name()
        for rnd in range(max(1, ctx.cfg.pipeline.max_fix_iterations)):
            statuses, errors = {}, {}
            backend = synth.backend_for(run.tools.roles["synth"].parse, self.options)
            for c in synth.corners(self.options):
                script = lay.rtl / f"synth_{c['name']}{synth.script_suffix(backend)}"
                script.write_text(synth.script_for(backend, files, top, c["liberty"], self.options))
                res = await anyio.to_thread.run_sync(lambda: run_role(
                    run.tools, "synth", work, {"script": script, "top": top, "filelist": lay.rtl / "filelist.f", "files": files,
                                               "workdir": work, "corner": c["name"], "liberty": c["liberty"] or "", "project": project.root},
                    log_name=f"synth_{c['name']}.log"))
                log = Path(res.log_path).read_text(errors="replace") if res.log_path else ""
                st = synth.parse_stat(log, top)
                ok = res.ok and (st["cell_count"] or 0) > 0
                if not ok:  # failed without an error it could read (or no cell count to read): the tool / its log format
                    from q3tui.eda.tools import ToolBroken, broken

                    msg = broken(res, "synth") or (None if res.diagnostics and any(d.severity == "error" for d in res.diagnostics) else
                                                   f"the `synth` tool finished but its log has no cell count to read (log: {res.log_path}) — "
                                                   "check the command, the library / script options and the report format")
                    if msg:
                        raise ToolBroken(msg)
                statuses[c["name"]] = {"status": "pass" if ok else "fail", "cell_count": st["cell_count"], "area_estimate_um2": st["area_um2"],
                                       "log": project.rel(Path(res.log_path)) if res.log_path else None}
                if st["tool"]:
                    report["tool"] = st["tool"]
                if not ok:
                    errors[c["name"]] = synth.attribute_errors(log, list(by_name)) or {"": [f"synthesis failed (rc={res.returncode})"]}
            for cname, by_mod in errors.items():  # errors that name no module of ours: the tool's setup (library, script), not the RTL
                if set(by_mod) == {""}:
                    from q3tui.eda.tools import ToolBroken

                    raise ToolBroken(f"synthesis ({cname}) failed with errors that mention none of the modules — a problem of the "
                                     f"tool's setup (library, script options), not of the RTL (no fix round was spent):\n"
                                     + "\n".join(by_mod[""][:6]) + f"\nLog: {statuses[cname]['log']}")
            report["corners"] = statuses
            report["synthesis"] = "pass" if statuses and all(s["status"] == "pass" for s in statuses.values()) else "fail"
            if report["synthesis"] == "pass":
                break
            per: dict[str, list[str]] = {}
            for by_mod in errors.values():
                for m, lines in by_mod.items():
                    per.setdefault(m if m in by_name else top, []).extend(f"synthesis: {ln}" for ln in lines)
            ctx.emit("warning", message=f"synthesis failed: {', '.join(per)} get the log; fixing …")
            async with anyio.create_task_group() as tg:
                for m, probs in per.items():
                    async def fix(m=m, probs=probs):
                        async with limiter:
                            await self._fix(run, by_name[m], probs)
                    tg.start_soon(fix)
            files = [(lay.rtl / f"{n}.sv").resolve() for n in order if (lay.rtl / f"{n}.sv").is_file()]
        if report["synthesis"] == "fail":
            leftover.setdefault(top, []).append("synthesis fails: " + "; ".join(
                f"{k}: {v['status']}" for k, v in report["corners"].items()) + " (see the synth logs under .q3tui/sim/rtl)")
        meta["synth_hash"] = sha256_json({"files": [f.read_text() for f in files], "opt": self.options.get("synth"), "top": top})
        return report

    def _write_report(self, run: _Run, order: list[str], report: dict) -> None:
        lay, project = run.lay, run.ctx.project
        mods: dict[str, dict] = {}
        total_tags, distinct = 0, set()
        for n in order:
            f = lay.rtl / f"{n}.sv"
            if not f.is_file():
                continue
            text = f.read_text()
            tags = rules_check.req_tags(text)
            total_tags += rules_check.count_tags(text)
            distinct |= tags
            pm = run.by_name()[n]
            mods[n] = {"file": project.rel(f), "lines": len(text.splitlines()), "req_ids": sorted(tags),
                       "lint": (run.lint_state.get(n) or {}).get("status", "not run"),
                       "lint_warnings": (run.lint_state.get(n) or {}).get("warnings", 0),
                       "rule_violations": len(rules_check.check_source(text, n, run.prof, pm.is_package))}
        design = run.lint_state.get("__design__") or {}
        lint_ok = all(m["lint"] in ("pass", "not run") for m in mods.values()) and design.get("status", "pass") == "pass"
        from q3tui.eda.tools import binary_of

        report.update({
            "lint": {"status": "pass" if lint_ok else "fail", "tool": binary_of(run.tools.roles["lint"]),
                     "warnings": design.get("warnings", sum(m["lint_warnings"] for m in mods.values())),
                     "max_warnings": int(self.options.get("lint_max_warnings", 0))},
            "modules": mods, "req_ids_tagged": total_tags, "req_ids_distinct": len(distinct),
            "rule_profile": run.prof.name, "generated_at": artifacts.now()})
        artifacts.write(project, "synth_report.json", report, check=False)

    def _write_questions(self, ctx, run: _Run, q_json: Path, answers_path: Path, q_md: Path) -> None:
        engine = ctx.engine
        prev_raw = read_json(q_json, default=[]) or []
        kept = [q for q in prev_raw if q.get("module") not in run.results]
        new = []
        for m, r in run.results.items():
            fresh, dropped = drop_settled(r.questions, engine.settled_questions(self.name))
            new += [(m, q) for q in fresh]
            if dropped:
                ctx.emit("log", message=f"{m}: dropped question(s) already settled: " + ", ".join(sid for _, sid in dropped))
        ided = assign_question_ids([q for _, q in new], [Question.model_validate(q) for q in prev_raw], "Q-R")
        kept_ids = {q["id"] for q in kept}
        entries = kept + [{**q.model_dump(), "module": m} for (m, _), q in zip(new, ided) if q.id not in kept_ids]
        q_json.parent.mkdir(parents=True, exist_ok=True)
        write_json(q_json, entries)
        answers = load_answers(answers_path)
        qs = [Question.model_validate(q) for q in entries]
        if qs:
            q_md.write_text(render_questions(qs, answers, f"Open questions — {self.title}", engine.state.accepted_defaults.get(self.name, {})))
        open_q = [q for q in qs if q.id not in answers]
        ctx.emit("questions", count=len(open_q), blocking=sum(q.blocking for q in open_q))
