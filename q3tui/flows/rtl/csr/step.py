"""Step `csr` of the RTL flow: the design's software-visible registers from the APB-CSR-Generator, not from hand-written RTL.

LLM (skills csr-register-map, apb-csr-tool; tool `csr_check`): drafts the register map from the spec into `src/rtl/csr/csr_map.json`. From then on the map
is yours (edit it: the step generates from it again) and the LLM is asked only to update it when the spec changed. Code: the cheap first look (a spec
that never says "register" needs no LLM), the checks the tool does not do (overlaps, duplicate / misaligned offsets, field widths, resets), `reserved`
filler for uncovered bits, the Excel workbook in the layout the tool reads, running the tool through the role `csr_gen`, installing its result
(`<rtl>/<module>.sv`, the CDC synchronizer when asynchronous) and declaring those files as *provided* (`src/rtl/csr/provided.json`): the `rtl` step
instantiates them but never writes, rule-checks or fixes them; `format` and `style_lint` leave them alone."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import anyio

from q3tui.core.project import read_json, sha256_file, sha256_json, write_json
from q3tui.flows import skill_sections
from q3tui.flows.rtl.lib import csr
from q3tui.flows.rtl.lib import skills as skillmod
from q3tui.flows.rtl.lib.base import RtlFlowStep
from q3tui.flows.rtl.lib.models import CsrMap
from q3tui.flows.rtl.lib.tools import csr_tools
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import Question, assign_question_ids, load_answers, spec_block
from q3tui.flows.spec.lib import artifacts

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["SYSTEM"]
SKILLS = ["csr-register-map", "apb-csr-tool"]


class Step(RtlFlowStep):
    name = "csr"
    title = "CSR (APB-CSR-Generator)"
    deps = ("config",)
    tool_roles = ("csr_gen",)

    # -- files -----------------------------------------------------------------------------------------------------

    def _dir(self, engine) -> Path:
        return self.rtl_dir(engine) / "csr"

    def _files(self, engine) -> dict[str, Path]:
        d = self._dir(engine)
        return {"map": d / "csr_map.json", "workbook": d / "csr.xlsx", "debug": d / "debug.json", "report": d / "csr_report.json", "provided": csr.provided_file(self.rtl_dir(engine))}

    def _generated(self, engine) -> list[Path]:
        """The RTL the tool made (what `provided.json` lists)."""
        rtl = self.rtl_dir(engine)
        return [rtl / f"{n}.sv" for n in csr.read_provided(rtl) if (rtl / f"{n}.sv").is_file()]

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        f = self._files(engine)["map"]
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "final_config.json"), *([f] if f.is_file() else []),
                self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        return [*self._generated(engine), *[f for k, f in self._files(engine).items() if f.is_file()]]

    def reset_files(self, engine) -> list[Path]:  # (the map is yours: a reset keeps it; --regenerate drafts it again)
        keep = self._files(engine)["map"]
        return [f for f in self.outputs(engine) if f != keep] + super().reset_files(engine)

    def mode(self) -> str:
        return str(self.options.get("mode") or "auto")

    def tool(self, engine) -> Path | None:
        role = self.tools(engine).roles.get("csr_gen")
        return csr.find_tool(engine.project.root, self.options.get("tool_dir"), (role.env or {}).get("CSR_GEN_HOME") if role else None)

    def _wants_tool(self, engine) -> bool:
        m = read_json(self._files(engine)["map"], None)
        return self.mode() == "on" or bool(m and m.get("has_registers", True) and m.get("registers"))

    def missing_input(self, engine) -> str | None:
        p = engine.project
        if not artifacts.path(p, "structured_spec.json").is_file() or not artifacts.path(p, "final_config.json").is_file():
            return "the parsed spec and the configuration are needed first (steps parse and config)"
        if self.mode() != "off" and self._wants_tool(engine):
            if self.tool(engine) is None:
                return ("the APB-CSR-Generator is not installed: `q3tui act csr_tool_install` clones " + csr.REPO + " (or set options.tool_dir, env.CSR_GEN_HOME of the csr_gen role in tools.json, or $CSR_GEN_HOME "
                        "to its folder); mode: off skips this step")
            return self.tool_problem(engine, "csr_gen")
        return None

    # -- the map ---------------------------------------------------------------------------------------------------

    def _snapshot(self, engine) -> dict:
        p = engine.project
        spec = artifacts.read(p, "structured_spec.json") or {}
        return {"spec": {p.rel(f): sha256_file(f) for f in p.spec_documents()},
                "modules": [{"name": m.get("name"), "description": m.get("description"), "reqs": m.get("req_ids")} for m in spec.get("modules") or [] if isinstance(m, dict)],
                "top": (spec.get("metadata") or {}).get("top_module"), "answers": load_answers(self._q_paths(engine)[1])}

    def _prompt(self, ctx: StepContext, snap: dict, current: CsrMap | None, changed: bool, problems: list[str], index: str) -> str:
        engine, project = ctx.engine, ctx.project
        spec = artifacts.read(project, "structured_spec.json") or {}
        config = artifacts.read(project, "final_config.json") or {}
        block, _ = spec_block(project)
        reqs = "\n".join(f"- {r['req_id']}: {r.get('text', '')}" for r in spec.get("requirements", []) if r.get("req_id"))
        mods = "\n".join(f"- {m['name']}: {m.get('description') or ''} (REQ {', '.join(m.get('reqs') or [])})" for m in snap["modules"] if m.get("name"))
        params = "\n".join(f"- {k} = {(v or {}).get('value')}" for k, v in (config.get("parameters") or {}).items())
        parts = ["# Task: give the register map of this design for the APB-CSR-Generator", "", "## Specification", block, "", "## Requirements", reqs or "(none)", "",
                 f"## Module map of the design (top: {snap['top']})", mods or "(none: name the module `<top>_csr`)", "", "## Parameters", params or "(none)"]
        if snap["answers"]:
            parts += ["", "## Your earlier answers (settled)", *[f"- {k}: {v}" for k, v in snap["answers"].items()]]
        if current is not None:
            parts += ["", "## The register map as it is now" + (" (the spec changed since: update only what the changes require, keep what the user edited)" if changed else ""),
                      "```json", current.model_dump_json(indent=1), "```"]
        if ctx.feedback:
            parts += ["", "## Change requests", *[f"- {f}" for f in ctx.feedback]]
        if problems:
            parts += ["", "## Problems with your previous answer (fix them)", *[f"- {x}" for x in problems]]
        return "\n".join([*parts, "", index])

    async def _draft(self, ctx: StepContext, snap: dict, current: CsrMap | None, changed: bool) -> CsrMap:
        engine, project = ctx.engine, ctx.project
        ts, work = self.tools(engine), self.workdir(engine)
        skills = skillmod.discover(project.config_dir, engine.flow.path, SKILLS)
        index = skillmod.index(skills, project.root)
        problems: list[str] = []
        for attempt in range(3):
            stage = Stage(name="csr_map" if not attempt else "csr_map_fix", system_prompt=SYSTEM, prompt=self._prompt(ctx, snap, current, changed, problems, index),
                          cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], sdk_tools=csr_tools(ts, self.tool(engine), work), add_dirs=skillmod.add_dirs(skills),
                          output_model=CsrMap, deny_dirs=[project.model_dir, project.build_dir, self.rtl_dir(engine)])
            m: CsrMap = (await ctx.llm(stage)).output
            if not m.has_registers:
                return m
            if not m.module_name:
                m.module_name = f"{snap['top'] or 'design'}_csr"
            csr.complete(m)
            problems = csr.validate(m)
            if not problems:
                return m
        raise StepFailed("the register map has problems after 3 tries: " + "; ".join(problems[:6]) + f" (edit {engine.project.rel(self._files(engine)['map'])} and run again)")

    # -- run -------------------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        f = self._files(engine)
        f["map"].parent.mkdir(parents=True, exist_ok=True)
        meta = engine.state.step_meta.setdefault(self.name, {})
        snap = self._snapshot(engine)
        text = "\n".join(p.read_text(errors="replace") for p in project.spec_documents() if p.suffix.lower() in (".md", ".txt", ".rst", ".adoc"))
        current: CsrMap | None = None
        if f["map"].is_file():
            try:
                current = CsrMap.model_validate(read_json(f["map"], {}))
            except Exception as exc:  # noqa: BLE001
                raise StepFailed(f"{project.rel(f['map'])} is not a register map ({str(exc).splitlines()[0]}): fix it, or delete it to have it drafted again") from exc
        if self.mode() == "off":
            self._none(ctx, "mode: off")
            return
        if self.mode() == "auto" and current is None and not csr.mentions_registers(text):
            write_json(f["map"], CsrMap(has_registers=False, summary="the spec names no register").model_dump(exclude={"questions"}))
            self._none(ctx, "the spec names no register")
            meta["snapshot"] = snap
            return
        spec_changed = meta.get("snapshot") != snap
        if ctx.regenerate or current is None or spec_changed or ctx.feedback:
            old = None if ctx.regenerate else current
            m = await self._draft(ctx, snap, old, spec_changed and old is not None)
            answers = load_answers(self._q_paths(engine)[1])
            kept = [q for q in self.questions_list(engine) if q.id in answers]
            self.save_questions(engine, [*kept, *assign_question_ids(m.questions, kept, "CS", taken=set(answers))])
            write_json(f["map"], m.model_dump(exclude={"questions"}))
            meta["snapshot"] = snap
            current = m
        if not current.has_registers:
            self._none(ctx, current.summary or "the spec has no software-visible registers")
            return
        csr.complete(current)
        problems = csr.validate(current)
        if problems:  # (your edit of the map, or an old one): the tool would produce broken RTL silently
            raise StepFailed(f"{project.rel(f['map'])}: " + "; ".join(problems[:8]))
        await self._generate(ctx, current, snap)

    def _none(self, ctx: StepContext, why: str) -> None:
        """No registers: nothing provided (a module the tool made before goes, backed up)."""
        engine = ctx.engine
        f = self._files(engine)
        old = self._generated(engine)
        if old:
            ctx.project.backup(old)
            for x in old:
                x.unlink()
        if f["provided"].is_file():
            f["provided"].unlink()
        ctx.emit("log", message=f"csr: nothing to generate ({why})")

    async def _generate(self, ctx: StepContext, m: CsrMap, snap: dict) -> None:
        engine, project = ctx.engine, ctx.project
        f, rtl = self._files(engine), self.rtl_dir(engine)
        tool = self.tool(engine)
        if tool is None:
            raise StepFailed(self.missing_input(engine) or "the APB-CSR-Generator is not installed")
        ts, work = self.tools(engine), self.workdir(engine) / "gen"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        wb = csr.write_workbook(m, work / "csr.xlsx")
        res = await anyio.to_thread.run_sync(lambda: csr.run_tool(ts, tool, wb, work))
        sv = work / "RTL" / f"{m.module_name}.sv"
        if not res.ok or not sv.is_file():
            tail = Path(res.log_path).read_text(errors="replace")[-1200:] if res.log_path else res.summary
            raise StepFailed(f"the APB-CSR-Generator did not produce {m.module_name}.sv (see {project.rel(Path(res.log_path)) if res.log_path else 'its log'}):\n{tail}")
        top = m.parent or snap["top"] or ""
        files: dict[str, str] = {m.module_name: self._with_header(sv.read_text(), m)}
        provided = {m.module_name: {"kind": "csr", "parent": top, "req_ids": m.req_ids, "instances": [csr.SYNC_MODEL] if m.asynchronous else []}}
        if m.asynchronous:
            files[csr.SYNC_MODEL] = (work / "RTL" / "models" / f"{csr.SYNC_MODEL}.sv").read_text()
            provided[csr.SYNC_MODEL] = {"kind": "model", "parent": m.module_name, "req_ids": [], "instances": []}
        rtl.mkdir(parents=True, exist_ok=True)
        written, dropped = [], []
        for name, text in files.items():
            dest = rtl / f"{name}.sv"
            if not dest.is_file() or csr.stable(dest.read_text()) != csr.stable(text):  # (the tool's header has the user and the time: not a change)
                project.backup([dest])
                dest.write_text(text)
                written.append(dest.name)
        for gone in set(csr.read_provided(rtl)) - set(provided):  # a module an earlier map made (another name, or no more synchronizer)
            x = rtl / f"{gone}.sv"
            if x.is_file():
                project.backup([x])
                x.unlink()
                dropped.append(x.name)
        write_json(f["provided"], provided)
        shutil.copy2(wb, f["workbook"])
        if (work / "Debug" / "debug.json").is_file():
            shutil.copy2(work / "Debug" / "debug.json", f["debug"])
        write_json(f["report"], {"module": m.module_name, "parent": top, "asynchronous": m.asynchronous, "registers": [r.name for r in m.registers],
                                 "fields": sum(len(r.fields) for r in m.registers), "files": sorted(files), "tool": str(tool),
                                 "ports": csr.port_summary(files[m.module_name], m.module_name), "req_ids": m.req_ids})
        ctx.emit("log", message=f"csr: {m.module_name} generated from {len(m.registers)} register(s)" + (f" ({', '.join(written)} written)" if written else " (unchanged)")
                                + (f"; removed {', '.join(dropped)}" if dropped else ""))
        ctx.unchanged = not written and not dropped

    def _with_header(self, text: str, m: CsrMap) -> str:
        lines = ["// Generated by the APB-CSR-Generator from src/rtl/csr/csr_map.json (Q3TUI step csr): do not edit; change the map or the spec.",
                 *([f"// REQ-IDs: {', '.join(m.req_ids)}"] if m.req_ids else [])]
        return "\n".join(lines) + "\n" + text
