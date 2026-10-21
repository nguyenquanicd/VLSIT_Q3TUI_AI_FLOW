"""Shared pieces of the spec flow's steps (and the rtl flow's): layout, questions, prompt sources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from q3tui.pipeline.base import StepDef
from q3tui.core.project import read_json, write_json
from q3tui.steps.common import Question, load_answers, render_questions, save_answer
from q3tui.flows.spec.lib import artifacts

FLOW_DIR = Path(__file__).parents[1]  # the spec flow folder: <step>/{step.py, md/, rules/, schemas/, scripts/}
RTL_FLOW_DIR = FLOW_DIR.parent / "rtl"  # the rtl flow shares these steps' base: its rule files and prompts are found the same way


def rule_dirs(flow_dir: Path | None = None) -> list[Path]:
    """Every step's rules/ folder of the flow (and of the rtl flow)."""
    return sorted(d for f in ([flow_dir] if flow_dir else [FLOW_DIR, RTL_FLOW_DIR]) for d in f.glob("*/rules"))


@dataclass
class Layout:
    """Where the flows keep things (project.schemas_dir / rtl_dir / sim_dir …)."""

    schemas: Path
    rtl: Path
    sva: Path
    tb: Path
    docs: Path
    sim: Path  # simulation / lint / synthesis work area (logs, simv): build/sim

    def artifact(self, name: str) -> Path:
        return self.schemas / name


def layout(engine) -> Layout:
    p = engine.project
    return Layout(p.schemas_dir, p.rtl_dir, p.src_dir / "sva", p.tb_dir, p.docs_dir, p.sim_dir)


def rules_text(names: list[str] | None = None, project_root: Path | None = None) -> str:
    """The text of rule files (`options.rules`): a path relative to the project, else one in a step's rules/ of the flow."""
    out = []
    for n in names or []:
        for cand in ([project_root / n] if project_root else []) + [d / n for d in rule_dirs()]:
            if cand.is_file():
                out.append(f"## {cand.name}\n\n{cand.read_text()}")
                break
        else:
            raise FileNotFoundError(f"rule file '{n}' not found (project dir or a step's rules/ folder of the spec / rtl flow)")
    return "\n\n".join(out)


def prompt_source(name: str) -> str:
    """An original VLSIT command prompt (<step>/md/<name>.md), the source the stage prompts are adapted from."""
    return next(p for f in (FLOW_DIR, RTL_FLOW_DIR) for p in f.glob(f"**/md/{name}.md")).read_text()


class VlsitStep(StepDef):
    """Base of the VLSIT step kinds: questions (questions / answers files per step), gate recording, layout."""

    artifact: str = ""  # the step's main JSON artifact
    gate_fields: dict[str, object] = {}  # written into the artifact's metadata when the gate is approved (see on_approve)

    def lay(self, engine) -> Layout:
        """The layout; `options.rtl_dir` (a project-relative folder) moves the RTL out of `project.rtl_dir` (default `src/rtl`)."""
        lay = layout(engine)
        d = self.options.get("rtl_dir")
        return replace(lay, rtl=engine.project.root / str(d)) if d else lay

    # -- questions: schemas/questions/<id>.json (+ .answers.json, .md) ---------------------------

    def _q_paths(self, engine) -> tuple[Path, Path, Path]:
        d = layout(engine).schemas / "questions"
        return d / f"{self.name}.json", d / f"{self.name}.answers.json", d / f"{self.name}.md"

    def questions_list(self, engine) -> list[Question]:
        return [Question.model_validate(q) for q in read_json(self._q_paths(engine)[0], default=[]) or []]

    def save_questions(self, engine, questions: list[Question]) -> None:
        q_json, _, _ = self._q_paths(engine)
        write_json(q_json, [q.model_dump() for q in questions])

    def question_list(self, engine) -> list[dict]:
        answers = load_answers(self._q_paths(engine)[1])
        return [dict(q.model_dump(), answer=answers.get(q.id)) for q in self.questions_list(engine)]

    def open_questions(self, engine) -> int:
        answers = load_answers(self._q_paths(engine)[1])
        return sum(q.id not in answers for q in self.questions_list(engine))

    def answer(self, engine, question_id: str, text: str) -> bool:
        if question_id not in {q.id for q in self.questions_list(engine)}:
            return False
        save_answer(self._q_paths(engine)[1], question_id, text)
        return True

    def write_questions_file(self, engine) -> Path | None:
        qs = self.questions_list(engine)
        if not qs:
            return None
        _, answers, q_md = self._q_paths(engine)
        q_md.parent.mkdir(parents=True, exist_ok=True)
        q_md.write_text(render_questions(qs, load_answers(answers), f"Open questions — {self.title}",
                                         engine.state.accepted_defaults.get(self.name, {})))
        return q_md

    def reset_files(self, engine) -> list[Path]:
        return [p for p in self._q_paths(engine) if p.is_file()]

    # -- gate -------------------------------------------------------------------------------------

    def on_approve(self, engine) -> None:
        if self.artifact and self.gate_fields:
            artifacts.set_gate(engine.project, self.artifact, {**self.gate_fields, **{k: artifacts.now() for k in self.gate_fields if k.endswith("_at")}})

