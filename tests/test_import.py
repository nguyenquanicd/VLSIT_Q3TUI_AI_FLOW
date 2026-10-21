"""Importing your own project (core/importer.py, Engine.import_path, Ops.import_files): sorted by code, RTL adopted and
brought in line with the RTL rules, tests / SVA as references the steps port. Fake LLM and fake tools, as in
test_rtl_gen.py."""

from types import SimpleNamespace

import pytest

from q3tui.core import importer
from q3tui.core.ops import Ops
from q3tui.flows.spec.parse.step import fit_to_design
from q3tui.flows.rtl.rtl.plan import PlanModule, RtlPlan
from q3tui.flows.rtl.rtl.gen import RtlGenStep
from q3tui.pipeline.engine import EngineError
from q3tui.flows.rtl.lib import rules_check
from tests.test_rtl_gen import MODULES, _rtl_text, calls, llm, make_engine, run_step, tools_dir  # noqa: F401 (fixtures)

TB_ENV = "class my_env extends uvm_env;\nendclass\n"
TEST = "// write then read back\nclass basic_test extends uvm_test;\nendclass\n"


def _rtl(name: str, tags: bool = True, bad: bool = False) -> str:
    text = _rtl_text(SimpleNamespace(vlsit_modules=MODULES), name, "rule" if bad else None)
    return text if tags else text.replace("REQ-", "X-")


def reference(root, rtl_ok=True):
    """A reference project as people keep them: docs, rtl/, a simulation env with tests, lint scripts."""
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# demo\nThe design.\n")
    (root / "docs" / "arch.html").write_text("<h1>arch</h1>")
    (root / "rtl").mkdir()
    for name in MODULES:
        (root / "rtl" / f"{name}.sv").write_text(_rtl(name, tags=name != "leaf_b", bad=name == "leaf_a" and not rtl_ok))
    (root / "rtl" / "filelist.f").write_text("rtl/top.sv\n")
    (root / "sim" / "vcs" / "env").mkdir(parents=True)
    (root / "sim" / "vcs" / "tests").mkdir()
    (root / "sim" / "vcs" / "env" / "my_env.sv").write_text(TB_ENV)
    (root / "sim" / "vcs" / "tests" / "ts.basic_test.sv").write_text(TEST)
    (root / "sim" / "vcs" / "README.md").write_text("how to run the tests\n")
    (root / "sim" / "vcs" / "Makefile").write_text("all:\n")
    (root / "lint").mkdir()
    (root / "lint" / "run.sh").write_text("#!/bin/sh\n")
    (root / "misc").mkdir()
    (root / "misc" / "checks.sv").write_text("module top_chk; endmodule\nbind top top_chk u_chk();\n"
                                             "property p; 1; endproperty\nassert property (p);\n")
    return root


def test_scan_sorts_a_reference_project(tmp_path):
    root = reference(tmp_path / "ref")
    plan = importer.scan(root)
    rel = {k: sorted(plan.rel(f) for f in v) for k, v in plan.files.items()}
    assert rel["spec"] == ["README.md", "docs/arch.html"]  # (sim/vcs/README.md describes the tests: tb)
    assert rel["rtl"] == ["rtl/leaf_a.sv", "rtl/leaf_b.sv", "rtl/top.sv"]
    assert rel["tb"] == ["sim/vcs/README.md", "sim/vcs/env/my_env.sv", "sim/vcs/tests/ts.basic_test.sv"]
    assert rel["sva"] == ["misc/checks.sv"]  # no folder name to go by: its content (bind / assert property)
    assert sorted(plan.rel(f) for f in plan.skipped) == ["lint/run.sh", "rtl/filelist.f", "sim/vcs/Makefile"]


