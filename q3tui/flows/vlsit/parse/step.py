"""VLSIT step `parse` (Phase 1, spec_parser): the spec → `schemas/structured_spec.json` (requirements with ids, categories,
ambiguity scores, SVA hints, parameter / module mapping). Gate 1.

LLM: one read-only stage (`parse_spec`) returns the requirements, parameters, constraints and modules as data. Code
assigns / keeps the ids (an unchanged requirement keeps its id), scores ambiguity into questions (kind `spec_gap`; blocking
from 0.6; asked up to 3 rounds, then `needs_human_decision` and a blocking keep / delete / split question), derives the
mapping, validates against structured_spec_schema.json and renders `structured_spec.md` for review.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.core.project import sha256_file, sha256_json
from q3tui.steps.common import Question, assign_question_ids, drop_settled, load_answers, settled_block, spec_block
from q3tui.steps.vlsit import artifacts
from q3tui.flows.vlsit.parse import task as parse_prompts
from q3tui.steps.vlsit.base import VlsitStep

AMBIGUITY_THRESHOLD = 0.3  # above: needs_review
BLOCKING_SCORE = 0.6  # at or above: the question blocks
MAX_ROUNDS = 3  # questions per requirement before it needs a human decision
Category = Literal["functional", "timing", "interface", "constraint"]
SvaHint = Literal["property", "static", "cover", "assume", "sequence"]


class ParsedReq(BaseModel):
    id: str = Field("", description='Keep the id of an unchanged requirement; "" for a new one')
    text: str
    category: Category
    ambiguity_score: float = Field(0.0, ge=0.0, le=1.0)
    ambiguity_issue: str = Field("", description="Why it is ambiguous (only when score > 0.3)")
    ambiguity_question: str = Field("", description="A yes/no or multiple-choice question (only when score > 0.3)")
    default_assumption: str = Field("", description="The reading assumed in `text` meanwhile")
    source_section: str = ""
    feature_id: str = ""
    parameters_affected: list[str] = Field(default_factory=list)
    rtl_modules: list[str] = Field(default_factory=list)
    sva_hint: SvaHint | None = None
    optional: bool = False


class ParsedParam(BaseModel):
    name: str
    type: str
    default: str
    valid_range: str = "any"
    description: str = ""
    locked: bool = False
    feature_ids: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list, description="Ids of the constraints it appears in")


class ParsedConstraint(BaseModel):
    id: str = Field(description="C1, C2, …")
    rule: str = Field(description="Checkable expression, e.g. \"PARA_IRQ_EN == 1 -> PARA_CSR_EN == 1\"")
    description: str = ""


class ParsedModule(BaseModel):
    name: str
    description: str = ""
    instances: list[str] = Field(default_factory=list, description="Modules it instantiates")


class ParsedSpec(BaseModel):
    ip_name: str
    spec_revision: str = "0.1"
    top_module: str
    description: str = Field("", description="One or two sentences: what the IP is")
    requirements: list[ParsedReq]
    parameters: list[ParsedParam] = Field(default_factory=list)
    constraints: list[ParsedConstraint] = Field(default_factory=list)
    modules: list[ParsedModule] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list, description="Global questions beyond per-requirement ambiguity")
    summary: str = ""


_NUM = re.compile(r"REQ-(\d+)")
_DECISION_MARK = "needs your decision"


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def modules_leaves_first(data: dict) -> list[str]:
    """Module names of a structured_spec, leaves first (every module after the modules it instantiates), top last."""
    mods = {m["name"]: m for m in data.get("modules", [])}
    order: list[str] = []

    def visit(name: str, seen: tuple = ()) -> None:
        if name in order or name in seen or name not in mods:
            return
        for child in mods[name].get("instances", []):
            visit(child, (*seen, name))
        order.append(name)

    top = (data.get("metadata") or {}).get("top_module")
    for name in [*(n for n in mods if n != top), *([top] if top else [])]:
        visit(name)
    return order


def assemble(parsed: ParsedSpec, previous: dict | None, meta: dict, decided: set[str]) -> tuple[dict, list[str]]:
    """structured_spec.json from the LLM's result: stable ids, needs_review, module / parameter mapping. Returns
    (data, warnings). `meta` holds the running requirement counter ("max_req")."""
    warnings: list[str] = []
    prev_reqs = {r["req_id"]: r for r in (previous or {}).get("requirements", [])}
    prev_by_text = {_norm(r["text"]): rid for rid, r in prev_reqs.items()}
    counter = max([meta.get("max_req", 0), *(int(m.group(1)) for rid in prev_reqs if (m := _NUM.fullmatch(rid)))], default=0)
    used: set[str] = set()
    reqs: list[dict] = []
    for r in parsed.requirements:
        rid = r.id if r.id in prev_reqs and r.id not in used else ""
        if not rid and _norm(r.text) in prev_by_text and prev_by_text[_norm(r.text)] not in used:
            rid = prev_by_text[_norm(r.text)]
        if not rid:
            counter += 1
            rid = f"REQ-{counter:03d}"
        used.add(rid)
        score = round(min(max(r.ambiguity_score, 0.0), 1.0), 2)
        needs_review = score > AMBIGUITY_THRESHOLD and rid not in decided
        reqs.append({"req_id": rid, "text": r.text.strip(), "category": r.category, "ambiguity_score": score,
                     "needs_review": needs_review, "needs_human_decision": False, "source_section": r.source_section,
                     "parameters_affected": list(dict.fromkeys(r.parameters_affected)), "feature_id": r.feature_id,
                     "sva_hint": r.sva_hint, "optional": r.optional, "rtl_modules": list(dict.fromkeys(r.rtl_modules)),
                     "_issue": r.ambiguity_issue, "_question": r.ambiguity_question, "_default": r.default_assumption})
    meta["max_req"] = counter

    names = [m.name for m in parsed.modules]
    modules = [{"name": m.name, "description": m.description, "instances": [i for i in dict.fromkeys(m.instances) if i != m.name]}
               for m in parsed.modules]
    top = parsed.top_module if parsed.top_module in names or not names else next(
        (n for n in names if n not in {i for m in modules for i in m["instances"]}), names[0])
    if not names:
        modules, top = [{"name": parsed.top_module, "description": parsed.description, "instances": []}], parsed.top_module
        names = [top]
    for r in reqs:  # every requirement maps to a module (the top when the spec does not say): Phase 3 tags REQs per module
        if not r["rtl_modules"]:
            r["rtl_modules"] = [top]
            warnings.append(f"{r['req_id']} has no RTL module in the spec: mapped to the top module {top}")
        for m in r["rtl_modules"]:
            if m not in names:
                modules.append({"name": m, "description": "", "instances": []})
                names.append(m)
                warnings.append(f"{r['req_id']} maps to module '{m}' which the module list does not have: added")
    for m in modules:
        m["req_ids"] = [r["req_id"] for r in reqs if m["name"] in r["rtl_modules"]]
    param_reqs: dict[str, list[str]] = {}
    for r in reqs:
        for p in r["parameters_affected"]:
            param_reqs.setdefault(p, []).append(r["req_id"])
    params = [{"name": p.name, "type": p.type, "default": p.default, "valid_range": p.valid_range, "description": p.description,
               "locked": p.locked, "req_ids": param_reqs.get(p.name, []), "feature_ids": p.feature_ids, "constraints": p.constraints}
              for p in parsed.parameters]
    data = {
        "metadata": {"ip_name": parsed.ip_name, "spec_revision": parsed.spec_revision, "phase": 1, "gate_status": "pending",
                     "gate_1_approved": False, "approved_at": None, "top_module": top, "description": parsed.description},
        "requirements": reqs, "parameters": params,
        "constraints": [{"id": c.id, "rule": c.rule, "description": c.description} for c in parsed.constraints],
        "modules": modules,
        "ambiguity_flags": [r["req_id"] for r in reqs if r["needs_review"]],
    }
    data["module_order"] = modules_leaves_first(data)
    return data, warnings


def _strip_private(data: dict) -> dict:
    for r in data["requirements"]:
        for k in [k for k in r if k.startswith("_")]:
            del r[k]
    return data


def render_md(data: dict, open_questions: int = 0) -> str:
    md = data["metadata"]
    lines = [f"# Requirements — {md['ip_name']} (spec rev {md['spec_revision']})", "",
             f"Top module `{md['top_module']}` · {len(data['requirements'])} requirement(s) · "
             f"{len(data['ambiguity_flags'])} need review · {sum(r.get('needs_human_decision', False) for r in data['requirements'])} need your decision"
             f" · gate: {md['gate_status']}", ""]
    for cat in ("functional", "interface", "timing", "constraint"):
        rows = [r for r in data["requirements"] if r["category"] == cat]
        if not rows:
            continue
        lines += [f"## {cat.capitalize()} ({len(rows)})", "", "| REQ | Score | Modules | SVA | Requirement |", "|---|---|---|---|---|"]
        for r in rows:
            mark = "🔴" if r.get("needs_human_decision") else ("⚠" if r["needs_review"] else "✓")
            text = " ".join(r["text"].split()).replace("|", "\\|")
            lines.append(f"| {r['req_id']} | {r['ambiguity_score']:.2f} {mark} | {', '.join(r['rtl_modules'])} | {r['sva_hint'] or '—'} | {text} |")
        lines.append("")
    lines += ["## Parameters", "", "| Name | Type | Default | Range | Locked |", "|---|---|---|---|---|"]
    lines += [f"| `{p['name']}` | {p['type']} | `{p['default']}` | {p['valid_range']} | {'yes' if p['locked'] else ''} |" for p in data["parameters"]]
    if data["constraints"]:
        lines += ["", "## Constraints", ""] + [f"- **{c['id']}** `{c['rule']}` {c['description']}" for c in data["constraints"]]
    lines += ["", "## Modules (leaves first)", ""]
    by_name = {m["name"]: m for m in data["modules"]}
    lines += [f"- `{n}` — {by_name[n]['description']} ({len(by_name[n]['req_ids'])} REQ)" for n in data["module_order"]]
    return "\n".join(lines) + "\n"


class ParseStep(VlsitStep):
    name = "parse"
    kind = "vlsit_parse"
    title = "Requirements (spec parser)"
    deps = ("spec",)
    artifact = "structured_spec.json"
    gate_fields = {"gate_status": "approved", "gate_1_approved": True, "approved_at": ""}

    # -- files -----------------------------------------------------------------------------

    def _md(self, engine) -> Path:
        return self.lay(engine).schemas / "structured_spec.md"

    def inputs(self, engine) -> list[Path]:
        return [*engine.project.spec_documents(), self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        return [p for p in (lay.artifact(self.artifact), self._md(engine), self._q_paths(engine)[2]) if p.is_file()]

    def missing_input(self, engine) -> str | None:
        if not engine.project.spec_documents():
            return f"no specification documents in {engine.project.rel(engine.project.spec_dir)}/ (write one, or run the spec step)"
        return None

    def reset_files(self, engine) -> list[Path]:
        return [*super().reset_files(engine), self.lay(engine).artifact(self.artifact), self._md(engine)]

    def load(self, engine) -> dict | None:
        return artifacts.read(engine.project, self.artifact)

    # -- run ------------------------------------------------------------------------------------

    def _answers_to_apply(self, engine, meta: dict) -> list[tuple[str, str, str]]:
        answers = load_answers(self._q_paths(engine)[1])
        closed = engine.state.closed_questions.get(self.name, {})
        known = {q.id: q.question for q in self.questions_list(engine)} | {k: v["question"] for k, v in closed.items()}
        applied = set(meta.get("applied_answers", []))
        return [(qid, known.get(qid, "(earlier question)"), a) for qid, a in answers.items() if qid not in applied]

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        meta = engine.state.step_meta.setdefault(self.name, {})
        previous = None if ctx.regenerate else self.load(engine)
        answers = load_answers(self._q_paths(engine)[1])
        new_answers = self._answers_to_apply(engine, meta)
        docs = project.spec_documents()
        sig = sha256_json({"spec": [sha256_file(p) for p in docs], "answers": answers})
        if previous is not None and meta.get("sig") == sig and not ctx.feedback and not artifacts.validate(self.artifact, previous):
            ctx.unchanged = True
            ctx.emit("log", message="the spec and the answers are as when the requirements were extracted: nothing to do")
            return

        block, needs_tools = spec_block(project, docs)
        settled = settled_block(engine.settled_questions(self.name))
        if previous is None:
            prompt = parse_prompts.first_prompt(block, settled, ctx.feedback)
            ctx.emit("log", message=f"extracting requirements from {', '.join(project.rel(p) for p in docs)}")
        else:
            compact = "\n".join(f"- {r['req_id']} [{r['category']}, {r['ambiguity_score']}] {' '.join(r['text'].split())} "
                                f"→ {', '.join(r['rtl_modules'])}" for r in previous["requirements"])
            prev_txt = compact + "\nParameters: " + ", ".join(f"{p['name']}={p['default']}" for p in previous["parameters"]) \
                + "\nModules: " + ", ".join(m["name"] for m in previous["modules"])
            prompt = parse_prompts.update_prompt(block, prev_txt, new_answers, settled, ctx.feedback)
            ctx.emit("log", message=f"updating the requirements ({len(new_answers)} new answer(s), {len(ctx.feedback)} change request(s))")
        stage = Stage(name="parse_spec", system_prompt=parse_prompts.SYSTEM, prompt=prompt, cwd=project.spec_dir,
                      builtin_tools=["Read", "Grep", "Glob"] if needs_tools else [], output_model=ParsedSpec)
        parsed: ParsedSpec = (await ctx.llm(stage)).output
        if not parsed.requirements:
            raise StepFailed("the spec parser found no requirements in the specification")

        # answers to "keep / delete / split" questions settle that requirement's review
        decided = set(meta.get("decided", []))
        for qid, q, _a in new_answers:
            if _DECISION_MARK in q and (m := re.match(r"(REQ-\d+)", q)):
                decided.add(m.group(1))
        data, warnings = assemble(parsed, previous, meta, decided)
        for w in warnings:
            ctx.emit("warning", message=w)
        if len(data["requirements"]) < 10:
            ctx.emit("warning", message=f"only {len(data['requirements'])} requirements: the spec looks incomplete")

        questions = self._questions_for(engine, data, parsed, answers, meta, decided)
        meta["decided"] = sorted(decided & {r["req_id"] for r in data["requirements"]})
        _strip_private(data)
        problems = artifacts.validate(self.artifact, data)
        if problems:
            raise StepFailed("structured_spec.json does not match its schema: " + "; ".join(problems[:5]))

        same = previous is not None and self._content(previous) == self._content(data)
        if same:
            ctx.unchanged = True  # (the review of an identical result stays)
        else:
            engine.project.backup([p for p in self.outputs(engine)])
            artifacts.write(project, self.artifact, data)
            self._md(engine).parent.mkdir(parents=True, exist_ok=True)
            self._md(engine).write_text(render_md(data))
        self.save_questions(engine, questions)
        self.write_questions_file(engine)
        meta["applied_answers"] = sorted(set(meta.get("applied_answers", [])) | {qid for qid, _, _ in new_answers})
        meta["sig"] = sig
        ctx.emit("questions", count=len(questions), blocking=sum(q.blocking for q in questions))
        ctx.emit("log", message=f"{len(data['requirements'])} requirement(s), {len(data['parameters'])} parameter(s), "
                                f"{len(data['modules'])} module(s); {len(data['ambiguity_flags'])} need review. {parsed.summary}")
        if self.options.get("auto_confirm", engine.auto_confirm_reviews()):  # auto approve (or the step option): no human review
            from q3tui.flows.vlsit.parse import review

            ctx.emit("warning", message="parse: auto approve — flagged requirements are confirmed automatically; nobody reviewed them")
            review.confirm_all(engine, note="auto-confirmed (auto approve), not reviewed")

    def pending_reviews(self, engine) -> list[str]:
        from q3tui.flows.vlsit.parse import review

        return review.pending_ids(engine)

    @staticmethod
    def _content(data: dict) -> dict:
        """The artifact without the gate bookkeeping (comparing a new result with the reviewed one)."""
        md = {k: v for k, v in data["metadata"].items() if k not in ("gate_status", "gate_1_approved", "approved_at")}
        return {**data, "metadata": md}

    @staticmethod
    def question_kind(engine) -> str:
        """`spec_gap` when the spec step wrote the spec (the answer goes back into it); a spec you wrote yourself is never
        edited by Q3TUI, so the answer stays here (`design_choice`) and is applied to the requirements."""
        rec = engine.state.steps.get("spec")
        return "spec_gap" if rec is not None and rec.origin == "generated" else "design_choice"

    def _questions_for(self, engine, data: dict, parsed: ParsedSpec, answers: dict, meta: dict, decided: set[str]) -> list[Question]:
        """Ambiguity → questions (rounds counted per requirement), global questions of the LLM; answered ones are gone."""
        previous = self.questions_list(engine)
        kind = self.question_kind(engine)
        taken = set(answers) | set(engine.state.closed_questions.get(self.name, {}))
        rounds: dict[str, int] = meta.setdefault("rounds", {})
        open_by_req = {m.group(1): q for q in previous if q.id not in answers and (m := re.match(r"(REQ-\d+)", q.question))}
        new: list[Question] = []
        by_id = {r["req_id"]: r for r in data["requirements"]}
        for rid, r in by_id.items():
            if r["ambiguity_score"] <= AMBIGUITY_THRESHOLD or rid in decided:
                continue
            still = open_by_req.get(rid)
            if still is not None:
                new.append(still)  # asked and not answered yet: the same question, no new round
                if _DECISION_MARK in still.question:
                    r["needs_human_decision"] = True
                continue
            n = rounds.get(rid, 0)
            if n >= MAX_ROUNDS:
                r["needs_human_decision"] = True
                new.append(Question(id="", question=f"{rid} {_DECISION_MARK}: still ambiguous after {MAX_ROUNDS} questions "
                                                    f"({r['_issue'] or 'see the requirement'}). Keep it as it is (risk of RTL hallucination), "
                                                    "delete it, or split it into smaller requirements? (keep / delete / split)",
                                    blocking=True, default_assumption="keep", kind=kind))
            else:
                rounds[rid] = n + 1
                q = r["_question"] or f"{r['_issue'] or 'The requirement can be read in more than one way.'} Which reading is intended?"
                new.append(Question(id="", question=f"{rid} [ambiguity {r['ambiguity_score']:.2f}] (round {n + 1}/{MAX_ROUNDS}): "
                                                    f"{' '.join(r['text'].split())[:160]} — {q}",
                                    blocking=r["ambiguity_score"] >= BLOCKING_SCORE, default_assumption=r["_default"], kind=kind))
        general, dropped = drop_settled(list(parsed.questions), engine.settled_questions(self.name))
        new += [q.model_copy(update={"kind": kind}) if q.kind == "spec_gap" else q for q in general if q.id not in answers]
        out = assign_question_ids(new, previous, "Q-P", taken)
        return [q for q in out if q.id not in answers]
