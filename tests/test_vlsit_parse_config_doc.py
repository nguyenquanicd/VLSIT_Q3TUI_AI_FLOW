"""The VLSIT flow's parse, config and doc steps (docs/spec/vlsit-flow.md)."""

import json

import anyio
import pytest

from q3tui.llm import runtime
from q3tui.llm.runtime import StageResult
from q3tui.pipeline.engine import Engine
from q3tui.core.project import Project
from q3tui.steps.vlsit import artifacts
from q3tui.flows.vlsit.config.step import ConfigOverrides
from q3tui.steps.vlsit.constraints import evaluate, in_range, to_int
from q3tui.flows.vlsit.doc.step import DocProse
from q3tui.flows.vlsit.parse.step import ParsedSpec, modules_leaves_first
from tests import vlsit_fakes
from tests.fakes import FakeLLM

SPEC_TEXT = "# Width downscaler\n\nF01 The block shall split a beat.\n"


def _spec(**over) -> dict:
    base = {
        "ip_name": "width_ds", "spec_revision": "0.3", "top_module": "ds_top", "description": "A width downscaler.",
        "requirements": [
            {"text": "Each input beat shall be split into slices.", "category": "functional", "feature_id": "F01",
             "rtl_modules": ["ds_split"], "sva_hint": "property", "parameters_affected": ["PR_IN_W"]},
            {"text": "TLAST shall be asserted on the last slice only.", "category": "interface", "rtl_modules": ["ds_split"], "sva_hint": "property"},
            {"text": "Reset shall clear the FIFO.", "category": "functional", "rtl_modules": [], "sva_hint": "cover"},
            {"text": "Optional debug counter.", "category": "functional", "feature_id": "F02", "optional": True,
             "parameters_affected": ["PR_CNT_EN"], "rtl_modules": ["ds_top"]},
        ],
        "parameters": [
            {"name": "PR_IN_W", "type": "int unsigned", "default": "64", "valid_range": "8..128", "description": "Input width"},
            {"name": "PR_CNT_EN", "type": "bit", "default": "1", "valid_range": "0/1", "description": "Counter"},
            {"name": "PR_DEPTH", "type": "int unsigned", "default": "4", "valid_range": "locked 4", "description": "FIFO depth", "locked": True},
        ],
        "constraints": [{"id": "C1", "rule": "PR_IN_W % 8 == 0", "description": "byte aligned"},
                        {"id": "C2", "rule": "PR_CNT_EN == 1 -> PR_IN_W >= 8"}],
        "modules": [{"name": "ds_top", "description": "top", "instances": ["ds_split", "ds_fifo"]},
                    {"name": "ds_split", "description": "splits beats"}, {"name": "ds_fifo", "description": "buffer"}],
    }
    base.update(over)
    return base


class VlsitFake:
    """Stages of parse / config / doc for FakeLLM (registered in tests/vlsit_fakes)."""

    def __init__(self):
        self.parsed: list[dict] = []
        self.overrides: list[dict] = []
        self.prose = {"introduction": "An intro."}

    def __call__(self, fake, stage, emit):
        if stage.name == "parse_spec":
            data = self.parsed.pop(0) if self.parsed else _spec()
            return StageResult("", ParsedSpec.model_validate(data), 0.01, 1, "s")
        if stage.name == "config_overrides":
            data = self.overrides.pop(0) if self.overrides else {}
            return StageResult("", ConfigOverrides.model_validate(data), 0.01, 1, "s")
        if stage.name == "doc_prose":
            return StageResult("", DocProse.model_validate(self.prose), 0.01, 1, "s")
        return None


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    vf = VlsitFake()
    saved = list(vlsit_fakes.HANDLERS)  # (the other modules' handlers registered at import must survive this test)
    vlsit_fakes.HANDLERS.clear()
    vlsit_fakes.register(vf)
    fake.vf = vf
    monkeypatch.setattr(runtime, "run_stage", fake)
    yield fake
    vlsit_fakes.HANDLERS[:] = saved


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: vlsit\n")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text(SPEC_TEXT)
    e = Engine(Project.open(tmp_path))
    return e


def run(engine, **kw):
    return anyio.run(lambda: engine.run(**kw))


def parsed(engine):
    return artifacts.read(engine.project, "structured_spec.json")


# -- constraints -----------------------------------------------------------------------------------


