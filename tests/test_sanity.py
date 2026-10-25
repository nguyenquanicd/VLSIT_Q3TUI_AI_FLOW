"""The Sanity flow: the VC Static report reader, the script, the waivers, the violation table, the steps with a fake tool and a fake LLM
(analysis, review, questions, check), the hand-off to the owner flow and the shared SDC."""

import json
import sys
from pathlib import Path

import pytest

from q3tui import flows
from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.eda import tools as T
from q3tui.flows.sanity.lib import script, table, vcstatic, waivers
from q3tui.flows.sanity.lib.waivers import Decision, Review
from q3tui.pipeline.engine import Engine, EngineError

DATA = Path(__file__).parent / "data" / "sanity"
RTL = """module blk (input i_clk_a, input i_clk_b, input i_rst_n, input i_d, output reg o_q);
  reg r_a;
  always @(posedge i_clk_a or negedge i_rst_n) if (!i_rst_n) r_a <= 0; else r_a <= i_d;
  always @(posedge i_clk_b or negedge i_rst_n) if (!i_rst_n) o_q <= 0; else o_q <= r_a;
endmodule
"""
USER_SDC = "create_clock -name A -period 10 [get_ports i_clk_a]\ncreate_clock -name B -period 8 [get_ports i_clk_b]\nset_clock_groups -asynchronous -group A -group B\n"
CDC = [  # (tag, severity, source object, destination object): what the fake tool reports
    ("CDC_UNSYNC_CTRL", "error", "u_a/reg_req/Q", "u_b/reg_sync_0/Q"),
    ("CDC_UNSYNC_NOSCHEME", "error", "u_a/reg_data/Q[7:0]", "u_b/reg_cap/D[7:0]"),
    ("SETUP_PORT_UNCONSTRAINED", "error", "", ""),
]
RDC = [("RDC_ON_CDC_DISABLED", "error", "", "")]
FAKE_VCST = r'''
import json, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
role = "rdc" if "check_rdc" in text else "cdc"
viol = json.loads(Path(%r, role + ".json").read_text())
waived = set()
for m in re.finditer(r"^source (\S+)$", text, re.M):  # the review's waivers, sourced after the check
    for cmd in Path(m.group(1)).read_text().splitlines():
        sel = re.search(r"-from \{(.*?)\} -to \{(.*?)\}", cmd) or re.search(r"-msg \{(.*?)\}", cmd)
        if sel and not %r:
            waived.add(tuple(g.replace("\\", "") for g in sel.groups()))
out = re.search(r"-file (\S+_violations\.rpt)", text).group(1)
lines = []
for n, (tag, sev, src, dst) in enumerate(viol, 1):
    if (src, dst) in waived or (tag + ": " + tag,) in waived or (tag,) in waived:
        continue
    if role == "rdc":
        lines += ["  %%s  (1 %%s/0 waived)" %% (tag, sev), "  ----", "  1.  %%s" %% tag, ""]
    else:
        lines += ["  %%s  (1 %%s/0 waived)" %% (tag, sev), "  ----", "  Tag : %%s" %% tag, "  Description : crossing from [SrcObject] to [DestObject]",
                  "  Violation : CDC:%%d" %% n] + (["  SrcObject : " + src, "  DestObject : " + dst] if src else []) + [""]
Path(out).write_text("\n".join(lines) + "\n")
print("VC Static ran", role)
'''


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "blk.sv").write_text(RTL)
    (tmp_path / "src" / "rtl" / "filelist.f").write_text("src/rtl/blk.sv\n")
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "structured_spec.json").write_text(json.dumps({"metadata": {"top_module": "blk"}}))
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "spec.md").write_text("The block has two asynchronous clocks.\n")
    (tmp_path / "constraints" / "sdc").mkdir(parents=True)
    (tmp_path / "constraints" / "sdc" / "sdc.tcl").write_text(USER_SDC)  # (a hand-written SDC: no sdc.json)
    _tools(tmp_path)
    return Project.open(tmp_path)


