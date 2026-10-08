from pathlib import Path

import anyio
import pytest
import yaml

from q3tui.llm import runtime
from q3tui.pipeline.engine import Engine
from q3tui.core.project import Project
from q3tui.steps.spec import document
from q3tui.steps.spec.document import SpecDraft, parse_sections
from q3tui.steps.spec.template import DEFAULT_TEMPLATE, SpecTemplate, describe, intent_skeleton, load_template
from tests.fakes import SPEC, FakeLLM, use_test_flow


def default():
    return SpecTemplate.model_validate(yaml.safe_load(DEFAULT_TEMPLATE.read_text()))


def test_default_template_and_describe():
    t = default()
    assert [s.id for s in t.sections][:3] == ["overview", "features", "parameters"]
    text = describe(t)
    assert 'field "Clock frequency"' in text and "optional" in text
    assert "## Clocks and Resets" in intent_skeleton(t) and "- Clock frequency: " in intent_skeleton(t)


def test_check_and_normalize():
    t = default()
    assert document.check(SPEC, t) == []
    broken = SPEC.model_copy(deep=True)
    broken.sections = [s for s in broken.sections if s.id != "constraints"]
    broken.sections[0].fields = []
    clk = next(s for s in broken.sections if s.id == "clocks_resets")
    clk.fields = [f for f in clk.fields if f.name != "Clock frequency"]
    errors = "\n".join(document.check(broken, t))
    assert "'constraints'" in errors and "Clock frequency" in errors

    fixed = document.normalize(broken, t)
    ids = [s.id for s in fixed.sections]
    assert "constraints" in ids and ids.index("constraints") > ids.index("performance")
    clk = next(s for s in fixed.sections if s.id == "clocks_resets")
    assert {f.name: f.value for f in clk.fields}["Clock frequency"] == "TBD"
    assert "Clocks and Resets: Clock frequency" in document.tbd_fields(fixed)


def test_render_and_parse_roundtrip():
    md = document.render(document.normalize(SPEC, default()))
    parsed = document.parse_sections(md)
    assert [p[0] for p in parsed][:2] == ["overview", "features"]
    clk = next(p for p in parsed if p[0] == "clocks_resets")
    assert "500 MHz" in clk[2]
    user = document.parse_sections("# X\n\n## 1. My Overview\nhi\n## Timing\nfast\n")
    assert [(i, t) for i, t, _ in user] == [("my_overview", "My Overview"), ("timing", "Timing")]


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    return fake


def test_project_template_drives_spec(tmp_path, llm):
    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    (tmp_path / "spec" / "template.yaml").write_text(yaml.safe_dump({
        "allow_extra_sections": False,
        "sections": [
            {"id": "overview", "title": "Overview"},
            {"id": "timing", "title": "Timing", "fields": [{"name": "Clock frequency"}]},
        ]}))
    assert load_template(tmp_path / "spec")[1].name == "template.yaml"
    llm.spec_outputs = [
        {"top_module": "t", "title": "T", "questions": [],
         "sections": [{"id": "overview", "title": "Overview", "fields": [], "content": "o"}]},
        {"top_module": "t", "title": "T", "questions": [],
         "sections": [{"id": "overview", "title": "Overview", "fields": [], "content": "o"},
                      {"id": "timing", "title": "Timing", "fields": [{"name": "Clock frequency", "value": "1 GHz"}], "content": ""}]},
    ]
    anyio.run(lambda: engine.run(only="spec"))
    assert [c.name for c in llm.calls] == ["spec_write", "spec_repair"]
    assert "id=timing" in llm.calls[0].prompt and "'timing'" in llm.calls[1].prompt
    spec = (tmp_path / "spec" / "spec.md").read_text()
    assert "## 2. Timing" in spec and "1 GHz" in spec

    # editing the template makes the spec stale
    t = tmp_path / "spec" / "template.yaml"
    t.write_text(t.read_text().replace("Timing", "Clocking"))
    assert {v.name: v.status for v in engine.status()}["spec"] == "stale"


