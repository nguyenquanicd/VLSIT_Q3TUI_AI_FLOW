"""User operations, shared by the TUI keys and the assistant (and usable from the CLI).

Everything a user can do to a project goes through here, so a prompt to the assistant
and a key press in the TUI do exactly the same thing.
"""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.pipeline.engine import Engine, EngineError
from q3tui.steps.spec import document
from q3tui.steps.spec.document import FieldValue
from q3tui.steps.spec.template import (
    SpecTemplate,
    TemplateField,
    TemplateSection,
    load_template,
    save_project_template,
)


def import_summary(report: dict) -> str:
    """What `Engine.import_path` did, as text: per kind the files taken, what no step takes, what was left out."""
    lines = [f"imported from {report['source']}:"]
    where = {"spec": "spec documents (references: the spec step ports them into spec/spec.md with the template's sections)", "rtl": "RTL (the rtl step checks it against the RTL rules and updates what does "
             "not follow them)", "tb": "testbench / tests (references: the tb step ports them into its test cases)",
             "sva": "SVA (references: the sva step ports them)"}
    for kind, label in where.items():
        if report.get(kind):
            files = report[kind]
            lines.append(f"- {len(files)} {label}: " + ", ".join(files[:8]) + (f" (+{len(files) - 8} more)" if len(files) > 8 else ""))
    for kind, files in report.get("not_taken", {}).items():
        lines.append(f"- {len(files)} {kind} file(s) not imported: no step of this flow takes {kind}")
    if report.get("skipped"):
        sk = report["skipped"]
        lines.append(f"- left out ({len(sk)}: scripts, filelists, other files): " + ", ".join(sk[:8]) + (" …" if len(sk) > 8 else ""))
    if len(lines) == 1:
        lines.append("- nothing to import")
    return "\n".join(lines)


