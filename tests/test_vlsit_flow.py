"""flows/vlsit/ end to end through Engine.run: spec → parse → config → rtl → tb → sva → verify → doc, with the human gates,
fake tools (scripts standing in for lint / synth / compile / simulate, described in tools.json) and fake LLM stages
(the handlers of the step tests, combined). No live LLM, no EDA tool."""

import json
import re
import sys
import textwrap
from pathlib import Path

import anyio
import pytest
from click.testing import CliRunner

from q3tui.core.cli import main
from q3tui.llm import runtime
from q3tui.llm.runtime import StageResult
from q3tui.pipeline.engine import Engine
from q3tui.core.project import Project
from q3tui.steps.vlsit import artifacts
from q3tui.flows.vlsit.sva import review
from q3tui.flows.vlsit.sva.step import SvaFile
from q3tui.flows.vlsit.verify.step import VerifyTriage
from tests import vlsit_fakes
from tests.fakes import FakeLLM
from tests.test_vlsit_parse_config_doc import VlsitFake
from tests.test_vlsit_rtl_tb import LINT, MODULES, SYNTH, vlsit_handler

# -- fake tools: compile keeps the RTL as a "model", run compares it with the first one it saw (a mutant differs → FAIL) --

SIM = '''
import pathlib, re, sys
fl, work = sys.argv[1], pathlib.Path(sys.argv[2])
work.mkdir(parents=True, exist_ok=True)
rtl, covers, bad = "", [], False
for line in pathlib.Path(fl).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("+") or not pathlib.Path(line).is_file():
        continue
    text = pathlib.Path(line).read_text()
    if "SYNTAX_ERR" in text:
        print(f"{line}:3: error: syntax error"); bad = True
    if "/rtl/" in line or "mutant" in line and "_sva" not in line:
        rtl += text
    covers += re.findall(r"(c_\\w+)\\s*:\\s*cover", text)
(work / "model.txt").write_text(rtl)
(work / "covers.txt").write_text(" ".join(covers))
sys.exit(1 if bad else 0)
'''
RUN = '''
import pathlib, sys
work, tc, golden = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
model = (work / "model.txt").read_text()
if not golden.exists():
    golden.write_text(model)
for label in (work / "covers.txt").read_text().split():
    print("COVER_HIT", label)
print(f"TC_RESULT {tc} {'PASS' if golden.read_text() == model else 'FAIL'}")
'''


def tools_json(d: Path) -> str:
    py = sys.executable
    return json.dumps({"roles": {
        "lint": {"cmd": f"{py} {d}/lint.py {{filelist}} {{top}}", "parse": "verilator"},
        "synth": {"cmd": f"{py} {d}/synth.py {{script}}", "parse": "yosys"},
        "sim": {"cmd": f"{py} {d}/sim.py {{filelist}} {{workdir}}", "parse": "iverilog", "supports": ["sva"]},
        "run": {"cmd": f"{py} {d}/run.py {{workdir}} {{tc}} {d}/golden.txt", "parse": "iverilog"},
    }})


def sva_text(module: str, reqs: list[str]) -> str:
    body = ""
    for i, r in enumerate(reqs, 1):
        body += (f"  // NL: {r} holds\n  // REQ: {r}\n  a_{module}_{i} : assert property (@(posedge i_clk) disable iff (!i_resetn) 1'b1) else $error(\"a\");\n"
                 f"  // NL: the trigger of {r}\n  // REQ: {r}\n  c_{module}_{i} : cover property (@(posedge i_clk) disable iff (!i_resetn) 1'b1) $display(\"COVER_HIT c_{module}_{i}\");\n")
    return f"`default_nettype none\nmodule {module}_sva (input logic i_clk, input logic i_resetn);\n`ifndef SYNTHESIS\n{body}`endif\nendmodule\n`default_nettype wire\n"


def sva_handler(fake, stage, emit):
    if stage.name.startswith("sva_fix_"):
        module = stage.name[len("sva_fix_"):]
    elif stage.name.startswith("sva_"):
        module = stage.name[4:]
    else:
        return None
    return StageResult("", SvaFile(content=sva_text(module, fake.vlsit_modules[module]["reqs"])), 0.0, 1, "s")


