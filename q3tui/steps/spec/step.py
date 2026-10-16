"""Step 1 — spec: write (or revise) spec/spec.md from spec/intent.md, and/or port the user's own (imported) documents
in spec/ref/ into the template's sections."""

from __future__ import annotations

from pathlib import Path

from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepDef, StepFailed
from q3tui.core.project import read_json, write_json
from q3tui.steps.common import Question, assign_question_ids, load_answers, render_questions, save_answer
from q3tui.steps.spec import document, prompts
from q3tui.core.project import sha256_file
from q3tui.steps.spec.document import ReviewPatch, SpecDraft, SpecPatch
from q3tui.steps.spec.template import describe, find_template, load_template

__all__ = ["SpecDraft", "SpecStep"]


class SpecStep(StepDef):
    name = "spec"
    title = "Specification"
    deps = ()

    # -- files ----------------------------------------------------------------------------

    def _paths(self, engine) -> tuple[Path, Path, Path, Path, Path]:
        d = engine.project.spec_dir
        return d / "intent.md", d / "spec.md", d / "questions.json", d / "questions.md", d / "answers.json"

    def inputs(self, engine) -> list[Path]:
        intent, _, _, _, answers = self._paths(engine)
        imported = engine.import_record_path("spec")  # (your imported documents, once there are some)
        return [intent, answers, find_template(engine.project.spec_dir, self.options.get("template")),
                *([imported] if imported.is_file() else [])]

    # -- imports: your documents are ported into the template's sections --------------------------------

    def import_dir(self, engine, kind: str) -> Path | None:
        """Imported documents are references in spec/ref/ (not spec documents themselves): this step ports them into
        spec/spec.md with the template's sections. (`--spec FILE` still takes a document as the spec, as it is.)"""
        return engine.project.spec_dir / "ref" if kind == "spec" else None

    def on_import(self, engine, kind: str, files: list[Path]) -> None:
        from q3tui.core.project import sha256_file as sha

        # the same documents taken as the spec before (copied into spec/ as they were): references now
        imported = {sha(f) for f in files}
        same = [d for d in self._user_documents(engine) if sha(d) in imported]
        engine.project.backup(same)
        for d in same:
            d.unlink()
        rec = engine.state.steps.get(self.name)
        if rec is not None and rec.origin == "user" and not self._user_documents(engine):
            engine.state.steps.pop(self.name)  # it was "your spec": now it is written from your documents

    def references(self, engine) -> list[Path]:
        rec = engine.import_record("spec") or {}
        return [engine.project.root / r for r in rec.get("files", {}) if (engine.project.root / r).is_file()]

    def _references_block(self, engine) -> tuple[str, bool]:
        """The imported documents for the LLM, unconverted: text files verbatim, the others (HTML, PDF, …) listed for it
        to read itself. Returns (block, whether the stage needs file tools)."""
        from q3tui.steps.common import spec_block

        return spec_block(engine.project, self.references(engine))

    @staticmethod
    def _generated_by_q3tui(path: Path) -> bool:
        """spec.md written by Q3TUI carries section markers (see document.render)."""
        try:
            return "<!-- section:" in path.read_text(errors="replace")[:4000]
        except OSError:
            return False

    def _user_documents(self, engine) -> list[Path]:
        return [d for d in engine.project.spec_documents() if not (d.name == "spec.md" and self._generated_by_q3tui(d))]

    def outputs(self, engine) -> list[Path]:
        rec = engine.state.steps.get(self.name)
        _, spec, qjson, qmd, _ = self._paths(engine)
        if rec and rec.origin == "generated":
            return [spec, qjson, qmd]
        return self._user_documents(engine)

    def existing_user_outputs(self, engine) -> bool:
        return bool(self._user_documents(engine))

    def missing_input(self, engine) -> str | None:
        intent = self._paths(engine)[0]
        if not intent.is_file() and not self.references(engine):
            return f"no spec and no intent: write {engine.project.rel(intent)}, pass --intent \"...\", or provide --spec FILE"
        return None

    def reset_files(self, engine) -> list[Path]:
        _, spec, qjson, qmd, answers = self._paths(engine)
        return [spec, qjson, qmd, answers]

    # -- questions --------------------------------------------------------------------------

    def _questions(self, engine) -> list[Question]:
        data = read_json(self._paths(engine)[2], default=[]) or []
        return [Question.model_validate(q) for q in data]

    def question_list(self, engine) -> list[dict]:
        answers = load_answers(self._paths(engine)[4])
        return [dict(q.model_dump(), answer=answers.get(q.id)) for q in self._questions(engine)]

    def open_questions(self, engine) -> int:
        answers = load_answers(self._paths(engine)[4])
        return sum(q.id not in answers for q in self._questions(engine))

    def write_questions_file(self, engine) -> Path | None:
        _, spec, _, qmd, answers = self._paths(engine)
        if not qmd.is_file():
            return None
        title = next((l[2:].strip() for l in spec.read_text().splitlines() if l.startswith("# ")), "spec") if spec.is_file() else "spec"
        qmd.write_text(render_questions(self._questions(engine), load_answers(answers), f"Open questions — {title}",
                                        engine.state.accepted_defaults.get(self.name, {})))
        return qmd

    def answer(self, engine, question_id: str, text: str) -> bool:
        if question_id not in {q.id for q in self._questions(engine)}:
            return False
        save_answer(self._paths(engine)[4], question_id, text)
        return True

    # -- run ------------------------------------------------------------------------------

    def _meta(self, engine) -> dict:
        return engine.state.step_meta.setdefault(self.name, {})

    def _record_meta(self, engine, template_path: Path, applied: list[str], template=None) -> None:
        intent_path = self._paths(engine)[0]
        meta = self._meta(engine)
        meta["intent"] = sha256_file(intent_path) if intent_path.is_file() else None
        meta["intent_text"] = intent_path.read_text() if intent_path.is_file() else None  # to diff the next intent change
        meta["template"] = sha256_file(template_path)
        meta["applied_answers"] = sorted(set(meta.get("applied_answers", [])) | set(applied))
        if template is not None:  # snapshot, so the next run knows exactly which sections changed
            meta["template_sections"] = [s.model_dump() for s in template.sections]
        refs = engine.import_record("spec") or {}
        meta["refs"] = refs.get("files") if self.references(engine) else None  # the documents it was ported from

    def _template_changes(self, engine, template, spec_text: str) -> dict:
        """What changed in the template since the spec was last written.

        Returns {"removed": [ids], "added": [ids], "revised": [ids], "restructure": bool}.
        Removals, reordering and renames are applied by code; additions and changed
        guidance/fields/required need the LLM, but only for those sections.
        """
        from q3tui.steps.spec.document import parse_sections

        snapshot = self._meta(engine).get("template_sections")
        in_spec = [sid for sid, _, _ in parse_sections(spec_text)]
        new = {s.id: s for s in template.sections}
        if snapshot is None:  # older project: compare with what the spec contains (no guidance to diff)
            old = {sid: None for sid in in_spec}
        else:
            old = {s["id"]: s for s in snapshot}
        removed = [sid for sid in old if sid not in new and sid in in_spec]
        added = [sid for sid in new if sid not in old and (snapshot is not None or new[sid].required)]
        revised = []
        for sid, s in new.items():
            o = old.get(sid)
            if o is None or sid not in in_spec:
                continue
            if (o.get("guidance"), o.get("required"), o.get("fields")) != (s.guidance, s.required, [f.model_dump() for f in s.fields]):
                revised.append(sid)
        order_changed = [sid for sid in in_spec if sid in new] != [s.id for s in template.sections if s.id in in_spec]
        titles_changed = snapshot is not None and any(o and new.get(sid) and o.get("title") != new[sid].title for sid, o in old.items())
        return {"removed": removed, "added": added, "revised": revised,
                "restructure": bool(removed) or order_changed or titles_changed}

    async def run(self, ctx: StepContext) -> None:
        engine = ctx.engine
        intent_path, spec_path, qjson, qmd, answers_path = self._paths(engine)
        answers = load_answers(answers_path)
        template, template_path = load_template(engine.project.spec_dir, self.options.get("template"))
        meta = self._meta(engine)
        rec = engine.state.steps.get(self.name)
        if "intent" not in meta and spec_path.is_file() and rec and rec.origin == "generated" and rec.finished:
            # project from before this bookkeeping existed: current intent/template are the baseline,
            # answers given before the last spec run are already in the text
            closed = engine.state.closed_questions.get(self.name, {})
            before = [qid for qid in answers if closed.get(qid, {}).get("at", "") <= rec.finished]
            self._record_meta(engine, template_path, before)
            meta = self._meta(engine)
        new_answers = {k: v for k, v in answers.items() if k not in set(meta.get("applied_answers", []))}
        intent_same = meta.get("intent") is not None and meta.get("intent") == (sha256_file(intent_path) if intent_path.is_file() else None)
        old_intent = meta.get("intent_text")
        refs = self.references(engine)
        ported = meta.get("refs") is not None  # the spec was ported from your documents
        if not intent_path.is_file() and ported:
            intent_same = True  # (no intent: the documents are what it was written from)
        if spec_path.is_file() and not ctx.regenerate and (intent_same or (old_intent is not None and intent_path.is_file())):
            # the spec exists: update only what changed (an intent change revises the sections it affects)
            changes = self._template_changes(engine, template, spec_path.read_text())
            intent_change = None if intent_same else (old_intent, intent_path.read_text())
            extra = []
            now = (engine.import_record("spec") or {}).get("files") if refs else None
            if now != meta.get("refs") and refs:  # re-imported documents: the sections they affect follow them
                block, _ = self._references_block(engine)
                changed = sorted(k for k in set(now or {}) | set(meta.get("refs") or {}) if (now or {}).get(k) != (meta.get("refs") or {}).get(k))
                extra.append("The user's own specification documents changed (" + ", ".join(changed) + "): update the sections "
                             "they affect so the spec states what they state (they are the source of truth); keep module and "
                             "port names as they give them.\n<user_documents>\n" + block + "\n</user_documents>")
            await self._apply_updates(ctx, template, template_path, new_answers, changes, intent_change, extra)
        else:
            await self._write_full(ctx, template, template_path, answers)

    async def _apply_updates(self, ctx: StepContext, template, template_path: Path, new_answers: dict[str, str], changes: dict,
                             intent_change: tuple[str, str] | None = None, extra: list[str] | None = None) -> None:
        """Answers, change requests and template edits: patch only what they affect (no full rewrite/review).

        Removed / reordered / renamed sections are applied by code (no LLM). New sections, sections
        whose template guidance/fields changed, answers and change requests go to one small LLM
        stage that returns only the sections it changes.
        """
        engine = ctx.engine
        _, spec_path, qjson, qmd, answers_path = self._paths(engine)
        previous = self._questions(engine)
        order = [s.id for s in template.sections]
        tmpl = template.by_id()
        text = original = spec_path.read_text()
        engine.project.backup([spec_path])

        if changes["restructure"]:
            text = document.restructure(text, order, {s.id: s.title for s in template.sections}, set(changes["removed"]))
            done = []
            if changes["removed"]:
                done.append(f"removed {', '.join(changes['removed'])}")
            ctx.emit("log", message="template structure applied" + (f" ({'; '.join(done)})" if done else " (order/titles)"))

        closed = engine.state.closed_questions.get(self.name, {})
        by_id = {q.id: q.question for q in previous} | {k: v["question"] for k, v in closed.items()}
        items = [(qid, by_id.get(qid, "(earlier question)"), a) for qid, a in new_answers.items()]
        add = [f"{sid} — \"{tmpl[sid].title}\" ({'required' if tmpl[sid].required else 'optional: write it only if it applies'}): "
               f"{' '.join(tmpl[sid].guidance.split())}" + (f" Fields: {', '.join(f.name for f in tmpl[sid].fields)}." if tmpl[sid].fields else "")
               for sid in changes["added"]]
        revise = [f"{sid} — \"{tmpl[sid].title}\": new guidance: {' '.join(tmpl[sid].guidance.split())}"
                  + (f" Fields: {', '.join(f.name for f in tmpl[sid].fields)}." if tmpl[sid].fields else "")
                  for sid in changes["revised"]]
        patch: SpecPatch | None = None
        feedback = [*ctx.feedback, *(extra or [])]
        if items or feedback or add or revise or intent_change:
            ctx.emit("log", message=f"updating the affected sections: {len(items)} answer(s), {len(feedback)} change request(s), "
                                    f"{len(add)} new section(s), {len(revise)} revised section(s)"
                                    + (", intent changed" if intent_change else ""))
            stage = Stage(name="spec_update", system_prompt=prompts.UPDATER, cwd=engine.project.root,
                          builtin_tools=["Read", "Grep", "Glob"] if extra else [],  # (re-imported documents to read)
                          prompt=prompts.apply_prompt(template=describe(template), spec=text, answers=items, feedback=feedback,
                                                      add=add, revise=revise, intent_change=intent_change),
                          output_model=SpecPatch)
            patch = (await ctx.llm(stage)).output
            for sec in patch.sections:
                sid = sec.id if sec.id in tmpl or sec.id in text else document.section_id_for(sec.title)
                title = tmpl[sid].title if sid in tmpl else sec.title
                text = document.upsert_section(text, sid, title, document.section_body(sec.fields, sec.content), order)
        spec_path.write_text(text)

        answers = load_answers(answers_path)
        conflicts = list(patch.conflicts) if patch else []
        ctx.unchanged = text == original and not conflicts
        questions = [q for q in previous if q.id not in answers]
        questions = assign_question_ids(questions + conflicts, previous, "Q-S")
        write_json(qjson, [q.model_dump() for q in questions])
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "spec")
        qmd.write_text(render_questions(questions, answers, f"Open questions — {title}", engine.state.accepted_defaults.get(self.name, {})))
        self._record_meta(engine, template_path, list(new_answers), template)
        from q3tui.steps.spec.sections import record_generated

        record_generated(engine)
        ctx.emit("questions", count=len(questions), blocking=sum(q.blocking for q in questions))
        if patch is None:
            ctx.emit("log", message="spec updated without the LLM (template structure only)")
        else:
            changed = ", ".join(s.title for s in patch.sections) or "no sections"
            conflict = f"; {len(conflicts)} conflict(s) raised" if conflicts else ""
            ctx.emit("log", message=f"updated {changed}{conflict}. {patch.summary}")

    async def _write_full(self, ctx: StepContext, template, template_path: Path, answers: dict[str, str]) -> None:
        """First draft, intent change, or --regenerate: write (and review) the whole spec."""
        engine = ctx.engine
        intent_path, spec_path, qjson, qmd, answers_path = self._paths(engine)
        intent = intent_path.read_text() if intent_path.is_file() else ""
        refs = self.references(engine)
        refs_tools = False
        if refs:  # your own documents: ported into the template (they are the source of truth)
            block, refs_tools = self._references_block(engine)
            intent = prompts.port_intent(intent, block)
        previous = self._questions(engine)
        existing = spec_path.read_text() if spec_path.is_file() and not ctx.regenerate else None
        tdesc = describe(template)
        ctx.emit("log", message=f"template: {engine.project.rel(template_path)} ({len(template.sections)} sections)")

        # the intent (and the current spec) are in the prompt; file tools only when there are
        # reference documents to read — otherwise the model spends turns re-reading its prompt
        others = [p for p in engine.project.spec_documents() if p != spec_path]
        tools = ["Read", "Grep", "Glob"] if others or refs_tools else []

        def stage(name: str, prompt: str, system: str, output=SpecDraft, resume: str | None = None) -> Stage:
            return Stage(name=name, system_prompt=system, prompt=prompt, cwd=engine.project.root, builtin_tools=tools,
                         output_model=output, resume=resume)

        if existing is None:
            ctx.emit("log", message=f"porting {', '.join(engine.project.rel(p) for p in refs)} into the template's sections"
                                    if refs else "drafting specification from intent")
            prompt = prompts.draft_prompt(intent=intent, template=tdesc, feedback=ctx.feedback)
        else:
            ctx.emit("log", message="revising the specification (the intent changed)")
            prompt = prompts.revise_prompt(intent=intent, template=tdesc, spec=existing, questions=previous, answers=answers, feedback=ctx.feedback)
        if others:
            prompt += ("\n\nReference documents in the project (read the relevant parts): "
                       + ", ".join(engine.project.rel(p) for p in others))
        # the document comes back as plain Markdown: a 250-line spec inside a JSON string breaks
        # easily (one bad escape = the whole spec written again)
        res = await ctx.llm(stage("spec_write", prompt, prompts.WRITER, output=None))
        draft, problems = document.parse_reply(res.text)
        if draft is None:
            ctx.emit("warning", message=f"spec reply not in the expected layout ({'; '.join(problems)}); asking again")
            res = await ctx.llm(stage("spec_write", prompts.reformat_prompt(problems), prompts.WRITER, output=None, resume=res.session_id))
            draft, problems = document.parse_reply(res.text)
            if draft is None:
                raise StepFailed("the spec writer's reply is not in the expected layout: " + "; ".join(problems))

        if ctx.cfg.spec.self_review:
            ctx.emit("log", message="reviewing specification (returns only the sections it changes)")
            review_prompt = prompts.review_prompt(intent=intent, template=tdesc, draft=draft, rendered=document.render(draft), answers=answers)
            review_stage = Stage(name="spec_review", system_prompt=prompts.REVIEWER, prompt=review_prompt, cwd=engine.project.root,
                                 builtin_tools=["Read", "Grep", "Glob"] if refs_tools else [], output_model=ReviewPatch)
            review: ReviewPatch = (await ctx.llm(review_stage)).output
            draft = document.apply_review(draft, review)
            changed = ", ".join(sec.title for sec in review.sections) or "no sections"
            ctx.emit("log", message=f"review changed {changed}. {review.summary}")
        else:
            ctx.emit("log", message="spec review is off (spec.self_review: false)")

        errors = document.check(draft, template)
        if errors:
            ctx.emit("warning", message=f"spec does not follow the template: {'; '.join(errors[:5])}")
            fixed = (await ctx.llm(stage("spec_repair", prompts.repair_prompt(template=tdesc, draft=draft, errors=errors), prompts.WRITER))).output
            draft, errors = fixed, document.check(fixed, template)
            if errors:
                ctx.emit("warning", message=f"still {len(errors)} template problem(s); missing parts are marked TBD")
        draft = document.normalize(draft, template)

        asked = " ".join(q.question.lower() for q in draft.questions)
        for item in document.tbd_fields(draft):
            if item.split(": ", 1)[1].lower() not in asked:
                draft.questions.append(Question(id="", question=f"{item} is TBD — what should it be?", blocking=False, default_assumption=""))
        questions = assign_question_ids(draft.questions, previous, "Q-S")
        questions = [q for q in questions if q.id not in answers]  # answered ones are resolved in the text
        text = document.render(draft)
        engine.project.backup([spec_path])
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(text)
        write_json(qjson, [q.model_dump() for q in questions])
        qmd.write_text(render_questions(questions, answers, f"Open questions — {draft.title}"))
        engine.state.top = engine.state.top or draft.top_module
        self._record_meta(engine, template_path, list(answers), template)
        from q3tui.steps.spec.sections import record_generated

        record_generated(engine)
        ctx.emit("questions", count=len(questions), blocking=sum(q.blocking for q in questions))
        ctx.emit("log", message=f"wrote {engine.project.rel(spec_path)} ({len(draft.sections)} sections, {len(text.splitlines())} lines), {len(questions)} open question(s)")