def test_design_facts_hierarchy_and_top(tmp_path):
    root = reference(tmp_path / "ref")
    facts = importer.design_facts(sorted((root / "rtl").glob("*.sv")))
    assert facts["top"] == "top"
    assert sorted(facts["modules"]["top"]["instances"]) == ["leaf_a", "leaf_b"]
    assert ["input", "i_clk_core"] in facts["modules"]["leaf_a"]["ports"]
    assert "`top`" in importer.facts_block(facts)


def test_flat_names_prefix_only_clashes(tmp_path):
    a, b, c = tmp_path / "x" / "README.md", tmp_path / "y" / "README.md", tmp_path / "y" / "spec.md"
    assert importer.flat_names([a, b, c], tmp_path) == {a: "x_README.md", b: "y_README.md", c: "spec.md"}


def test_fit_to_design_takes_the_imported_module_map():
    data = {"metadata": {"top_module": "chip"}, "requirements": [
        {"req_id": "REQ-001", "rtl_modules": ["leaf_a"]}, {"req_id": "REQ-002", "rtl_modules": ["made_up"]}],
        "modules": [{"name": "leaf_a", "description": "the a", "instances": []}, {"name": "made_up", "instances": []}]}
    design = {"top": "top", "modules": {"top": {"kind": "module", "instances": ["leaf_a"]}, "leaf_a": {"kind": "module", "instances": []}}}
    warnings = fit_to_design(data, design)
    assert data["metadata"]["top_module"] == "top" and [m["name"] for m in data["modules"]] == ["top", "leaf_a"]
    assert data["requirements"][1]["rtl_modules"] == ["top"] and "made_up" in warnings[0]
    assert data["modules"][1]["description"] == "the a" and data["modules"][0]["instances"] == ["leaf_a"]
    assert data["module_order"] == ["leaf_a", "top"]