def test_section_review_tracking(tmp_path, llm):
    from q3tui.steps.spec.sections import mark_reviewed, section_states

    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(only="spec"))
    states = {s.id: s for s in section_states(engine)}
    assert states["overview"].label[0] == "⚑ not reviewed"
    assert states["parameters"].label[0] == "○ missing"  # optional, not written

    mark_reviewed(engine, "features")
    assert {s.id: s.reviewed for s in section_states(engine)}["features"] is True

    # hand edit of one section
    spec = tmp_path / "spec" / "spec.md"
    spec.write_text(spec.read_text().replace("Interfaces text.", "Interfaces text, edited."))
    st = {s.id: s for s in section_states(engine)}["interfaces"]
    assert st.edited and "✎ edited" in st.label[0]

    engine.approve("spec")
    assert all(s.reviewed for s in section_states(engine) if s.in_spec)

    engine.request_change("spec", "In section 'Performance': add a latency figure")
    assert {s.id: s for s in section_states(engine)}["performance"].change_requested

    # regeneration keeps reviews of unchanged sections only
    anyio.run(lambda: engine.run(only="spec"))
    states = {s.id: s for s in section_states(engine)}
    assert states["overview"].reviewed is False  # fake LLM changes the first section each run
    assert states["features"].reviewed is True


def test_template_edits_update_only_what_changed(tmp_path, llm):
    """Removing/reordering/renaming sections needs no LLM; adding or re-guiding one updates only it."""
    from q3tui.core.ops import Ops

    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(only="spec"))
    ops = Ops(engine)
    spec = tmp_path / "spec" / "spec.md"

    # remove + move: code only, no LLM call
    ops.remove_section("corner_cases")
    ops.move_section("performance", after="start")
    n = len(llm.calls)
    anyio.run(lambda: engine.run(only="spec"))
    assert len(llm.calls) == n
    ids = [sid for sid, _, _ in parse_sections(spec.read_text())]
    assert "corner_cases" not in ids and ids[0] == "performance"
    assert "Features text." in spec.read_text()  # untouched content kept

    # add a section + change another section's guidance: one small update, only those sections
    ops.add_section("Power Management", guidance="clock gating", after="performance")
    ops.update_section("overview", guidance="mention the target market")
    n = len(llm.calls)
    anyio.run(lambda: engine.run(only="spec"))
    assert [c.name for c in llm.calls[n:]] == ["spec_update"]
    prompt = llm.calls[n].prompt
    assert "power_management" in prompt and "Sections added to the template" in prompt
    assert "overview" in prompt and "new guidance: mention the target market" in prompt
    assert engine.evaluate(engine.by_name["spec"]).status == "done"  # nothing left stale


def test_review_changes_only_what_it_fixes_and_can_be_turned_off(tmp_path, llm):
    from q3tui.core.ops import Ops

    use_test_flow(tmp_path)          # self_review on (default)
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(only="spec"))
    assert [c.name for c in llm.calls] == ["spec_write", "spec_review"]
    review = llm.calls[1]
    assert review.output_model.__name__ == "ReviewPatch" and "Return only the sections you changed" in review.prompt
    spec = (tmp_path / "spec" / "spec.md").read_text()
    assert "Features reviewed" in spec              # the section the review changed
    assert "Overview text." in spec                 # untouched draft sections kept as written

    Ops(engine).set_spec_review(False)
    engine.request_change("spec", "x")
    anyio.run(lambda: engine.run(only="spec", regenerate={"spec"}))
    assert [c.name for c in llm.calls[2:]] == ["spec_write"]   # no review pass


