"""dv step `prepare`: the programs and data the plain testbench's tests load (docs/spec/dv-flow.md "Test programs").

The LLM (one stage, `prepare`) reads the testbench, the test plan, the spec and the requirements (never the RTL: deny_dirs), finds for every test that loads files
where it reads them and in which format, and writes `src/sw/` (sources, scripts, a Makefile); riscv-tests in `~/.q3tui/tools` may be read and reused. It builds with
the in-process tool `build`: code runs the tool role `sw_build` (`make -C src/sw PROG_DIR=… RISCV_PREFIX=… SPIKE=…`, the toolchain of `toolchain.find`) and
answers with the errors and the files still missing. Its answer (`models.PrepareResult`) says the folder and, per test, the files it reads; code builds once more,
checks every file exists (a missing one: a small fix session with the build log, up to `rtl.max_fix_attempts` rounds) and writes `schemas/sw_manifest.json`
(per test: ready / missing / external / not built). A file only you can provide is `external`: a question, never a fix round.

Tools missing: the LLM still writes the sources; the build waits, the step warns (`q3tui act riscv_tools_setup` finds or installs them). The UVM testbench kind
has nothing to prepare. Update, not re-run: the step records the hashes of its inputs; a run with sources already there lists what changed and asks for only
that; change requests scoped to a test (`[prepare:TC-004] …`, from `simulate` when a test is blocked) name what to make."""

from __future__ import annotations

import os
from pathlib import Path

from q3tui.core.project import read_json, sha256_file, write_json
from q3tui.flows import skill_sections
from q3tui.flows.dv.prepare import toolchain
from q3tui.flows.spec.lib import artifacts
from q3tui.flows.spec.lib.base import VlsitStep
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import assign_question_ids, load_answers, split_feedback

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["SYSTEM"]
MANIFEST = "sw_manifest.json"


def _sw_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts) if d.is_dir() else []