def _tools(root: Path, cdc=CDC, rdc=RDC, ignore_waivers: bool = False, extra: str = "") -> None:
    (root / "cdc.json").write_text(json.dumps(cdc))
    (root / "rdc.json").write_text(json.dumps(rdc))
    (root / "fake_vcst.py").write_text(FAKE_VCST % (str(root), ignore_waivers))
    cmd = f"{sys.executable} {root / 'fake_vcst.py'} {{script}}"
    (root / "q3tui.yaml").write_text(f"pipeline:\n  flow: sanity\n{extra}tools:\n  roles:\n    cdc: {{cmd: \"{cmd}\", parse: generic}}\n    rdc: {{cmd: \"{cmd}\", parse: generic}}\n")


class ReviewLLM:
    """The fake review: `decide(batch ids) -> {id: Decision kwargs}`; records the stages."""

    def __init__(self, decide):
        self.decide, self.stages = decide, []

    async def __call__(self, stage, cfg, emit):
        from q3tui.llm.runtime import StageResult

        self.stages.append(stage)
        ids = [line.split("**")[1] for line in stage.prompt.splitlines() if line.startswith("- **")]
        emit.add_cost(0.01)
        return StageResult("ok", Review(decisions=[Decision(id=i, **self.decide(i)) for i in ids if self.decide(i)]), 0.01, 2, "s")


def _decide(i):
    return {"V1": dict(action="waive", rule="K1", reason="valid/data: src/rtl/blk.sv:3 the data is captured only with the synchronized valid"),
            "V2": dict(action="fix_rtl", module="blk", change="add a two-flop synchronizer on r_a", reason="blk.sv:4 r_a feeds o_q directly"),
            "V3": dict(action="sdc", reason="the port is not constrained", sdc_request="set_input_delay 2.0 -clock A [get_ports i_d]",
                       question="Add the input delay of i_d to the SDC?")}.get(i)


# -- the flow -----------------------------------------------------------------------------------------------------------


def test_flow_loads():
    flow = flows.load_flow("sanity", Path.cwd())
    assert flows.validate(flow) == []
    assert [s.id for s in flow.steps] == ["sdc", "cdc", "cdc_waivers", "cdc_check", "rdc", "rdc_waivers", "rdc_check"]
    assert flow.gates == {"cdc_waivers": "human", "rdc_waivers": "human"}
    assert {"sanity_handoff", "sdc_set", "sdc_show"} <= set(flow.actions)
    assert {"cdc", "rdc"} <= set(T.roles_needed(flow))
    assert "sanity" in flows.list_flows()


def test_the_cdc_knowledge_is_the_flows_own_file():
    text = (flows.BUILTIN_DIR / "sanity" / "cdc_waivers" / "md" / "knowledge.md").read_text()
    assert text.startswith("# CDC knowledge") and "## K1" in text and "## K5" in text


# -- the report reader, the script, the waivers, the table --------------------------------------------------------------------


def test_the_real_reports_are_read():
    found, infos = vcstatic.parse_report((DATA / "vcst_cdc.rpt").read_text())
    assert [(v["tag"], v["severity"]) for v in found] == [("SETUP_PORT_PARTIALLY_CONSTRAINED", "error")] and infos == 25
    assert found[0]["message"] == "Port i_rst_n is partially constrained"
    rdc, infos = vcstatic.parse_report((DATA / "vcst_rdc_list.rpt").read_text())
    assert [v["tag"] for v in rdc] == ["RDC_ON_CDC_DISABLED"] and infos == 1  # (the list format: numbered lines)
    text = (DATA / "vcst_rdc_list_compressed.rpt").read_text()  # (a header with compressed items: `(5 errors/0 waived/6 compressed)`)
    rdc, infos = vcstatic.parse_report(text)
    assert [v["tag"] for v in rdc] == ["RDC_CORRUPT_OBSERVED"] * 5 and vcstatic.declared(text) == 5


