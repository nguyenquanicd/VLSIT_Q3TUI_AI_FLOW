"""TUI assistant: everything a user can do, by prompting.

The assistant has read access to the project, can edit spec/, schemas/ and src/ files like a user
in an editor, and has a tool for every user action (all implemented in `q3tui.core.ops`).
Destructive actions ask the user through the TUI before they happen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from q3tui.core.events import StepEmitter
from q3tui.llm import runtime
from q3tui.llm.runtime import Stage
from q3tui.core.ops import Ops
from q3tui.pipeline.engine import Engine, EngineError
from q3tui.core.project import read_json, write_json

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool

SYSTEM = """\
Reply in whichever language the user's own message is written in (English, Vietnamese, Korean, …) — this is \
independent of the project's `tui.language` setting, which only covers the TUI's own chrome and the pipeline \
steps' generated content, never this conversation.

You are the Q3TUI assistant inside the Q3TUI TUI. Q3TUI takes a hardware \
block from its spec to verified RTL through a flow of steps (the default, VLSIT: spec → parse \
(requirements) → config → rtl ∥ tb → sva → verify → doc; pipeline_status lists the project's steps).

The user drives the project by prompting you. Everything they could do with keys or \
commands you can do with your tools, so act instead of explaining how:
- Spec structure = the spec template (sections with id, title, required, guidance, \
fields). Use add_section / update_section / remove_section / move_section.
- Spec content: write_section writes or replaces one section of the spec right \
away (creating it if missing) — use it for targeted writing like "write the power \
section". For broad rewrites use request_change (optionally for one section) and then \
run_steps, which regenerates with the LLM spec writer.
- A new section the user wants permanently: add_section (template) and, if they want \
content now, write_section.
- Intent: set_intent / append_intent. Questions: list_questions, answer_question. \
Review: mark_reviewed (spec sections), approve, and the per-item reviews of the steps \
(review_parse_requirement, review_config_parameter, review_property, sign_requirement). \
Pipeline: run_steps, stop_run, reset_step. Settings: get_settings / set_settings \
(everything in the settings dialog: general, llm, per step), set_model, show_cost, reset_cost, \
reset_stats. Bring-your-own spec: import_file.
- You can also Edit/Write files under spec/, schemas/ and src/ directly (as the user could \
in an editor); prefer the dedicated tools when one fits. You cannot modify Q3TUI's internal state.
- Changes to a step's result: request_change(step=..., ...) then run_steps.

Where things are (read the files of the step the user asks about, not just the spec):
- spec: spec/*.md (sections), spec/intent.md, spec/questions.json; section structure: get_template
- parse: schemas/structured_spec.json (requirements REQ-nnn, parameters, module map)
- config: schemas/final_config.json (resolved parameter values)
- rtl: src/rtl/*.sv, src/rtl/filelist.f; schemas/synth_report.json
- tb: src/tb/*.sv, src/tb/filelist.f; schemas/selected_testplan.json
- sva: src/sva/*.sv, the bind file, src/sva/GATE3_REVIEW.md
- verify: schemas/verification_report.json; schemas/rtm.json (the requirements traceability matrix)
- doc: docs/specification.md (.pdf)
- questions of any step: list_questions; step states: pipeline_status
Each message starts with a [TUI context] line saying which step/tab the user is looking \
at and what is selected. "This", "it", "the requirements" etc. refer to that view.

Rules:
- Do exactly what the user asked, on the step they named. Step names: "parse" = \
the requirements step (also "reqs"/"requirements"), "spec" = specification. "approve all reqs" means: \
approve the requirements review (open non-blocking questions get their defaults). Do \
not approve, run or change other steps unless asked; if the request is unclear, ask.
- Stay in the step the user is working on (see [TUI context]); do not change other \
steps. If you find a problem in the SPEC while working on a later step, hand it off: \
request_change(step='spec', ...) and then run_steps(only='spec', wait=true) — never \
edit the spec or other steps from here. Tools that touch another step ask the user first.
- You cannot act after your reply: nothing wakes you up later. Never say "once X \
finishes I'll do Y". If Y depends on a run, call run_steps with wait=true and then do \
Y in the same turn, or tell the user what to do next.
- Destructive actions (reset_step, approve with force, reset_cost, reset_stats) show the user a \
confirmation dialog; just call the tool — if they decline, say so and stop.
- Only approve when the user asks for it.
- Starting a run returns immediately; progress appears in the activity panel.
- After acting, say briefly what changed and what (if anything) is now out of date.
- Ground answers in the project files; cite file names and REQ ids."""


class AssistantHost(Protocol):
    engine: Engine
    ops: Ops

    def start_run(self, start: str | None = None, stop: str | None = None, only: str | None = None,
                  regenerate: set[str] | None = None) -> str: ...

    async def run_and_wait(self, start: str | None = None, stop: str | None = None, only: str | None = None,
                           regenerate: set[str] | None = None) -> str: ...

    def stop_run(self) -> str: ...

    async def confirm(self, question: str, detail: str = "") -> bool: ...

    def focus_step(self) -> str | None: ...  # step the user is working on (None: no restriction)

    async def ask_user(self, questions: list[dict]) -> list[str]: ...  # the TUI picker; answers as text


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _schema(props: dict[str, tuple[str, str]], required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {k: ({"type": t, "description": d} if t != "array" else {"type": "array", "items": {"type": "object"}, "description": d})
                       for k, (t, d) in props.items()},
        "required": required,
    }


def assistant_tools(host: AssistantHost) -> list[SdkMcpTool]:
    from claude_agent_sdk import tool  # loaded on the first chat message, not at TUI start

    eng, ops = host.engine, host.ops
    tools: list[SdkMcpTool] = []

    def question_step(qid: str) -> str | None:
        return next((q["step"] for q in eng.questions() if q["id"] == qid), None)

    async def out_of_scope(target: str | None, action: str) -> str | None:
        """Stay in the step the user is on. Handing a problem to the spec (change request + rerun
        of the spec) is always allowed; anything else on another step needs the user's OK."""
        focus = host.focus_step()
        if not target or not focus or target == focus:
            return None
        if target == "spec" and action in ("request a change to", "run"):
            return None  # the hand-off: spec problems go to the spec step
        if await host.confirm(f"The assistant wants to {action} '{target}' while you are working on '{focus}'. Allow?",
                              "Steps are kept separate: problems in the spec are handed to the spec step instead."):
            return None
        return (f"the user declined: stay on step '{focus}'. If the spec has a problem, hand it off with "
                f"request_change(step='spec', ...) and run_steps(only='spec', wait=true).")

    def add(name: str, description: str, schema: dict, scope=None):
        """scope(args) -> (target step, action) for tools that change a step."""

        def deco(fn):
            async def wrapper(args: dict[str, Any]) -> dict:
                try:
                    if scope is not None:
                        target, action = scope(args)
                        refusal = await out_of_scope(target, action)
                        if refusal:
                            return _text(refusal)
                    return _text(str(await fn(args)))
                except (EngineError, ValueError, KeyError) as exc:
                    return {"content": [{"type": "text", "text": f"error: {exc}"}], "is_error": True}

            tools.append(tool(name, description, schema)(wrapper))
            return fn

        return deco

    none: dict = {"type": "object", "properties": {}}

    # -- status / questions --------------------------------------------------------------
    @add("pipeline_status", "Step states, review gates, open question counts, hand-edited files, running step.", none)
    async def _status(a):
        lines = [f"design: {eng.state.top or '(unnamed)'} · spent ${eng.state.total_cost_usd:.2f} · model {eng.project.cfg.llm.model}"]
        lines += [f"{v.name}: {v.status}, review={v.gate}, open_questions={v.open_questions}" + (f" ({v.detail})" if v.detail else "")
                  + (f", hand-edited {v.edited}" if v.edited else "") for v in eng.status()]
        if eng.running:
            lines.append(f"currently running: {eng.running}")
        return "\n".join(lines)

    @add("list_questions", "All questions (open, answered, default-accepted) with ids and status.", none)
    async def _questions(a):
        return json.dumps(eng.questions(), indent=1)

    @add("answer_question", "Record the user's answer to a question (e.g. Q-M2); the step updates on the next run.",
         _schema({"question_id": ("string", ""), "text": ("string", "")}, ["question_id", "text"]), scope=lambda a: (question_step(a["question_id"]), "answer a question of"))
    async def _answer(a):
        return eng.answer(a["question_id"], a["text"])

    # -- spec template -----------------------------------------------------------------------
    @add("get_template", "The spec template: sections in order with id, title, required, guidance, fields.", none)
    async def _tpl(a):
        return ops.template_text()

    @add("add_section", "Add a section to the spec template (project copy). fields: [{name, guidance, required}].",
         _schema({"title": ("string", ""), "guidance": ("string", "what the section must contain"),
                  "required": ("boolean", "default true"), "fields": ("array", "key facts to state explicitly"),
                  "after": ("string", "section id/title to insert after; omit for the end")}, ["title"]), scope=lambda a: ("spec", "change the template of"))
    async def _add(a):
        return ops.add_section(a["title"], a.get("guidance", ""), a.get("required", True), a.get("fields"), a.get("after"))

    @add("update_section", "Change a template section: title, guidance, required, or its fields (replaces the list).",
         _schema({"section": ("string", "id or title"), "title": ("string", ""), "guidance": ("string", ""),
                  "required": ("boolean", ""), "fields": ("array", "[{name, guidance, required}]")}, ["section"]), scope=lambda a: ("spec", "change the template of"))
    async def _update(a):
        return ops.update_section(a["section"], a.get("title"), a.get("guidance"), a.get("required"), a.get("fields"))

    @add("remove_section", "Remove a section from the spec template.", _schema({"section": ("string", "id or title")}, ["section"]), scope=lambda a: ("spec", "change the template of"))
    async def _remove(a):
        return ops.remove_section(a["section"])

    @add("move_section", "Move a template section after another one ('start' for first) or by delta positions.",
         _schema({"section": ("string", ""), "after": ("string", ""), "delta": ("integer", "")}, ["section"]), scope=lambda a: ("spec", "change the template of"))
    async def _move(a):
        return ops.move_section(a["section"], a.get("after"), a.get("delta"))

    # -- spec content ---------------------------------------------------------------------------
    @add("write_section", "Write/replace one section of spec/spec.md now (creates it if missing). "
         "content: Markdown body without the heading (### for subsections). fields: {field name: value}.",
         _schema({"section": ("string", "template id or title (or a new title)"), "content": ("string", ""),
                  "fields": ("object", "values for the section's template fields"), "title": ("string", "optional heading")},
                 ["section", "content"]), scope=lambda a: ("spec", "edit"))
    async def _write(a):
        return ops.write_section(a["section"], a["content"], a.get("fields"), a.get("title"))

    @add("mark_reviewed", "Mark a spec section (or all when omitted) reviewed / not reviewed.",
         _schema({"section": ("string", ""), "reviewed": ("boolean", "default true")}, []), scope=lambda a: ("spec", "mark sections reviewed in"))
    async def _review(a):
        return ops.mark_reviewed(a.get("section") or None, a.get("reviewed", True))

    @add("set_intent", "Replace spec/intent.md.", _schema({"text": ("string", "")}, ["text"]), scope=lambda a: ("spec", "edit the intent of"))
    async def _intent(a):
        return ops.set_intent(a["text"])

    @add("append_intent", "Append to spec/intent.md.", _schema({"text": ("string", "")}, ["text"]), scope=lambda a: ("spec", "edit the intent of"))
    async def _intent_add(a):
        return ops.append_intent(a["text"])

    # -- pipeline ---------------------------------------------------------------------------------
    @add("run_steps", "Run the pipeline. Optional start/stop/only step, regenerate [steps]. wait=false (default) returns "
         "immediately (progress shows in the activity panel); wait=true waits until the run stops (done, review gate, "
         "error) and returns the outcome, so you can continue (e.g. approve) in the same turn.",
         _schema({"start": ("string", ""), "stop": ("string", ""), "only": ("string", ""),
                  "regenerate": ("array", "step names to rebuild from scratch"), "wait": ("boolean", "")}, []), scope=lambda a: (a.get("only") or "the whole pipeline", "run"))
    async def _run(a):
        regen = {str(x if isinstance(x, str) else x.get("name", "")) for x in a.get("regenerate") or []} - {""}
        args = (a.get("start") or None, a.get("stop") or None, a.get("only") or None, regen or None)
        if a.get("wait"):
            return await host.run_and_wait(*args)
        return host.start_run(*args)

    @add("stop_run", "Stop the running pipeline.", none)
    async def _stop(a):
        return host.stop_run()

    @add("approve", "Approve a review gate (a step of the flow that has one; see `gates`): exactly the step the user named, only when they asked. "
         "step='all' approves every gate waiting for review, only when the user asks to approve all/everything (they confirm). "
         "The step must be done and up to date. force=true approves despite "
         "blocking questions (the user is asked to confirm).",
         _schema({"step": ("string", ""), "force": ("boolean", "")}, ["step"]), scope=lambda a: (None if a["step"] == "all" else a["step"], "approve"))
    async def _approve(a):
        force = bool(a.get("force"))
        if a["step"] == "all":
            pending = eng.pending_approvals()
            if not pending:
                return "nothing waiting for review"
            if not await host.confirm(f"Approve {', '.join(pending)}?", "Open non-blocking questions take their default answers."
                                      + (" Blocking questions are overridden." if force else " Steps with blocking questions are skipped.")):
                return "the user declined"
            return "\n".join(eng.approve_all(force=force))
        if force and not await host.confirm(f"Approve {a['step']} with unanswered blocking questions?"):
            return "the user declined"
        return eng.approve(a["step"], force=force)

    @add("request_change", "Queue a change request for a step of the flow (optionally scoped to one spec section or unit); "
         "applied by the LLM on the next run.",
         _schema({"step": ("string", ""), "text": ("string", ""), "section": ("string", "")}, ["step", "text"]), scope=lambda a: (a["step"], "request a change to"))
    async def _change(a):
        return ops.request_change(a["step"], a["text"], a.get("section") or None)

    @add("reset_step", "Delete a step's generated outputs (and downstream unless only=true) to start over. "
         "The user is asked to confirm.", _schema({"step": ("string", ""), "only": ("boolean", "")}, ["step"]), scope=lambda a: (a["step"], "reset"))
    async def _reset(a):
        plan = eng.reset_plan(a["step"], bool(a.get("only")))
        files = [eng.project.rel(f) for fs in plan.values() for f in fs]
        if not await host.confirm(f"Reset {', '.join(plan)}?", "Deletes: " + (", ".join(files) or "(no generated files)")):
            return "the user declined"
        return ops.reset(a["step"], bool(a.get("only")))

    @add("import_file", "Use a user-provided spec document (kind spec).",
         _schema({"kind": ("string", ""), "path": ("string", "")}, ["kind", "path"]), scope=lambda a: (a["kind"], "replace the files of"))
    async def _import(a):
        return ops.import_files(a["kind"], a["path"])

    # -- gates, VLSIT review, asking the user --------------------------------------------------------
    @add("gates", "Every review gate of the flow with its mode (human | auto | auto_answer | none) and state.", none)
    async def _gates(a):
        return ops.gates_text()

    @add("set_gate_mode", "Set a step's review gate mode: human (stops for the user), auto (approves itself; blocking questions "
         "still stop it), auto_answer (unattended: also takes every default), none (no gate); step='all' for every gate. "
         "Turning a gate to auto / auto_answer asks the user. save=true writes q3tui.yaml.",
         _schema({"step": ("string", "a step id, or 'all'"), "mode": ("string", "human|auto|auto_answer|none"), "save": ("boolean", "")},
                 ["step", "mode"]))
    async def _gate_mode(a):
        if a["mode"] in ("auto", "auto_answer") and not await host.confirm(
                f"Set the gate of '{a['step']}' to {a['mode']}?", "It will " + ("approve itself" if a["mode"] == "auto" else
                                                                             "run unattended: approve itself and take every default") + "."):
            return "the user declined"
        return ops.set_gate_mode(a["step"], a["mode"], bool(a.get("save")))

    @add("set_step_setting", "Customise a step of the flow: key 'view' (extra files its Files tab shows, globs), 'pass' (conditions it "
         "must meet when done: 'exists <glob>', 'contains <file> <regex>', 'run <cmd>'; several split by ';;') or 'notes' (extra "
         "instructions for its LLM tasks). Empty value clears. Saved to q3tui.yaml unless save=false.",
         _schema({"step": ("string", "a step id"), "key": ("string", "view|pass|notes"), "value": ("string", ""), "save": ("boolean", "")},
                 ["step", "key", "value"]))
    async def _step_setting(a):
        return ops.set_step_setting(a["step"], a["key"], a["value"], a.get("save", True))

    @add("get_skill", "Show a step's skill.md (settings, notes and the stage system prompts as `<!-- NAME -->` sections).",
         _schema({"step": ("string", "a step id")}, ["step"]))
    async def _get_skill(a):
        return ops.skill_get(a["step"])["text"]

    @add("set_skill", "Replace a step's skill.md with the given full text (a built-in flow is copied to the project first).",
         _schema({"step": ("string", "a step id"), "text": ("string", "the whole file")}, ["step", "text"]))
    async def _set_skill(a):
        return ops.skill_set(a["step"], a["text"])

    @add("get_prompt", "Show the built-in system prompt(s) recorded for a step or stage (after it ran once) and the user's override.",
         _schema({"target": ("string", "a step id or an exact stage name")}, ["target"]))
    async def _get_prompt(a):
        d = ops.prompt_get(a["target"])
        return f"override ({d['mode']}, {d['file'] or 'none'}):\n{d['text']}\n\nbuilt-in:\n{d['builtin']}"

    @add("set_prompt", "Customise the system prompt of a step (all its stages) or one stage: mode 'append' adds the text to the built-in "
         "prompt, 'replace' swaps it. Empty text removes the override. Saved in <project>/prompts/<target>.md.",
         _schema({"target": ("string", "a step id or an exact stage name"), "text": ("string", ""), "mode": ("string", "append|replace")},
                 ["target", "text"]))
    async def _set_prompt(a):
        return ops.set_prompt(a["target"], a["text"], a.get("mode", "append"))

    @add("ask_user", "Ask the user to choose: a picker with your options plus 'Other…' for their own answer. questions: "
         "[{question, header (short title), options: [{label, description}], multi (boolean)}] — up to 4; returns one answer per "
         "question. Use it for decisions that are the user's (never guess them).",
         {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "object"}, "description": "see above"}},
          "required": ["questions"]})
    async def _ask(a):
        qs = [q for q in (a.get("questions") or []) if isinstance(q, dict) and q.get("question")][:4]
        if not qs:
            raise ValueError("questions: at least one {question, options}")
        answers = await host.ask_user(qs)
        return "\n".join(f"{q.get('header') or q['question']}: {ans}" for q, ans in zip(qs, answers))

    @add("list_properties", "VLSIT flow: the SVA properties with their NL text, vacuity and review status (pending|confirmed|rejected).", none)
    async def _props(a):
        return json.dumps(ops.properties(), indent=1, default=str)

    @add("review_property", "VLSIT flow: confirm / reject (with a reason) / reset an assertion; only what the user asked. "
         "Rejected ones are rewritten by the sva step.",
         _schema({"label": ("string", "assertion label, e.g. a_no_x_on_pc"), "status": ("string", "confirmed|rejected|pending"),
                  "note": ("string", "reason (for rejected)")}, ["label", "status"]), scope=lambda a: ("sva", "review assertions of"))
    async def _review_prop(a):
        return ops.review_property(a["label"], a["status"], a.get("note", ""))

    @add("confirm_all_properties", "VLSIT flow: confirm every pending assertion (vacuous ones stay pending unless include_vacuous); only when "
         "the user asks to confirm them all.", _schema({"include_vacuous": ("boolean", "")}, []), scope=lambda a: ("sva", "review assertions of"))
    async def _confirm_props(a):
        return ops.confirm_properties(bool(a.get("include_vacuous")))

    @add("review_parse_requirement", "VLSIT flow (parse, Gate 1): confirm a flagged requirement as read, or take it back to pending; only "
         "what the user asked. A requirement needing a decision (red) is a blocking question instead — answer it with answer_question.",
         _schema({"req_id": ("string", "REQ-nnn"), "confirmed": ("boolean", "")}, ["req_id", "confirmed"]),
         scope=lambda a: ("parse", "review requirements of"))
    async def _review_req(a):
        return ops.review_parse_requirement(a["req_id"], bool(a["confirmed"]))

    @add("confirm_all_requirements", "VLSIT flow (parse, Gate 1): confirm every requirement still flagged for review; only when the "
         "user asks to confirm them all (never the blocking, red ones — those need an actual answer).", none, scope=lambda a: ("parse", "review requirements of"))
    async def _confirm_reqs(a):
        return ops.confirm_requirements()

    @add("review_config_parameter", "VLSIT flow (config, Gate 2): confirm a parameter changed from default as read, or take it back "
         "to pending; only what the user asked. A parameter at its default needs no review.",
         _schema({"name": ("string", "parameter name, e.g. PR_DATA_W"), "confirmed": ("boolean", "")}, ["name", "confirmed"]),
         scope=lambda a: ("config", "review parameters of"))
    async def _review_param(a):
        return ops.review_parameter(a["name"], bool(a["confirmed"]))

    @add("confirm_all_parameters", "VLSIT flow (config, Gate 2): confirm every parameter still changed from default and not yet "
         "reviewed; only when the user asks to confirm them all.", none, scope=lambda a: ("config", "review parameters of"))
    async def _confirm_params(a):
        return ops.confirm_parameters()

    @add("rtm_status", "VLSIT flow: the requirements traceability matrix — the six sign-off conditions per requirement.", none)
    async def _rtm(a):
        return json.dumps(ops.rtm(), indent=1, default=str)

    @add("sign_requirement", "VLSIT flow: sign a requirement off in the RTM (only when the user says so; refused unless RTL, SVA, TC, "
         "simulation and mutation hold; signed=false withdraws it).",
         _schema({"req_id": ("string", "REQ-nnn"), "signed": ("boolean", "default true"), "note": ("string", "")}, ["req_id"]),
         scope=lambda a: ("verify", "sign off requirements in"))
    async def _sign_req(a):
        return ops.sign_req(a["req_id"], a.get("signed", True), a.get("note", ""))

    @add("sign_all_ready_requirements", "VLSIT flow: sign off every requirement whose conditions already hold (RTL, SVA, TC, "
         "simulation and mutation) — \"approve all\" at Gate 5; only when the user asks to sign them all. Requirements that are "
         "not ready are left alone.", _schema({"note": ("string", "")}, []), scope=lambda a: ("verify", "sign off requirements in"))
    async def _sign_all(a):
        return ops.sign_all_ready(a.get("note", ""))

    # -- settings ---------------------------------------------------------------------------------
    @add("set_model", "Change the LLM model/effort for the next stages; save=true writes the project q3tui.yaml.",
         _schema({"model": ("string", ""), "effort": ("string", "low|medium|high|xhigh|max|none"), "save": ("boolean", "")}, []))
    async def _model(a):
        effort = a.get("effort")
        return ops.set_model(a.get("model") or None, False if effort is None else (None if effort == "none" else effort), bool(a.get("save")))

    @add("set_spec_review", "Turn the spec review pass on or off (off: faster full drafts, no ambiguity check). save=true writes q3tui.yaml.",
         _schema({"on": ("boolean", ""), "save": ("boolean", "")}, ["on"]))
    async def _spec_review(a):
        return ops.set_spec_review(bool(a["on"]), bool(a.get("save")))

    @add("set_auto_approve", "Turn auto-approve of review gates on or off (the user is asked to confirm turning it on).",
         _schema({"on": ("boolean", ""), "save": ("boolean", "")}, ["on"]))
    async def _auto_approve(a):
        if a["on"] and not await host.confirm("Turn on auto-approve?", "Review gates will approve themselves (blocking questions still stop the run)."):
            return "the user declined"
        return ops.set_auto_approve(bool(a["on"]), bool(a.get("save")))

    @add("set_auto_answer", "Turn unattended runs on or off: gates approve themselves, every question takes its default and "
         "proposals are accepted (the user is asked to confirm turning it on).",
         _schema({"on": ("boolean", ""), "save": ("boolean", "")}, ["on"]))
    async def _auto_answer(a):
        if a["on"] and not await host.confirm("Turn on auto-answer?", "Runs go start to finish: every question takes its "
                                              "default and proposals are accepted without you."):
            return "the user declined"
        return ops.set_auto_answer(bool(a["on"]), bool(a.get("save")))

    @add("set_auto_confirm_reviews", "Turn auto-confirm of a step's own reviewable items (SVA assertions, flagged requirements) on or "
         "off; gates still stop for Approve either way (the user is asked to confirm turning it on).",
         _schema({"on": ("boolean", ""), "save": ("boolean", "")}, ["on"]))
    async def _auto_confirm(a):
        if a["on"] and not await host.confirm("Turn on auto-confirm reviews?",
                                              "SVA assertions / flagged requirements are confirmed without you reading them; "
                                              "gates still stop for Approve."):
            return "the user declined"
        return ops.set_auto_confirm_reviews(bool(a["on"]), bool(a.get("save")))

    @add("get_settings", "Every user-facing setting (the TUI settings dialog: general, llm, per step) with its current value and help.", none)
    async def _get_settings(a):
        from q3tui.core import settings

        vals = ops.settings()
        return "\n".join(f"{t.title}:\n" + "\n".join(
            f"  {s.path} = {json.dumps(vals[s.path])}  ({s.kind}{', or null' if s.nullable else ''}"
            + (f"; one of {[v for v, _ in s.choices]}" if s.choices else "") + f") — {s.help}" for s in t.settings)
            for t in settings.TABS)

    @add("set_settings", "Change settings by path, e.g. {\"llm.effort\": \"low\", \"spec.self_review\": false}; use "
         "get_settings for paths and allowed values. save=true also writes them to the project's q3tui.yaml. "
         "Turning on pipeline.auto_approve or pipeline.auto_answer asks the user.",
         {"type": "object", "properties": {"changes": {"type": "object", "description": "path -> new value"},
                                           "save": {"type": "boolean", "description": ""}}, "required": ["changes"]})
    async def _set_settings(a):
        changes = a.get("changes") or {}
        if changes.get("pipeline.auto_approve") in (True, "on", "true") and not await host.confirm(
                "Turn on auto-approve?", "Review gates will approve themselves (blocking questions still stop the run)."):
            return "the user declined"
        if changes.get("pipeline.auto_answer") in (True, "on", "true") and not await host.confirm(
                "Turn on auto-answer?", "Runs go start to finish: every question takes its default and proposals "
                                        "are accepted without you."):
            return "the user declined"
        return ops.apply_settings(changes, bool(a.get("save")))

    @add("show_stats", "Usage statistics: tokens (in, cache, out, thinking) and cost per model and per step, LLM time and "
         "step wall time.", none)
    async def _stats(a):
        return ops.stats_text()

    @add("show_cost", "LLM cost recorded for this project and this session.", none)
    async def _cost(a):
        return ops.cost_text()

    @add("reset_stats", "Start the usage statistics over (tokens, cost, times; the user is asked to confirm).", none)
    async def _reset_stats(a):
        if not await host.confirm("Reset the usage statistics?", "Counting starts from now; the run logs are kept."):
            return "the user declined"
        return ops.reset_stats()

    @add("reset_cost", "Reset the project's LLM cost counter to $0 (the user is asked to confirm).", none)
    async def _reset_cost(a):
        if not await host.confirm("Reset the LLM cost counter to $0?"):
            return "the user declined"
        return ops.reset_cost()

    return tools


class Assistant:
    def __init__(self, host: AssistantHost, session_file: Path):
        self.host = host
        self.session_file = session_file
        self.session_id: str | None = (read_json(session_file, default={}) or {}).get("session_id")

    def reset(self) -> None:
        self.session_id = None
        write_json(self.session_file, {})

    async def ask(self, text: str, emit: StepEmitter, context: str | None = None) -> str:
        project = self.host.engine.project
        stage = Stage(
            name="assistant",
            system_prompt=SYSTEM,
            prompt=f"[TUI context] {context}\n\n{text}" if context else text,
            cwd=project.root,
            builtin_tools=["Read", "Grep", "Glob", "Edit", "Write"],
            sdk_tools=assistant_tools(self.host),
            max_turns=40,
            resume=self.session_id,
            deny_dirs=[project.state_dir],
            write_dirs=[project.spec_dir, project.schemas_dir, project.src_dir],
        )
        result = await runtime.run_stage(stage, project.cfg, emit)
        if result.session_id:
            self.session_id = result.session_id
            write_json(self.session_file, {"session_id": self.session_id})
        return result.text