def _v2_compliant(text: str) -> str:
    """vlsit_handler's shared fake RTL follows rtl_rule.md's ("project" profile) naming; this flow's real default
    rule file is vlsit_rtl_rule_default.md ("default" profile) — translate the parts the module name prefix (set
    on fake.vlsit_modules' keys, below) does not already fix: reset name, block label, parameter name."""
    text = text.replace("i_resetn_core", "i_rst_n_core").replace("begin : p_q", "begin : p_ff_q").replace("PR_W", "PARA_W")
    return re.sub(r"\bu_m_(\w+)", r"u_\1", text)  # instance name = module name without its m_ prefix (NAM-08)


def rtl_handler(fake, stage, emit):
    """The step test's RTL plus a comparison a mutant can flip (the plain fake RTL has nothing to mutate)."""
    out = vlsit_handler(fake, stage, emit)
    if out is not None and stage.name.startswith("rtl_") and stage.name != "rtl_plan":
        f = stage.write_files[0]
        text = f.read_text().replace("assign o_y = reg_q;", "assign o_y = (reg_q == reg_q) ? reg_q : '0;")
        f.write_text(_v2_compliant(text))
    return out


def triage_handler(fake, stage, emit):
    return StageResult("", VerifyTriage(verdicts=[]), 0.0, 1, "s") if stage.name == "verify_triage" else None


def parsed_spec(req2="b is registered") -> dict:
    return {
        "ip_name": "demo", "spec_revision": "0.1", "top_module": "m_top", "description": "demo",
        "requirements": [
            {"id": "REQ-001", "text": "a is registered", "category": "functional", "rtl_modules": ["m_leaf_a"], "sva_hint": "property"},
            {"id": "REQ-002", "text": req2, "category": "functional", "rtl_modules": ["m_leaf_b"], "sva_hint": "property"},
            {"id": "REQ-003", "text": "top ties them", "category": "interface", "rtl_modules": ["m_top"], "sva_hint": "property"}],
        "parameters": [{"name": "PARA_W", "type": "int unsigned", "default": "8", "valid_range": "8..64", "description": "width"}],
        "constraints": [{"id": "C1", "rule": "PARA_W % 8 == 0", "description": "byte aligned"}],
        "modules": [{"name": "m_top", "description": "top", "instances": ["m_leaf_a", "m_leaf_b"]},
                    {"name": "m_leaf_a", "description": "a"}, {"name": "m_leaf_b", "description": "b"}],
    }


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    # module names follow vlsit_rtl_rule_default.md's "default" profile (m_<function>): this flow's real default rule
    fake.vlsit_modules = {f"m_{k}": {**v, "children": [f"m_{c}" for c in v["children"]]} for k, v in MODULES.items()}
    fake.vlsit_bad, fake.vlsit_always_bad, fake.vlsit_questions = {}, {}, {}
    fake.vlsit_plan_calls = fake.vlsit_tb_plan_calls = fake.vlsit_top_bad = 0
    fake.vlsit_tb_plans = [[{"tc_id": "TC-001", "name": "a_reg", "description": "a", "req_ids": ["REQ-001", "REQ-003"]},
                            {"tc_id": "TC-002", "name": "b_reg", "description": "b", "req_ids": ["REQ-002"]}]]
    fake.vlsit_tc_bad, fake.vlsit_tc_always_bad = {}, {}
    vf = VlsitFake()
    vf.parsed = [parsed_spec()]
    fake.vf = vf
    saved = list(vlsit_fakes.HANDLERS)
    vlsit_fakes.HANDLERS[:] = [vf, rtl_handler, sva_handler, triage_handler]
    monkeypatch.setattr(runtime, "run_stage", fake)
    yield fake
    vlsit_fakes.HANDLERS[:] = saved


def make_engine(tmp_path: Path, cfg: str = "") -> Engine:
    d = tmp_path / "faketools"
    d.mkdir()
    for name, text in (("lint.py", LINT), ("synth.py", SYNTH), ("sim.py", SIM), ("run.py", RUN)):
        (d / name).write_text(text)
    (tmp_path / "tools.json").write_text(tools_json(d))
    (tmp_path / "q3tui.yaml").write_text("pipeline:\n  flow: vlsit\n" + cfg)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "demo_spec.md").write_text("# demo spec\n\nF01 a is registered.\n")
    return Engine(Project.open(tmp_path))