def test_writer_reply_round_trips():
    from q3tui.steps.common import Question

    draft = SpecDraft.model_validate({
        "top_module": "apb_slave", "title": "APB slave", "questions": [
            {"id": "", "question": "Error on unmapped address?", "blocking": False, "default_assumption": "pslverr=0", "kind": "spec_gap"},
            {"id": "Q-S3", "question": "Wait states?", "blocking": True, "default_assumption": "", "kind": "design_choice"}],
        "sections": [
            {"id": "overview", "title": "Overview", "fields": [], "content": "An APB slave.\n\n### Notes\nquote \" and \\ backslash"},
            {"id": "timing", "title": "Timing", "fields": [{"name": "Clock", "value": "100 MHz | 200 MHz"}], "content": "text"}]})
    parsed, problems = document.parse_reply("Here is the spec:\n\n```markdown\n" + document.render_reply(draft) + "```\n")
    assert problems == []
    assert parsed.model_dump() == draft.model_dump()
    assert isinstance(parsed.questions[1], Question) and parsed.questions[1].id == "Q-S3" and parsed.questions[1].blocking

    # the model's own table header is fine too
    reply = document.render_reply(draft).replace("| | |\n|---|---|", "| Constraint | Value |\n|-----------|:------:|")
    parsed, _ = document.parse_reply(reply)
    assert parsed.sections[1].fields[0].value == "100 MHz | 200 MHz" and parsed.sections[1].content == "text"

    bad, problems = document.parse_reply("I think the spec should say ...")
    assert bad is None and "title" in problems[0]
    bad, problems = document.parse_reply("# T\n\nsome text without sections\n")
    assert bad is None and any("Top module" in p for p in problems) and any("no sections" in p for p in problems)


def test_writer_reply_in_wrong_layout_is_asked_again(tmp_path, llm):
    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    llm.spec_replies = ["Sure! The block is a FIFO."]  # first reply unusable; the second is rendered from the draft
    anyio.run(lambda: engine.run(only="spec"))
    assert [c.name for c in llm.calls] == ["spec_write", "spec_write"]
    assert llm.calls[1].resume == "sess-1" and "could not be used" in llm.calls[1].prompt
    assert llm.calls[0].output_model is None and llm.calls[0].builtin_tools == []  # plain Markdown; nothing to explore
    assert (tmp_path / "spec" / "spec.md").is_file()


def test_a_table_the_model_puts_into_an_invented_field_becomes_body_text():
    """Real run (Haiku, VLSIT template): the parameter table came back as a field called `table`, rendered as a broken row."""
    from q3tui.steps.spec.document import SectionOut, SpecDraft, FieldValue, normalize
    from q3tui.steps.spec.template import SpecTemplate, TemplateSection

    template = SpecTemplate(sections=[TemplateSection(id="parameters", title="Parameters", required=True)])
    table = "| Parameter | Default |\n|---|---|\n| `PR_W` | 8 |"
    draft = SpecDraft(sections=[SectionOut(id="parameters", title="Parameters", content="Constraints: C1.",
                                           fields=[FieldValue(name="table", value=table), FieldValue(name="Note", value="short")])],
                      top_module="demo", title="Demo", questions=[])
    out = normalize(draft, template).sections[0]
    assert table in out.content and out.content.startswith("Constraints: C1.")  # the table is body text, after the existing body
    assert [f.name for f in out.fields] == ["Note"]  # a short extra field stays a field


def test_everything_that_shows_the_sections_uses_the_flows_template(tmp_path):
    """Real TUI: the Sections table compared a VLSIT spec with the DEFAULT template — the VLSIT sections showed as "extra" and
    the default ones (Clocks and Resets, Numerical Accuracy …) as "missing"."""
    from q3tui.core.ops import Ops
    from q3tui.pipeline.engine import Engine
    from q3tui.core.project import Project
    from q3tui.steps.spec.sections import section_states

    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: vlsit\n")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("# T\n\n<!-- section: purpose_scope -->\n## 1. Purpose, Scope, and Product Context\ntext\n\n<!-- section: interfaces -->\n## 2. External Interfaces and Ports\ni_clk\n")
    eng = Engine(Project.open(tmp_path))
    assert eng.spec_template() == "vlsit"
    ids = {s.id: s for s in section_states(eng)}
    assert ids["purpose_scope"].in_spec and ids["interfaces"].in_spec and ids["purpose_scope"].required is True  # (the template's own)
    assert ids["references"].in_spec is False and ids["references"].required is True  # a real gap, not an "extra"
    assert "references" in ids and "clocks_resets" not in ids  # the VLSIT sections, not the default ones
    assert [s.id for s in Ops(eng).template().sections][:3] == ["purpose_scope", "references", "requirements"]
    use_test_flow(tmp_path)                                          # a spec step without a template option
    assert Engine(Project.open(tmp_path)).spec_template() is None


# -- review fixes ---------------------------------------------------------------------------------------------------------

def _draft(questions, **kw):
    return {"top_module": "fifo_top", "title": "T", "questions": questions, "sections": SPEC.model_dump()["sections"], **kw}


