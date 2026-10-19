"""VLSIT step `doc` (Phase 6, spec_pdf_generator): `docs/specification.md` (+ PDF) from the artifacts and the RTL.

Code assembles everything (docgen.py); the optional `doc_prose` stage (read-only, `options.prose`, default on) writes only
the introduction and a few notes. PDF: tool role `pdf` of tools.json ({md} {pdf} {docs}); without it the Markdown is the
deliverable (`options.pdf_builtin: true` uses the bundled Matplotlib converter)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import spec_block
from q3tui.steps.vlsit import artifacts
from q3tui.flows.vlsit.doc import gen as docgen
from q3tui.steps.vlsit.base import VlsitStep

from pathlib import Path as _Path

from q3tui.flows import skill_sections as _skill_sections

# the stage system prompts are the named sections of md/skill.md
TEXT = _skill_sections(_Path(__file__).parent / "md" / "skill.md")

SYSTEM = TEXT["SYSTEM"]


class DocProse(BaseModel):
    introduction: str = Field("", description="1.1 Introduction: what the IP is, its interfaces and use cases (Markdown, 1-3 paragraphs)")
    microarchitecture: str = Field("", description="Short architecture overview: how the modules cooperate (Markdown)")
    clock_reset_notes: str = Field("", description="Reset behaviour / clock domain notes stated by the spec (Markdown)")
    register_map: str = Field("", description="Register map as a Markdown table, ONLY if the spec defines registers")


class DocStep(VlsitStep):
    name = "doc"
    kind = "vlsit_doc"
    title = "Specification document"
    deps = ("verify",)
    tool_roles = ()  # `pdf` is optional

    def _md(self, engine) -> Path:
        return self.lay(engine).docs / "specification.md"

    def _pdf(self, engine) -> Path:
        return self.lay(engine).docs / "specification.pdf"

    def inputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        files = [lay.artifact(n) for n in ("structured_spec.json", "final_config.json", "rtm.json", "verification_report.json", "synth_report.json")]
        rtl = sorted(lay.rtl.glob("*.sv")) if lay.rtl.is_dir() else []
        return [*files, *rtl, *engine.project.spec_documents()]

    def outputs(self, engine) -> list[Path]:
        return [p for p in (self._md(engine), self._pdf(engine)) if p.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return [self._md(engine), self._pdf(engine)]

    def missing_input(self, engine) -> str | None:
        if not self.lay(engine).artifact("structured_spec.json").is_file():
            return "the requirements (structured_spec.json, step parse) are needed first"
        return None

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        spec = artifacts.read(project, "structured_spec.json")
        if not spec:
            raise StepFailed("schemas/structured_spec.json is missing or unreadable")
        prose = None
        if self.options.get("prose", True):
            block, needs_tools = spec_block(project)
            reqs = "\n".join(f"- {r['req_id']} ({', '.join(r['rtl_modules'])}): {' '.join(r['text'].split())}" for r in spec["requirements"])
            mods = "\n".join(f"- {m['name']}: {m.get('description', '')}" for m in spec["modules"])
            prompt = f"IP `{spec['metadata']['ip_name']}`, top `{spec['metadata']['top_module']}`.\n\nModules:\n{mods}\n\nRequirements:\n{reqs}\n\n{block}"
            prose = (await ctx.llm(Stage(name="doc_prose", system_prompt=SYSTEM, prompt=prompt, cwd=project.spec_dir,
                                         builtin_tools=["Read", "Grep", "Glob"] if needs_tools else [], output_model=DocProse))).output
        text = docgen.build(engine, prose)
        md = self._md(engine)
        md.parent.mkdir(parents=True, exist_ok=True)
        if md.is_file():
            project.backup([md])
        md.write_text(text)
        ctx.emit("log", message=f"wrote {project.rel(md)} ({len(text.splitlines())} lines)")
        self._pdf_out(ctx, md)

    def _pdf_out(self, ctx: StepContext, md: Path) -> None:
        from q3tui.eda import tools as toolmod
        from q3tui.eda.base import ToolUnavailable

        engine, pdf = ctx.engine, self._pdf(ctx.engine)
        try:
            ts = toolmod.load_tools(ctx.cfg, ctx.project.root)
            if toolmod.available(ts, "pdf"):
                res = toolmod.run_role(ts, "pdf", md.parent, {"md": md, "pdf": pdf, "docs": md.parent})
                ctx.emit("log" if res.ok else "warning", message=f"pdf: {res.summary}" + ("" if res.ok else f" — see {res.log_path}"))
                return
        except ToolUnavailable as exc:
            ctx.emit("warning", message=f"pdf tool: {exc}")
        if self.options.get("pdf_builtin"):
            proc = subprocess.run([sys.executable, str(Path(__file__).parent / "scripts" / "md_to_pdf.py"), str(md), str(pdf)], capture_output=True, text=True)
            ctx.emit("log" if proc.returncode == 0 else "warning",
                     message="pdf: built with the bundled converter" if proc.returncode == 0 else f"pdf: bundled converter failed: {proc.stderr[-300:]}")
            return
        ctx.emit("log", message="no `pdf` tool configured (tools.json): the Markdown is the deliverable")