def test_the_script_reads_the_shared_sdc(tmp_path):
    t = script.text("cdc", "blk", tmp_path, tmp_path / "f.f", tmp_path / "w", tmp_path / "constraints" / "sdc" / "sdc.tcl",
                    {"clocks": [{"name": "A"}, {"name": "B"}], "resets": ["i_rst_n", "i_rst"]}, "set_app_var x 1")
    assert t.index("elaborate blk") < t.index(f"read_sdc {tmp_path / 'constraints' / 'sdc' / 'sdc.tcl'}") < t.index("create_reset -sense low i_rst_n")
    assert "create_reset -sense high i_rst\n" in t and "set_clock_groups -asynchronous -group {A} -group {B}" in t
    assert t.index("set_app_var x 1") < t.index("check_cdc") < t.index("report_violations -app CDC -verbose")
    assert "source" not in t.split("check_cdc")[1]  # (no waivers in the analysis run)
    w = script.text("rdc", "blk", tmp_path, tmp_path / "f.f", tmp_path / "w", tmp_path / "s.tcl", {"clock_groups": [{}]}, waive=tmp_path / "rdc_waive.tcl")
    assert "configure_rdc_corrupt" in w and w.index("check_rdc") < w.index(f"source {tmp_path / 'rdc_waive.tcl'}") < w.index("report_rdc -list") and "set_clock_groups" not in w


def test_a_waiver_names_the_violation_by_what_it_is():
    v = {"id": "V2", "tag": "CDC_UNSYNC_CTRL", "message": "m", "fields": {"SrcObject": "u/r_val/Q[7:0]", "DestObject": "u/r_sync/Q[7:0]"}}
    d = {"id": "V2", "action": "waive", "rule": "K3", "reason": "Gray code {one bit}"}
    cmd = waivers.waive_command(v, d, "cdc")
    assert cmd.startswith("waive_cdc -add W_V2 -tag CDC_UNSYNC_CTRL -from {u/r_val/Q\\[7:0\\]} -to {u/r_sync/Q\\[7:0\\]} -comment {K3: Gray code (one bit)}")
    assert waivers.waive_command({"id": "V3", "tag": "X", "message": "m", "fields": {}}, d, "cdc") is None  # no objects in the report
    rdc = waivers.waive_command({"id": "V1", "tag": "RDC_X", "message": "reset {a} [b]", "fields": {}}, d, "rdc")
    assert rdc.startswith("waive_rdc -add W_V2 -tag RDC_X -msg {reset *a* \\[b\\]}")
    tcl, skipped = waivers.waive_tcl([v], [d, {"id": "V9", "action": "fix_rtl"}], "abc", "cdc")
    assert tcl.count("waive_cdc") == 1 and skipped == []  # (one waiver line: the fix_rtl item has none)
    assert waivers.sig_id("cdc", v) == waivers.sig_id("cdc", dict(v, id="V77")) and waivers.sig_id("cdc", v).startswith("CDC-")


def test_the_table_has_the_columns_a_review_needs():
    report = {"top": "blk", "violations": [{"id": "V1", "tag": "CDC_UNSYNC_CTRL", "severity": "error", "message": "a | b\nc"},
                                           {"id": "V2", "tag": "SETUP_X", "severity": "warning", "message": "w"}]}
    plan = {"items": [{"id": "V1", "action": "waive", "rule": "K2", "reason": "static CSR"},
                      {"id": "V2", "action": "fix_rtl", "module": "blk", "change": "add a synchronizer", "handed_off": {"flow": "rtl", "step": "rtl"}}]}
    assert table.COLUMNS == ("No.", "Name", "Severity", "Description", "Waived", "Comment")
    rows = table.rows(report, plan)
    assert rows[0] == ["V1", "CDC_UNSYNC_CTRL", "Error", "a | b\nc", "Yes", "K2: static CSR"]
    assert rows[1][2:5] == ["Warning", "w", "No"] and "handed off to rtl:rtl" in rows[1][5]
    md = table.markdown("cdc", report, plan)
    assert "| No. | Name | Severity | Description | Waived | Comment |" in md and "a \\| b c" in md and "1 violation(s)" not in md and "2 violation(s), 1 waived, 1 open" in md
    assert "(not reviewed yet)" in table.markdown("cdc", report, None)


# -- the steps ----------------------------------------------------------------------------------------------------------------