def _q(text, **kw):
    return {"id": "", "question": text, "blocking": False, "default_assumption": "", **kw}


def test_regenerate_keeps_the_users_answers(tmp_path, llm):
    use_test_flow(tmp_path, "spec: {self_review: true}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(only="spec"))
    qid = engine.questions()[0]["id"]
    engine.answer(qid, "wrap on overflow")
    n = len(llm.calls)
    anyio.run(lambda: engine.run(only="spec", regenerate={"spec"}))
    write, review = llm.calls[n], llm.calls[n + 1]
    assert write.name == "spec_write" and "wrap on overflow" in write.prompt and "Should drop_count saturate?" in write.prompt
    assert review.name == "spec_review" and "Should drop_count saturate?" in review.prompt  # the answer comes with its question


def test_answered_question_ids_are_never_reused(tmp_path, llm):
    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    llm.spec_outputs = [_draft([_q("first?"), _q("second?")])]
    anyio.run(lambda: engine.run(only="spec"))
    assert [q["id"] for q in engine.questions()] == ["Q-S1", "Q-S2"]
    engine.answer("Q-S2", "yes")
    anyio.run(lambda: engine.run(only="spec"))  # the answer is applied; questions.json no longer lists Q-S2
    llm.spec_outputs = [_draft([_q("first?"), _q("third?")])]
    anyio.run(lambda: engine.run(only="spec", regenerate={"spec"}))
    ids = {q["question"]: q["id"] for q in engine.questions()}
    assert ids["first?"] == "Q-S1" and ids["third?"] == "Q-S3"  # not Q-S2: its answer is on file
    assert not any(q["question"] == "third?" and q["status"] != "open" for q in engine.questions())


def test_update_path_normalizes_patches_and_fills_required_sections(tmp_path, llm, monkeypatch):
    from q3tui.llm.runtime import StageResult
    from q3tui.steps.spec.document import SpecPatch

    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(only="spec"))
    spec = tmp_path / "spec" / "spec.md"
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    patch = SpecPatch(sections=[{"id": "features", "title": "Features", "content": "## Features\nIntro.",
                                 "fields": [{"name": "table", "value": table}]}], conflicts=[], summary="x")

    async def fake(stage, cfg, emit):
        if stage.output_model is SpecPatch:
            return StageResult("ok", patch, 0.0, 1, "s")
        return await llm(stage, cfg, emit)

    monkeypatch.setattr(runtime, "run_stage", fake)
    text = spec.read_text()
    assert "<!-- section: constraints -->" in text
    sections = parse_sections(text)
    cons = next(t for sid, t, _ in sections if sid == "constraints")
    # a hand edit dropped a required section; the top module was renamed by hand
    start = text.index("<!-- section: constraints -->")
    nxt = text.find("<!-- section:", start + 5)
    text = text[:start] + (text[nxt:] if nxt != -1 else "")
    spec.write_text(text.replace("Top module: `fifo_top`", "Top module: `new_top`"))
    engine.request_change("spec", "x")
    before = len(list((tmp_path / ".q3tui" / "bkp").glob("*")))
    anyio.run(lambda: engine.run(only="spec"))
    out = spec.read_text()
    feat = dict((sid, b) for sid, _, b in parse_sections(out))["features"]
    assert table in feat and feat.startswith("Intro.") and "## Features" not in feat   # table is body text, heading stripped
    assert "| **table**" not in out
    restored = dict((sid, b) for sid, _, b in parse_sections(out))["constraints"]
    assert "TBD" in restored and cons in out                                          # required section is back, as TBD
    assert engine.state.top == "new_top"
    assert len(list((tmp_path / ".q3tui" / "bkp").glob("*"))) == before + 1           # backed up once, because it changed


def test_update_without_changes_makes_no_backup(tmp_path, llm):
    use_test_flow(tmp_path, "spec: {self_review: false}\n")
    engine = Engine(Project.open(tmp_path))
    engine.import_intent("fifo")
    anyio.run(lambda: engine.run(only="spec"))
    bkp = tmp_path / ".q3tui" / "bkp"
    before = len(list(bkp.glob("*"))) if bkp.exists() else 0
    anyio.run(lambda: engine.run(only="spec"))
    assert (len(list(bkp.glob("*"))) if bkp.exists() else 0) == before


