"""Step interface shared by all pipeline steps."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from q3tui.core.events import StepEmitter
from q3tui.llm import runtime
from q3tui.llm.runtime import Stage, StageResult

if TYPE_CHECKING:
    from q3tui.pipeline.engine import Engine
    from q3tui.core.project import Project


class StepFailed(RuntimeError):
    pass


@dataclass
class StepContext:
    engine: "Engine"
    emit: StepEmitter
    feedback: list[str] = field(default_factory=list)  # change requests / rejection comments to apply
    regenerate: bool = False  # ignore previous outputs and start from scratch
    cost_usd: float = 0.0
    unchanged: bool = False  # set by a step whose update left its result as it was: the review stays approved
    notes: str = ""  # the flow file's notes for this step: appended to every LLM task it runs

    @property
    def project(self) -> "Project":
        return self.engine.project

    @property
    def cfg(self):
        return self.engine.project.cfg

    async def llm(self, stage: Stage) -> StageResult:
        # Q3TUI's own state (backups of old outputs, run logs) must never feed an LLM:
        # a reset or regeneration would otherwise "start from scratch" with the old answer.
        if self.project.state_dir not in stage.deny_dirs:
            stage.deny_dirs.append(self.project.state_dir)
        # the originals of an import (e.g. ref/<project>/): their RTL / tests are in src/ now, under the independence rules
        stage.deny_dirs.extend(d for d in self.engine.import_sources() if d not in stage.deny_dirs)
        # documents a step ports (spec/ref/): only that step reads them; the others read what it made of them
        for s in self.engine.steps:
            d = s.import_dir(self.engine, "spec")
            if d is not None and s.name != self.emit.step and d not in stage.deny_dirs:
                stage.deny_dirs.append(d)
        # every stage runs at its configured effort (no lowered effort is remembered: it capped rv32im's designer,
        # modeler and debugger at "low" for good after a few long thoughts — Claude Code has no such guard either)
        from q3tui import flows
        from q3tui.llm import prompts

        path = self.engine.flow.path
        prompts.apply(stage, self.project.root, self.emit.step, self.project.state_dir,
                      path.parent if path and path.name == "FLOW.md" else None,
                      flows.BUILTIN_DIR / self.engine.flow.name)
        from q3tui.core.language import instruction as lang_instruction

        stage.prompt += f"\n\n## Language\n{lang_instruction(self.cfg.tui.language)}"
        if self.notes:
            stage.prompt += f"\n\n## Flow notes for this step\n{self.notes}"
        path = self._checkpoint(stage)
        if path is not None and path.is_file():
            try:  # an interrupted run: this exact stage already finished — its answer is reused, not paid again
                saved = json.loads(path.read_text())
                output = stage.output_model.model_validate(saved["output"]) if stage.output_model and saved.get("output") is not None else None
                self.emit("log", message=f"{stage.name}: reusing its result from the interrupted run (no LLM)")
                return StageResult(saved.get("text", ""), output, 0.0, saved.get("num_turns", 0), None)
            except Exception:  # noqa: BLE001 - an unreadable checkpoint is just not used
                pass
        from q3tui.llm import roles

        if self.engine.session_mode() == "flow":  # the flow's one session (flows/*.yaml `session: flow`), like one conversation
            # pipeline.parallel_rtl_tb + parallel_multi_agent: tb runs alongside rtl in its own session instead
            # (engine._run_parallel sets this for the duration of that parallel run only)
            role = self.engine._step_role_override.get(self.emit.step, "flow")
        else:
            role = roles.role_of(stage.name) if self.cfg.team.enabled else None
            if role and self.cfg.team.unit_sessions == "own" and roles.unit_level(stage.name):
                role = None  # its own small session (see roles.UNIT_LEVEL)
        try:
            if role:  # M10: the task goes to its role's persistent session
                result = await roles.team(self.project).run(stage, role, self.cfg, self.emit)
            else:
                result = await runtime.run_stage(stage, self.cfg, self.emit)
        except runtime.StageError as exc:
            self.cost_usd += exc.cost_usd
            raise
        self.cost_usd += result.cost_usd
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"stage": stage.name, "text": result.text, "num_turns": result.num_turns,
                                       "output": result.output.model_dump(mode="json") if result.output is not None else None}))
            tmp.replace(path)
        return result

    def _checkpoint(self, stage: Stage) -> Path | None:
        """Where this stage's answer is kept until the step finishes (None: not resumable — it has side effects:
        file writes, tools that act, a resumed session)."""
        if (stage.write_dirs is not None or stage.write_files is not None or stage.resume or stage.sdk_tools
                or {"Edit", "Write", "Bash"} & set(stage.builtin_tools)):
            return None
        llm = self.cfg.llm.for_stage(self.emit.step, stage.name)
        schema = json.dumps(stage.output_model.model_json_schema(), sort_keys=True) if stage.output_model else ""
        # a stage that reads files depends on them too: the same prompt over changed files (a re-review after a fix)
        # is a new question, never the old answer
        files = _readable_digest(stage) if {"Read", "Grep", "Glob"} & set(stage.builtin_tools) else ""
        if files is None:
            return None
        h = hashlib.sha256("\0".join([stage.name, stage.system_prompt, stage.prompt, ",".join(sorted(stage.builtin_tools)),
                                      str(stage.max_turns), llm.model, str(llm.effort), schema, files]).encode()).hexdigest()[:24]
        return checkpoint_dir(self.project, self.emit.step) / f"{h}.json"


def _readable_digest(stage: Stage, limit: int = 20_000) -> str | None:
    """Path, size and mtime of every file the stage may read (hidden and denied folders skipped); None when there are
    too many to tell (the stage is then not resumable)."""
    import os

    denied = {d.resolve() for d in stage.deny_dirs}
    h, n = hashlib.sha256(), 0
    for root in stage.allowed_dirs:
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and (Path(dirpath) / d).resolve() not in denied)
            for name in sorted(names):
                try:
                    st = (Path(dirpath) / name).stat()
                except OSError:
                    continue
                n += 1
                if n > limit:
                    return None
                h.update(f"{dirpath}/{name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def checkpoint_dir(project: "Project", step: str | None) -> Path:
    """Finished LLM stages of a step that has not finished yet (resumed after an interruption)."""
    return project.state_dir / "cache" / "stages" / (step or "_")


class StepDef:
    """A pipeline step. Subclasses describe their files; the engine handles state."""

    name: str = ""
    kind: str = ""  # the registered kind (steps.KINDS) this step was made from; name = its id in the flow
    title: str = ""
    deps: tuple[str, ...] = ()
    options: dict = {}  # `options:` of the flow file's step (kind-specific; part of the step's staleness)
    implemented: bool = True
    milestone: str = ""  # for unimplemented steps
    # bumped when Q3TUI's checks on this step's result change: done results go stale and are re-checked (a
    # result that passes is kept without the LLM; one that fails is repaired, not regenerated)
    checks_version: int = 0

    def inputs(self, engine: "Engine") -> list[Path]:
        """Files whose content determines this step's result (for staleness)."""
        return []

    def outputs(self, engine: "Engine") -> list[Path]:
        """Files this step produced (or that the user provided in its place)."""
        return []

    def existing_user_outputs(self, engine: "Engine") -> bool:
        """True when this step's outputs exist without Q3TUI having generated them."""
        return False

    def missing_input(self, engine: "Engine") -> str | None:
        return None

    def open_questions(self, engine: "Engine") -> int:
        return 0

    def pending_reviews(self, engine: "Engine") -> list[str]:
        """Ids of this step's own reviewable items (an SVA assertion, a flagged requirement, …) you have not yet
        confirmed or rejected — distinct from blocking questions (a gap only you can decide). Non-empty: `approve()`
        refuses the gate unless forced or the project runs unattended (`engine.auto_reviews()`)."""
        return []

    def default_to_apply(self, q: dict) -> str | None:
        """A question of this step whose default is NOT in its output yet (a change still to make): accepting the
        default hands this text back to the step as a change request. None: the default is already applied."""
        return None

    def rebase_problems(self, engine: "Engine") -> list[str]:
        """Deterministic checks of this step's current result against its new inputs, run before it is marked up to
        date without re-running (a rebase). Any problem: it is not rebased, it runs again."""
        return []

    def on_reset(self, engine: "Engine") -> None:
        """Called after a reset deleted this step's files (a step that moved your documents aside puts them back)."""

    def import_dir(self, engine: "Engine", kind: str) -> Path | None:
        """Where this step takes imported files of `kind` (rtl | tb | sva; core/importer.py), None: it takes none.
        Files imported into the step's own output folder are its starting point (it checks them and updates what does
        not follow its rules); a sub-folder of its own (e.g. src/tb/imported/) holds references it ports from."""
        return None

    def on_import(self, engine: "Engine", kind: str, files: list[Path]) -> None:
        """Called after `files` of `kind` were imported into `import_dir` (engine state is saved afterwards)."""

    def on_approve(self, engine: "Engine") -> None:
        """Called when this step's review gate was approved (before downstream steps run): a step whose artifact records
        the gate (VLSIT: `gate_status`, `gate_2_approved`, …) writes it here. Its outputs are re-hashed afterwards."""

    tool_roles: tuple[str, ...] = ()  # tool roles (tools.json) this kind runs: `q3tui flow check` lists them

    def on_rebase(self, engine: "Engine") -> None:
        """Called when this step is marked up to date without re-running (its output already
        reflects a change made upstream on its behalf)."""

    def refresh_reports(self, engine: "Engine") -> list[Path]:
        """Re-render derived reports whose layout is older than the code (no LLM); returns the
        files written. Their recorded hashes are updated so they don't show as edited."""
        return []

    def reset_files(self, engine: "Engine") -> list[Path]:
        """Files besides the recorded outputs that a reset removes (answers, caches). Never user inputs."""
        return []

    async def run(self, ctx: StepContext) -> None:
        raise NotImplementedError


class PlannedStep(StepDef):
    """Placeholder for steps from later milestones."""

    implemented = False

    def __init__(self, name: str, title: str, deps: tuple[str, ...], milestone: str, user_outputs=None):
        self.name, self.title, self.deps, self.milestone = name, title, deps, milestone
        self._user_outputs = user_outputs

    def existing_user_outputs(self, engine: "Engine") -> bool:
        return bool(self._user_outputs and self._user_outputs(engine))

    def outputs(self, engine: "Engine") -> list[Path]:
        return self._user_outputs(engine) if self._user_outputs else []
