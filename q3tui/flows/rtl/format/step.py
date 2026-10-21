"""Step `format` of the RTL flow: Verible's formatter on `src/rtl/` (code only, no LLM).

Runs by itself whenever the RTL changed (generated, or fixed through `style_lint`). Guardrails: the formatter works on copies; a result is
written only when it is the same code (whitespace differences only) and is backed up first; a file you edited by hand (its hash is not the one
`rtl` recorded) waits for your yes (a question) — Q3TUI never overwrites your edit silently. The formatted files are recorded as the `rtl`
step's own outputs again, so a format is not mistaken for a hand edit; your edited files stay flagged as yours."""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio

from q3tui.core.project import sha256_file, sha256_json, write_json
from q3tui.flows.rtl.lib import verible
from q3tui.flows.rtl.lib.base import RtlFlowStep
from q3tui.pipeline.base import StepContext
from q3tui.steps.common import Question, load_answers


class Step(RtlFlowStep):
    name = "format"
    title = "Format (Verible)"
    deps = ("rtl",)
    tool_roles = ("verible_format",)

    def _report(self, engine) -> Path:
        return self.style_dir(engine) / "format.json"

    def _flags_file(self, engine) -> Path:
        return self.style_dir(engine) / verible.FORMAT_FLAGS

    def inputs(self, engine) -> list[Path]:
        f = self._flags_file(engine)
        return [*self.rtl_files(engine), *([f] if f.is_file() else []), self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        r = self._report(engine)
        return [*self.rtl_files(engine), *([r] if r.is_file() else [])]

    def reset_files(self, engine) -> list[Path]:  # (the RTL itself is the rtl step's: a reset of this step keeps it)
        r = self._report(engine)
        return [*([r] if r.is_file() else []), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        if not self.rtl_files(engine):
            return f"the RTL is needed first ({engine.project.rel(self.rtl_dir(engine))}/: step rtl)"
        return self.tool_problem(engine, "verible_format")

    def hand_edited(self, engine, files: list[Path]) -> list[Path]:
        """Files whose content is not what the `rtl` step recorded when it wrote them: your edits."""
        rec = engine.state.steps.get("rtl")
        recorded = rec.outputs if rec else {}
        return [f for f in files if (h := recorded.get(engine.project.rel(f))) is not None and h != sha256_file(f)]

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        files = self.rtl_files(engine)
        flags = verible.read_flags(self._flags_file(engine))
        edited = self.hand_edited(engine, files)
        skipped: list[str] = []
        if edited:
            qid = "FM-" + sha256_json(sorted((project.rel(f), sha256_file(f)) for f in edited))[:6]
            answer = load_answers(self._q_paths(engine)[1]).get(qid)
            if answer is None:
                names = ", ".join(f.name for f in edited)
                self.save_questions(engine, [Question(
                    id=qid, blocking=True, kind="design_choice", default_assumption="yes — whitespace only, a backup is kept",
                    question=f"{names} {'was' if len(edited) == 1 else 'were'} edited by hand. Format {'it' if len(edited) == 1 else 'them'} too? "
                             "(the formatter changes whitespace only; the file is backed up first)",
                    options=[{"label": "yes — format it (a backup is kept)"}, {"label": "no — leave it as it is"}])])
                skipped = [f.name for f in edited]
            elif answer.strip().lower().startswith(("no", "không", "khong")):
                skipped = [f.name for f in edited]
            else:
                self.save_questions(engine, [])
        else:
            self.save_questions(engine, [])
        targets = [f for f in files if f.name not in skipped]
        changed, rejected, syntax = await anyio.to_thread.run_sync(lambda: self.apply(engine, targets, edited))
        report = {"formatted": sorted(changed), "already_formatted": sorted(f.name for f in targets if f.name not in changed
                  and f.name not in rejected and f.name not in syntax), "skipped_hand_edited": skipped, "rejected_changed_code": rejected,
                  "syntax_errors": syntax, "flags": flags}
        write_json(self._report(engine), report)
        ctx.emit("log", message=f"format: {len(changed)} file(s) formatted, {len(report['already_formatted'])} already formatted"
                                + (f", {len(skipped)} hand-edited waiting for your answer" if skipped and edited else "")
                                + (f", {len(syntax)} with syntax errors left alone" if syntax else ""))
        for n in rejected:
            ctx.emit("warning", message=f"format: the formatter would have changed the code of {n} (not only whitespace): left as it was")
        ctx.unchanged = not changed

    def apply(self, engine, targets: list[Path], edited: list[Path]) -> tuple[list[str], list[str], dict[str, str]]:
        """Format `targets` (copies first; a result is written only when it is the same code, after a backup). The `rtl` step's record of
        the files that were not hand-edited follows. Returns (names formatted, names refused because the code would change, syntax errors)."""
        project = engine.project
        ts = self.tools(engine)
        flags = verible.read_flags(self._flags_file(engine))
        work = self.workdir(engine) / "copies"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        changed: dict[Path, str] = {}
        rejected: list[str] = []
        syntax: dict[str, str] = {}
        for f in targets:
            copy = work / f.name
            shutil.copy2(f, copy)
            res = verible.run_format(ts, work, [copy], flags)
            errs = verible.syntax_errors(res.diagnostics)
            if errs:  # (not a style matter: the RTL step's lint reports it, the generator fixes it)
                syntax[f.name] = errs[0]
                continue
            verible.check(res, "verible_format")
            before, after = f.read_text(), copy.read_text()
            if after == before:
                continue
            if verible.same_code(before, after):
                changed[f] = after
            else:
                rejected.append(f.name)
        if changed:
            project.backup(list(changed))
            for f, text in changed.items():
                f.write_text(text)
        rec = engine.state.steps.get("rtl")
        if rec is not None:  # the formatted files are the rtl step's outputs as it wrote them (yours stay yours)
            mine = {project.rel(f) for f in self.rtl_files(engine) if f not in edited}
            for rel in mine & set(rec.outputs):
                rec.outputs[rel] = sha256_file(project.root / rel)
            engine.save()
        return sorted(f.name for f in changed), rejected, syntax