async def test_a_run_without_waivers_writes_the_report_and_the_table(proj):
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc")
    rep = json.loads((proj.schemas_dir / "cdc_report.json").read_text())
    assert rep["status"] == "violations" and [v["id"] for v in rep["violations"]] == ["V1", "V2", "V3"] and rep["top"] == "blk"
    assert rep["violations"][0]["fields"]["SrcObject"] == "u_a/reg_req/Q"
    setup = (proj.root / "build" / "sanity" / "cdc_setup.tcl").read_text()
    assert "read_sdc " + str(proj.root / "constraints" / "sdc" / "sdc.tcl") in setup and "create_reset -sense low i_rst_n" in setup  # (the reset is read from the RTL)
    md = (proj.root / "reports" / "cdc_report.md").read_text()
    assert "| V1 | CDC_UNSYNC_CTRL | Error |" in md and md.count("| No |") == 3 and "(not reviewed yet)" in md
    assert (proj.root / "reports" / "cdc_report.csv").read_text().splitlines()[0] == "No.,Name,Severity,Description,Waived,Comment"
    assert (proj.root / "constraints" / "sdc" / "sdc.tcl").read_text() == USER_SDC and not (proj.root / "constraints" / "sdc" / "sdc.json").exists()  # the SDC is yours: untouched


async def test_a_missing_tool_is_a_failed_step_not_a_clean_one(proj):
    (proj.root / "q3tui.yaml").write_text("pipeline:\n  flow: sanity\ntools:\n  roles:\n    cdc: {cmd: '/nonexistent/vc_static_shell {script}', parse: generic}\n")
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc")
    st = {v.name: v for v in eng.status()}["cdc"]
    assert st.status == "failed" and "not run" in st.detail and not (proj.schemas_dir / "cdc_report.json").exists()


async def test_the_review_decides_every_violation_and_the_table_follows(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = ReviewLLM(_decide)
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    (stage,) = fake.stages
    assert set(stage.builtin_tools) == {"Read", "Grep", "Glob"} and stage.write_dirs is None and stage.output_model.__name__ == "Review"
    assert "K1 — Valid / data crossing" in stage.prompt and "u_a/reg_req/Q" in stage.prompt and "RTL modules" in stage.prompt  # (the knowledge, the facts)
    assert any(d.name == "tb" for d in stage.deny_dirs) and any(d.name == "sva" for d in stage.deny_dirs)  # independence
    assert proj.model_dir in stage.deny_dirs and proj.state_dir in stage.deny_dirs  # (the old outputs are never read)
    assert "sdc/sdc.tcl" in stage.prompt and "You never change the SDC yourself" in stage.system_prompt
    plan = json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())
    assert [(i["id"], i["action"]) for i in plan["items"]] == [("V1", "waive"), ("V2", "fix_rtl"), ("V3", "sdc")]
    rows = table.rows(json.loads((proj.schemas_dir / "cdc_report.json").read_text()), plan)
    assert [r[4] for r in rows] == ["Yes", "No", "No"] and rows[0][5].startswith("K1:") and "SDC update needed" in rows[2][5] and "set_input_delay" in rows[2][5]
    assert "2 open" in (proj.root / "reports" / "cdc_report.md").read_text()