class PrepareStep(VlsitStep):
    name = "prepare"
    title = "Test programs"
    deps = ("tb",)
    artifact = MANIFEST
    tool_roles = ("sw_build",)  # riscv_gcc / iss are optional roles: found by a search when absent (toolchain.find)

    def _plain(self, engine) -> bool:
        from q3tui.flows.dv import uvm

        return uvm.testbench_kind(uvm.tb_options(engine)) == "plain"

    def sw_dir(self, engine) -> Path:
        return engine.project.src_dir / "sw"

    # -- files -------------------------------------------------------------------------------------

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        tb = engine.by_name.get("tb")
        tb_files = [f for f in tb.outputs(engine) if f.suffix in (".sv", ".svh")] if tb is not None else []
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "test_plan.json"), *tb_files, self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        return [*_sw_files(self.sw_dir(engine)), *(f for f in (artifacts.path(engine.project, MANIFEST), self._q_paths(engine)[2]) if f.is_file())]

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        if not self._plain(engine):
            return None
        if not artifacts.path(engine.project, "selected_testplan.json").is_file():
            return "the testbench is not built yet (step tb: schemas/selected_testplan.json)"
        return None

    # -- the toolchain and the build ----------------------------------------------------------------

    def _tools(self, project):
        from q3tui.eda.base import ToolUnavailable
        from q3tui.eda.tools import load_tools

        try:
            return load_tools(project.cfg, project.root)
        except ToolUnavailable:
            return None

    def build(self, ctx: StepContext, tools, tc: toolchain.Toolchain, prog_dir: Path) -> tuple[bool, str]:
        """Run `sw_build` once: (ok, the log's end)."""
        from dataclasses import replace

        from q3tui.eda.tools import run_role

        project = ctx.project
        role = tools.roles["sw_build"]
        env = {**role.env}
        env["PATH"] = os.pathsep.join([*tc.path_dirs, env.get("PATH") or os.environ.get("PATH", "")])  # (Spike runs dtc from PATH)
        tools = replace(tools, roles={**tools.roles, "sw_build": role.model_copy(update={"env": env})})
        work = project.build_dir / "sw"
        work.mkdir(parents=True, exist_ok=True)
        prog_dir.mkdir(parents=True, exist_ok=True)
        res = run_role(tools, "sw_build", work, {"src": self.sw_dir(ctx.engine), "out": prog_dir, "riscv_prefix": tc.prefix, "spike": tc.spike or "spike",
                                                 "project": project.root, "workdir": work}, log_name="sw_build.log")
        text = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
        tail = "\n".join(text.splitlines()[-60:])
        ok = res.returncode in tools.roles["sw_build"].ok_returncodes and not res.timed_out
        return ok, (f"rc={res.returncode}{' (timed out)' if res.timed_out else ''}\n" + tail)

    @staticmethod
    def _missing(prog_dir: Path, tests: list[dict]) -> dict[str, list[str]]:
        return {t["tc_id"]: m for t in tests if (m := [f for f in t.get("files") or [] if not (prog_dir / f).is_file()])}

    # -- run -------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        import anyio

        engine, project = ctx.engine, ctx.project
        man_path = artifacts.path(project, MANIFEST)
        if not self._plain(engine):
            write_json(man_path, {"prog_dir": "", "tests": [], "note": "UVM testbench: nothing to prepare"})
            ctx.emit("log", message="prepare: the UVM testbench loads no programs")
            return
        meta = engine.state.step_meta.setdefault(self.name, {})
        tools = self._tools(project)
        tc = await anyio.to_thread.run_sync(lambda: toolchain.find(tools))
        can_build = tools is not None and "sw_build" in tools.roles and tc.ready
        why_not = ([] if tools is not None and "sw_build" in tools.roles else ["no tool role 'sw_build' in config/tools.json"]) + tc.problems
        old = read_json(man_path, None) or {}
        snap = {project.rel(f): sha256_file(f) for f in self.inputs(engine) if f.is_file()}
        changed = sorted(k for k in set(snap) | set(meta.get("inputs") or {}) if snap.get(k) != (meta.get("inputs") or {}).get(k))
        sw = self.sw_dir(engine)
        update = bool(_sw_files(sw)) and bool(old) and not ctx.regenerate
        by_unit, general = split_feedback(ctx.feedback, self.name)

        state = {"prog_dir": Path(old.get("prog_dir") or "build/tb/programs")}

        def tools_fn():
            from claude_agent_sdk import tool

            @tool("build", "Build src/sw with its Makefile into PROG_DIR (make, the RISC-V GCC and Spike) and list the errors and the files still missing. "
                           "Argument prog_dir: the folder the testbench reads, relative to the project.", {"prog_dir": str})
            async def build_tool(args: dict) -> dict:
                d = str(args.get("prog_dir") or state["prog_dir"])
                target = (project.root / d).resolve()
                if not target.is_relative_to(project.build_dir.resolve()):
                    return {"content": [{"type": "text", "text": f"prog_dir must be inside {project.rel(project.build_dir)}/ (got {d})"}]}
                state["prog_dir"] = Path(d)
                ok, log = await anyio.to_thread.run_sync(lambda: self.build(ctx, tools, tc, target))
                return {"content": [{"type": "text", "text": ("clean\n" if ok else "the build failed\n") + log}]}

            return [build_tool]

        tb_files = [project.rel(f) for f in self.inputs(engine) if f.suffix in (".sv", ".svh")]
        parts = ["# Task: " + ("update the test programs" if update else "prepare the test programs"), "",
                 f"Specification: {', '.join(project.rel(d) for d in project.spec_documents()) or 'spec/'} (read the parts you need: ISA, memory map, reset, halt)",
                 "Requirements: schemas/structured_spec.json. Test plan: schemas/test_plan.json.",
                 f"Testbench (read it: it says what each test loads, from where, in which format): {', '.join(tb_files)}",
                 f"Write in: {project.rel(sw)}/ (with a Makefile)."]
        if tc.tests:
            parts.append(f"riscv-tests checkout you may read and reuse: {tc.tests}")
        if can_build:
            parts.append(f"Toolchain: RISCV_PREFIX={tc.prefix}, SPIKE={tc.spike}. Build with the `build` tool until it says clean.")
        else:
            parts.append("The tools are not there yet (" + "; ".join(why_not) + "): write the sources and the Makefile, no `build` tool this time.")
        if update:
            parts += ["", f"`{project.rel(sw)}/` already has the programs of the last run (manifest: schemas/{MANIFEST}): change only what the changes below need."]
            if changed:
                parts += ["Inputs changed since:", *(f"- {c}" for c in changed[:40])]
        if general or by_unit:
            parts += ["", "## Change requests"] + [f"- {f}" for f in general] + [f"- {u}: {f}" for u, fs in by_unit.items() for f in fs]
        answers = load_answers(self._q_paths(engine)[1])
        if answers:
            parts += ["", "## Your answers"] + [f"- {k}: {v}" for k, v in answers.items()]
        sw.mkdir(parents=True, exist_ok=True)
        before = {project.rel(f): sha256_file(f) for f in _sw_files(sw)}
        deny = [project.rtl_dir, project.src_dir / "sva", project.model_dir, project.arch_dir, project.build_dir]  # never the RTL, nor old tool output
        add = [tc.tests] if tc.tests else []
        stage = Stage(name="prepare", system_prompt=SYSTEM, prompt="\n".join(parts), cwd=project.root, builtin_tools=["Read", "Grep", "Glob", "Write", "Edit"],
                      sdk_tools=tools_fn() if can_build else [], output_model=_result_model(), add_dirs=add, deny_dirs=deny, write_dirs=[sw],
                      max_turns=ctx.cfg.tb.max_turns)
        out = (await ctx.llm(stage)).output
        prog_rel = Path(out.prog_dir or str(state["prog_dir"]))
        prog_dir = (project.root / prog_rel).resolve()
        if not prog_dir.is_relative_to(project.build_dir.resolve()):
            raise StepFailed(f"prepare: prog_dir {out.prog_dir} is outside {project.rel(project.build_dir)}/ (the testbench's default must be under build/)")
        tests = [t.model_dump() for t in out.tests]
        missing: dict[str, list[str]] = {}
        log = ""
        if can_build:
            ok, log = await anyio.to_thread.run_sync(lambda: self.build(ctx, tools, tc, prog_dir))
            missing = self._missing(prog_dir, tests)
            for i in range(int(ctx.cfg.rtl.max_fix_attempts)):
                if ok and not missing:
                    break
                fix = ["# Task: fix the build of the test programs", "", f"prog_dir: {project.rel(prog_dir)}",
                       f"Toolchain: RISCV_PREFIX={tc.prefix}, SPIKE={tc.spike}. Build with the `build` tool until it says clean.", "",
                       "## The build" + (" failed" if not ok else ""), "```", log, "```"]
                if missing:
                    fix += ["", "## Files the tests read that the build did not make"] + [f"- {t}: {', '.join(m)}" for t, m in missing.items()]
                stage = Stage(name=f"prepare_fix{i + 1}", system_prompt=SYSTEM, prompt="\n".join(fix), cwd=project.root, builtin_tools=["Read", "Grep", "Glob", "Write", "Edit"],
                              sdk_tools=tools_fn(), output_model=_result_model(), add_dirs=add, deny_dirs=deny, write_dirs=[sw], max_turns=ctx.cfg.tb.max_turns)
                fixed = (await ctx.llm(stage)).output
                if fixed.tests:
                    tests = [t.model_dump() for t in fixed.tests]
                ok, log = await anyio.to_thread.run_sync(lambda: self.build(ctx, tools, tc, prog_dir))
                missing = self._missing(prog_dir, tests)
        rows = []
        for t in tests:
            status = ("missing" if t["tc_id"] in missing else "external" if not all((prog_dir / f).is_file() for f in t.get("external") or [])
                      else "ready" if can_build else "not built")
            rows.append({**t, "status": status, "missing": missing.get(t["tc_id"], [])})
        write_json(man_path, {"prog_dir": project.rel(prog_dir), "built": can_build, "toolchain": {"gcc": tc.gcc, "spike": tc.spike, "dtc": tc.dtc},
                              "problems": why_not if not can_build else [], "tests": rows, "summary": out.summary})
        kept = [q for q in self.questions_list(engine) if q.id in answers]
        asks = [q for q in out.questions if q.question.strip()]
        self.save_questions(engine, [*kept, *assign_question_ids(asks, kept, "PRE", taken=set(answers))])
        self.write_questions_file(engine)
        meta["inputs"] = snap
        after = {project.rel(f): sha256_file(f) for f in _sw_files(sw)}
        ctx.unchanged = after == before and old.get("tests") == rows
        n_ready = sum(r["status"] == "ready" for r in rows)
        ctx.emit("log", message=f"prepare: {n_ready} of {len(rows)} test(s) have their files in {project.rel(prog_dir)}")
        if not can_build:
            ctx.emit("warning", message="prepare: the programs are written but not built: " + "; ".join(why_not)
                                        + " — `q3tui act riscv_tools_setup` finds the tools (`install=yes`: installs them into ~/.q3tui/tools) and writes config/tools.json")
        elif missing:
            ctx.emit("warning", message="prepare: still missing after the fix rounds: " + "; ".join(f"{t}: {', '.join(m[:4])}" for t, m in missing.items()))
        ext = [f"{r['tc_id']}: {', '.join(r['external'])}" for r in rows if r["status"] == "external"]
        if ext:
            ctx.emit("warning", message="prepare: files only you can provide (put them in " + project.rel(prog_dir) + "): " + "; ".join(ext))


def _result_model():
    from q3tui.flows.rtl.lib.models import PrepareResult

    return PrepareResult
