"""VLSIT step `tb` (phase 4, /tb_generator): spec + structured spec + final config → src/tb/ (plain SystemVerilog,
no UVM) and schemas/selected_testplan.json.

Independence: the testbench is written from the spec, the requirements and the configuration only — the stages never
see src/rtl or src/sva (deny_dirs); the RTL is used by code for the compile check only.

Code: the TC ↔ REQ bookkeeping, REQ coverage (an uncovered REQ is always reported — a blocking question when it cannot be
fixed), conditional test cases by parameter, the result macros / test includes / runner (`TC_RESULT <tc_id> PASS|FAIL`),
filelists, the compile check (tool role `sim`) and the test plan file. The LLM plans the test cases, writes `tb_top.sv`
and, in groups, the test case files (only its own files). Changes propagate as updates: a test case is rewritten only
when what it was built from changed, or a scoped change request `[tb:<TC-id>]` names it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.core.project import read_json, sha256_json, write_json
from q3tui.steps.common import Question, assign_question_ids, drop_settled, load_answers, render_questions, settled_block, split_feedback
from q3tui.steps.vlsit import artifacts
from q3tui.flows.vlsit.tb import gen as tb_gen
from q3tui.flows.vlsit.tb import plan as tb_plan
from q3tui.flows.vlsit.tb import task as tb_prompts
from q3tui.steps.vlsit.base import VlsitStep
from q3tui.flows.vlsit.tb.plan import TbCase, TbPlan, TbUnitResult


@dataclass
class _Run:
    ctx: StepContext
    lay: object
    tools: object
    spec: dict
    params: dict[str, str]
    reqs: dict[str, str]
    spec_files: list[str]
    top: str | None
    answers: dict[str, str]
    settled: str
    general: list[str]
    by_unit: dict[str, list[str]]
    meta: dict
    refs: dict = field(default_factory=dict)  # your imported testbench: {tests, env, docs: [relpath], hash}
    asked: dict[str, list[Question]] = field(default_factory=dict)  # unit -> questions its stage raised this run
    wrote: set[str] = field(default_factory=set)
    llm_calls: int = 0

    def tb_path(self, name: str) -> Path:
        return self.lay.tb / name

    def tc_path(self, c: dict) -> Path:
        return self.lay.tb / "tests" / tb_plan.file_name(c)


class TbGenStep(VlsitStep):
    name = "tb"
    title = "Testbench (plain SystemVerilog)"
    deps = ("config",)
    artifact = "selected_testplan.json"
    gate_fields = {"gate_4_approved": True, "gate_4_at": ""}
    tool_roles = ("sim",)
    checks_version = 3  # 2: the filelist lists tb_top only (test cases are included, not sources); 3: `TC_SUMMARY ends with ;

    # -- files -------------------------------------------------------------------------------------

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        imported = engine.import_record_path("tb")  # (your imported testbench / tests, once there are some)
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "final_config.json"),
                self._q_paths(engine)[1], *([imported] if imported.is_file() else [])]  # (not the RTL: the testbench is written without it)

    # -- imports: your testbench and tests are references the test cases are ported from ----------------------

    def import_dir(self, engine, kind: str) -> Path | None:
        return self.lay(engine).tb / "imported" if kind == "tb" else None

    def _references(self, engine) -> dict:
        from q3tui.core.project import SPEC_DOC_EXTS

        rec = engine.import_record("tb") or {}
        files = [r for r in rec.get("files", {}) if (engine.project.root / r).is_file()]
        docs = [r for r in files if Path(r).suffix.lower() in SPEC_DOC_EXTS]
        hdl = [r for r in files if r not in docs]
        tests = [r for r in hdl if {"tests", "test", "testcases", "tc"} & {x.lower() for x in Path(r).parts[:-1]} or "test" in Path(r).stem.lower()]
        return {"tests": tests, "env": [r for r in hdl if r not in tests], "docs": docs,
                "hash": sha256_json(rec.get("files", {})) if files else ""}

    def outputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        files: list[Path] = []
        if lay.tb.is_dir():
            files += sorted(lay.tb.glob("*.sv")) + sorted(lay.tb.glob("*.svh")) + sorted((lay.tb / "tests").glob("*.sv"))
            files += [f for f in (lay.tb / "filelist.f", lay.tb / "run.sh") if f.is_file()]
        files += [f for f in (lay.artifact("selected_testplan.json"), self._q_paths(engine)[2]) if f.is_file()]
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
        if "sim" not in tools.roles:
            return "no tool for role 'sim': add roles.sim (compile command, e.g. vcs …) to tools.json or tools.roles — `q3tui tools init` writes one to start from"
        if not available(tools, "sim"):
            return f"the simulator '{binary_of(tools.roles['sim'])}' (role 'sim') was not found (check tools.json, modules / setup_script)"
        return None

    # -- stages --------------------------------------------------------------------------------------

    def _deny(self, run: _Run) -> list[Path]:
        p = run.ctx.project
        return [run.lay.rtl, run.lay.sva, p.model_dir, p.arch_dir, p.rtl_dir, p.tb_dir]  # independence: spec + requirements only

    def _write_stage(self, run: _Run, name: str, prompt: str, files: list[Path]) -> Stage:
        ctx = run.ctx
        return Stage(name=name, system_prompt=tb_prompts.WRITE_SYSTEM, prompt=prompt, cwd=ctx.project.root,
                     builtin_tools=["Read", "Grep", "Glob", "Write", "Edit"], output_model=TbUnitResult, deny_dirs=self._deny(run),
                     write_dirs=[run.lay.tb], write_files=files, max_turns=ctx.cfg.tb.max_turns)

    async def _plan(self, run: _Run, previous: list[dict] | None, changes: list[str], uncovered: list[str],
                    imported: list[dict] | None = None) -> TbPlan:
        ctx = run.ctx
        reqs = [(r["req_id"], r.get("category", ""), r.get("text", "")) for r in run.spec.get("requirements", []) if r.get("req_id")]
        res = await ctx.llm(Stage(
            name="tb_plan", system_prompt=tb_prompts.PLAN_SYSTEM, cwd=ctx.project.root, builtin_tools=["Read", "Grep", "Glob"],
            output_model=TbPlan, deny_dirs=self._deny(run), max_turns=ctx.cfg.tb.max_turns,
            prompt=tb_prompts.plan_prompt(reqs=reqs, params=run.params, spec_files=run.spec_files, top=run.top, previous=previous,
                                          changes=changes, answers=run.answers, settled=run.settled,
                                          feedback=[*run.general, *run.by_unit.get("plan", [])], uncovered=uncovered,
                                          imported=imported, imported_docs=run.refs.get("docs", []))))
        run.llm_calls += 1
        return res.output

    # -- run -------------------------------------------------------------------------------------------

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
        params = {k: str((v or {}).get("value")) for k, v in (config.get("parameters") or {}).items()}
        reqs = {r["req_id"]: r.get("text", "") for r in spec.get("requirements", []) if r.get("req_id")}
        meta = engine.state.step_meta.setdefault(self.name, {})
        q_json, answers_path, q_md = self._q_paths(engine)
        by_unit, general = split_feedback(ctx.feedback, self.name)
        run = _Run(ctx, lay, tools, spec, params, reqs, [project.rel(p) for p in project.spec_documents()],
                   (spec.get("metadata") or {}).get("top_module"), load_answers(answers_path), settled_block(engine.settled_questions(self.name)),
                   general, by_unit, meta)
        run.refs = self._references(engine)
        lay.tb.mkdir(parents=True, exist_ok=True)
        (lay.tb / "tests").mkdir(parents=True, exist_ok=True)
        if not run.top:
            from q3tui.flows.vlsit.rtl.plan import plan_from_spec

            pl = plan_from_spec(spec)
            run.top = pl.top if pl else None

        # 1. the plan -------------------------------------------------------------------------
        snapshot = {"reqs": {r["req_id"]: [r.get("category"), r.get("sva_hint"), r.get("text")] for r in spec.get("requirements", []) if r.get("req_id")},
                    "params": params, "top": run.top, **({"imported": run.refs["hash"]} if run.refs.get("hash") else {})}
        plan_hash = sha256_json({**snapshot, "answers": run.answers})
        old = meta.get("plan") or {}
        need_plan = bool(ctx.regenerate or general or "plan" in by_unit or not old.get("cases") or old.get("hash") != plan_hash)
        cases: list[dict] = list(old.get("cases") or [])
        warnings: list[str] = []
        if need_plan:
            changes = self._plan_changes(old.get("snapshot"), snapshot, old.get("answers"), run.answers)
            ctx.emit("log", message="planning the test cases …" if not cases else "updating the test plan …")
            # your tests are the plan (one test case each, id / name / source fixed by code); the LLM maps them to REQs
            seed = tb_plan.seed_imported(run.refs.get("tests", []), cases)
            plan = await self._plan(run, cases or None, changes, [], seed)
            sources = set(run.refs.get("tests", []))
            norm, warnings = tb_plan.normalize(plan.test_cases, set(reqs), cases, sources)
            cases = [c.model_dump() for c in norm]
            run.asked["plan"] = plan.questions
            sel = [c for c in cases if tb_plan.applies(c, params)]
            cov = tb_plan.coverage(sel, spec)
            unported = tb_plan.unported(cases, run.refs.get("tests", []))
            if (cov["uncovered_req_ids"] or unported) and not ctx.cfg.tb.max_fix_attempts == 0:
                ctx.emit("warning", message="asking for more test cases: " + "; ".join(
                    ([f"no test case covers {', '.join(cov['uncovered_req_ids'])}"] if cov["uncovered_req_ids"] else [])
                    + ([f"{len(unported)} imported test(s) not ported"] if unported else [])))
                plan = await self._plan(run, cases, [], cov["uncovered_req_ids"], [s for s in seed if s["source"] in unported])
                norm, w2 = tb_plan.normalize(plan.test_cases, set(reqs), cases, sources)
                warnings += w2
                cases = [c.model_dump() for c in norm]
                run.asked["plan"] = [*run.asked.get("plan", []), *plan.questions]
            if seed:
                cases, w3 = tb_plan.apply_imported(cases, seed)
                warnings += w3
            meta["plan"] = {"hash": plan_hash, "snapshot": snapshot, "answers": dict(run.answers), "cases": cases}
            run.wrote.add("plan")
        for w in warnings:
            ctx.emit("warning", message=w)
        unported = tb_plan.unported(cases, run.refs.get("tests", []))
        if unported:
            ctx.emit("warning", message=f"imported test(s) no test case is ported from: {', '.join(Path(t).name for t in unported[:10])}"
                                        + (" …" if len(unported) > 10 else ""))
        selected = [c for c in cases if tb_plan.applies(c, params)]
        excluded = [c for c in cases if c not in selected]
        for c in excluded:
            ctx.emit("log", message=f"{c['tc_id']} {c['name']}: left out — {c['conditional_param']} is {params.get(c['conditional_param'])}")
        cov = tb_plan.coverage(selected, spec)
        if cov["uncovered_req_ids"]:
            ctx.emit("warning", message=f"requirements with no test case: {', '.join(cov['uncovered_req_ids'])}")

        # 2. generated framework, tb_top, test case files ---------------------------------------------
        tb_gen.write_framework(lay.tb, selected)
        await self._write_top(run, selected)
        await self._write_cases(run, selected)
        wanted = {tb_plan.file_name(c) for c in selected}
        for f in sorted((lay.tb / "tests").glob("*.sv")):  # test cases no longer in the plan
            if f.name not in wanted:
                project.backup([f])
                f.unlink()
                ctx.emit("log", message=f"{f.name}: no longer in the test plan — removed (backed up)")
        # a test case file missing after all this = a stage that did not write it
        missing = [tb_plan.file_name(c) for c in selected if not run.tc_path(c).is_file()]
        if missing:
            raise StepFailed("test case file(s) not written: " + ", ".join(missing))

        # 3. filelist + compile check (+ fixes) -----------------------------------------------------------
        compile_info = await self._compile_loop(run, selected)

        # 4. test plan file, questions -------------------------------------------------------------------
        self._write_plan_file(run, cases, selected, cov, compile_info, params)
        code_questions = self._code_questions(run, cov, compile_info)
        self._write_questions(run, q_json, answers_path, q_md, code_questions)
        ctx.unchanged = not run.wrote and compile_info["status"] in ("pass", "skipped")
        ctx.emit("log", message=f"testbench: {len(selected)}/{len(cases)} test case(s), REQ coverage {cov['coverage_pct']}%"
                                f" ({len(cov['covered_req_ids'])}/{cov['total_req_ids']}), compile {compile_info['status']}")

    def _plan_changes(self, old_snap: dict | None, new: dict, old_answers: dict | None, answers: dict) -> list[str]:
        if not old_snap:
            return []
        out = []
        a, b = old_snap.get("reqs", {}), new["reqs"]
        for r in sorted(set(b) - set(a)):
            out.append(f"new requirement {r}: {b[r][2]}")
        for r in sorted(set(a) - set(b)):
            out.append(f"{r} was removed: drop it from the test cases")
        for r in sorted(set(a) & set(b)):
            if a[r] != b[r]:
                out.append(f"{r} changed: {b[r][2]} (was: {a[r][2]})")
        for k in sorted(set(old_snap.get("params", {})) | set(new["params"])):
            if old_snap.get("params", {}).get(k) != new["params"].get(k):
                out.append(f"parameter {k} = {new['params'].get(k)} (was {old_snap.get('params', {}).get(k)})")
        if (old_answers or {}) != answers:
            out.append("the user's answers changed (see below)")
        return out

    # -- tb_top --------------------------------------------------------------------------------------------

    async def _write_top(self, run: _Run, selected: list[dict]) -> None:
        import anyio

        ctx, project = run.ctx, run.ctx.project
        path = run.tb_path("tb_top.sv")
        h = sha256_json({"top": run.top, "params": run.params, "answers": run.answers,
                         "spec": [p.read_text() if p.suffix in (".md", ".txt") else p.name for p in project.spec_documents()],
                         **({"imported": run.refs["hash"]} if run.refs.get("hash") else {})})
        saved = run.meta.get("top_hash")
        todo = bool(ctx.regenerate or run.general or "top" in run.by_unit or not path.is_file() or saved != h)
        problems = ""
        if not todo:
            problems = "\n".join(f"- {p}" for p in tb_gen.top_problems(path.read_text()))
            if not problems:
                return
        ctx.emit("log", message="writing tb_top.sv …")
        res = await ctx.llm(self._write_stage(run, "tb_top", tb_prompts.top_prompt(
            path=project.rel(path), top=run.top, params=run.params, spec_files=run.spec_files, answers=run.answers, settled=run.settled,
            feedback=[*run.general, *run.by_unit.get("top", [])], problems=problems, existing=path.is_file(), cases=selected,
            imported=[*run.refs.get("env", []), *run.refs.get("docs", [])]), [path]))
        run.llm_calls += 1
        run.asked["top"] = res.output.questions if res.output else []
        for attempt in range(ctx.cfg.tb.max_fix_attempts):
            probs = tb_gen.top_problems(path.read_text()) if path.is_file() else [f"{path.name} was not written"]
            if not probs:
                break
            ctx.emit("warning", message=f"tb_top: {len(probs)} problem(s); fixing …")
            await ctx.llm(self._write_stage(run, "tb_top_fix", tb_prompts.fix_prompt(
                label="tb_top", problems="\n".join(f"- {p}" for p in probs),
                files={project.rel(path): path.read_text() if path.is_file() else ""}), [path]))
            run.llm_calls += 1
        if not path.is_file() or tb_gen.top_problems(path.read_text()):
            raise StepFailed("tb_top.sv does not meet the framework contract: " + "; ".join(tb_gen.top_problems(path.read_text() if path.is_file() else "")))
        run.meta["top_hash"] = h
        run.wrote.add("top")

    # -- test case files --------------------------------------------------------------------------------------

    def _tc_hash(self, run: _Run, c: dict) -> str:
        src = run.ctx.project.root / c["source"] if c.get("source") else None
        return sha256_json({"tc": c, "reqs": {r: run.reqs.get(r, "") for r in c["req_ids"]}, "params": run.params, "answers": run.answers,
                            **({"source": sha256_json(src.read_text(errors="replace"))} if src and src.is_file() else {})})

    def _tc_problems(self, run: _Run, c: dict) -> list[str]:
        p = run.tc_path(c)
        if not p.is_file() or not p.read_text().strip():
            return [f"{p.name} was not written"]
        text = p.read_text()
        out = []
        if f"task automatic {tb_plan.task_name(c)}" not in text:
            out.append(f"{p.name} must define `task automatic {tb_plan.task_name(c)}();` (no arguments)")
        if "`TC_CHECK" not in text:
            out.append(f"{p.name}: no `TC_CHECK in the test case (every test case must check something)")
        if c["tc_id"] not in text.splitlines()[0]:
            out.append(f"{p.name}: the first line must be `// {c['tc_id']} | {', '.join(c['req_ids'])}`")
        return out

    async def _write_cases(self, run: _Run, selected: list[dict]) -> None:
        import anyio

        ctx, project = run.ctx, run.ctx.project
        hashes = run.meta.setdefault("tcs", {})
        todo = []
        for c in selected:
            h = self._tc_hash(run, c)
            scoped = c["tc_id"] in run.by_unit or tb_plan.file_name(c) in run.by_unit
            if ctx.regenerate or run.general or scoped or not run.tc_path(c).is_file() or hashes.get(c["tc_id"]) != h or "top" in run.wrote:
                todo.append((c, h))
        # (a test case file that changed outside this step or fails the code checks is repaired too)
        for c in selected:
            if c not in [t[0] for t in todo] and self._tc_problems(run, c):
                todo.append((c, self._tc_hash(run, c)))
        if not todo:
            return
        size = max(1, ctx.cfg.tb.tests_per_agent)
        groups = [todo[i:i + size] for i in range(0, len(todo), size)]
        limiter = anyio.CapacityLimiter(max(1, ctx.cfg.tb.parallel))
        top_path = project.rel(run.tb_path("tb_top.sv"))

        async def one(k: int, group: list[tuple[dict, str]]) -> None:
            async with limiter:
                cs = [c for c, _ in group]
                paths = {c["tc_id"]: project.rel(run.tc_path(c)) for c in cs}
                fb = [*run.general] + [f for c in cs for f in run.by_unit.get(c["tc_id"], []) + run.by_unit.get(tb_plan.file_name(c), [])]
                ctx.emit("log", message=f"writing {', '.join(c['tc_id'] for c in cs)} …")
                res = await ctx.llm(self._write_stage(run, f"tb_g{k + 1}", tb_prompts.group_prompt(
                    cases=cs, paths=paths, reqs=run.reqs, params=run.params, spec_files=run.spec_files, top_path=top_path,
                    answers=run.answers, settled=run.settled, feedback=fb, problems="",
                    existing={c["tc_id"] for c in cs if run.tc_path(c).is_file()}), [run.tc_path(c) for c in cs]))
                run.llm_calls += 1
                run.asked[f"g{k + 1}"] = res.output.questions if res.output else []
                for attempt in range(ctx.cfg.tb.max_fix_attempts):
                    bad = {c["tc_id"]: self._tc_problems(run, c) for c in cs}
                    bad = {k2: v for k2, v in bad.items() if v}
                    if not bad:
                        break
                    ctx.emit("warning", message=f"{', '.join(bad)}: {sum(map(len, bad.values()))} problem(s); fixing …")
                    files = {project.rel(run.tc_path(c)): run.tc_path(c).read_text() if run.tc_path(c).is_file() else ""
                             for c in cs if c["tc_id"] in bad}
                    await ctx.llm(self._write_stage(run, f"tb_fix_g{k + 1}", tb_prompts.fix_prompt(
                        label=", ".join(bad), problems="\n".join(f"- {p}" for v in bad.values() for p in v), files=files),
                        [run.tc_path(c) for c in cs if c["tc_id"] in bad]))
                    run.llm_calls += 1
                for c, h in group:
                    if not self._tc_problems(run, c):
                        hashes[c["tc_id"]] = h
                    run.wrote.add(c["tc_id"])

        async with anyio.create_task_group() as tg:
            for k, g in enumerate(groups):
                tg.start_soon(one, k, g)
        left = {c["tc_id"]: self._tc_problems(run, c) for c in selected}
        left = {k: v for k, v in left.items() if v}
        if left:
            raise StepFailed("test case files fail the framework checks after the fix rounds:\n" + "\n".join(
                f"- {k}: {'; '.join(v)}" for k, v in left.items()))

    # -- compile check --------------------------------------------------------------------------------------------

    def _compile(self, run: _Run, selected: list[dict]):
        from q3tui.eda.tools import run_role

        lay, project = run.lay, run.ctx.project
        work = lay.sim / "tb"
        fl, absf, files = tb_gen.filelists(project.root, lay.tb, lay.rtl, work, selected)
        res = run_role(run.tools, "sim", work, {"filelist": absf, "top": "tb_top", "files": [str(f) for f in files],
                                                "incdirs": [f"+incdir+{lay.tb.resolve()}"], "workdir": work, "project": project.root},
                       log_name="compile.log")
        from q3tui.eda.tools import check_broken

        check_broken(res, "sim")
        return res

    async def _compile_loop(self, run: _Run, selected: list[dict]) -> dict:
        import anyio

        from q3tui.eda.tools import binary_of

        ctx, project, lay = run.ctx, run.ctx.project, run.lay
        if not tb_gen.rtl_files(project.root, lay.rtl):
            ctx.emit("warning", message="no RTL yet (src/rtl): the testbench compile check is skipped")
            tb_gen.filelists(project.root, lay.tb, lay.rtl, lay.sim / "tb", selected)
            self._run_script(run)
            return {"tool": binary_of(run.tools.roles["sim"]), "status": "skipped", "error_count": 0, "errors": [], "by_file": {},
                    "note": "no RTL to compile against"}
        by_file: dict[str, list[str]] = {}
        external: list[str] = []
        errors: list = []
        res = None
        for attempt in range(ctx.cfg.tb.max_fix_attempts + 1):
            res = await anyio.to_thread.run_sync(lambda: self._compile(run, selected))
            errors = [d for d in res.diagnostics if d.severity == "error"]
            by_file, external = {}, []
            if res.ok and not errors:
                break
            for d in errors:
                msg = f"{d.code}{f' line {d.line}' if d.line else ''}: {d.message}"
                name = Path(d.file).name if d.file else ""
                if name and ((lay.tb / "tests" / name).is_file() or (lay.tb / name).is_file()):
                    by_file.setdefault(name, []).append(msg)  # a testbench file: ours to fix
                else:
                    external.append(f"{name}: {msg}" if name else msg)  # RTL or unknown: not ours
            if not errors:
                external.append(f"the compile failed (rc={res.returncode}) without a diagnostic; see {project.rel(Path(res.log_path))}")
            if not by_file or attempt == ctx.cfg.tb.max_fix_attempts:
                break
            ctx.emit("warning", message=f"compile: {sum(map(len, by_file.values()))} error(s) in {', '.join(by_file)}; fixing …")
            limiter = anyio.CapacityLimiter(max(1, ctx.cfg.tb.parallel))

            async def fix(path: Path, msgs: list[str]):
                async with limiter:
                    await ctx.llm(self._write_stage(run, f"tb_fix_{path.stem}", tb_prompts.fix_prompt(
                        label=path.name, problems="\n".join(f"- {m}" for m in msgs), files={project.rel(path): path.read_text()}), [path]))
                    run.llm_calls += 1
                    run.wrote.add(path.stem)

            async with anyio.create_task_group() as tg:
                for name, msgs in by_file.items():
                    path = lay.tb / "tests" / name if (lay.tb / "tests" / name).is_file() else lay.tb / name
                    tg.start_soon(fix, path, msgs)
        self._run_script(run)
        passed = res is not None and res.ok and not errors
        return {"tool": binary_of(run.tools.roles["sim"]), "status": "pass" if passed else "fail", "error_count": len(errors),
                "errors": [f"{d.file}:{d.line}: {d.message}" if d.file else d.message for d in errors[:10]],
                "by_file": by_file, "external": external}

    def _run_script(self, run: _Run) -> None:
        from q3tui.eda.tools import render

        lay, project = run.lay, run.ctx.project
        try:
            absf = lay.sim / "tb" / "filelist_tb_abs.f"
            variables = {"filelist": absf, "top": "tb_top", "files": [], "incdirs": [f"+incdir+{lay.tb.resolve()}"], "workdir": lay.sim / "tb",
                         "project": project.root}
            import shlex

            def cmd(role: str) -> str | None:
                r = run.tools.roles.get(role)
                return shlex.join(render(r, variables)) if r else None

            (lay.tb / "run.sh").write_text(tb_gen.run_script(cmd("sim"), cmd("run")))
        except Exception:  # noqa: BLE001 - a convenience file: never fails the step
            pass

    # -- outputs -----------------------------------------------------------------------------------------------------

    def _write_plan_file(self, run: _Run, cases: list[dict], selected: list[dict], cov: dict, compile_info: dict, params: dict) -> None:
        project, ids = run.ctx.project, {c["tc_id"] for c in selected}
        bad_files = set(compile_info.get("by_file") or {})
        tcs = []
        for c in cases:
            sel = c["tc_id"] in ids
            fname = tb_plan.file_name(c)
            tcs.append({
                "tc_id": c["tc_id"], "name": c["name"], "title": c["name"], "description": c.get("description", ""),
                "req_ids": c["req_ids"], "selected": sel, "conditional_param": c.get("conditional_param"),
                **({"source": c["source"]} if c.get("source") else {}),
                **({"file": project.rel(run.tc_path(c))} if sel else {}),
                "compile_status": ("not selected" if not sel else "fail" if fname in bad_files else
                                   "skipped" if compile_info["status"] == "skipped" else "pass" if compile_info["status"] == "pass" else "fail")})
        old = artifacts.read(project, "selected_testplan.json") or {}
        md = {"ip_name": (run.spec.get("metadata") or {}).get("ip_name") or run.top or "", "phase": 4,
              "gate_4_approved": False, "gate_4_at": None}
        if run.ctx.engine.state.gates.get(self.name) and run.ctx.engine.state.gates[self.name].status == "approved" and not run.wrote:
            md.update({k: v for k, v in (old.get("metadata") or {}).items() if k.startswith("gate_4")})  # unchanged: the approval stays
        data = {"metadata": md, "test_cases": tcs,
                "coverage": cov,
                "compile_check": {"tool": compile_info["tool"], "status": compile_info["status"], "error_count": compile_info["error_count"]},
                "result_format": tb_gen.RESULT_FORMAT, "filelist": project.rel(run.lay.tb / "filelist.f")}
        artifacts.write(project, "selected_testplan.json", data)

    def _code_questions(self, run: _Run, cov: dict, compile_info: dict) -> list[Question]:
        out = []
        answers = run.answers
        q_raw = read_json(self._q_paths(run.ctx.engine)[0], default=[]) or []
        if cov["uncovered_req_ids"]:
            ids = ", ".join(cov["uncovered_req_ids"])
            text = (f"No test case covers {ids}. Add test cases for them, or accept that they are not checked by simulation "
                    f"(SVA / review only)?")
            prior = [q for q in q_raw if q.get("code") == "uncovered" and q["id"] in answers]
            if not any(set(cov["uncovered_req_ids"]) <= set(q.get("uncovered") or []) for q in prior):
                out.append(Question(id="", question=text, blocking=True,
                                    default_assumption="Accept: not checked by simulation (recorded as uncovered in selected_testplan.json)",
                                    kind="design_choice"))
        if compile_info["status"] == "fail":
            where = compile_info.get("errors", [])[:3] + compile_info.get("external", [])[:3]
            out.append(Question(id="", question="The testbench does not compile against the RTL (after the fix rounds): "
                                + " | ".join(where) + ". Which side is wrong — the testbench (spec reading) or the RTL (port / interface names)?",
                                blocking=True, default_assumption="Regenerate the testbench from the spec once the RTL interface is checked",
                                kind="design_choice"))
        return out

    def _write_questions(self, run: _Run, q_json: Path, answers_path: Path, q_md: Path, code_questions: list[Question]) -> None:
        ctx, engine = run.ctx, run.ctx.engine
        prev_raw = read_json(q_json, default=[]) or []
        rerun = set(run.asked) | {"code"}
        kept = [q for q in prev_raw if (q.get("unit") or "") not in rerun and q.get("code") is None]
        new: list[tuple[str, Question, dict]] = []
        for unit, qs in run.asked.items():
            fresh, dropped = drop_settled(qs, engine.settled_questions(self.name))
            new += [(unit, q, {}) for q in fresh]
            if dropped:
                ctx.emit("log", message=f"{unit}: dropped question(s) already settled: " + ", ".join(sid for _, sid in dropped))
        cov_ids = []
        for q in code_questions:
            code = "uncovered" if q.question.startswith("No test case") else "compile"
            extra = {"code": code}
            if code == "uncovered":
                extra["uncovered"] = tb_plan.coverage([c for c in run.meta["plan"]["cases"] if tb_plan.applies(c, run.params)], run.spec)["uncovered_req_ids"]
            new.append(("code", q, extra))
        # code questions keep their answered history: earlier answered ones stay as they are
        old_code = [q for q in prev_raw if q.get("code") and q["id"] in run.answers]
        ided = assign_question_ids([q for _, q, _ in new], [Question.model_validate(q) for q in prev_raw], "Q-TB")
        kept_ids = {q["id"] for q in kept} | {q["id"] for q in old_code}
        entries = kept + [q for q in old_code if q["id"] not in {n.id for n in ided}] + [
            {**q.model_dump(), "unit": unit if unit != "code" else "", **extra} for (unit, _, extra), q in zip(new, ided) if q.id not in kept_ids]
        q_json.parent.mkdir(parents=True, exist_ok=True)
        write_json(q_json, entries)
        answers = load_answers(answers_path)
        qs = [Question.model_validate(q) for q in entries]
        if qs:
            q_md.write_text(render_questions(qs, answers, f"Open questions — {self.title}", engine.state.accepted_defaults.get(self.name, {})))
        open_q = [q for q in qs if q.id not in answers]
        ctx.emit("questions", count=len(open_q), blocking=sum(q.blocking for q in open_q))