async def test_the_sdc_request_is_a_question_for_the_user(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = ReviewLLM(_decide)
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(stop="cdc_waivers")  # (no --yes: the gate waits for you, the question stays open)
    qs = [q for q in eng.questions() if q["step"] == "cdc_waivers"]
    assert len(qs) == 1 and qs[0]["id"].startswith("CDC-") and qs[0]["status"] == "open" and "input delay" in qs[0]["question"]
    assert [o["label"] for o in qs[0]["options"]][0] == "I will update the SDC"
    step = eng.by_name["cdc_waivers"]
    assert step.open_questions(eng) == 1 and step.pending_reviews(eng) == ["V3"]
    with pytest.raises(EngineError, match="not reviewed"):
        eng.approve("cdc_waivers")  # an open question holds the gate
    Ops(eng).answer(qs[0]["id"], "No SDC change: decide again")
    assert json.loads((proj.root / "reviews" / "cdc_answers.json").read_text()) == {qs[0]["id"]: "No SDC change: decide again"}
    assert {v.name: v.status for v in Engine(proj).status()}["cdc_waivers"] == "stale"  # the answer is an input
    assert eng.by_name["cdc_waivers"].open_questions(eng) == 0


async def test_only_what_is_new_or_waiting_goes_to_the_llm_again(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = ReviewLLM(_decide)
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    qid = next(q["id"] for q in eng.questions() if q["step"] == "cdc_waivers")
    Ops(eng).answer(qid, "No SDC change: decide again")
    eng2 = Engine(Project.open(proj.root))
    await eng2.run(yes=True, stop="cdc_waivers")
    assert len(fake.stages) == 2
    again = fake.stages[1].prompt
    assert "**V3**" in again and "**V1**" not in again and "**V2**" not in again  # the settled decisions stay
    assert "The user answered: No SDC change: decide again" in again


async def test_a_waiver_the_tool_cannot_express_is_asked_not_assumed(proj, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(lambda i: dict(action="waive", reason="safe: blk.sv:3")))
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    plan = json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())
    by = {i["id"]: i for i in plan["items"]}
    assert by["V1"]["action"] == "waive" and by["V2"]["action"] == "waive"
    assert by["V3"]["action"] == "ask" and "no waive command can be written" in by["V3"]["reason"]  # (no objects in the report)


async def test_the_check_runs_the_tool_again_with_the_waivers(proj, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    eng = Engine(proj)
    await eng.run(yes=True)
    st = {v.name: v for v in eng.status()}
    assert st["cdc_check"].status == "done" and st["rdc_check"].status == "done"
    chk = json.loads((proj.schemas_dir / "cdc_check.json").read_text())
    assert chk["total"] == 3 and chk["waived"] == 1 and chk["open"] == 2 and chk["expected_open"] == 2 and chk["ok"] is True
    assert "waive_cdc -add W_V1 -tag CDC_UNSYNC_CTRL -from {u_a/reg_req/Q} -to {u_b/reg_sync_0/Q}" in (proj.root / "build" / "sanity" / "cdc_waive.tcl").read_text()
    assert "source " + str(proj.root / "build" / "sanity" / "cdc_waive.tcl") in (proj.root / ".q3tui" / "sanity" / "cdc_check" / "cdc_waive_setup.tcl").read_text()
    assert (proj.root / "reports" / "cdc_report.md").read_text().count("| Yes |") == 1  # (the analysis report itself stays the run without waivers)


async def test_the_check_runs_the_script_of_the_analysis_with_its_extra_tcl(proj, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    proj.save_config_values({"pipeline": {"step_options": {"cdc": {"extra_tcl": "set_input_delay -clock A 1 [get_ports i_rst_n]"}}}})
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    line = "set_input_delay -clock A 1"  # (set on the analysis step only: the check must run the same script)
    assert line in (proj.root / "build" / "sanity" / "cdc_setup.tcl").read_text()
    assert line in (proj.root / ".q3tui" / "sanity" / "cdc_check" / "cdc_waive_setup.tcl").read_text()


async def test_a_tool_that_ignores_the_waivers_fails_the_check(proj, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    _tools(proj.root, ignore_waivers=True)
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    st = {v.name: v for v in eng.status()}
    assert st["cdc_check"].status == "failed" and "waived but still reported: V1" in st["cdc_check"].detail and "count 3 ≠ 2" in st["cdc_check"].detail


async def test_a_clean_analysis_needs_no_llm(proj, monkeypatch):
    from q3tui.llm import runtime

    fake = ReviewLLM(_decide)
    monkeypatch.setattr(runtime, "run_stage", fake)
    _tools(proj.root, cdc=[], rdc=[])
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    assert fake.stages == [] and {v.name: v.status for v in eng.status()} == {s: "done" for s in ("sdc", "cdc", "cdc_waivers", "cdc_check", "rdc", "rdc_waivers", "rdc_check")}
    assert json.loads((proj.schemas_dir / "cdc_check.json").read_text())["ok"] is True


# -- the SDC is shared ----------------------------------------------------------------------------------------------------------


async def test_an_sdc_json_is_rendered_like_the_other_flows(proj):
    (proj.root / "constraints" / "sdc" / "sdc.tcl").unlink()
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc")
    assert (proj.root / "constraints" / "sdc" / "sdc.json").is_file() and (proj.root / "constraints" / "sdc" / "sdc.tcl").is_file()  # (the FPGA flow's SDC step)
    Ops(eng).run_action("sdc_set", key="clock.i_clk_a.freq_mhz", value="200")
    st = {v.name: v.status for v in Engine(proj).status()}
    assert st["cdc"] == "stale"  # a changed constraint makes the analysis run again


async def test_the_handoff_adds_an_sdc_request_to_extra_tcl_when_there_is_an_sdc_json(proj, monkeypatch):
    from q3tui.llm import runtime

    (proj.root / "constraints" / "sdc" / "sdc.tcl").unlink()  # (no hand-written SDC: the flow renders constraints/sdc from sdc.json)
    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    ops = Ops(eng)
    pending = [i["id"] for i in eng.by_name["cdc_waivers"].pending_handoff(eng)]
    assert pending == ["V2", "V3"]  # (the RTL fix and the SDC request: a plain SDC command can be applied here)
    msg = ops.run_action("sanity_handoff", ids="cdc:V3")
    assert "cdc:V3 → sdc: set_input_delay 2.0 -clock A [get_ports i_d]" in msg
    extra = (proj.root / "constraints" / "sdc" / "extra.tcl").read_text()
    assert "set_input_delay 2.0 -clock A [get_ports i_d]" in extra and "source $_d/extra.tcl" in (proj.root / "constraints" / "sdc" / "sdc.tcl").read_text()
    assert json.loads((proj.root / "constraints" / "sdc" / "sdc.json").read_text())["extra_tcl"] == ["set_input_delay 2.0 -clock A [get_ports i_d]"]
    plan = json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())
    assert plan["items"][2]["handed_off"]["step"] == "sdc" and plan["items"][1].get("handed_off") is None
    assert "SDC updated (constraints/sdc/extra.tcl)" in (proj.root / "reports" / "cdc_report.md").read_text()
    assert eng.by_name["cdc_waivers"].question_list(eng) == []  # (nothing is waiting for you any more for V3)
    assert {v.name: v.status for v in Engine(proj).status()}["cdc"] == "stale"  # the analysis runs again on the changed SDC


async def test_a_request_that_is_not_an_sdc_command_stays_yours(proj, monkeypatch):
    from q3tui.llm import runtime

    (proj.root / "constraints" / "sdc" / "sdc.tcl").unlink()
    def decide(i):
        return dict(action="sdc", reason="x", sdc_request="exec rm -rf /", question="ok?") if i == "V3" else _decide(i)

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(decide))
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    assert [i["id"] for i in eng.by_name["cdc_waivers"].pending_handoff(eng)] == ["V2"]
    with pytest.raises(EngineError, match="no fix to hand off"):
        Ops(eng).run_action("sanity_handoff", ids="cdc:V3")
    assert not (proj.root / "constraints" / "sdc" / "extra.tcl").is_file() or "exec" not in (proj.root / "constraints" / "sdc" / "extra.tcl").read_text()


# -- the hand-off ---------------------------------------------------------------------------------------------------------------


async def test_handoff_sends_the_fixes_to_the_owning_flow_but_not_the_sdc_request(proj, monkeypatch):
    from q3tui.llm import runtime

    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text("name: owner\nsteps:\n  - {id: spec, kind: reads}\n  - {id: rtl, kind: reads, deps: [spec]}\n")
    _tools(proj.root, extra="  step_options:\n    cdc_waivers:\n      handoff_flow: owner\n    rdc_waivers:\n      handoff_flow: owner\n")
    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc_waivers")
    ops = Ops(eng)
    msg = ops.run_action("sanity_handoff", ids="cdc:V2")
    assert "cdc:V2 → owner:rtl" in msg and "q3tui --flow owner run" in msg
    other = Engine(proj, flow=flows.load_flow("owner", proj.root))
    (req,) = other.state.feedback["rtl"]
    assert req.startswith("[rtl:blk] CDC CDC_UNSYNC_NOSCHEME:") and "add a two-flop synchronizer on r_a" in req and "Evidence:" in req
    with pytest.raises(EngineError, match="no fix to hand off"):
        ops.run_action("sanity_handoff")  # (the SDC request is not a fix for another flow; V2 is sent)
    plan = json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())
    assert plan["items"][1]["handed_off"]["flow"] == "owner" and plan["items"][2].get("handed_off") is None
    assert "handed off to owner:rtl" in (proj.root / "reports" / "cdc_report.md").read_text()
    assert {v.name: v for v in Engine(Project.open(proj.root)).status()}["cdc_waivers"].edited == []  # recording the hand-off is not a user edit


async def test_a_gate_offers_the_handoff(proj, monkeypatch):
    from q3tui.llm import runtime

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    offer = eng.by_name["cdc_waivers"].follow_up(eng)
    assert offer["action"] == "sanity_handoff" and offer["switch"] == "rtl" and "V2" in offer["detail"] and "V3" not in offer["detail"]


def test_the_tool_runs_in_its_work_folder_not_in_the_project(tmp_path):
    """VC Static writes elab_summary.rpt / .vcstPref into its working directory: the script moves to the work folder and reads an
    absolute filelist, so nothing is left in the project folder."""
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "a.sv").write_text("module a; endmodule\n")
    fl = tmp_path / "src" / "rtl" / "filelist.f"
    fl.write_text("// files\n+incdir+src/rtl\nsrc/rtl/a.sv\n")
    work = tmp_path / ".q3tui" / "sanity" / "cdc"
    absf = script.absolute_filelist(fl, tmp_path, work)
    assert absf.read_text().splitlines() == ["// files", f"+incdir+{tmp_path / 'src' / 'rtl'}", str(tmp_path / "src" / "rtl" / "a.sv")]
    t = script.text("cdc", "a", tmp_path, absf, work, tmp_path / "s.tcl", {})
    assert t.splitlines()[1] == f"cd {work}" and f'-f {absf}' in t and f"cd {tmp_path}\n" not in t


