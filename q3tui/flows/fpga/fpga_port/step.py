"""FPGA step `fpga_port`: the RTL made ready for the chosen part (`schemas/fpga_port.json`, `src/fpga/`); every problem of the synthesis comes back here.

Deterministic first: the scan (`lib/port.py`) runs always and is ground truth: the memory initialization files the RTL names (missing, or
in an attribute this part's tool ignores), another vendor's primitives. The porter (one LLM stage with the playbook skill `fpga-port` and
in-process tools) does the porting when there is something to do: a finding of the scan, a change request (fpga_synth hands every failed
synthesis here, timing_util_report a result that is not the design), or an answer of yours. It makes what the FPGA flow owns: the memory
images in `src/fpga/` (`mem_image`), the synthesis setup (`synth_setting`); what the RTL has to change becomes a work order for the flow that
owns the RTL (`handoff_flow`, default rtl: handed at once; an unattended run runs that flow in its fix loop); what is yours to decide
becomes a question (FP<n>, blocking, with the options). Nothing new: no LLM, the last result stands."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import sha256_json
from q3tui.flows import skill_sections
from q3tui.flows.fpga.lib import port, templates
from q3tui.flows.fpga.lib.base import FpgaStep
from q3tui.flows.rtl.lib import skills as skillmod
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import StepQuestions, assign_question_ids, load_answers, scoped, spec_block

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["PORT"]
FPGA_FLOW_FILE = Path(__file__).parents[1] / "FLOW.md"  # (the skill is found next to the fpga flow whichever flow file runs this step)


class Step(StepQuestions, FpgaStep):
    name = "fpga_port"
    title = "FPGA port"
    deps = ()
    hashed_options = ("handoff_flow",)

    def inputs(self, engine) -> list[Path]:
        d = engine.project.fpga_src_dir
        return [self.filelist(engine), *self.rtl_files(engine), *(sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []),
                self._q_paths(engine)[1]]  # (an answer of yours is new information for the porter)

    def outputs(self, engine) -> list[Path]:
        p = self.artifact(engine, port.PORT_JSON)
        d = engine.project.fpga_src_dir  # (the memory images it makes: in a lane they become its own copies before it writes, and a merge takes them)
        return [*([p] if p.is_file() else []), *(sorted(f for f in d.rglob("*") if f.is_file()) if d.is_dir() else [])]

    def reset_files(self, engine) -> list[Path]:
        p = self.artifact(engine, port.PORT_JSON)  # (not the images: you may have edited them)
        return [*([p] if p.is_file() else []), *(q for q in self._q_paths(engine) if q.is_file())]

    def missing_input(self, engine) -> str | None:
        return self.missing_rtl(engine)

    def _synth_options(self, engine) -> dict:
        s = engine.by_name.get("fpga_synth")
        return s.options if s is not None else (engine.project.cfg.pipeline.step_options.get("fpga_synth") or {})

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        t, device, note = templates.resolve(self._synth_options(engine))
        if t is None:
            raise StepFailed(note)
        files = self.rtl_files(engine)
        found = port.scan(project, files, t)
        prev = self.read(engine, port.PORT_JSON, {}) or {}
        answers = load_answers(self._q_paths(engine)[1])
        decisions = {q.id: (q.question, answers[q.id]) for q in self.questions_list(engine) if q.id in answers}
        new = {k: v for k, v in decisions.items() if (prev.get("decisions") or {}).get(k) != v[1]}
        key = sha256_json({"scan": [x.to_dict() for x in found], "family": t.id, "device": device})
        report = {"part": device, "family": t.id, "tool": t.tool, "scan": [x.to_dict() for x in found], "key": key,
                  "decisions": {k: v[1] for k, v in decisions.items()}}
        requests = [f for f in ctx.feedback if not f.startswith(port.PART_CHANGED)]
        if not (requests or new or ctx.regenerate or (found and prev.get("key") != key)):
            # nothing new: the porter is not asked again (its last result, and the findings it left, stand)
            self.write(engine, port.PORT_JSON, {**{k: prev[k] for k in ("summary", "done", "orders", "handed", "requests") if k in prev}, **report})
            ctx.unchanged = prev.get("key") == key
            ctx.emit("log", message=f"fpga_port: {len(found)} finding(s) for {t.family} {device}" + (" — nothing new for the porter" if found else ""))
            return
        if not found and not requests and not new:
            self.write(engine, port.PORT_JSON, {**report, "summary": f"nothing to port for {t.family} {device}"})
            ctx.emit("log", message=f"fpga_port: nothing to port for {t.family} {device}")
            return
        done: list[str] = []
        skills = skillmod.discover(project.config_dir, FPGA_FLOW_FILE, ["fpga-port"])
        stage = Stage(name="fpga_port", system_prompt=SYSTEM, cwd=project.root, builtin_tools=["Read", "Grep", "Glob", "Write", "Edit"],
                      prompt=self._prompt(ctx, t, device, found, requests, decisions, prev, skillmod.index(skills, project.root)),
                      sdk_tools=port.tools(engine, project, t, lambda: self.rtl_files(engine), done), add_dirs=skillmod.add_dirs(skills),
                      output_model=port.PortResult, write_dirs=[project.fpga_src_dir], deny_dirs=[project.src_dir / "sva", project.model_dir])
        project.fpga_src_dir.mkdir(parents=True, exist_ok=True)
        try:
            res: port.PortResult = (await ctx.llm(stage)).output
        except StepFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - no credentials, a stage that failed: said, with what the scan found
            raise StepFailed(f"fpga_port: the porter could not run ({str(exc).splitlines()[0] if str(exc) else type(exc).__name__}); the scan found:\n"
                             + port.show(found)) from exc
        kept = [q for q in self.questions_list(engine) if q.id in answers]
        asked = assign_question_ids(res.questions, kept, "FP", taken=set(answers))
        self.save_questions(engine, [*kept, *asked])
        self.write_questions_file(engine)
        modules = {f.stem for f in files}
        orders = [o for o in res.orders if o.module in modules and o.change.strip()]
        owner, handed = str(self.options.get("handoff_flow") or "rtl"), []
        for o in orders:
            owner = port.hand_to_flow(ctx, owner, scoped("rtl", o.module, f"FPGA port ({t.family} {device}, {t.tool}): {o.change} Why: {o.why} "
                                                                       "Keep the behaviour, the ports and every requirement tag; lint stays clean."))
            handed.append(f"{o.module} → {owner}")
            ctx.emit("log", message=f"fpga_port: RTL change → {owner}:rtl ({o.module}): {o.change[:120]}")
        after = port.scan(project, self.rtl_files(engine), t)  # (what is left once the porter acted: the next run compares with it)
        report.update(scan=[x.to_dict() for x in after], key=sha256_json({"scan": [x.to_dict() for x in after], "family": t.id, "device": device}))
        self.write(engine, port.PORT_JSON, {**report, "summary": res.summary, "done": done or res.done, "notes": res.done if done else [],
                                            "orders": [o.model_dump() for o in orders], "handed": handed, "requests": requests,
                                            "questions": [q.id for q in asked]})
        for d in done:
            ctx.emit("log", message=f"fpga_port: {d}")
        ctx.emit("log", message=f"fpga_port: {res.summary}")
        if handed:
            if engine.unattended_run():
                engine.hand_to_flow(self.name, owner)  # (the fix loop runs that flow, then this one again)
                return
            raise StepFailed(f"fpga_port: RTL change(s) for this part handed to the {owner} flow ({', '.join(handed)}): run it "
                             f"(q3tui --flow {owner} run), then this flow again. {res.summary}")
        if requests and not (done or asked):
            raise StepFailed(f"fpga_port: nothing the port can change for: {requests[-1][:400]}\nThe porter: {res.summary}")

    def _prompt(self, ctx: StepContext, t, device: str, found: list, requests: list[str], decisions: dict, prev: dict, index: str) -> str:
        engine, project = ctx.engine, ctx.project
        block, _ = spec_block(project)
        wanted = [x.code for x in found] + [n for _f, _l, n, _r in port.named_inits(self.rtl_files(engine), t)]
        have = sorted(project.rel(p) for p in project.fpga_src_dir.rglob("*") if p.is_file()) if project.fpga_src_dir.is_dir() else []
        o = self._synth_options(engine)
        parts = ["# Task: port the RTL to this FPGA part", "",
                 f"Part {device}: {t.vendor} {t.family}, synthesized by {t.tool}. Top `{engine.by_name['fpga_synth'].top(engine) if 'fpga_synth' in engine.by_name else '?'}`; "
                 f"synthesis setup: defines {o.get('defines') or {}}, synth_args {o.get('synth_args') or []}.", "",
                 "## What the scan found (code, ground truth)", port.show(found), "",
                 f"## Problems handed to you ({len(requests)})", *(f"- {r}" for r in requests), *([] if requests else ["(none)"]), "",
                 "## Your decisions (the user's answers: settled, act on them)", *(f"- {k}: {q} → {a}" for k, (q, a) in decisions.items()),
                 *([] if decisions else ["(none)"]), "",
                 f"## The FPGA flow's own sources ({project.rel(project.fpga_src_dir)}/: you write here)", *(f"- {p}" for p in have), *([] if have else ["(empty)"]), "",
                 "## Memory images in the project (the ones named like the RTL's files first)", *(port.images(project, wanted) or ["(none)"]), ""]
        if prev.get("done") or prev.get("orders"):
            parts += ["## Done in the last port (do not repeat what did not help: say why it did not)", *(f"- {d}" for d in prev.get("done") or []),
                      *(f"- order {x['module']}: {x['change']}" for x in prev.get("orders") or []), ""]
        parts += [f"## The RTL (read what you need)", ", ".join(project.rel(f) for f in self.rtl_files(engine)[:80]), "", "## Specification", block, "", index]
        return "\n".join(parts)