def test_constraint_expressions():
    v = {"PR_BOOT": "32'h8000_0000", "PR_IRQ": "1", "PR_CSR": "0", "PR_W": "16"}
    assert evaluate("PR_BOOT[1:0] == 2'b00", v) == (True, "holds")
    assert evaluate("PR_BOOT % 4 == 0", v)[0] is True
    ok, detail = evaluate("PR_IRQ == 1 -> PR_CSR == 1", v)
    assert ok is False and "PR_CSR == 1" in detail
    assert evaluate("PR_IRQ == 0 → PR_CSR == 1", v)[0] is True  # the condition is not active
    assert evaluate("PR_W in (8, 16, 32) && PR_W >= 2", v)[0] is True
    assert evaluate("$clog2(PR_W) == 4", v)[0] is True
    assert evaluate("PR_MISSING == 1", v)[0] is None
    assert to_int("4'b0101") == 5 and to_int("0x10") == 16 and to_int("true") == 1
    assert in_range("2", "0/1") is False and in_range("4", "1..8") and in_range("3", "align4") is False and in_range("9", "whatever")


# -- parse --------------------------------------------------------------------------------------------


def test_parse_writes_a_valid_structured_spec(engine, llm):
    assert run(engine, only="parse") == "gate"
    data = parsed(engine)
    assert artifacts.validate("structured_spec.json", data) == []
    ids = [r["req_id"] for r in data["requirements"]]
    assert ids == ["REQ-001", "REQ-002", "REQ-003", "REQ-004"]
    assert data["metadata"]["top_module"] == "ds_top" and data["metadata"]["gate_status"] == "pending"
    by = {r["req_id"]: r for r in data["requirements"]}
    assert by["REQ-003"]["rtl_modules"] == ["ds_top"]  # unmapped: the top module
    assert data["module_order"][-1] == "ds_top" and data["module_order"].index("ds_split") < data["module_order"].index("ds_top")
    assert {m["name"]: m["req_ids"] for m in data["modules"]}["ds_split"] == ["REQ-001", "REQ-002"]
    assert {p["name"]: p["req_ids"] for p in data["parameters"]}["PR_IN_W"] == ["REQ-001"]
    assert (engine.project.schemas_dir / "structured_spec.md").is_file()
    assert [s.name for s in engine.status() if s.gate == "open"] == ["parse"]

    engine.approve("parse")
    data = parsed(engine)
    assert data["metadata"]["gate_status"] == "approved" and data["metadata"]["gate_1_approved"] is True and data["metadata"]["approved_at"]
    assert [s for s in engine.status() if s.name == "parse"][0].edited == []  # the approval's write is not a user edit


def test_gate_1_refuses_approval_while_a_flagged_requirement_is_unconfirmed(engine, llm):
    """A requirement between the review threshold (0.3) and the blocking one (0.6) is needs_review (no blocking
    question): Gate 1 still refuses Approve for it (pending_reviews / review.pending_ids), separately from any
    blocking question."""
    from q3tui.flows.vlsit.parse import review

    spec = _spec()
    spec["requirements"][1].update(ambiguity_score=0.4, ambiguity_issue="wording could be read two ways",
                                   ambiguity_question="is this about the input or the output?", default_assumption="input")
    llm.vf.parsed = [spec]
    assert run(engine, only="parse") == "gate"
    data = parsed(engine)
    r2 = next(r for r in data["requirements"] if r["req_id"] == "REQ-002")
    assert r2["needs_review"] and not r2["needs_human_decision"]
    qs = [q for q in engine.questions() if q["step"] == "parse" and q["status"] == "open"]
    assert qs and qs[0]["blocking"] is False  # a question, but not a blocking one
    assert engine.by_name["parse"].pending_reviews(engine) == ["REQ-002"]
    with pytest.raises(Exception, match="1 item.*not reviewed yet.*REQ-002"):
        engine.approve("parse")

    review.review_requirement(engine, "REQ-002", True)
    assert engine.by_name["parse"].pending_reviews(engine) == []
    engine.approve("parse")  # the (non-blocking) question's default is still accepted along the way
    assert parsed(engine)["metadata"]["gate_status"] == "approved"


def test_gate_1_force_and_auto_confirm_reviews_bypass_pending_requirements(engine, llm):
    spec = _spec()
    spec["requirements"][1].update(ambiguity_score=0.4, ambiguity_issue="wording could be read two ways",
                                   ambiguity_question="is this about the input or the output?", default_assumption="input")
    llm.vf.parsed = [spec]
    assert run(engine, only="parse") == "gate"
    engine.approve("parse", force=True)
    assert parsed(engine)["metadata"]["gate_status"] == "approved"