# -- memory: what was tried is remembered, a repeat becomes a question --------------------------------------------------------------


def test_memory_blocks_the_same_request_and_the_third_attempt(tmp_path):
    from q3tui.flows.sanity.lib import memory

    project = Project.open(tmp_path)
    assert memory.repeated(project, "CDC-1", "fix_rtl", "add a synchronizer") == []
    memory.record(project, "CDC-1", "fix_rtl", "add a synchronizer", "blk")
    assert memory.repeated(project, "CDC-1", "fix_rtl", "Add a  synchronizer")  # (the same request, whatever the spacing and case)
    assert memory.repeated(project, "CDC-1", "fix_rtl", "use a handshake") == []  # a different one may still be tried
    memory.record(project, "CDC-1", "fix_rtl", "use a handshake", "blk")
    assert len(memory.repeated(project, "CDC-1", "fix_rtl", "use an async fifo")) == 2  # (MAX_TRIES reached)
    assert memory.repeated(project, "CDC-1", "sdc", "set_false_path") == [] and memory.repeated(project, "CDC-2", "spec", "x") == []
    assert memory.conflicts_with_requirement("REQ-RESET-004 forbids a synchronous clear", "make it asynchronous")
    assert not memory.conflicts_with_requirement("the data is not synchronized", "add a two-flop synchronizer") and \
        not memory.conflicts_with_requirement("this goes against the rules", "x")  # (no requirement cited)