def test_import_folder_into_a_vlsit_project(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    src = reference(tmp_path / "ref" / "demo", rtl_ok=False)
    text = Ops(eng).import_files("all", "ref/demo")
    assert "3 RTL" in text  # (this flow has no tb / sva step: those files are left out)
    assert (tmp_path / "spec" / "README.md").is_file() and (tmp_path / "spec" / "arch.html").is_file()
    assert sorted(p.name for p in (tmp_path / "src" / "rtl").glob("*.sv")) == ["leaf_a.sv", "leaf_b.sv", "top.sv"]
    assert eng.import_record("rtl")["design"]["top"] == "top"
    assert eng.import_sources() == [src]

    # rtl: the imported modules are checked, not rewritten; only what breaks the rules goes to a fix session
    run_step(eng, "rtl")
    assert not calls(llm, "rtl_top")  # follows the rules: kept as it is, no LLM
    fix_a, fix_b = calls(llm, "rtl_leaf_a"), calls(llm, "rtl_leaf_b")
    assert len(fix_a) == 1 and "initial block" in fix_a[0].prompt and "RTL của người dùng" in fix_a[0].prompt
    assert len(fix_b) == 1 and "REQ trace tags missing: REQ-002" in fix_b[0].prompt and "REQ-002: b is registered" in fix_b[0].prompt
    assert src in fix_a[0].deny_dirs  # the originals are never read by a stage
    assert eng.state.steps["rtl"].status == "done" and set(eng.state.step_meta["rtl"]["modules"]) == set(MODULES)

def test_import_kind_and_errors(tmp_path, tools_dir, llm):
    eng = make_engine(tmp_path, tools_dir)
    reference(tmp_path / "ref")
    ops = Ops(eng)
    with pytest.raises(EngineError):
        ops.import_files("bogus", "ref")
    with pytest.raises(EngineError):
        ops.import_files("all", "nowhere")
    text = ops.import_files("spec", "ref")
    assert "2 spec documents" in text and not (tmp_path / "src" / "rtl").exists()
    single = ops.import_files("spec", "ref/docs/arch.html")  # (this flow has no spec step: documents are the spec)
    assert "1 spec documents" in single and (tmp_path / "spec" / "arch.html").is_file()


def test_imported_top_keeps_its_port_names():
    top = "module top (input logic clk, output logic o_y);\n  assign o_y = clk;\nendmodule\n"
    prof = rules_check.profile_for(["rtl_rule.md"])
    run = SimpleNamespace(plan=RtlPlan(top="top", modules=[PlanModule(name="top")]), prof=prof, keep_ports={"clk"},
                          file=lambda n: None)
    step = RtlGenStep()
    pm = run.plan.modules[0]
    assert any("`clk`" in v.message for v in rules_check.check_source(top, "top", prof))
    assert not any("`clk`" in v.message for v in step._violations(run, pm, top))
    run.keep_ports = set()  # (not imported: every rule applies)
    assert any("`clk`" in v.message for v in step._violations(run, pm, top))


def test_imported_documents_are_ported_into_the_template_by_the_llm(tmp_path, monkeypatch):
    import anyio

    from q3tui.core.project import Project
    from q3tui.llm import runtime
    from q3tui.pipeline.engine import Engine
    from tests.fakes import FakeLLM, use_test_flow

    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    use_test_flow(tmp_path)
    ref = tmp_path / "ref" / "blk"
    (ref / "docs").mkdir(parents=True)
    (ref / "README.md").write_text("# blk\nINCR, WRAP and FIXED bursts.\n")
    (ref / "docs" / "arch.html").write_text("<h1>arch</h1><p>two banks</p>")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "README.md").write_text("# blk\nINCR, WRAP and FIXED bursts.\n")  # taken as the spec by an older import
    eng = Engine(Project.open(tmp_path))
    assert eng.status()[0].status == "user", [(v.name, v.status, v.detail) for v in eng.status()]
    text = Ops(eng).import_files("all", "ref/blk")
    assert "ports them into spec/spec.md" in text
    assert sorted(p.name for p in (tmp_path / "spec" / "ref").iterdir()) == ["README.md", "arch.html"]
    assert not (tmp_path / "spec" / "README.md").exists()  # (the same document, now a reference; backed up)
    assert eng.status()[0].status == "pending"  # the spec step runs: no intent needed, the documents are its input

    anyio.run(lambda: eng.run_step(eng.by_name["spec"]))
    write = next(c for c in fake.calls if c.name == "spec_write")
    assert "Port the user's own specification documents" in write.prompt
    assert "INCR, WRAP and FIXED bursts." in write.prompt  # text verbatim
    assert "spec/ref/docs_arch.html" in write.prompt or "spec/ref/arch.html" in write.prompt  # HTML: the LLM reads it itself
    assert "<h1>" not in write.prompt and "Read" in write.builtin_tools
    assert (tmp_path / "spec" / "spec.md").is_file() and eng.state.step_meta["spec"]["refs"]
    assert eng.project.spec_documents() == [tmp_path / "spec" / "spec.md"]  # downstream reads the ported spec only

    # downstream stages never read the references; the spec step's own do
    from q3tui.llm.runtime import Stage
    from q3tui.pipeline.base import StepContext

    for step, denied in (("rtl", True), ("spec", False)):
        st = Stage(name="x", system_prompt="", prompt="", cwd=tmp_path)
        anyio.run(lambda: StepContext(eng, eng.bus.scoped(step)).llm(st))
        assert ((tmp_path / "spec" / "ref") in st.deny_dirs) is denied

    # a re-import: the sections the changed documents affect are updated (a patch, not a rewrite)
    (ref / "README.md").write_text("# blk\nINCR and WRAP bursts only.\n")
    Ops(eng).import_files("spec", "ref/blk")
    anyio.run(lambda: eng.run_step(eng.by_name["spec"]))
    upd = [c for c in fake.calls if c.name == "spec_update"]
    assert upd and "INCR and WRAP bursts only." in upd[-1].prompt and "spec/ref/README.md" in upd[-1].prompt
    assert [c.name for c in fake.calls].count("spec_write") == 1