def test_ambiguity_becomes_questions_and_ids_stay_stable(engine, llm):
    spec = _spec()
    spec["requirements"][1].update(ambiguity_score=0.7, ambiguity_issue="slice order unclear", ambiguity_question="LSB first? (yes/no)",
                                   default_assumption="LSB first")
    llm.vf.parsed = [spec]
    assert run(engine, only="parse") == "gate"
    data = parsed(engine)
    assert data["ambiguity_flags"] == ["REQ-002"]
    qs = [q for q in engine.questions() if q["step"] == "parse"]
    assert len(qs) == 1 and qs[0]["blocking"] is True and qs[0]["question"].startswith("REQ-002 [ambiguity 0.70] (round 1/3)")
    # (a spec you wrote yourself is never edited: the answer stays in parse)
    assert qs[0]["kind"] == "design_choice" and qs[0]["default_assumption"] == "LSB first"
    with pytest.raises(Exception, match="blocking"):
        engine.approve("parse")

    # the answer is applied as an update: the ambiguous requirement is rewritten and keeps its id, a new one gets the next id
    engine.answer(qs[0]["id"], "yes, LSB first")
    spec2 = _spec()
    spec2["requirements"][1].update(id="REQ-002", text="TLAST shall be asserted on the last (most significant) slice only.")
    spec2["requirements"].insert(0, {"text": "A new requirement.", "category": "timing", "rtl_modules": ["ds_fifo"]})
    spec2["requirements"][1].update(id="REQ-001")  # (the model kept the id of the unchanged one)
    llm.vf.parsed = [spec2]
    assert run(engine, only="parse") == "gate"
    data = parsed(engine)
    by_text = {r["text"]: r["req_id"] for r in data["requirements"]}
    assert by_text["Each input beat shall be split into slices."] == "REQ-001"
    assert by_text["TLAST shall be asserted on the last (most significant) slice only."] == "REQ-002"
    assert by_text["A new requirement."] == "REQ-005"  # never a reused number
    assert data["ambiguity_flags"] == []
    assert [q for q in engine.questions() if q["step"] == "parse" and q["status"] == "open"] == []
    engine.approve("parse")


def test_questions_go_back_to_a_generated_spec(engine, llm):
    from q3tui.pipeline.state import StepRecord
    from q3tui.flows.vlsit.parse.step import ParseStep

    assert ParseStep.question_kind(engine) == "design_choice"
    engine.state.steps["spec"] = StepRecord(status="done", origin="generated")
    assert ParseStep.question_kind(engine) == "spec_gap"


def test_still_ambiguous_after_three_rounds_needs_a_decision(engine, llm):
    def ambiguous():
        s = _spec()
        s["requirements"][1].update(ambiguity_score=0.5, ambiguity_issue="x", ambiguity_question="which?")
        return s

    llm.vf.parsed = [ambiguous()]
    run(engine, only="parse")
    for round_no in range(1, 4):
        q = next(q for q in engine.questions() if q["step"] == "parse" and q["status"] == "open")
        assert f"(round {round_no}/3)" in q["question"] or round_no == 3
        engine.answer(q["id"], "still unclear")
        llm.vf.parsed = [ambiguous()]
        run(engine, only="parse")
    q = next(q for q in engine.questions() if q["step"] == "parse" and q["status"] == "open")
    assert "needs your decision" in q["question"] and q["blocking"]
    assert next(r for r in parsed(engine)["requirements"] if r["req_id"] == "REQ-002")["needs_human_decision"] is True
    engine.answer(q["id"], "keep")
    llm.vf.parsed = [ambiguous()]
    run(engine, only="parse")
    r = next(r for r in parsed(engine)["requirements"] if r["req_id"] == "REQ-002")
    assert r["needs_human_decision"] is False and r["needs_review"] is False


def test_an_unchanged_result_keeps_the_approval(engine, llm):
    run(engine, only="parse")
    engine.approve("parse")
    calls = len(llm.calls)
    assert run(engine, only="parse") == "complete" and len(llm.calls) == calls  # up to date: no LLM
    engine.push_decision("parse", "config", "From config: nothing that changes a requirement.", rebase=False)  # (keeps an approved gate)
    llm.vf.parsed = [_spec()]  # same content
    assert run(engine, only="parse") == "complete"
    assert len(llm.calls) == calls + 1 and [s for s in engine.status() if s.name == "parse"][0].gate == "approved"
    assert parsed(engine)["metadata"]["gate_status"] == "approved"