async def test_a_handed_off_fix_is_remembered_and_the_repeat_becomes_a_question(proj, monkeypatch):
    from q3tui.flows.sanity.lib import memory
    from q3tui.llm import runtime

    (proj.root / "flows").mkdir(exist_ok=True)
    (proj.root / "flows" / "owner.yaml").write_text("name: owner\nsteps:\n  - {id: spec, kind: reads}\n  - {id: rtl, kind: reads, deps: [spec]}\n")
    _tools(proj.root, extra="  step_options:\n    cdc_waivers:\n      handoff_flow: owner\n")
    llm = ReviewLLM(_decide)
    monkeypatch.setattr(runtime, "run_stage", llm)
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc_waivers")
    sig = next(i["sig"] for i in eng.by_name["cdc_waivers"].items(eng) if i["id"] == "V2")
    Ops(eng).run_action("sanity_handoff", ids="cdc:V2")
    (att,) = memory.attempts(proj, sig)
    assert att["kind"] == "fix_rtl" and att["module"] == "blk" and "two-flop synchronizer on r_a" in att["text"]
    # the RTL changed, the violation is still there: the review is told, and its same proposal does not go out again
    (proj.root / "src" / "rtl" / "blk.sv").write_text((proj.root / "src" / "rtl" / "blk.sv").read_text() + "// round 2\n")
    llm.stages.clear()
    await Engine(Project.open(proj.root)).run(yes=True, stop="cdc_waivers")
    prompt = llm.stages[0].prompt
    assert "Already tried for this violation (1 time(s))" in prompt and "two-flop synchronizer on r_a" in prompt
    assert "Your earlier decision for it: waive (K1)" in prompt and "keep the decision if it still holds" in prompt
    plan = json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())
    v2 = next(i for i in plan["items"] if i["id"] == "V2")
    assert v2["action"] == "ask" and "still open after 1 hand-off" in v2["question"] and len(v2["options"]) == 3 and v2["handed_off"] is None
    assert "Question for you" in (proj.root / "reports" / "cdc_report.md").read_text()