def run(eng, **kw):
    return anyio.run(lambda: eng.run(**kw))


def names(llm):
    return [c.name for c in llm.calls]


def errors(eng):
    return {k: r.error for k, r in eng.state.steps.items() if r.error}


def drive(eng, sign=()):
    """Run to the end like a user would: confirm the properties at Gate 3b, sign requirements at Gate 5, approve every gate."""
    for _ in range(12):
        out = run(eng)
        if out not in ("gate", "complete"):
            raise AssertionError(f"{out}: {errors(eng)}")
        if out != "gate":
            return out
        for name in eng.pending_approvals():
            if name == "sva":
                review.confirm_all(eng, include_vacuous=True)
            if name == "verify":
                for rid in sign:
                    review.sign_req(eng, rid, True, "reviewed")
            eng.approve(name)
    raise AssertionError("the flow keeps stopping at gates")


def statuses(eng):
    return {v.name: (v.status, v.gate) for v in eng.status()}


# -- the whole flow -----------------------------------------------------------------------------------------------


def test_the_vlsit_flow_end_to_end(tmp_path, llm):
    eng = make_engine(tmp_path)
    assert [s.name for s in eng.pipeline_steps] == ["spec", "parse", "config", "rtl", "tb", "sva", "verify", "doc"]
    assert eng.gate_modes() == {"spec": "human", "parse": "human", "config": "auto", "tb": "auto", "sva": "human", "verify": "human"}
    p = eng.project

    # Gate 1 (human): parse stops the run
    assert run(eng) == "gate", errors(eng)
    assert statuses(eng)["parse"] == ("done", "open") and statuses(eng)["config"][0] == "pending"
    spec = artifacts.read(p, "structured_spec.json")
    assert artifacts.validate("structured_spec.json", spec) == []
    assert spec["metadata"]["gate_status"] == "pending"
    assert [r["req_id"] for r in spec["requirements"]] == ["REQ-001", "REQ-002", "REQ-003"]
    eng.approve("parse")
    assert artifacts.read(p, "structured_spec.json")["metadata"]["gate_status"] == "approved"
    assert eng.evaluate(eng.by_name["parse"]).edited == [] and statuses(eng)["parse"] == ("done", "approved")

    # config (Gate 2 auto), rtl, tb (Gate 4 auto), sva: stops at Gate 3b
    assert run(eng) == "gate", errors(eng)
    st = statuses(eng)
    assert st["config"] == ("done", "approved") and st["rtl"][0] == "done" and st["tb"] == ("done", "approved")
    assert st["sva"] == ("done", "open") and st["verify"][0] == "pending"
    cfg = artifacts.read(p, "final_config.json")
    assert artifacts.validate("final_config.json", cfg) == [] and cfg["metadata"]["gate_2_approved"] is True
    plan = artifacts.read(p, "selected_testplan.json")
    assert artifacts.validate("selected_testplan.json", plan) == [] and plan["metadata"]["gate_4_approved"] is True
    assert {m: (p.src_dir / "rtl" / f"m_{m}.sv").is_file() for m in MODULES} == dict.fromkeys(MODULES, True)
    assert (p.src_dir / "rtl" / "filelist.f").is_file() and (p.src_dir / "tb" / "tb_top.sv").is_file()
    assert artifacts.read(p, "synth_report.json")["synthesis"] == "pass"
    props = review.load_properties(eng)
    assert len(props) == 6 and all(x["status"] == "pending" for x in props)
    assert artifacts.read(p, "rtm.json")["metadata"]["gate_3_approved"] is False

    # Gate 3b: confirm the properties, approve
    review.confirm_all(eng, include_vacuous=True)
    eng.approve("sva")
    assert artifacts.read(p, "rtm.json")["metadata"]["gate_3_approved"] is True

    # verify stops at Gate 5: simulation, mutation, RTM; nothing is signed off by the tool
    assert run(eng) == "gate", eng.state.steps["verify"].error
    assert statuses(eng)["verify"] == ("done", "open")
    report = artifacts.read(p, "verification_report.json")
    assert artifacts.validate("verification_report.json", report) == []
    assert report["sim_summary"]["pass"] == 2 and report["sim_summary"]["fail"] == 0
    rtm = artifacts.read(p, "rtm.json")
    assert artifacts.validate("rtm.json", rtm) == []
    rows = {r["req_id"]: r for r in rtm["requirements"]}
    for r in rows.values():  # the six conditions: 1-5 by evidence, 6 by you
        assert r["rtl_traced"] and r["sva_traced"] and r["tc_traced"] and r["sim_pass"]
        assert isinstance(r["mutation_score"], float) and r["mutation_score"] >= 0.85, r
        assert r["signed_off"] is False
    assert review.sign_req(eng, "REQ-001", True, "reviewed") == "REQ-001 signed off"
    eng.approve("verify")
    rtm = artifacts.read(p, "rtm.json")
    assert rtm["metadata"]["gate_5_approved"] is True and rtm["metadata"]["gate_3_approved"] is True
    assert [r["signed_off"] for r in rtm["requirements"]] == [True, False, False]  # Gate 5 approved ≠ everything signed
    assert artifacts.read(p, "verification_report.json")["metadata"]["gate_5_approved"] is True

    assert run(eng) == "complete"
    doc = (p.docs_dir / "specification.md").read_text()
    assert "REQ-001" in doc and "m_leaf_a" in doc
    # nothing went stale behind the gate approvals, and a further run does nothing
    before = len(llm.calls)
    assert run(eng) == "complete" and len(llm.calls) == before
    assert {k: s for k, s in statuses(eng).items() if s[0] not in ("done", "user") or s[1] == "open"} == {}
    assert {s.name: eng.evaluate(s).edited for s in eng.steps if eng.evaluate(s).edited} == {}


