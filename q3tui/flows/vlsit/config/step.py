"""VLSIT step `config` (Phase 2, config_ui): `structured_spec.json` → `schemas/final_config.json`. Gate 2.

Mostly code: parameters are locked at their defaults, the spec's elaboration constraints (C1…Cn, checkable expressions —
`constraints.py`) are evaluated by code, the feature matrix follows the parameters, the style rules come from the RTL rule
file. Non-default values come only from what the user asks for — `options.overrides` of the flow file, change requests,
answers to this step's questions — and go through one small LLM stage (`config_overrides`) that only translates the wording
into parameter values; code validates them (known, not locked, inside the range). The gate signs itself (mode `auto`)
only when every constraint passes: a violated or unevaluable constraint is a blocking question.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.core.project import sha256_json, write_json
from q3tui.steps.common import Question, QuestionOption, assign_question_ids, load_answers
from q3tui.steps.vlsit import artifacts
from q3tui.steps.vlsit.base import VlsitStep, rule_dirs
from q3tui.steps.vlsit.constraints import NotEvaluable, evaluate, in_range, to_int

CONFIRM = {"yes", "y", "ok", "confirm", "confirmed", "holds", "true", "đúng", "có"}

from pathlib import Path as _Path

from q3tui.flows import skill_sections as _skill_sections

# the stage system prompts are the named sections of md/skill.md
TEXT = _skill_sections(_Path(__file__).parent / "md" / "skill.md")

SYSTEM = TEXT["SYSTEM"]


class Override(BaseModel):
    name: str
    value: str
    reason: str = ""


class StyleRule(BaseModel):
    nl: str
    rule: str


class Judgement(BaseModel):
    """What the model decided about the user's answer to a "does this constraint hold?" question."""

    constraint: str
    verdict: Literal["confirm", "handover", "followup"]
    handover_to: Literal["spec", "req"] | None = None
    text: str = ""  # handover: what the owner must change; followup: the next question
    options: list[str] = Field(default_factory=list)
    reason: str = ""


class ConfigOverrides(BaseModel):
    overrides: list[Override] = Field(default_factory=list)
    style_rules: list[StyleRule] = Field(default_factory=list)
    judgements: list[Judgement] = Field(default_factory=list)
    notes: str = ""


def validate_final_config(data: dict) -> list[str]:
    """Schema problems of a final_config.json. The original schema pins the rules of each constraint to one design's
    (`"const": "PR_BOOT_ADDR[1:0] == 2'b00"`); here a constraint's rule is the spec's own, so the pin is dropped."""
    import jsonschema

    schema = copy.deepcopy(artifacts.schema_for("final_config.json"))
    for c in schema["properties"]["constraints"].get("properties", {}).values():
        c.get("properties", {}).get("rule", {}).pop("const", None)
    v = jsonschema.Draft7Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in v.iter_errors(data)]


def _rule_file(engine, flow_options: dict) -> Path | None:
    names = flow_options.get("rules") or []
    for n in names[:1]:
        for cand in (engine.project.root / n, *(d / n for d in rule_dirs())):
            if cand.is_file():
                return cand
    return None


def lint_rules_from(path: Path | None) -> list[dict]:
    """The rule file's headings as rule ids (a summary of what it sets; the file itself is what stages read)."""
    if path is None:
        return []
    rules = []
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"^#{2,3}\s+(.*\S)", line)
        if m and len(rules) < 200:
            rules.append({"id": f"STYLE-{len(rules) + 1:03d}", "source": "rtl_rule.md", "rule": m.group(1).strip()})
    return rules


def feature_matrix(spec: dict, values: dict[str, str]) -> dict:
    """F-id → enabled / mandatory / driving parameters / REQs (an optional feature is off when a parameter driving it is 0)."""
    feats: dict[str, dict] = {}
    for r in spec["requirements"]:
        fid = r.get("feature_id")
        if not fid:
            continue
        f = feats.setdefault(fid, {"reqs": [], "optional": [], "params": []})
        f["reqs"].append(r["req_id"])
        f["optional"].append(bool(r.get("optional")))
        f["params"] += [p for p in r.get("parameters_affected", []) if p not in f["params"]]
    out = {}
    for fid, f in sorted(feats.items()):
        mandatory = not all(f["optional"])
        enabled = True
        if not mandatory:
            for p in f["params"]:
                try:
                    if to_int(values.get(p, "1")) == 0:
                        enabled = False
                except NotEvaluable:
                    pass
        out[fid] = {"enabled": enabled, "mandatory": mandatory, "parameters_driving": f["params"], "req_ids": f["reqs"]}
    return out


