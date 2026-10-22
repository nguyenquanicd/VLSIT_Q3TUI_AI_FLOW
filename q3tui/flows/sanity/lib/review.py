"""The waiver review of the Sanity flow (`<role>_waivers`): one decision per violation (`schemas/<role>_waivers.json`).

Deterministic first: the report is ground truth (code reads it, code writes the waivers). One read-only LLM stage per batch of violations reads
the RTL, the SDC and the spec, applies the knowledge (`<step>/md/knowledge.md`, the project's `config/sanity/<role>_knowledge.md`) and decides each one:

  waive   safe, with the evidence cited                      → a `waive_*` command (the `<role>_check` step applies it)
  fix_rtl the RTL is unprotected                              → a scoped change request for the owner flow's `rtl` step (`sanity_handoff`)
  spec    the spec owner has to decide                        → a change request for its `spec` step
  sdc     a constraint is missing or wrong in the SDC         → a request to YOU (the SDC is yours): the constraint to add, a question
  ask     a condition cannot be verified                      → a question to you, with suggested answers

Changes propagate as updates: a decision stays while the RTL, the SDC and the knowledge it was made on are unchanged; only new violations and
the ones waiting for you (a new answer) go to the LLM again."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import read_json, sha256_json, write_json
from q3tui.flows.sanity.lib import memory, table, waivers
from q3tui.flows.sanity.lib.base import SanityStep, report_json, waivers_json
from q3tui.flows.sanity.lib.waivers import Decision, Review
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext

BATCH = 40  # violations per LLM stage (option `batch`)
SDC_OPTIONS = [{"label": "I will update the SDC", "description": "you change constraints/sdc/ (sdc_add / sdc_set, the SDC tab); the analysis runs again"},
               {"label": "No SDC change: decide again", "description": "the review looks again, with this answer, for a design reason"}]


class WaiverStep(SanityStep):
    system = ""  # the system prompt (the step's md/skill.md)
    skill_path = Path()  # the step folder (md/knowledge.md is the flow's built-in knowledge)

    # -- files ------------------------------------------------------------------------------------

    def knowledge_file(self) -> Path:
        return self.skill_path / "md" / "knowledge.md"

    def project_knowledge(self, engine) -> Path:
        return engine.project.config_dir / "sanity" / f"{self.role}_knowledge.md"

    def answers_file(self, engine) -> Path:
        return engine.project.reviews_dir / f"{self.role}_answers.json"

    def answers(self, engine) -> dict[str, str]:
        return read_json(self.answers_file(engine), {}) or {}

    def inputs(self, engine) -> list[Path]:
        extra = [f for f in (self.knowledge_file(), self.project_knowledge(engine), self.answers_file(engine)) if f.is_file()]
        return [self.artifact(engine, report_json(self.role)), *self.rtl_files(engine), *self.sdc_files(engine), *engine.project.spec_documents(), *extra]

    def outputs(self, engine) -> list[Path]:
        return [p for p in (self.artifact(engine, waivers_json(self.role)), *self.table_files(engine)) if p.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)  # (your answers and your knowledge are yours: a reset keeps them)

    def missing_input(self, engine) -> str | None:
        if not self.artifact(engine, report_json(self.role)).is_file():
            return f"the analysis is needed first (step {self.role})"
        return None

    def plan(self, engine) -> dict:
        return self.read(engine, waivers_json(self.role), {}) or {}

    def items(self, engine) -> list[dict]:
        return self.plan(engine).get("items", [])

    # -- the review -------------------------------------------------------------------------------

    def _knowledge_hash(self, engine) -> str:
        return sha256_json(waivers.knowledge(self.knowledge_file(), engine.project.config_dir, self.role))[:12]

    def _applied(self, engine) -> str:
        """What the analysis script adds after `read_sdc` (clock groups, resets, your extra Tcl): the SDC files alone do not show it."""
        f = self.setup_script(engine)
        if not f.is_file():
            return ""
        lines = f.read_text().splitlines()
        start = next((i for i, x in enumerate(lines) if x.startswith("read_sdc")), None)
        end = next((i for i, x in enumerate(lines) if x.startswith("check_")), None)
        return "\n".join(lines[start + 1:end]) if start is not None and end is not None else ""

    def _prompt(self, engine, batch: list[dict], answers: dict[str, str], modules: list[str], prev_by_sig: dict | None = None) -> str:
        role = self.role
        lines = [f"## {role.upper()} violations to decide (the tool's report: ground truth)",
                 f"Top `{self.top(engine)}`; RTL modules (src/rtl/<module>.sv): {', '.join(modules)}.",
                 "SDC: " + ", ".join(f"`{engine.project.rel(f)}`" for f in self.sdc_files(engine)) + "; the spec: "
                 + ", ".join(f"`{engine.project.rel(f)}`" for f in engine.project.spec_documents()) + "\n"]
        applied = self._applied(engine)
        if applied:
            lines += ["The tool run adds these after the SDC (`" + engine.project.rel(self.setup_script(engine)) + "`; they are part of the constraints, "
                      "do not ask for them in the SDC):", "```tcl", applied, "```\n"]
        for v in batch:
            fields = "; ".join(f"{k}={str(x)[:160]}" for k, x in (v.get("fields") or {}).items())
            lines.append(f"- **{v['id']}** {v['severity']} `{v['tag']}`: {v['message'][:300]}" + (f"\n  fields: {fields}" if fields else ""))
            sig = waivers.sig_id(role, v)
            if answers.get(sig):
                lines.append(f"  The user answered: {answers[sig]} (settled: follow it, do not ask again)")
            lines += memory.tried_lines(engine.project, sig)
            p = (prev_by_sig or {}).get(sig)
            if p:  # the same violation was decided before the RTL / SDC changed: the decision is the starting point, not a blank page
                lines.append(f"  Your earlier decision for it: {p['action']}"
                             + (f" ({p.get('rule')})" if p.get("rule") else "") + f" — {(p.get('reason') or '')[:260]}. The RTL or SDC changed since: "
                             "check the evidence again and keep the decision if it still holds; change it only for a reason you can cite.")
        lines += ["", waivers.knowledge(self.knowledge_file(), engine.project.config_dir, role)]
        return "\n".join(lines)

    def _ask_repeated(self, d: Decision, v: dict, tries: list[dict]) -> Decision:
        last = tries[-1]
        return Decision(id=d.id, action="ask", reason=f"{len(tries)} RTL / spec request(s) were handed off for this violation and it is still open. "
                        f"Last: {last['kind']}{' in ' + last['module'] if last.get('module') else ''}: {last['text'][:200]}. The review wanted: {d.change or d.reason}"[:600],
                        question=f"{v['tag']} is still open after {len(tries)} hand-off(s) (last: {last['text'][:140]}). How should it be settled?",
                        options=["Decide it in the spec: say what the design must do", "Waive it: give the design reason", "Try another RTL fix: give a hint"])

    def _validate(self, engine, out: Review, batch: list[dict], modules: list[str], answers: dict[str, str]) -> list[dict]:
        by_id = {d.id: d for d in out.decisions}
        items = []
        for v in batch:
            d = by_id.get(v["id"]) or Decision(id=v["id"], action="ask", reason="the review gave no decision for this violation",
                                               question=f"The review made no decision for {v['tag']}: {v['message'][:120]}. How should it be handled?")
            if d.action == "fix_rtl" and d.module not in modules:
                d = Decision(id=d.id, action="spec", reason=d.reason, change=f"{d.change} (the review named the module '{d.module}', which is not in the RTL)")
            if d.action == "fix_rtl" and memory.conflicts_with_requirement(d.reason, d.change):
                # its own text says the change goes against a requirement: that is the spec's decision (or yours), never a silent RTL change
                d = Decision(id=d.id, action="spec", reason=d.reason, change=f"{d.change} (an RTL change here would go against a requirement the reason cites: decide the requirement first)")
            if d.action in ("fix_rtl", "spec"):
                tries = memory.repeated(engine.project, waivers.sig_id(self.role, v), d.action, d.change or d.reason)
                if tries:
                    d = self._ask_repeated(d, v, tries)
            if d.action == "waive" and not d.reason.strip():
                d = Decision(id=d.id, action="ask", reason="a waiver needs its evidence and none was given", question=f"Waive {v['tag']}: {v['message'][:120]}?")
            if d.action == "waive" and waivers.waive_command(v, {"id": v["id"], **d.model_dump()}, self.role) is None:
                d = Decision(id=d.id, action="ask", reason=f"waivable by the review ({d.reason[:200]}), but the tool's report does not say which objects it is "
                             "about, so no waive command can be written", question=f"{v['tag']} cannot be waived by command (no objects in the report). Accept it as open?",
                             options=["Yes, accept it as open", "No, fix the RTL"])
            if d.action in waivers.OPEN_ASKING and not d.question.strip():
                d.question = f"{v['tag']}: {d.sdc_request or d.reason}"[:300]
            items.append(self._item(v, d.model_dump(), answers))
        return items

    def _item(self, v: dict, d: dict, answers: dict[str, str]) -> dict:
        sig = waivers.sig_id(self.role, v)
        return {**d, "id": v["id"], "sig": sig, "answer": answers.get(sig, ""), "handed_off": d.get("handed_off")}

    async def run(self, ctx: StepContext) -> None:
        engine, project, role = ctx.engine, ctx.project, self.role
        report = self.read(engine, report_json(role), {})
        violations = report.get("violations", [])
        prev = self.plan(engine)
        answers = self.answers(engine)
        rtl, sdc, kn = self.rtl_hash(engine), self.sdc_hash(engine), self._knowledge_hash(engine)
        same = prev.get("rtl") == rtl and prev.get("sdc") == sdc and prev.get("knowledge") == kn and not ctx.regenerate
        prev_by_sig = {i["sig"]: i for i in prev.get("items", [])}
        kept: dict[str, dict] = {}
        todo = []
        for v in violations:
            p = prev_by_sig.get(waivers.sig_id(role, v))
            if p and same and (p["action"] not in waivers.OPEN_ASKING or p.get("answer") == answers.get(p["sig"], "")):
                kept[v["id"]] = self._item(v, p, answers)
            else:
                todo.append(v)
        modules = sorted(f.stem for f in self.rtl_files(engine))
        decided: dict[str, dict] = {}
        size = int(self.options.get("batch") or BATCH)
        for n in range(0, len(todo), size):
            batch = todo[n:n + size]
            stage = Stage(name=f"{role}_waivers_{n // size + 1}", system_prompt=self.system, prompt=self._prompt(engine, batch, answers, modules, prev_by_sig),
                          cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], output_model=Review,
                          deny_dirs=[project.src_dir / "tb", project.src_dir / "sva", project.tb_dir, project.model_dir])
            out: Review = (await ctx.llm(stage)).output
            for it in self._validate(engine, out, batch, modules, answers):
                old = prev_by_sig.get(it["sig"])
                it["handed_off"] = (old or {}).get("handed_off") if old and old["action"] == it["action"] and old.get("change") == it.get("change") else None
                decided[it["id"]] = it
        items = [kept.get(v["id"]) or decided[v["id"]] for v in violations]
        plan = {"role": role, "top": report.get("top"), "rtl": rtl, "sdc": sdc, "knowledge": kn, "items": items}
        ctx.unchanged = not todo and prev.get("items") == items and bool(prev)
        write_json(self.artifact(engine, waivers_json(role)), plan)
        table.write(self.table_files(engine), role, report, plan)
        n_waive = sum(i["action"] == "waive" for i in items)
        asking = [i["id"] for i in items if i["action"] in waivers.OPEN_ASKING and not i["answer"] and not i.get("handed_off")]
        sent = sum(i["action"] in ("fix_rtl", "spec") for i in items)
        ctx.emit("log", message=f"{role.upper()} review: {len(items)} violation(s): {n_waive} waived, {sent} for the RTL / spec, "
                                f"{sum(i['action'] == 'sdc' for i in items)} SDC request(s), {len(asking)} question(s) for you"
                                + (f" ({len(todo)} decided now, {len(kept)} kept)" if kept else "") + f"; table: {project.rel(self.table_files(engine)[0])}")

    # -- questions (to you): the decisions `ask` and `sdc` -----------------------------------------

    def _asking(self, engine) -> list[dict]:
        """The decisions that wait for you, each with your latest answer (the answers file is ahead of the plan until the review runs again)."""
        answers = self.answers(engine)
        return [{**i, "answer": answers.get(i["sig"], i.get("answer", ""))} for i in self.items(engine) if i["action"] in waivers.OPEN_ASKING and not i.get("handed_off")]

    def question_list(self, engine) -> list[dict]:
        out = []
        for i in self._asking(engine):
            opts = SDC_OPTIONS if i["action"] == "sdc" else [{"label": o, "description": ""} for o in i.get("options") or []]
            text = i["question"] + (f"\n{i['reason']}" if i.get("reason") else "")
            out.append({"id": i["sig"], "question": text, "blocking": False, "default_assumption": "", "kind": "design_choice",
                        "options": opts, "answer": i["answer"] or None})
        return out

    def open_questions(self, engine) -> int:
        return sum(not i["answer"] for i in self._asking(engine))

    def pending_reviews(self, engine) -> list[str]:
        return [i["id"] for i in self._asking(engine) if not i["answer"]]

    def answer(self, engine, question_id: str, text: str) -> bool:
        if question_id not in {i["sig"] for i in self._asking(engine)}:
            return False
        answers = self.answers(engine)
        answers[question_id] = text.strip()
        write_json(self.answers_file(engine), answers)
        return True

    # -- after the gate ---------------------------------------------------------------------------

    def pending_handoff(self, engine) -> list[dict]:
        from q3tui.flows.fpga.lib import sdc as sdcmod

        has_json = sdcmod.load(engine.project) is not None  # (a hand-written SDC has no sdc.json to add to: its requests stay yours)
        return [i for i in self.items(engine) if not i.get("handed_off")
                and (i["action"] in ("fix_rtl", "spec") or (has_json and i["action"] == "sdc" and sdcmod.extra_lines(i.get("sdc_request", "")) is not None))]

    def follow_up(self, engine):
        """Gate approved, RTL / spec fixes proposed: offer to hand them to the flow that owns them and move there."""
        pending = self.pending_handoff(engine)
        if not pending:
            return None
        owner = str(self.options.get("handoff_flow") or "rtl")
        return {"title": f"Hand {len(pending)} {self.role.upper()} fix(es) to '{owner}' and switch to it?",
                "detail": f"{', '.join(i['id'] for i in pending)}: each becomes a scoped change request of the {owner} step that owns it (rtl / spec); an SDC request is added to "
                          f"constraints/sdc/extra.tcl here; run {owner} to apply the rest "
                          f"to apply them, then come back and run {engine.flow.name} again. No keeps them proposed (hand off later with sanity_handoff).",
                "action": "sanity_handoff", "switch": owner}