def test_parse_waits_for_a_spec(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: vlsit\n")
    e = Engine(Project.open(tmp_path))
    assert e.step("parse").missing_input(e).startswith("no specification documents")


# -- config -----------------------------------------------------------------------------------------


def _through_parse(engine):
    run(engine, only="parse")
    engine.approve("parse")


def test_config_defaults_constraints_and_auto_gate(engine, llm):
    _through_parse(engine)
    assert run(engine, only="config") == "complete"  # gate mode auto: signs itself, no questions
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["metadata"]["gate_2_approved"] is True and cfg["metadata"]["approved_at"]
    from q3tui.flows.vlsit.config.step import validate_final_config

    assert validate_final_config(cfg) == []
    assert cfg["parameters"]["PR_IN_W"] == {**cfg["parameters"]["PR_IN_W"], "value": "64", "changed_from_default": False, "confirmed_by_user": True}
    assert cfg["constraints"]["C1"]["pass"] and cfg["constraints"]["C2"]["pass"]
    assert cfg["feature_matrix"]["F02"] == {"enabled": True, "mandatory": False, "parameters_driving": ["PR_CNT_EN"], "req_ids": ["REQ-004"]}
    assert cfg["feature_matrix"]["F01"]["mandatory"] is True
    assert cfg["style_constraints"]["source_file"] is None or cfg["style_constraints"]["source_file"].endswith(".md")
    assert [c for c in llm.names() if c == "config_overrides"] == []  # nothing asked: no LLM


def test_config_override_from_a_change_request_and_refusals(engine, llm):
    _through_parse(engine)
    run(engine, only="config")
    engine.request_change("config", "Turn the debug counter off, use a 20-bit input and change the FIFO depth to 8. No for-loops in modules.")
    llm.vf.overrides = [{"overrides": [{"name": "PR_CNT_EN", "value": "0", "reason": "requested"},
                                       {"name": "PR_IN_W", "value": "200", "reason": "requested"},   # outside 8..128
                                       {"name": "PR_DEPTH", "value": "8"}],                          # locked
                        "style_rules": [{"nl": "No for-loops in modules", "rule": "no for loops"}]}]
    assert run(engine, only="config") == "complete"
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["parameters"]["PR_CNT_EN"]["value"] == "0" and cfg["parameters"]["PR_CNT_EN"]["changed_from_default"]
    assert cfg["parameters"]["PR_IN_W"]["value"] == "64" and cfg["parameters"]["PR_DEPTH"]["value"] == "4"
    assert {r["name"] for r in cfg["metadata"]["rejected_overrides"]} == {"PR_IN_W", "PR_DEPTH"}
    assert cfg["feature_matrix"]["F02"]["enabled"] is False  # its driving parameter is 0
    assert cfg["style_constraints"]["natural_language_additions"] == ["No for-loops in modules"]
    assert cfg["style_constraints"]["lint_rules"][-1]["source"] == "user_addition"
    # the override is kept by later runs without asking again
    llm.vf.overrides.clear()
    engine.request_change("config", "nothing new")
    run(engine, only="config")
    assert artifacts.read(engine.project, "final_config.json")["parameters"]["PR_CNT_EN"]["value"] == "0"


def test_gate_2_auto_mode_signs_itself_without_confirming_changed_parameters(engine, llm):
    """Gate 2 is "auto" by default (FLOW.md): it self-signs via `approve(..., auto=True)`, which bypasses
    pending_reviews() same as a human who signs with nothing confirmed — unconfirmed changes simply are not."""
    _through_parse(engine)
    run(engine, only="config")
    engine.request_change("config", "Turn the debug counter off.")
    llm.vf.overrides = [{"overrides": [{"name": "PR_CNT_EN", "value": "0", "reason": "requested"}], "style_rules": []}]
    assert run(engine, only="config") == "complete"  # auto gate: no stop, even though PR_CNT_EN is changed and unconfirmed
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["parameters"]["PR_CNT_EN"]["changed_from_default"] and cfg["metadata"]["gate_2_approved"]


def test_gate_2_human_mode_refuses_approval_while_a_changed_parameter_is_unconfirmed(engine, llm):
    from q3tui.flows.vlsit.config import review

    engine.project.cfg.pipeline.gate_modes = {"config": "human"}
    _through_parse(engine)
    run(engine, only="config")
    engine.request_change("config", "Turn the debug counter off.")
    llm.vf.overrides = [{"overrides": [{"name": "PR_CNT_EN", "value": "0", "reason": "requested"}], "style_rules": []}]
    assert run(engine, only="config") == "gate"
    assert engine.by_name["config"].pending_reviews(engine) == ["PR_CNT_EN"]
    with pytest.raises(Exception, match="1 item.*not reviewed yet.*PR_CNT_EN"):
        engine.approve("config")
    engine.approve("config", force=True)  # force still bypasses it
    assert artifacts.read(engine.project, "final_config.json")["metadata"]["gate_2_approved"] is True

    # confirming first lets a plain approve through, on the next round
    engine.request_change("config", "nothing new")
    llm.vf.overrides = [{"overrides": [{"name": "PR_CNT_EN", "value": "0", "reason": "requested"}], "style_rules": []}]
    run(engine, only="config")
    review.review_parameter(engine, "PR_CNT_EN", True)
    assert engine.by_name["config"].pending_reviews(engine) == []
    engine.approve("config")
    assert artifacts.read(engine.project, "final_config.json")["metadata"]["gate_2_approved"] is True


def test_a_violated_constraint_blocks_the_gate_until_fixed(engine, llm):
    spec = _spec(constraints=[{"id": "C1", "rule": "PR_CNT_EN == 0 -> PR_IN_W == 32"}, {"id": "C2", "rule": "PR_IN_W % 8 == 0"}])
    llm.vf.parsed = [spec]
    _through_parse(engine)
    engine.request_change("config", "counter off")
    llm.vf.overrides = [{"overrides": [{"name": "PR_CNT_EN", "value": "0"}]}]
    assert run(engine, only="config") == "gate"  # auto gate, but a blocking question stops it
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["metadata"]["gate_2_approved"] is False and cfg["constraints"]["C1"]["pass"] is False
    q = next(q for q in engine.questions() if q["step"] == "config" and q["status"] == "open")
    assert q["blocking"] and "C1" in q["question"]
    engine.answer(q["id"], "PR_IN_W = 32")
    llm.vf.overrides = [{"overrides": [{"name": "PR_IN_W", "value": "32"}]}]
    assert run(engine, only="config") == "complete"
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["constraints"]["C1"]["pass"] and cfg["metadata"]["gate_2_approved"] is True
    assert cfg["parameters"]["PR_IN_W"]["value"] == "32" and cfg["parameters"]["PR_CNT_EN"]["value"] == "0"


def test_an_unevaluable_constraint_needs_the_users_confirmation(engine, llm):
    llm.vf.parsed = [_spec(constraints=[{"id": "C1", "rule": "the clock is faster than the bus"}])]
    _through_parse(engine)
    assert run(engine, only="config") == "gate"
    q = next(q for q in engine.questions() if q["step"] == "config" and q["status"] == "open")
    assert "cannot be evaluated" in q["question"] and q["blocking"]
    engine.answer(q["id"], "yes")
    assert run(engine, only="config") == "complete"
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["constraints"]["C1"]["pass"] and "confirmed" in cfg["constraints"]["C1"]["detail"]
    assert [c for c in llm.names() if c == "config_overrides"] == []


def _ask_constraint(engine, llm):
    llm.vf.parsed = [_spec(constraints=[{"id": "C1", "rule": "the clock is faster than the bus"}])]
    _through_parse(engine)
    assert run(engine, only="config") == "gate"
    q = next(q for q in engine.questions() if q["step"] == "config" and q["status"] == "open")
    assert q["kind"] == "design_choice" and {o["label"] for o in q["options"]} >= {"yes"}
    return q


def test_the_model_reads_an_answer_and_hands_it_over_to_the_owner(engine, llm):
    q = _ask_constraint(engine, llm)
    engine.answer(q["id"], "the bus is faster")  # not a yes: nothing is routed by itself, the model judges
    assert not engine.state.feedback.get("parse")
    llm.vf.overrides = [{"judgements": [{"constraint": "C1", "verdict": "handover", "handover_to": "req",
                                         "text": "C1 is wrong: the bus is faster than the clock", "reason": "changes the rule"}]}]
    assert run(engine, only="config") == "gate"
    assert any("the bus is faster" in f for f in engine.state.feedback.get("parse", []))  # handed to parse
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["metadata"]["gate_2_approved"] is False  # the rule is still unconfirmed: it does not pass the gate


def test_the_model_can_ask_a_follow_up_or_confirm(engine, llm):
    q = _ask_constraint(engine, llm)
    engine.answer(q["id"], "depends")
    llm.vf.overrides = [{"judgements": [{"constraint": "C1", "verdict": "followup", "text": "Which clock is meant?",
                                         "options": ["core clock", "bus clock"]}]}]
    assert run(engine, only="config") == "gate"
    follow = next(x for x in engine.questions() if x["step"] == "config" and x["status"] == "open" and "Which clock" in x["question"])
    assert follow["blocking"] and [o["label"] for o in follow["options"]] == ["core clock", "bus clock"]
    engine.answer(follow["id"], "core clock")
    llm.vf.overrides = [{"judgements": [{"constraint": "C1", "verdict": "confirm", "reason": "holds for the core clock"}]}]
    assert run(engine, only="config") == "complete"
    cfg = artifacts.read(engine.project, "final_config.json")
    assert cfg["constraints"]["C1"]["pass"] and cfg["metadata"]["gate_2_approved"] is True


def test_config_approved_with_failing_constraints_stays_unsigned(engine, llm):
    llm.vf.parsed = [_spec(constraints=[{"id": "C1", "rule": "PR_IN_W == 1"}])]
    _through_parse(engine)
    run(engine, only="config")
    engine.approve("config", force=True)
    assert artifacts.read(engine.project, "final_config.json")["metadata"]["gate_2_approved"] is False


# -- doc --------------------------------------------------------------------------------------------


def _rtl(engine):
    d = engine.project.src_dir / "rtl"
    d.mkdir(parents=True)
    (d / "ds_top.sv").write_text("""module ds_top #(parameter PR_IN_W = 64) (
  input  logic i_clk_core, input logic i_resetn_core,
  input  logic [PR_IN_W-1:0] i_s_axis_tdata, input logic i_s_axis_tvalid, output logic o_s_axis_tready,
  output logic [31:0] o_m_axis_tdata, output logic o_m_axis_tvalid, input logic i_m_axis_tready);
  ds_split u_split (.i_clk_core(i_clk_core));
endmodule
""")
    (d / "ds_split.sv").write_text("module ds_split (input logic i_clk_core); endmodule\n")


def test_doc_is_assembled_from_the_artifacts(engine, llm):
    _through_parse(engine)
    run(engine, only="config")
    _rtl(engine)
    artifacts.write(engine.project, "synth_report.json", {"tool": "Yosys", "pdk": "gf180", "top_module": "ds_top", "corners": {"tt": {"status": "pass"}}})
    llm.vf.prose = {"introduction": "A width downscaler IP.", "clock_reset_notes": "Reset is active low."}
    anyio.run(lambda: engine.run_step(engine.step("doc")))  # (the steps between are other tracks': run the step itself)
    text = (engine.project.docs_dir / "specification.md").read_text()
    assert 'title: "width_ds — Hardware IP Specification"' in text and "A width downscaler IP." in text
    assert "### Clock & Reset" in text and "`i_clk_core`" in text and "`i_resetn_core`" in text
    assert "### s_axis" in text and "### m_axis" in text and "`i_s_axis_tdata`" in text  # ports grouped by their protocol prefix
    assert "ds_top" in text and "ds_split `u_split`" in text  # the elaborated hierarchy
    assert "| tool | Yosys |" in text and "| corner tt | pass |" in text
    assert "Verification has not run" in text and "REQ-001" in text and "Reset is active low." in text
    assert "no `pdf` tool" not in text


def test_doc_without_prose_makes_no_llm_call(engine, llm):
    _through_parse(engine)
    engine.project.cfg  # noqa: B018
    engine.step("doc").options["prose"] = False
    anyio.run(lambda: engine.run_step(engine.step("doc")))
    assert "doc_prose" not in llm.names()
    assert "A width downscaler." in (engine.project.docs_dir / "specification.md").read_text()


# -- the spec step's VLSIT template --------------------------------------------------------------------


def test_spec_template_option(engine):
    from q3tui.steps.spec.template import load_template

    t, path = load_template(engine.project.spec_dir, "vlsit")
    assert path.name == "vlsit_template.yaml" and [s.id for s in t.sections][:3] == ["purpose_scope", "references", "requirements"]
    spec_step = engine.step("spec")
    assert spec_step.options == {"template": "vlsit"}
    assert spec_step.inputs(engine)[-1].name == "vlsit_template.yaml"
    (engine.project.spec_dir / "template.yaml").write_text(path.read_text())
    assert spec_step.inputs(engine)[-1].name == "template.yaml"  # the project's own template still wins
    with pytest.raises(ValueError):
        load_template(engine.project.spec_dir.parent, "nope")