class Ops:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.project = engine.project

    def _log(self, step: str | None, message: str) -> str:
        self.engine.bus.emit("log", step, message=message)
        return message

    def _not_running(self, what: str) -> None:
        if self.engine.running:
            raise EngineError(f"'{self.engine.running}' is running; stop it before you {what}")

    # -- spec template ---------------------------------------------------------------------

    def template(self) -> SpecTemplate:
        return load_template(self.project.spec_dir, self.engine.spec_template())[0]

    def template_text(self) -> str:
        t = self.template()
        lines = [f"allow_extra_sections: {t.allow_extra_sections}"]
        for i, s in enumerate(t.sections, 1):
            lines.append(f"{i}. {s.id} — {s.title} ({'required' if s.required else 'optional'})")
            if s.guidance:
                lines.append(f"   guidance: {' '.join(s.guidance.split())}")
            for f in s.fields:
                lines.append(f"   field: {f.name}{'' if f.required else ' (optional)'} — {f.guidance}")
        return "\n".join(lines)

    def _save_template(self, t: SpecTemplate, message: str) -> str:
        path = save_project_template(self.project.spec_dir, t)
        return self._log("spec", f"{message} → {self.project.rel(path)}; the spec is now out of date (run to apply)")

    @staticmethod
    def section_id(title: str) -> str:
        sid = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "section"
        return sid if sid[0].isalpha() else f"s_{sid}"

    def _find(self, t: SpecTemplate, ref: str) -> TemplateSection:
        for s in t.sections:
            if ref in (s.id, s.title) or ref.lower() == s.title.lower():
                return s
        raise EngineError(f"no section '{ref}' in the template (sections: {', '.join(s.id for s in t.sections)})")

    def add_section(self, title: str, guidance: str = "", required: bool = True,
                    fields: list[dict] | None = None, after: str | None = None) -> str:
        t = self.template()
        sid = self.section_id(title)
        while sid in t.by_id():
            sid += "_2"
        section = TemplateSection(id=sid, title=title, required=required, guidance=guidance,
                                  fields=[TemplateField(**f) if isinstance(f, dict) else TemplateField(name=str(f)) for f in fields or []])
        ids = [s.id for s in t.sections]
        pos = ids.index(self._find(t, after).id) + 1 if after else len(ids)
        t.sections.insert(pos, section)
        return self._save_template(t, f"added section '{title}' (id {sid})")

    def remove_section(self, ref: str) -> str:
        t = self.template()
        s = self._find(t, ref)
        if len(t.sections) == 1:
            raise EngineError("the template needs at least one section")
        t.sections.remove(s)
        return self._save_template(t, f"removed section '{s.title}'")

    def update_section(self, ref: str, title: str | None = None, guidance: str | None = None,
                       required: bool | None = None, fields: list[dict] | None = None) -> str:
        t = self.template()
        s = self._find(t, ref)
        changes = []
        if title:
            s.title, changes = title, [*changes, "title"]
        if guidance is not None:
            s.guidance, changes = guidance, [*changes, "guidance"]
        if required is not None:
            s.required, changes = required, [*changes, "required" if required else "optional"]
        if fields is not None:
            s.fields = [TemplateField(**f) if isinstance(f, dict) else TemplateField(name=str(f)) for f in fields]
            changes.append("fields")
        if not changes:
            return "nothing to change"
        return self._save_template(t, f"updated section '{s.title}' ({', '.join(changes)})")

    def move_section(self, ref: str, after: str | None = None, delta: int | None = None) -> str:
        t = self.template()
        s = self._find(t, ref)
        ids = [x.id for x in t.sections]
        i = ids.index(s.id)
        t.sections.pop(i)
        if after is not None:
            j = 0 if after in ("", "start", "top") else [x.id for x in t.sections].index(self._find(t, after).id) + 1
        else:
            j = max(0, min(len(t.sections), i + (delta or 0)))
        t.sections.insert(j, s)
        return self._save_template(t, f"moved section '{s.title}' to position {j + 1}")

    # -- spec content -----------------------------------------------------------------------

    def write_section(self, ref: str, content: str, fields: dict[str, str] | None = None, title: str | None = None) -> str:
        """Write one section of spec.md directly (create it if missing). Counts as an edit of the spec."""
        spec = self.project.spec_dir / "spec.md"
        if not spec.is_file():
            raise EngineError("spec/spec.md does not exist yet; run the spec step first (or write an intent)")
        t = self.template()
        try:
            ts = self._find(t, ref)
            sid, sec_title = ts.id, title or ts.title
            names = [f.name for f in ts.fields]
        except EngineError:
            sid, sec_title, names = self.section_id(title or ref), title or ref, []
        given = fields or {}
        extra = [k for k in given if k not in names]
        field_values = [FieldValue(name=n, value=given[n]) for n in names if n in given] + [FieldValue(name=k, value=given[k]) for k in extra]
        body = document.section_body(field_values, content)
        self.project.backup([spec])
        spec.write_text(document.upsert_section(spec.read_text(), sid, sec_title, body, [s.id for s in t.sections]))
        from q3tui.steps.spec.sections import current_sections

        assert sid in current_sections(self.engine)
        return self._log("spec", f"wrote section '{sec_title}' in spec/spec.md (the steps after it are now out of date)")

    def set_section_body(self, ref: str, body: str, title: str | None = None) -> str:
        """Replace one section's Markdown body (fields table included) in spec.md as the user typed it."""
        spec = self.project.spec_dir / "spec.md"
        if not spec.is_file():
            raise EngineError("spec/spec.md does not exist yet")
        t = self.template()
        from q3tui.steps.spec.sections import current_sections

        written = current_sections(self.engine)
        sid = ref if ref in written else next((k for k, (ti, _) in written.items() if ti.lower() == ref.lower()), None)
        if sid is None:
            try:
                sid = self._find(t, ref).id
            except EngineError:
                sid = self.section_id(title or ref)
        sec_title = title or written.get(sid, (None,))[0] or (t.by_id()[sid].title if sid in t.by_id() else ref)
        self.project.backup([spec])
        spec.write_text(document.upsert_section(spec.read_text(), sid, sec_title, body, [s.id for s in t.sections]))
        return self._log("spec", f"edited section '{sec_title}' (the steps after it are now out of date)")

    def mark_reviewed(self, ref: str | None = None, reviewed: bool = True) -> str:
        from q3tui.steps.spec.sections import mark_reviewed

        sid = None
        if ref:
            from q3tui.steps.spec.sections import current_sections

            written = current_sections(self.engine)
            sid = ref if ref in written else next((k for k, (title, _) in written.items() if title.lower() == ref.lower()), None)
            if sid is None:
                raise EngineError(f"section '{ref}' is not in spec/spec.md")
        mark_reviewed(self.engine, sid, reviewed)
        what = f"section '{ref}'" if ref else "all sections"
        return self._log("spec", f"marked {what} {'reviewed' if reviewed else 'not reviewed'}")

    def set_intent(self, text: str) -> str:
        path = self.engine.import_intent(text)
        return self._log("spec", f"wrote {self.project.rel(path)}")

    def append_intent(self, text: str) -> str:
        path = self.project.spec_dir / "intent.md"
        current = path.read_text().rstrip() + "\n\n" if path.is_file() else ""
        return self.set_intent(current + text.strip())

    # -- pipeline -----------------------------------------------------------------------------

    def request_change(self, step: str, text: str, section: str | None = None) -> str:
        if section:
            title = section
            try:
                title = self._find(self.template(), section).title
            except EngineError:
                pass
            text = f"In section '{title}': {text}"
        return self.engine.request_change(step, text)

    def reset(self, step: str, only: bool = False) -> str:
        self._not_running("reset")
        files = self.engine.reset(step, only)
        return f"reset {step}{' only' if only else ' and downstream'}: removed {len(files)} file(s) (backed up)"

    def import_files(self, kind: str, path: str) -> str:
        """Import a file or a whole folder. kind `all`: sorted by code into spec documents, RTL, testbench / tests and
        SVA (core/importer.py); spec | rtl | tb | sva: only that kind."""
        from q3tui.core.importer import KINDS

        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project.root / p
        if not p.exists():
            raise EngineError(f"no such file or folder: {path}")
        if kind not in ("all", *KINDS):
            raise EngineError(f"kind must be all, {', '.join(KINDS)}")
        self._not_running("import")
        report = self.engine.import_path(p, None if kind == "all" else {kind})
        return self._log(None, import_summary(report))

    # -- settings -------------------------------------------------------------------------------

    def stats_text(self) -> str:
        """Usage statistics (tokens per model / step, cost, LLM and wall time) as text."""
        from q3tui.core import stats

        return stats.text_report(stats.report(self.project.state_dir), self.project.cfg.llm.stage_token_budget)

    def settings(self) -> dict:
        """Current values of every user-facing setting (see q3tui/core/settings.py)."""
        from q3tui.core import settings

        return settings.current(self.project.cfg)

    def apply_settings(self, changes: dict, save: bool = False) -> str:
        """Validate and apply {"llm.effort": "low", ...}; `save` also writes them to q3tui.yaml."""
        from pydantic import ValidationError

        from q3tui.core import settings
        from q3tui.core.config import Config

        cfg = self.project.cfg
        values: dict = {}
        for path, raw in changes.items():
            setting = settings.BY_PATH.get(path)
            if setting is None:
                raise EngineError(f"unknown setting '{path}' (known: {', '.join(settings.BY_PATH)})")
            try:
                values[path] = settings.coerce(setting, raw)
            except ValueError as exc:
                raise EngineError(str(exc)) from None
        if str(values.get("llm.model", "")).startswith("claude-haiku-4") and "llm.effort" not in changes:
            values["llm.effort"] = None  # Haiku 4.5 has no effort setting
        values = {p: v for p, v in values.items() if settings.get_value(cfg, p) != v}
        if not values:
            return "settings unchanged"
        try:  # validate against the real schema before touching the live config
            from q3tui.core.config import _merge

            checked = Config.model_validate(_merge(cfg.model_dump(), settings.nested(values)))
        except ValidationError as exc:
            raise EngineError("invalid setting: " + "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())) from None
        for path in values:
            section, leaf = path.rsplit(".", 1)
            setattr(settings.get_value(cfg, section), leaf, settings.get_value(checked, path))
        shown = ", ".join(f"{p} = {'none' if v is None else v}" for p, v in values.items())
        msg = f"settings: {shown}"
        if save:
            msg += f" (saved to {self.project.rel(self.project.save_config_values(settings.nested(values)))})"
        return self._log(None, msg)

    def set_model(self, model: str | None = None, effort: str | None | bool = False, save: bool = False) -> str:
        """Change the LLM for subsequent stages; `effort=False` leaves effort unchanged."""
        llm = self.project.cfg.llm
        if model:
            llm.model = model
            if model.startswith("claude-haiku-4"):
                llm.effort = None
        if effort is not False:
            if effort not in (None, "low", "medium", "high", "xhigh", "max"):
                raise EngineError(f"unknown effort '{effort}'")
            llm.effort = effort
        msg = f"model: {llm.model} · effort: {llm.effort or 'n/a'}"
        if save:
            path = self.project.save_config_values({"llm": {"model": llm.model, "effort": llm.effort}})
            msg += f" (saved to {self.project.rel(path)})"
        if self.engine.running:
            msg += " — the running stage keeps its model; the next stage uses this"
        return self._log(None, msg)

    def set_spec_review(self, on: bool, save: bool = False) -> str:
        """Turn the spec step's self-review pass on/off (off = faster first drafts, no ambiguity check)."""
        self.project.cfg.spec.self_review = on
        msg = f"spec review: {'on' if on else 'off'}"
        if save:
            path = self.project.save_config_values({"spec": {"self_review": on}})
            msg += f" (saved to {self.project.rel(path)})"
        return self._log(None, msg + ("" if on else " — full spec drafts skip the review pass"))

    def set_auto_approve(self, on: bool, save: bool = False) -> str:
        """Approve review gates automatically; a gate with blocking questions still stops and asks."""
        self.project.cfg.pipeline.auto_approve = on
        msg = f"auto-approve: {'on — review gates approve themselves unless a blocking question is open' if on else 'off'}"
        if save:
            msg += f" (saved to {self.project.rel(self.project.save_config_values({'pipeline': {'auto_approve': on}}))})"
        return self._log(None, msg)

    def set_auto_answer(self, on: bool, save: bool = False) -> str:
        """Unattended runs: every gate approves itself, every question takes its default, proposals are
        accepted as suggested — the run goes from start to finish without stopping for you."""
        self.project.cfg.pipeline.auto_answer = on
        msg = (f"auto-answer: {'on — runs go start to finish: gates approve themselves, questions take their defaults, '
               'proposals are accepted' if on else 'off'}")
        if save:
            msg += f" (saved to {self.project.rel(self.project.save_config_values({'pipeline': {'auto_answer': on}}))})"
        return self._log(None, msg)

    def set_auto_confirm_reviews(self, on: bool, save: bool = False) -> str:
        """Confirm a step's own reviewable items (SVA assertions, flagged requirements, …) without asking. A human
        gate still stops for Approve; Approve itself no longer refuses for unconfirmed items."""
        self.project.cfg.pipeline.auto_confirm_reviews = on
        msg = f"auto-confirm reviews: {'on — gates still stop, but no longer wait for per-item review' if on else 'off'}"
        if save:
            msg += f" (saved to {self.project.rel(self.project.save_config_values({'pipeline': {'auto_confirm_reviews': on}}))})"
        return self._log(None, msg)

    def set_parallel_rtl_tb(self, on: bool, multi_agent: bool | None = None, save: bool = False) -> str:
        """vlsit: run rtl (lint + synthesis) and tb (testbench generator) together instead of one after the
        other — neither reads the other's output. multi_agent (only meaningful when on): tb gets its own
        persistent LLM session instead of queuing behind rtl's on the flow's shared one (real concurrency
        for the LLM calls too, not just their lint/synth/tool runs)."""
        pc = self.project.cfg.pipeline
        pc.parallel_rtl_tb = on
        if multi_agent is not None:
            pc.parallel_multi_agent = multi_agent and on
        msg = f"parallel rtl/tb: {'on' if on else 'off'}"
        if on:
            msg += f" ({'separate agent for tb' if pc.parallel_multi_agent else 'shared session: tb queues behind rtl'})"
        if save:
            values = {"parallel_rtl_tb": pc.parallel_rtl_tb}
            if multi_agent is not None:
                values["parallel_multi_agent"] = pc.parallel_multi_agent
            msg += f" (saved to {self.project.rel(self.project.save_config_values({'pipeline': values}))})"
        return self._log(None, msg)

    # -- questions, VLSIT review (steps/vlsit/review.py) ---------------------------------------------

    def answer(self, question_id: str, text: str) -> str:
        """Answer a question (the step, and the step it is handed to, update on the next run)."""
        return self.engine.answer(question_id, text)

    def answer_all(self) -> str:
        """Answer every open question with its default assumption."""
        return self._log(None, self.engine.answer_all())

    def _review(self):
        import importlib

        try:
            return importlib.import_module("q3tui.flows.vlsit.sva.review")
        except ImportError as exc:  # no such module yet: a friendly message, never a traceback
            raise EngineError(f"the VLSIT review module is not available ({exc})") from None

    def properties(self) -> list[dict]:
        """The SVA properties with their NL text, vacuity and your review status (VLSIT flow, step sva)."""
        return self._review().load_properties(self.engine)

    def review_property(self, label: str, status: str, note: str = "") -> str:
        """Confirm / reject / reset an assertion (status confirmed | rejected | pending). Rejected → a change request to the sva step."""
        if status not in ("confirmed", "rejected", "pending"):
            raise EngineError(f"unknown review status '{status}' (confirmed, rejected, pending)")
        self._review().review_property(self.engine, label, status, note)
        return self._log("sva", f"assertion {label}: {status}" + (f" — {note}" if note else ""))

    def confirm_properties(self, include_vacuous: bool = False) -> str:
        """Confirm every pending assertion (vacuous ones only when asked) — "approve all" at Gate 3b."""
        return self._log("sva", self._review().confirm_all(self.engine, include_vacuous))

    def rtm(self) -> list[dict]:
        """The requirements traceability matrix rows (six conditions per requirement)."""
        return self._review().rtm_rows(self.engine)

    def sign_req(self, req_id: str, signed: bool = True, note: str = "") -> str:
        """Sign a requirement off in the RTM (you, never the LLM); refused unless its conditions 1–5 hold."""
        msg = self._review().sign_req(self.engine, req_id, signed, note)
        return self._log("verify", msg if isinstance(msg, str) and msg else f"{req_id}: {'signed off' if signed else 'sign-off withdrawn'}")

    def sign_all_ready(self, note: str = "") -> str:
        """Sign off every requirement whose conditions 1-5 already hold — "approve all" at Gate 5 (requirements that
        are not ready are left alone; sign each one yourself once it is)."""
        return self._log("verify", self._review().sign_all(self.engine, note))

    def _parse_review(self):
        import importlib

        try:
            return importlib.import_module("q3tui.flows.vlsit.parse.review")
        except ImportError as exc:  # no such module yet: a friendly message, never a traceback
            raise EngineError(f"the VLSIT review module is not available ({exc})") from None

    def requirement_reviews(self) -> dict[str, dict]:
        """req_id -> {status, at} for every requirement you have confirmed or taken back to pending (Gate 1)."""
        return self._parse_review().reviews(self.engine)

    def review_parse_requirement(self, req_id: str, confirmed: bool) -> str:
        """VLSIT flow: confirm a flagged requirement as read, or take it back to pending (Gate 1)."""
        return self._log("parse", self._parse_review().review_requirement(self.engine, req_id, confirmed))

    def confirm_requirements(self) -> str:
        """Confirm every requirement still flagged for review — "approve all" at Gate 1 (blocking ones need an
        actual answer; this never touches them)."""
        return self._log("parse", self._parse_review().confirm_all(self.engine))

    def _config_review(self):
        import importlib

        try:
            return importlib.import_module("q3tui.flows.vlsit.config.review")
        except ImportError as exc:  # no such module yet: a friendly message, never a traceback
            raise EngineError(f"the VLSIT review module is not available ({exc})") from None

    def parameter_reviews(self) -> dict[str, dict]:
        """name -> {status, at} for every config parameter you have confirmed or taken back to pending (Gate 2)."""
        return self._config_review().reviews(self.engine)

    def review_parameter(self, name: str, confirmed: bool) -> str:
        """VLSIT flow: confirm a parameter changed from default, or take it back to pending (Gate 2)."""
        return self._log("config", self._config_review().review_parameter(self.engine, name, confirmed))

    def confirm_parameters(self) -> str:
        """Confirm every parameter still changed from default and not yet reviewed — "approve all" at Gate 2."""
        return self._log("config", self._config_review().confirm_all(self.engine))

    def set_gate_mode(self, step: str, mode: str, save: bool = False) -> str:
        """A step's review gate: human (stops for you), auto (approves itself unless a blocking question is open),
        auto_answer (unattended: also takes every default), none (no gate). `step` = "all": every gated step."""
        from q3tui.flows import GATE_MODES

        if mode not in GATE_MODES:
            raise EngineError(f"unknown gate mode '{mode}' (one of {', '.join(GATE_MODES)})")
        eng = self.engine
        if step == "all":
            names = list(eng.gate_modes())
        else:
            eng.step(step)
            names = [step]
        modes = self.project.cfg.pipeline.gate_modes
        for n in names:
            modes[n] = mode
        if mode != "none":  # a step that had no gate gets one
            if "gates" in self.project.cfg.pipeline.model_fields_set and step != "all" and step not in self.project.cfg.pipeline.gates:
                self.project.cfg.pipeline.gates.append(step)
        msg = f"gate {', '.join(names)}: {mode}" + {"human": " — stops for your review", "auto": " — approves itself (blocking questions still stop it)",
                                                     "auto_answer": " — unattended: approves itself, questions take their defaults",
                                                     "none": " — no review gate"}[mode]
        if save:
            msg += f" (saved to {self.project.rel(self.project.save_config_values({'pipeline': {'gate_modes': {n: mode for n in names}}}))})"
        return self._log(None, msg)

    def set_step_setting(self, step: str, key: str, value: str | list[str], save: bool = True) -> str:
        """Customise a step of the flow: `view` (extra files shown, globs), `pass` (conditions it must meet: `exists <glob>`,
        `contains <file> <regex>`, `run <cmd>`) or `notes` (instructions for its LLM tasks). Empty value: clear it."""
        from q3tui.flows import STEP_KEYS, apply_overrides

        if key not in STEP_KEYS:
            raise EngineError(f"unknown setting '{key}' (one of {', '.join(STEP_KEYS)})")
        self.engine.step(step)
        if key != "notes" and isinstance(value, str):
            value = [v.strip() for v in value.split(";;") if v.strip()]
        self.project.cfg.pipeline.step_flow.setdefault(step, {})[key] = value
        apply_overrides(self.engine.flow, {step: {key: value}})
        msg = f"{step}: {key} = {value!r}" if value else f"{step}: {key} cleared"
        if save:
            msg += f" (saved to {self.project.rel(self.project.save_config_values({'pipeline': {'step_flow': {step: {key: value}}}}))})"
        return self._log(None, msg)

    def save_flow_steps(self, steps: list[dict]) -> str:
        """Add / remove / reorder / rewire the flow's steps: `steps` = the new list (id, kind, deps, gate, title, label, options,
        view, pass, notes). Saved into the project's flows/ (a built-in flow is forked there); takes effect when Q3TUI restarts."""
        from q3tui import flows

        old = self.engine.flow
        data = old.to_file_dict()
        data["steps"] = [{k: v for k, v in st.items() if v not in ("", None) and k not in ("gate_set",)} for st in steps]
        try:
            flow = flows.parse_flow(data, old.path)
            flow.path = old.path
            path = flows.save_flow(flow, self.project.root)
        except flows.FlowError as exc:
            raise EngineError(str(exc)) from exc
        return self._log(None, f"flow {flow.name} saved to {self.project.rel(path)} ({len(flow.steps)} steps) — restart Q3TUI to use it")

    # -- skills: <flow folder>/<step>/md/skill.md ------------------------------------------------------

    def _skill_dir(self) -> Path:
        path = self.engine.flow.path
        if not (path and path.name == "FLOW.md"):
            raise EngineError(f"flow '{self.engine.flow.name}' is not a flow folder: it has no skill files")
        return path.parent

    def skill_steps(self) -> list[str]:
        d = self._skill_dir()
        return [s.name for s in self.engine.steps if (d / s.name / "md" / "skill.md").is_file()]

    def skill_get(self, step: str) -> dict:
        """{text: the step's skill.md, file}."""
        f = self._skill_dir() / step / "md" / "skill.md"
        if not f.is_file():
            raise EngineError(f"step '{step}' has no skill.md")
        return {"text": f.read_text(), "file": str(f)}

    def skill_set(self, step: str, text: str) -> str:
        """Save a step's skill.md (front matter + notes + `<!-- NAME -->` prompt sections). A built-in flow is first copied to
        `<project>/flows/<name>/` (the copy wins by name). Notes, view and pass apply at once; deps / kind / gate / options on restart."""
        import shutil

        from q3tui import flows

        try:
            meta, notes = flows._front(text)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"the front matter is not valid YAML ({exc})") from exc
        eng, src = self.engine, self._skill_dir()
        dest = self.project.root / "flows" / eng.flow.name
        if src.resolve() != dest.resolve():  # fork the folder's markdown (the code stays in the package)
            for f in [src / "FLOW.md", *src.glob("*/md/skill.md")]:
                t = dest / f.relative_to(src)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
            eng.flow.path = dest / "FLOW.md"
        (dest / step / "md").mkdir(parents=True, exist_ok=True)
        (dest / step / "md" / "skill.md").write_text(text if text.endswith("\n") else text + "\n")
        spec = eng.flow.spec(step)
        if spec is not None:
            spec.notes = flows._SECTION.sub("", notes).strip()
            for key, attr in (("view", "view"), ("pass", "passes")):
                v = meta.get(key) or []
                setattr(spec, attr, [v] if isinstance(v, str) else [str(x) for x in v])
        return self._log(None, f"skill {step}: saved to {self.project.rel(dest / step / 'md' / 'skill.md')}")

    # -- prompts (q3tui/llm/prompts.py) ----------------------------------------------------------------

    def prompt_targets(self) -> list[str]:
        from q3tui.llm import prompts

        return prompts.targets(self.project.root, self.project.state_dir, [s.name for s in self.engine.steps])

    def prompt_get(self, target: str) -> dict:
        """{builtin: recorded system prompts of the step's stages, mode, text: the override, file}."""
        from q3tui.llm import prompts

        mode, text = prompts.read(self.project.root, target)
        f = prompts.find(self.project.root, target)
        return {"builtin": prompts.builtin(self.project.state_dir, target), "mode": mode, "text": text,
                "file": self.project.rel(f) if f else None}

    def set_prompt(self, target: str, text: str, mode: str = "append") -> str:
        """Customise the system prompt of a step (all its stages) or of one stage: `append` adds your text, `replace` swaps the
        built-in prompt for it. Empty text removes the override."""
        from q3tui.llm import prompts

        if mode not in prompts.MODES:
            raise EngineError(f"mode must be one of {', '.join(prompts.MODES)}")
        path = prompts.write(self.project.root, target, text, mode)
        return self._log(None, f"prompt {target}: " + (f"{mode} saved to {self.project.rel(path)}" if path else "override removed"))

    def gates_text(self) -> str:
        """Every review gate of the flow with its mode and state."""
        eng = self.engine
        modes = eng.gate_modes()
        lines = [f"flow {eng.flow.name}: gates"]
        for v in eng.status():
            mode = modes.get(v.name)
            lines.append(f"  {v.name:<12} {mode or 'no gate':<12} {v.gate if mode else ''}")
        return "\n".join(lines)

    def cost_text(self) -> str:
        s = self.engine.cost_summary()
        per_step = ", ".join(f"{n} ${u:.2f}" for n, u in s["steps"].items()) or "no step costs"
        return f"pipeline ${s['total_usd']:.2f} ({per_step}); this session ${self.engine.bus.cost_usd:.2f}"

    def reset_stats(self) -> str:
        """Usage statistics start over (tokens, cost, LLM and wall time per model / step / stage)."""
        from q3tui.core import stats

        return self._log(None, stats.reset(self.project.state_dir))

    def reset_cost(self) -> str:
        msg = self.engine.reset_cost()
        self.engine.bus.cost_usd = 0.0
        return self._log(None, msg)