async def test_a_fix_that_goes_against_a_requirement_becomes_a_spec_decision(proj, monkeypatch):
    from q3tui.llm import runtime

    def decide(i):
        if i == "V2":
            return dict(action="fix_rtl", module="blk", change="make the toggle reset asynchronous", reason="REQ-RESET-004 forbids it, but the RDC tool wants it")
        return _decide(i)

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(decide))
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    v2 = next(i for i in eng.by_name["cdc_waivers"].items(eng) if i["id"] == "V2")
    assert v2["action"] == "spec" and "go against a requirement" in v2["change"]


# -- the TUI panels -----------------------------------------------------------------------------------------------------


async def test_the_lane_panels_show_the_violations_the_review_and_the_check(proj, monkeypatch):
    import sys

    from textual.widgets import DataTable

    from q3tui.llm import runtime
    from q3tui.tui.app import Q3TUIApp

    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(_decide))
    eng = Engine(proj)
    await eng.run(yes=True, stop="cdc_waivers")
    app = Q3TUIApp(proj)
    sent = []
    monkeypatch.setattr(app, "action_switch_flow", lambda name=None: sent.append(name))
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        mod = sys.modules[type(next(iter(app.query("ReviewTab")))).__module__]  # (the flow's panels.py is loaded by path)
        for step in ("cdc", "cdc_waivers", "cdc_check", "rdc", "sdc"):
            assert type(app._panel(step)).__name__ == "VlsitPanel", step  # a panel of its own, not the Files / Questions / Log default
        vrows = mod._violation_rows("cdc")(app)
        assert [r[0] for r in vrows] == ["V1", "V2", "V3"] and [str(r[5]) for r in vrows] == ["Yes", "No", "No"]
        assert [str(r[6]) for r in vrows] == ["waive", "RTL fix", "SDC"]
        assert "**Review:** waive (rule K1)" in mod._violation_detail("cdc")(app, "V1")
        rrows = mod._review_rows("cdc")(app)
        assert [(r[0], str(r[3]), str(r[7])) for r in rrows] == [("V1", "waive", ""), ("V2", "RTL fix", "not yet"), ("V3", "SDC", "not yet")]
        assert "set_input_delay" in mod._review_detail("cdc")(app, "V3") and mod._review_urgent("cdc")(app) == 2

        tab = app._panel("cdc_waivers").query_one("ReviewTab")
        tab.refresh_view()
        table = tab.query_one(DataTable)
        assert table.row_count == 3
        table.move_cursor(row=1)  # V2: H hands this one only
        await pilot.pause()
        tab.action_handoff_one()
        await pilot.pause()
        plan = json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())
        assert {i["id"]: bool(i.get("handed_off")) for i in plan["items"]} == {"V1": False, "V2": True, "V3": False}
        assert sent == ["rtl"]  # (and the TUI moves to the owner flow, as the other flows do)
        assert str(mod._review_rows("cdc")(app)[1][7]) == "→ rtl:rtl"

    await eng.run(yes=True, only="cdc_check")
    app = Q3TUIApp(proj)
    async with app.run_test(size=(170, 50)):
        mod = sys.modules[type(next(iter(app.query("ReviewTab")))).__module__]
        by_item = {r[0]: r for r in mod._check_rows("cdc")(app)}
        assert str(by_item["result"][4]) in ("pass", "fail") and by_item["total"][2] == "3"
        assert mod._check_rows("rdc")(app) == []  # (a lane that has not run shows its empty message, not an error)
