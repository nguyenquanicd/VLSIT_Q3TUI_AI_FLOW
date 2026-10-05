"""Stub LLM for pipeline tests: returns canned structured outputs per output model."""

from __future__ import annotations

from q3tui.llm.runtime import StageResult
from q3tui.pipeline.base import StepContext, StepDef
from q3tui.steps.spec.document import ReviewPatch, SpecPatch
from q3tui.steps.spec.step import SpecDraft


def _sections():
    from q3tui.steps.spec.template import DEFAULT_TEMPLATE, SpecTemplate
    import yaml

    t = SpecTemplate.model_validate(yaml.safe_load(DEFAULT_TEMPLATE.read_text()))
    return [{"id": s.id, "title": s.title, "content": f"{s.title} text.",
             "fields": [{"name": f.name, "value": "500 MHz" if f.name == "Clock frequency" else "x"} for f in s.fields]}
            for s in t.sections if s.required]


SPEC = SpecDraft(
    top_module="fifo_top",
    title="Elastic buffer",
    sections=_sections(),
    questions=[{"id": "", "question": "Should drop_count saturate?", "blocking": False, "default_assumption": "saturate"}],
)


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.spec_outputs: list[dict] = []
        self.spec_replies: list[str] = []            # raw Markdown replies of spec_write (else rendered from the draft)

    async def __call__(self, stage, cfg, emit):
        self.calls.append(stage)
        emit("llm_text", stage=stage.name, text="thinking…")
        from tests.vlsit_fakes import dispatch  # the VLSIT flow's stages (tests/vlsit_fakes.py)

        handled = dispatch(self, stage, emit)
        if handled is not None:
            return handled
        if stage.output_model is SpecDraft or (stage.name == "spec_write" and stage.output_model is None):
            secs = [dict(sec.model_dump()) for sec in SPEC.sections]
            secs[0]["content"] += f" (rev {len(self.calls)})"
            out = SPEC.model_copy(update={"sections": [type(SPEC.sections[0]).model_validate(x) for x in secs]})
            if self.spec_outputs:
                out = SpecDraft.model_validate(self.spec_outputs.pop(0))
            if stage.output_model is None:  # the writer replies in Markdown
                from q3tui.steps.spec.document import render_reply

                text = self.spec_replies.pop(0) if self.spec_replies else render_reply(out)
                emit.add_cost(0.01)
                return StageResult(text, None, 0.01, 2, "sess-1")
        elif stage.output_model is ReviewPatch:
            out = ReviewPatch(sections=[{"id": "features", "title": "Features", "fields": [],
                                         "content": f"Features reviewed (call {len(self.calls)})."}],
                              questions=list(SPEC.questions), summary="tightened features")
        elif stage.output_model is SpecPatch:
            out = SpecPatch(sections=[{"id": "overview", "title": "Overview", "fields": [],
                                       "content": f"Overview updated (call {len(self.calls)})."}],
                            conflicts=[], summary="applied")
        else:
            out = None
        emit.add_cost(0.01)
        return StageResult("ok", out, 0.01, 2, "sess-1")

    def names(self):
        return [c.name for c in self.calls]


class ReadsStep(StepDef):
    """A code-only step after the spec: its inputs are its deps' outputs, it writes notes/<name>.txt (no LLM)."""

    title = "Reads"

    def inputs(self, engine):
        return [f for d in self.deps for f in engine.by_name[d].outputs(engine)]

    def outputs(self, engine):
        p = engine.project.root / "notes" / f"{self.name}.txt"
        return [p] if p.is_file() else []

    def missing_input(self, engine):
        return None if self.inputs(engine) else "nothing to read yet"

    async def run(self, ctx: StepContext) -> None:
        d = ctx.project.root / "notes"
        d.mkdir(exist_ok=True)
        (d / f"{self.name}.txt").write_text(f"{self.name}: {len(self.inputs(ctx.engine))} input(s)\n")


TEST_FLOW = """name: t
steps:
  - {id: spec, kind: spec, gate: human}
  - {id: rtl, kind: reads, deps: [spec]}
  - {id: tb, kind: reads, deps: [spec]}
"""


def use_test_flow(root, config: str = "") -> None:
    """The project at `root` runs the test flow: the spec step (default template), then code-only `rtl` and `tb`
    (kind `reads`, registered in conftest). `config`: more q3tui.yaml lines."""
    (root / "flows").mkdir(exist_ok=True)
    (root / "flows" / "t.yaml").write_text(TEST_FLOW)
    (root / "q3tui.yaml").write_text("pipeline: {flow: t}\n" + config)