def test_a_changed_requirement_updates_only_what_it_touches(tmp_path, llm):
    eng = make_engine(tmp_path)
    assert drive(eng) == "complete"
    llm.calls.clear()
    (tmp_path / "faketools" / "golden.txt").unlink()  # the "golden" RTL of the fake simulator moves with the change
    llm.vf.parsed = [parsed_spec(req2="b is registered and cleared by reset")]
    eng.request_change("parse", "REQ-002 also needs the reset clearing")
    assert run(eng) == "gate"  # Gate 1 again
    eng.approve("parse")
    assert drive(eng) == "complete"
    called = names(llm)
    assert "rtl_m_leaf_b" in called and "rtl_m_leaf_a" not in called
    assert "sva_m_leaf_b" in called and "sva_m_leaf_a" not in called
    assert not any(n.startswith("rtl_plan") for n in called)
    assert {k: v for k, v in statuses(eng).items() if v[0] not in ("done", "user")} == {}


def test_reset_follows_the_flows_dependencies(tmp_path, llm):
    eng = make_engine(tmp_path)
    assert drive(eng) == "complete"
    assert eng.downstream("config") == ["config", "rtl", "tb", "sva", "verify", "doc"]
    assert eng.downstream("tb") == ["tb", "sva", "verify", "doc"]
    eng.reset("config")
    p = eng.project
    assert (p.schemas_dir / "structured_spec.json").is_file() and not (p.schemas_dir / "final_config.json").is_file()
    assert not list((p.src_dir / "rtl").glob("*.sv")) and not (p.docs_dir / "specification.md").is_file()
    st = statuses(eng)
    assert st["parse"][0] == "done" and st["config"][0] == "pending" and st["rtl"][0] in ("pending", "missing_input")
    # your reviews of assertions survive a reset (they are yours)
    assert (p.schemas_dir / "sva_reviews.json").is_file()


def test_gate_modes_decide_where_the_run_stops(tmp_path, llm):
    eng = make_engine(tmp_path, "  gate_modes: {parse: auto, sva: auto, verify: auto}\n")
    assert run(eng) == "complete"  # no human gate is left: parse / sva / verify sign themselves
    assert statuses(eng)["parse"] == ("done", "approved")
    # the assertions nobody confirmed do not count, and nobody signed a requirement
    rows = artifacts.read(eng.project, "rtm.json")["requirements"]
    assert all(not r["sva_traced"] and not r["signed_off"] for r in rows)


