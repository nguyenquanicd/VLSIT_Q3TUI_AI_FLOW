"""Milestone M5: the agent's implementation of the smoke-path regions: splicing, ownership, validation, update-not-rerun, compile fix rounds."""

import json
import shutil

import pytest

from q3tui.flows.vlsit.tb.uvm import lint as L
from q3tui.flows.vlsit.tb.uvm import scaffold as S
from q3tui.flows.vlsit.tb.uvm import semantic as sem
from q3tui.flows.vlsit.tb.uvm import worklist as W
from q3tui.flows.vlsit.tb.uvm.semantic import RegionImpl, SemanticResult
from tests.test_dut_dv_intent import EXAMPLE, engine, intent_from_contract, llm, load_contract, run_tb  # noqa: F401  (fixtures)
from tests.test_dut_dv_scaffold import OPTS, baseline, gen


def test_smoke_path_regions_and_units():
    c, _ = baseline()
    ids = sem.smoke_region_ids(c)
    assert ids[:3] == ["sram.monitor", "sram.responder", "seq.smoke"] and {"SBR-001", "SBR-001.sram", "SBR-002", "SBR-003", "SBR-008", "SBR-008.sram"} <= set(ids) and len(ids) == 10
    assert "SBR-004" not in ids and "test.TC-002" not in ids  # other tests and rules stay stubs
    u = sem.units(c)
    assert [x["name"] for x in u] == ["sram", "checker"] and u[0]["regions"] == ["sram.monitor", "sram.responder"]


def test_scoreboard_runs_only_the_rules_a_test_enabled():
    c, _ = baseline()
    f = gen()
    sb = f["scoreboard/dut_scoreboard.svh"]
    assert 'if (enabled.exists("SBR-001")) check_sbr_001(t);' in sb and 'if (enabled.exists("SBR-008")) sram_sbr_008(s);' in sb
    assert 'rules.push_back("SBR-001");' in f["tests/dut_test_lib.svh"] and "env.scb.enable(rules[i])" in f["tests/dut_test_lib.svh"]
    assert "uvm_top.set_timeout" in f["tests/dut_test_lib.svh"] and "bit has_coverage   = 0;" in f["env/dut_env.svh"]
    assert "clocking drv_cb" in f["interfaces/dut_pins_if.sv"] and "output i_sram_rdata" in f["interfaces/dut_pins_if.sv"]


def test_apply_splices_bodies_marks_the_agent_as_owner_and_checks_them(tmp_path):
    c, _ = baseline()
    S.write(tmp_path, gen(), {})
    items = W.scan(tmp_path, c)
    ok = RegionImpl(id="SBR-001", body="checks++;\nif (t.is_write) exp_mem[t.addr] = 8'h5a;")
    bad = [RegionImpl(id="SBR-002", body="`DV_TODO(SBR-002, x)"), RegionImpl(id="SBR-003", body="#10;"), RegionImpl(id="sram.monitor", body="x = u_dut.q;"),
           RegionImpl(id="seq.smoke", body="$finish;"), RegionImpl(id="SBR-004", body="checks++;")]
    problems, written = sem.apply(tmp_path, items, [ok, *bad], ["SBR-001", "SBR-002", "SBR-003", "sram.monitor", "seq.smoke", "sram.responder"])
    assert written == ["SBR-001"]
    t = (tmp_path / "scoreboard" / "dut_scoreboard.svh").read_text()
    assert "// DV_REGION(SBR-001) BEGIN agent" in t and "exp_mem[t.addr] = 8'h5a;" in t and "DV_TODO(SBR-001," not in t
    joined = "\n".join(problems)
    for needle in ("SBR-002: DV_TODO", "SBR-003: delay controls", "sram.monitor: hierarchical", "seq.smoke: $finish", "SBR-004: not a region", "sram.responder: no implementation"):
        assert needle in joined
    assert [i for i in W.scan(tmp_path, c) if i["id"] == "SBR-001"][0]["owner"] == "agent"
    assert not [x for x in L.lint(tmp_path, c, list(gen())) if x["severity"] == "error"]
    # an agent-owned region survives regeneration; a user-owned region is never touched
    t2 = (tmp_path / "scoreboard" / "dut_scoreboard.svh")
    t2.write_text(t2.read_text().replace("// DV_REGION(SBR-004) BEGIN scaffold", "// DV_REGION(SBR-004) BEGIN user", 1))
    items = W.scan(tmp_path, c)
    user_id = [i for i in items if i["owner"] == "user"][0]["id"]
    p2, w2 = sem.apply(tmp_path, items, [RegionImpl(id=user_id, body="checks++;")], [user_id])
    assert w2 == [] and "owned by the user" in p2[0]


def prepare(engine, llm):
    root = engine.project.root
    for n in ("golden_manifest", "verification_contract", "tb_architecture"):
        shutil.copy(EXAMPLE / "schemas" / f"{n}.json", root / "schemas")
    cfg = (root / "q3tui.yaml").read_text().replace("sim: {cmd: 'true', parse: vcs}", "sim: {cmd: 'true', parse: vcs}\n    sim_uvm: {cmd: 'true', parse: vcs}")
    (root / "q3tui.yaml").write_text(cfg)
    eng = type(engine)(type(engine.project).open(root))
    llm.dv_intents = [intent_from_contract(load_contract(root))]
    return root, eng


def test_step_runs_the_agent_per_unit_and_does_not_redo_unchanged_units(engine, llm):
    root, eng = prepare(engine, llm)
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(eng)
    sem_calls = [c for c in llm.calls if c.name.startswith("tb_semantic_")]
    assert [c.name for c in sem_calls] == ["tb_semantic_sram", "tb_semantic_checker"]
    st = sem_calls[0]
    # independence: the agent reads the spec and the generated testbench, never the RTL / assertions / schemas
    assert root / "src" / "rtl" in st.deny_dirs and root / "src" / "sva" in st.deny_dirs and root / "schemas" in st.deny_dirs
    assert st.builtin_tools == ["Read", "Grep", "Glob"] and "RTL_SENTINEL_TOKEN" not in st.prompt and "IMPLEMENT THIS REGION: sram.responder" in st.prompt
    assert "dut_pins_if" in st.prompt and "approved; the specification is the only source" in st.prompt
    wl = json.loads((root / "schemas" / "dv_worklist.json").read_text())
    owners = {i["id"]: i["owner"] for i in wl["items"]}
    assert owners["sram.responder"] == owners["SBR-001"] == owners["seq.smoke"] == "agent" and owners["SBR-004"] == "scaffold" and owners["test.TC-002"] == "scaffold"
    assert wl["summary"]["open"] == wl["summary"]["regions"] - 10
    ev = json.loads((root / "schemas" / "dv_evidence.json").read_text())["entries"]
    assert ev and all(e["kind"] == "compile" and e["status"] == "PASS" for e in ev)
    n = len(llm.calls)
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(eng)
    assert len(llm.calls) == n  # nothing changed: no new LLM call, the agent-owned regions stay


def test_forbidden_code_is_sent_back_with_the_problem(engine, llm):
    root, eng = prepare(engine, llm)
    ids = ["sram.monitor", "sram.responder"]
    llm.dv_semantic = [SemanticResult(regions=[RegionImpl(id=i, body="#5 x = 1;") for i in ids])]
    with pytest.raises(Exception, match="NOT_RUN|nothing was simulated"):
        run_tb(eng)
    calls = [c for c in llm.calls if c.name == "tb_semantic_sram"]
    assert len(calls) == 2 and "delay controls are not allowed" in calls[1].prompt