def test_noop_review_keeps_the_writers_questions():
    from q3tui.steps.common import Question
    from q3tui.steps.spec.document import ReviewPatch, apply_review

    draft = SPEC.model_copy(update={"questions": [Question(id="", question="keep me", blocking=True, default_assumption="")]})
    assert apply_review(draft, ReviewPatch(sections=[], questions=[], summary="ok")).questions[0].question == "keep me"
    changed = apply_review(draft, ReviewPatch(sections=[], questions=[_q_model("other")], summary="ok"))
    assert [q.question for q in changed.questions] == ["other"]


def _q_model(text):
    from q3tui.steps.common import Question

    return Question(id="", question=text, blocking=False, default_assumption="")


def test_reply_parsing_edge_cases():
    draft = SpecDraft.model_validate(_draft([]))
    body = document.render(draft)
    # no marker, but the model wrote the heading: it is the questions block, not a section
    parsed, problems = document.parse_reply(body + "\n## Open questions\n\n- Q: width? | assumed: 8 | blocking: yes | kind: design_choice\n")
    assert problems == [] and [s.id for s in parsed.sections] == [s.id for s in draft.sections]
    assert parsed.questions[0].question == "width?" and parsed.questions[0].blocking
    # " | " inside a question / assumption does not cut it
    q = document.parse_reply(body + "\n" + document.QUESTIONS_MARK + "\n- Q: a | b mode? | assumed: x | y | blocking: no | kind: spec_gap | id: Q-S4\n")[0].questions[0]
    assert (q.question, q.default_assumption, q.id) == ("a | b mode?", "x | y", "Q-S4")
    # a `## ` line at column 0 inside a code fence is not a heading
    md = "# X\n\n<!-- section: sva -->\n## 1. Assertions\n\n```systemverilog\n## 2 b\n<!-- section: fake -->\n```\n\n<!-- section: b -->\n## 2. B\nx\n"
    assert [s[0] for s in parse_sections(md)] == ["sva", "b"] and "## 2 b" in parse_sections(md)[0][2]


def test_tbd_questions_match_whole_words_only():
    from q3tui.steps.spec.document import FieldValue, SectionOut, tbd_questions

    secs = [SectionOut(id="s", title="Clocks", fields=[FieldValue(name="reset", value="TBD")], content="")]
    unrelated = [_q_model("Should the reset be async?")]            # mentions reset, but not the section: still asks
    assert len(tbd_questions(secs, unrelated)) == 1
    covered = [_q_model("Clocks: what is the reset style?")]
    assert tbd_questions(secs, covered) == []


def test_vlsit_template_follows_the_asic_ip_design_specification():
    t, path = load_template(Path("/nonexistent"), "vlsit")
    ids = [s.id for s in t.sections]
    assert path.name == "vlsit_template.yaml" and len(ids) == 21 and len(set(ids)) == 21   # sections 1-21; 22 (sources) is not part of the spec
    assert ids[:3] == ["purpose_scope", "references", "requirements"] and ids[-2:] == ["assumptions", "glossary"]
    assert [s.id for s in t.sections if not s.required] == ["glossary"]
    by = t.by_id()
    assert "PARA_" in by["parameters"].guidance and "C1" in by["parameters"].guidance   # constraints stay checkable rules for `parse`
    assert "Do NOT declare" in by["csr"].guidance                                      # registers live in the CSR workbook only
    text = describe(t)
    assert text.startswith("Document rules:") and "REQ-<AREA>-<NNN>" in text and "N/A" in text
    # the long guidance does not flood the intent skeleton
    assert max(len(line) for line in intent_skeleton(t).splitlines()) < 260


def test_template_instructions_are_kept_only_when_set(tmp_path):
    from q3tui.steps.spec.template import save_project_template

    d = default()
    assert d.instructions == "" and "Document rules" not in describe(d)
    assert "instructions" not in save_project_template(tmp_path, d).read_text()
    d.instructions = "Be terse."
    assert "instructions: Be terse." in save_project_template(tmp_path, d).read_text()
    assert describe(d).startswith("Document rules: Be terse.")