class ConfigStep(VlsitStep):
    name = "config"
    kind = "vlsit_config"
    title = "Configuration"
    deps = ("parse",)
    artifact = "final_config.json"
    gate_fields = {"gate_status": "approved", "gate_2_approved": True, "approved_at": ""}

    def inputs(self, engine) -> list[Path]:
        return [self.lay(engine).artifact("structured_spec.json"), self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        return [p for p in (self.lay(engine).artifact(self.artifact), self._q_paths(engine)[2]) if p.is_file()]

    def missing_input(self, engine) -> str | None:
        if not self.lay(engine).artifact("structured_spec.json").is_file():
            return "the requirements (structured_spec.json, step parse) are needed first"
        return None

    def reset_files(self, engine) -> list[Path]:
        return [*super().reset_files(engine), self.lay(engine).artifact(self.artifact)]

    def load(self, engine) -> dict | None:
        return artifacts.read(engine.project, self.artifact)

    def pending_reviews(self, engine) -> list[str]:
        from q3tui.flows.vlsit.config import review

        return review.pending_ids(engine)

    def on_approve(self, engine) -> None:
        data = self.load(engine)
        if not data:
            return
        failing = [k for k, c in data.get("constraints", {}).items() if not c.get("pass")]
        if failing:  # (approved with --yes / force): the artifact does not claim what is not true
            engine.bus.emit("warning", self.name, message=f"gate approved although constraint(s) {', '.join(failing)} do not pass: "
                                                          "final_config.json stays unsigned (gate_2_approved: false)")
            return
        for p in data["parameters"].values():
            p["confirmed_by_user"] = True
        write_json(self.lay(engine).artifact(self.artifact), data)
        super().on_approve(engine)

    # -- run ------------------------------------------------------------------------------------

    def _apply_overrides(self, spec: dict, wanted: dict[str, dict]) -> tuple[dict[str, dict], list[dict]]:
        """(accepted overrides, rejected) — known parameter, not locked, inside its range."""
        by_name = {p["name"]: p for p in spec["parameters"]}
        ok, rejected = {}, []
        for name, o in wanted.items():
            p = by_name.get(name)
            if p is None:
                rejected.append({"name": name, "value": o["value"], "why": "no such parameter"})
            elif p.get("locked"):
                rejected.append({"name": name, "value": o["value"], "why": "the parameter is locked"})
            elif not in_range(o["value"], p.get("valid_range", "")):
                rejected.append({"name": name, "value": o["value"], "why": f"outside the range {p.get('valid_range')}"})
            else:
                ok[name] = o
        return ok, rejected

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        spec = artifacts.read(project, "structured_spec.json")
        if not spec:
            raise StepFailed("schemas/structured_spec.json is missing or unreadable")
        meta = engine.state.step_meta.setdefault(self.name, {})
        if ctx.regenerate:
            meta.clear()
        answers = load_answers(self._q_paths(engine)[1])
        previous_q = self.questions_list(engine)
        closed = engine.state.closed_questions.get(self.name, {})
        known = {q.id: q.question for q in previous_q} | {k: v["question"] for k, v in closed.items()}
        applied = set(meta.get("applied_answers", []))
        new_answers = {qid: a for qid, a in answers.items() if qid not in applied}

        # answers that confirm a constraint the code cannot evaluate
        confirmed: set[str] = set(meta.get("confirmed_constraints", []))
        wording: list[str] = list(ctx.feedback)
        for qid, a in new_answers.items():
            m = re.match(r"Constraint (\S+)", known.get(qid, ""))
            if m and "cannot be evaluated" in known.get(qid, "") and a.strip().lower().rstrip(".!") in CONFIRM:
                confirmed.add(m.group(1))
            else:  # the model reads it: a parameter / style change, a confirmation, a hand-over to the owner, or a follow-up
                wording.append(f"{known.get(qid, 'question')} → {a}")
        answered_texts = {known[qid].strip().lower() for qid in answers if qid in known}
        followups: list[dict] = [f for f in meta.get("followups", []) if f["question"].strip().lower() not in answered_texts]
        # rules the user did not confirm, with what they said: asked again (worded differently, so it is a new question) unless
        # the owner changed the rule — an unconfirmed rule must never pass the gate
        refused: dict[tuple[str, str], str] = {}
        for qid, a in answers.items():
            m = re.match(r"Constraint (\S+) `(.*?)` cannot be evaluated", known.get(qid, ""), re.S)
            if m and a.strip().lower().rstrip(".!") not in CONFIRM:
                refused[(m.group(1), m.group(2))] = a.strip()
        meta["confirmed_constraints"] = sorted(confirmed)

        wanted: dict[str, dict] = {k: {"value": str(v), "reason": "flow file options.overrides"}
                                   for k, v in (self.options.get("overrides") or {}).items()}
        wanted.update(meta.get("overrides", {}))
        additions: list[dict] = list(meta.get("style_additions", []))
        nl_seen = {a["nl"] for a in additions}
        for nl in self.options.get("style_additions") or []:
            if nl not in nl_seen:
                additions.append({"nl": nl, "rule": nl})
        if wording:  # the user asked for something: one small stage turns the wording into values / rules
            table = "\n".join(f"- {p['name']} ({p['type']}) default {p['default']}, range {p['valid_range']}"
                              f"{' [LOCKED]' if p.get('locked') else ''}: {p['description']}" for p in spec["parameters"])
            prompt = ("Parameter của IP:\n" + table + "\n\nNgười dùng yêu cầu:\n" + "\n".join(f"- {w}" for w in wording))
            out: ConfigOverrides = (await ctx.llm(Stage(name="config_overrides", system_prompt=SYSTEM, prompt=prompt, cwd=project.root,
                                                        builtin_tools=[], output_model=ConfigOverrides))).output
            for o in out.overrides:
                wanted[o.name] = {"value": o.value, "reason": o.reason or "requested"}
            for s in out.style_rules:
                if s.nl not in {a["nl"] for a in additions}:
                    additions.append({"nl": s.nl, "rule": s.rule})
            if out.notes:
                ctx.emit("log", message=f"config: {out.notes}")
            owners = {"spec": engine.flow.gap_targets.get("spec_gap", "spec"), "req": engine.flow.gap_targets.get("req_gap", "parse")}
            for j in out.judgements:
                if j.verdict == "confirm":
                    confirmed.add(j.constraint)
                elif j.verdict == "handover" and j.handover_to and j.text.strip():
                    target = owners[j.handover_to]
                    engine.push_decision(target, self.name, f"From config, constraint {j.constraint}: {j.text.strip()}", rebase=False)
                    ctx.emit("log", message=f"config: {j.constraint} handed to {target} ({j.reason or j.text[:80]})")
                elif j.verdict == "followup" and j.text.strip():
                    followups.append({"question": j.text.strip(), "options": j.options[:4], "constraint": j.constraint})
            meta["confirmed_constraints"] = sorted(confirmed)
            meta["followups"] = followups
        accepted, rejected = self._apply_overrides(spec, wanted)
        for r in rejected:
            ctx.emit("warning", message=f"override {r['name']} = {r['value']} refused: {r['why']}")
            wanted.pop(r["name"], None)
        meta["overrides"] = wanted if not rejected else {k: v for k, v in wanted.items() if k in accepted}
        meta["style_additions"] = additions

        sig = sha256_json({"spec": spec, "overrides": meta["overrides"], "additions": additions, "confirmed": sorted(confirmed),
                           "refused": sorted(f"{c}:{r}:{t}" for (c, r), t in refused.items()),
                           "followups": sorted(f["question"] for f in followups)})
        previous = None if ctx.regenerate else self.load(engine)
        if previous is not None and meta.get("sig") == sig and not wording and not validate_final_config(previous):
            ctx.unchanged = True
            ctx.emit("log", message="requirements and requested changes are as when the configuration was built: nothing to do")
            return

        params: dict[str, dict] = {}
        for p in spec["parameters"]:
            o = accepted.get(p["name"])
            value = o["value"] if o else p["default"]
            params[p["name"]] = {"value": value, "default": p["default"], "type": p["type"],
                                 "changed_from_default": value != p["default"], "confirmed_by_user": False,
                                 "locked": bool(p.get("locked")), "req_ids_affected": p.get("req_ids", []),
                                 "feature_ids_affected": p.get("feature_ids", []),
                                 "change_reason": o["reason"] if o else None}
        values = {n: e["value"] for n, e in params.items()}
        constraints, questions = {}, []
        for c in spec.get("constraints", []):
            verdict, detail = evaluate(c["rule"], values)
            if verdict is None and c["id"] in confirmed:
                verdict, detail = True, "confirmed by the user (code cannot evaluate the rule)"
            constraints[c["id"]] = {"rule": c["rule"], "pass": bool(verdict), "detail": detail}
            if verdict is None:
                earlier = refused.get((c["id"], c["rule"]))
                tail = (f" You answered «{earlier}», which was handed to the spec requirements, but the rule is still the same. "
                        "Does it hold now? (yes to confirm, or say what to change)") if earlier else \
                       " Does it hold for the configuration? (answer yes to confirm, or say what to change)"
                questions.append(Question(id="", question=f"Constraint {c['id']} `{c['rule']}` {detail}.{tail}",
                                          blocking=True, default_assumption="", kind="design_choice",
                                          options=[QuestionOption(label="yes", description="the rule holds for this configuration: confirmed, no change"),
                                                   QuestionOption(label="no — it does not hold", description="then use Other… to say what to change")]))
            elif not verdict:
                questions.append(Question(id="", question=f"Constraint {c['id']} `{c['rule']}` is violated: {detail}. "
                                                          "Which parameter value should change (e.g. \"PR_CSR_EN = 1\")?",
                                          blocking=True, default_assumption="", kind="design_choice"))
        for f in followups:  # questions the model asked after reading an answer: they stay until answered
            questions.append(Question(id="", question=f["question"], blocking=True, default_assumption="", kind="design_choice",
                                      options=[QuestionOption(label=o) for o in f.get("options", [])]))
        meta["followups"] = followups
        rule_file = _rule_file(engine, (engine.flow.spec("rtl").options if engine.flow.spec("rtl") else {}))
        lint = lint_rules_from(rule_file) + [{"id": f"STYLE-U{i:02d}", "source": "user_addition", "rule": a["rule"], "original_nl": a["nl"]}
                                             for i, a in enumerate(additions, 1)]
        md = spec["metadata"]
        data = {
            "metadata": {"ip_name": md["ip_name"], "spec_revision": md["spec_revision"], "phase": 2, "gate_status": "pending",
                         "gate_2_approved": False, "approved_at": None, "top_module": md.get("top_module"),
                         "rejected_overrides": rejected},
            "parameters": params, "constraints": constraints,
            "style_constraints": {"source_file": rule_file.name if rule_file else None,
                                  "natural_language_additions": [a["nl"] for a in additions], "lint_rules": lint},
            "feature_matrix": feature_matrix(spec, values),
        }
        problems = validate_final_config(data)
        if problems:
            raise StepFailed("final_config.json does not match its schema: " + "; ".join(problems[:5]))

        taken = set(answers) | set(closed)
        questions = [q for q in assign_question_ids(questions, previous_q, "Q-C", taken) if q.id not in answers]
        if previous is not None and self._content(previous) == self._content(data):
            ctx.unchanged = True
        else:
            engine.project.backup(self.outputs(engine))
            write_json(self.lay(engine).artifact(self.artifact), data)
        self.save_questions(engine, questions)
        self.write_questions_file(engine)
        meta["applied_answers"] = sorted(applied | set(new_answers))
        meta["sig"] = sig
        changed = [n for n, e in params.items() if e["changed_from_default"]]
        failed = [k for k, c in constraints.items() if not c["pass"]]
        ctx.emit("questions", count=len(questions), blocking=sum(q.blocking for q in questions))
        ctx.emit("log", message=f"{len(params)} parameter(s), {len(changed)} changed from default"
                                f"{' (' + ', '.join(changed) + ')' if changed else ''}; constraints: "
                                f"{len(constraints) - len(failed)}/{len(constraints)} pass" + (f" — failing: {', '.join(failed)}" if failed else ""))
        if self.options.get("auto_confirm", engine.auto_confirm_reviews()):  # auto approve (or the step option): no human review of the changes
            from q3tui.flows.vlsit.config import review

            ctx.emit("warning", message="config: auto approve — parameters changed from default are confirmed automatically; nobody reviewed them")
            review.confirm_all(engine, note="auto-confirmed (auto approve), not reviewed")

    @staticmethod
    def _content(data: dict) -> str:
        d = json.loads(json.dumps(data))
        for k in ("gate_status", "gate_2_approved", "approved_at"):
            d["metadata"].pop(k, None)
        for p in d["parameters"].values():
            p.pop("confirmed_by_user", None)
        return json.dumps(d, sort_keys=True)
