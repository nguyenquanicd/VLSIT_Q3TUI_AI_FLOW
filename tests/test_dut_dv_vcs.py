"""Simulator tests of the `uvm_vip` backend: real VCS. They SKIP (with the reason) unless `Q3TUI_VCS_MODULE` names the environment
module that provides VCS, e.g. `Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 uv run pytest -m vcs`. Never faked."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from q3tui.flows.dv.uvm import evidence as ev
from q3tui.flows.dv.uvm import scaffold as S
from tests.test_dut_dv_scaffold import baseline, gen

MODULE = os.environ.get("Q3TUI_VCS_MODULE")
pytestmark = [pytest.mark.vcs, pytest.mark.skipif(not MODULE, reason="Q3TUI_VCS_MODULE is not set: no VCS available for the simulator tests")]


def sh(cwd: Path, cmd: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-lc", f"source /etc/profile.d/modules.sh; module load {MODULE}; {cmd}"], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def test_scaffold_compiles_with_vcs_and_the_smoke_test_is_incomplete_not_pass(tmp_path):
    S.write(tmp_path, gen(), {})
    comp = sh(tmp_path, "vcs -full64 -sverilog -ntb_opts uvm-ieee-2020-2.0 -timescale=1ns/1ps -top tb_top -f filelist_tb.f -Mdir=csrc -o simv -l compile.log")
    assert comp.returncode == 0, comp.stdout[-2000:] + comp.stderr[-500:]
    run = sh(tmp_path, "./simv +UVM_TESTNAME=dut_smoke_test +UVM_NO_RELNOTES -l run.log")
    log = (tmp_path / "run.log").read_text()
    counts = ev.uvm_counts(log)
    assert run.returncode == 0 and counts == {"UVM_ERROR": 0, "UVM_FATAL": 0}
    assert "status=INCOMPLETE" in log and "todo=" in log and "TC_RESULT TC-001 INCOMPLETE" in log
    # the judge refuses to call it a PASS even though the simulator exited with 0 and there is no UVM error
    assert ev.judge_run(log, run.returncode, "dut_smoke_test")[0] == "FAIL"


REAL_VIP = os.environ.get("AXI_VIP_HOME")


@pytest.mark.skipif(not REAL_VIP, reason="AXI_VIP_HOME is not set: the real axi4_avip is not available")
def test_real_vip_with_the_scaffold_compiles_and_elaborates_with_vcs(tmp_path):
    """The scaffold with the real axi4_avip bound (stand-ins for what VCS rejects), against the interface shell: a compile and elaboration check."""
    from q3tui.flows.dv.uvm import binding as B
    from q3tui.flows.dv.uvm import vip as V

    c, a = baseline()
    vip_dir = Path(os.path.expandvars(REAL_VIP))
    p = V.probe(vip_dir, "axi4_avip", path_expr="$AXI_VIP_HOME")
    b = B.derive(p, c)
    S.write(tmp_path, S.generate(c, a, {"vip": {"kind": "axi4_avip"}, "integration": {"smoke_test": "dut_smoke_test"}}, "demo", "engineer", "2026-10-19",
                                 vip={"probe": p, "binding": b, "root": "$AXI_VIP_HOME", "dir": vip_dir}), {})
    opts = " ".join(b["compile_options"]["vcs"])
    comp = sh(tmp_path, f"vcs -full64 -sverilog -ntb_opts uvm-ieee-2020-2.0 -timescale=1ns/1ps {opts} -top tb_top -f filelist_tb.f -Mdir=csrc -o simv -l compile.log")
    assert comp.returncode == 0, (tmp_path / "compile.log").read_text()[-2500:]
    run = sh(tmp_path, "./simv +UVM_TESTNAME=dut_smoke_test +UVM_NO_RELNOTES -l run.log")
    log = (tmp_path / "run.log").read_text()
    assert run.returncode == 0 and ev.uvm_counts(log) == {"UVM_ERROR": 0, "UVM_FATAL": 0} and "status=INCOMPLETE" in log


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "axi4_sram_ctrl"


@pytest.mark.skipif(not REAL_VIP, reason="AXI_VIP_HOME is not set: the real axi4_avip is not available")
def test_example_runs_end_to_end_with_vcs_and_the_smoke_test_passes(tmp_path, monkeypatch):
    """The committed example project: the tb step (no live LLM: the reviewed contract stands in for the intent stage) compiles the testbench
    with the real VIP and RTL, runs the smoke test, and the evidence is read from the log. Generated files stay untouched."""
    import anyio

    from q3tui.core.project import Project
    from q3tui.flows.dv.uvm import contract as C
    from q3tui.llm import runtime
    from q3tui.pipeline.engine import Engine
    from tests.test_dut_dv_intent import intent_from_contract

    root = tmp_path / "axi4_sram_ctrl"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("expected"))
    before = {p.relative_to(root): p.read_bytes() for p in (root / "src" / "tb").rglob("*") if p.is_file()}
    base = C.VerificationContract.model_validate(json.loads((root / "schemas" / "verification_contract.json").read_text()))

    async def only_intent(stage, cfg, emit):
        assert stage.name == "tb_intent", f"unexpected LLM stage {stage.name}"
        return runtime.StageResult("ok", intent_from_contract(base), 0.0, 1, "s")

    monkeypatch.setattr(runtime, "run_stage", only_intent)
    monkeypatch.setenv("PATH", os.environ["PATH"])
    eng = Engine(Project.open(root))
    for name in ("testplan", "tb", "simulate"):
        anyio.run(lambda: eng.run_step(eng.by_name[name]))
    entries = json.loads((root / "schemas" / "dv_evidence.json").read_text())["entries"]
    runs = {e["name"]: e for e in entries if e["kind"] == "run"}  # the last run of each test
    smoke = runs["dut_smoke_test"]
    assert smoke["status"] == "PASS" and smoke["uvm_error"] == 0 and smoke["uvm_fatal"] == 0 and "status=PASS" in (root / smoke["log"]).read_text()
    # the directed tests: all pass except TC-007 (backpressure cannot be stimulated through the VIP adapter: it makes no check, INCOMPLETE, never a PASS)
    not_pass = sorted(n for n, e in runs.items() if e["status"] != "PASS")
    assert not_pass == ["dut_tc_007_backpressure_test"] and len(runs) == 12  # the smoke test and 11 directed tests
    assert "| TC-007 | `dut_tc_007_backpressure_test` | INCOMPLETE |" in (root / "reports" / "dv_test_results.md").read_text()
    after = {p.relative_to(root): p.read_bytes() for p in (root / "src" / "tb").rglob("*") if p.is_file()}
    assert after == before and not list(root.rglob("*.scaffold_new"))