def test_auto_approve_runs_the_whole_flow_signing_what_is_ready(tmp_path, llm):
    """auto approve = approve the gates + confirm the assertions + sign the ready requirements — all marked "not reviewed";
    nothing is signed that is locked (conditions 1–5 must hold)."""
    eng = make_engine(tmp_path, "  auto_answer: true\n")
    assert run(eng) == "complete"
    rtm = artifacts.read(eng.project, "rtm.json")
    assert rtm["metadata"]["gate_5_approved"] is True and rtm["metadata"]["gate_3_approved"] is True
    signed = [r for r in rtm["requirements"] if r["signed_off"]]
    assert signed and all("auto-signed" in (r["sign_off_note"] or "") for r in signed)
    for r in rtm["requirements"]:
        if r["signed_off"]:
            assert r["rtl_traced"] and r["sva_traced"] and r["tc_traced"] and r["sim_pass"]  # conditions 1–5 held
        else:
            assert r["status"] == "locked"


def test_flow_check_lists_the_vlsit_flow_and_its_tools(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: vlsit}\n")
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "flow", "check"])
    assert r.exit_code == 0, r.output
    assert "vlsit_parse" in r.output and "tools needed: lint, synth, sim, run" in r.output and "flow is valid" in r.output
    r = CliRunner().invoke(main, ["-C", str(tmp_path), "gate", "parse", "auto"])
    assert r.exit_code == 0 and "parse: auto" in (tmp_path / "q3tui.yaml").read_text().replace("'", "")


def test_the_whole_flow_is_one_session(tmp_path, llm):
    """Like the original single Claude Code conversation: every LLM task of the flow goes to the one `flow` session."""
    from q3tui.llm import roles

    eng = make_engine(tmp_path)
    assert eng.session_mode() == "flow"
    drive(eng)
    tasks = [c for c in llm.calls if c.role]
    assert tasks and len(tasks) == len(llm.calls)  # nothing ran in a session of its own
    assert {c.role for c in tasks} == {"flow"} and all(c.system_prompt == roles.CHARTERS["flow"] for c in tasks)  # one stable charter
    assert "# Project brief" in tasks[0].prompt and tasks[0].resume is None
    assert all(c.prompt.startswith("# Task:") or "# Task:" in c.prompt for c in tasks)
    assert roles.notes_path(eng.project, "flow").name == "NOTES.md"


def test_session_can_be_set_back_to_fresh_per_stage(tmp_path, llm):
    eng = make_engine(tmp_path, cfg="  session: fresh\n")
    assert eng.session_mode() == "fresh"
    run(eng)
    assert llm.calls and all(c.role is None for c in llm.calls)


def test_the_example_flow_uses_fresh_sessions(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {flow: example}\n")
    assert Engine(Project.open(tmp_path)).session_mode() == "fresh"


def test_the_flow_session_gets_every_prompt_in_full_and_a_full_reset_starts_it_over(tmp_path, llm):
    """A real run: the failing tests' evidence was replaced by "[unchanged, as sent to you earlier]" stubs (so the triage saw
    nothing), and a reset left the session remembering the old design."""
    import json

    from q3tui.llm import roles

    eng = make_engine(tmp_path)
    drive(eng)
    team = roles.team(eng.project)
    assert team.roles["flow"].session is not None or team.roles["flow"].tasks > 0
    long = "evidence line with the failing check\n" * 40
    # the same long text twice in one flow-session task each time: in full both times (never a stub)
    from q3tui.llm.runtime import Stage

    seen = []

    async def capture(stage, cfg, emit):
        seen.append(stage.prompt)
        from q3tui.llm.runtime import StageResult

        return StageResult("ok", None, 0.0, 1, "sess-x")

    import q3tui.llm.runtime as rt

    orig = rt.run_stage
    rt.run_stage = capture
    try:
        for _ in range(2):
            anyio.run(lambda: team.run(Stage(name="verify_triage", system_prompt="x", prompt=long, cwd=tmp_path), "flow", eng.project.cfg, lambda *a, **k: None))
    finally:
        rt.run_stage = orig
    assert all(long in p and "[unchanged" not in p for p in seen) and len(seen) == 2

    eng.reset("spec")  # everything: the session starts over too
    state = json.loads((tmp_path / ".q3tui" / "team" / "roles.json").read_text())["flow"]
    assert state["session"] is None and state["tasks"] == 0 and state["sent"] == []
    assert not roles.notes_path(eng.project, "flow").is_file()
