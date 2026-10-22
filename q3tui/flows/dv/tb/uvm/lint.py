"""Deterministic validators of the generated testbench (rules DUT-DV-007..009, 013, 019, 020, 024, 025, 028). No tool needed."""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.flows.dv.tb.uvm import svgen as g
from q3tui.flows.dv.tb.uvm.contract import VerificationContract

HIER = re.compile(r"\b(?:u_dut|dut|tb_top\.u_dut)\s*\.\s*\w+")  # a hierarchical reference into the DUT
TRACE = {"TC": re.compile(r"^\s*//\s*(TC-\d+)\s*\|\s*([^|\n]*)", re.M), "SBR": re.compile(r"^\s*//\s*(SBR-\d+)\s*\|\s*([^|\n]*)", re.M),
         "COV": re.compile(r"^\s*//\s*(COV-\d+)\s*\|\s*([^|\n]*)", re.M)}


def _strip_comments(t: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", t, flags=re.S)


def lint(tb_dir: Path, c: VerificationContract, expected_files: list[str], vip_names: set[str] | None = None,
         vip_root: tuple[str, Path] | None = None) -> list[dict]:
    """Findings {code, severity, message}. Errors block the step; warnings are reported."""
    out: list[dict] = []
    add = lambda code, sev, msg: out.append({"code": code, "severity": sev, "message": msg})  # noqa: E731
    for rel in expected_files:
        if not (tb_dir / rel).is_file():
            add("DV-F01", "error", f"required generated file missing: {rel}")
    texts = {str(p.relative_to(tb_dir)): p.read_text(errors="replace") for p in tb_dir.rglob("*") if p.suffix in (".sv", ".svh")}
    req = {r.req_id for r in c.requirements}
    # filelist: every entry exists (no dangling file or include directory)
    fl = tb_dir / "filelist_tb.f"
    if fl.is_file():
        for ln in fl.read_text().splitlines():
            ln = ln.split("//")[0].strip()
            if not ln:
                continue
            tgt = ln[len("+incdir+"):] if ln.startswith("+incdir+") else ln
            if vip_root:
                tgt = tgt.replace(vip_root[0], str(vip_root[1]))
            if not (tb_dir / tgt).exists():
                add("DV-F02", "error", f"filelist_tb.f lists a missing path: {tgt}")
    # traces: ids in comments match the contract exactly (no dangling, no dropped)
    for kind, items, key in (("TC", c.tests, "tc_id"), ("SBR", c.scoreboard_rules, "id"), ("COV", c.coverage, "id")):
        seen: dict[str, str] = {}
        for rel, t in texts.items():
            for m in TRACE[kind].finditer(t):
                seen[m.group(1)] = m.group(2)
                for rid in re.findall(r"REQ-\d+", m.group(2)):
                    if rid not in req:
                        add("DV-T01", "error", f"{rel}: {m.group(1)} cites unknown requirement {rid}")
        want = {getattr(i, key) for i in items}
        for rid in sorted(seen.keys() - want):
            add("DV-T02", "error", f"{kind} {rid} appears in the testbench but not in the contract")
        for rid in sorted(want - seen.keys()):
            add("DV-T03", "error", f"{kind} {rid} of the contract has no code (dropped)")
    # DUT-DV-028: no hierarchical reference into the DUT outside the integration layer
    for rel, t in texts.items():
        if rel.startswith("integration/"):
            continue
        for m in HIER.finditer(_strip_comments(t)):
            add("DV-I01", "error", f"{rel}: hierarchical reference into the DUT ({m.group(0)}) (DUT-DV-028)")
        if "\t" in t:
            add("DV-H01", "error", f"{rel}: tab character (spaces only)")
        if rel != "tb_top.sv" and re.search(r"\$(display|write|finish)\b", _strip_comments(t)) and not rel.endswith(".sv"):
            add("DV-H02", "error", f"{rel}: $display/$write/$finish in UVM code")
        if not t.startswith("// " + g.HEADER_MARK) and not rel.endswith(".scaffold_new"):
            add("DV-G01", "warning", f"{rel}: no scaffold header (edited or not generated)")
    # regions: a scaffold-owned region must still be a stub; an agent-owned one should not keep the scaffold's DV_TODO
    for rel, t in texts.items():
        for m in g.REGION_RE.finditer(t):
            todo = "DV_TODO(" in m.group(4)
            if m.group(3) == "scaffold" and not todo:
                add("DV-R01", "error", f"{rel}: region {m.group(2)} is scaffold-owned but has no DV_TODO: mark the owner as agent or user (DUT-DV-013)")
            if m.group(3) != "scaffold" and todo:
                add("DV-R02", "warning", f"{rel}: region {m.group(2)} is owned by {m.group(3)} but still carries DV_TODO")
    # VIP is reached through the adapter only (DUT-DV-019): VIP package and class names must not appear in the scoreboard, coverage, tests, env, sequences
    for rel in ("scoreboard/dut_scoreboard.svh", "coverage/dut_cov.svh", "tests/dut_test_lib.svh", "env/dut_env.svh", "sequences/dut_seq_lib.svh",
                "env/dut_axi_pin_monitor.svh"):
        body = _strip_comments(texts.get(rel, ""))
        for n in sorted(vip_names or ()):
            if re.search(rf"\b{re.escape(n)}\b", body):
                add("DV-V01", "error", f"{rel} references the VIP name {n} (DUT-DV-019: only the adapter may)")
    return out
