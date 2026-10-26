"""Binding of the existing VIP to the DUT interface contract (rules DUT-DV-010, 019, 020, 030): the signal map, the VIP configuration the
DUT needs, which VIP role drives the DUT, and what the testbench observes with. Derived by code from the VIP probe and the contract.

The VIP is never edited. Where the VIP's configuration surface (a constants package) differs from the approved configuration, the binding
records an override that the generator turns into a derived copy of that one package inside the testbench tree; the VIP's own file is untouched."""

from __future__ import annotations

import re

from q3tui.flows.dv.uvm.contract import VerificationContract

PREFIX = re.compile(r"^[io]_")


def safe_eval(expr: str, names: dict[str, int]) -> int | None:
    """Integer value of a width expression (`DATA_WIDTH/8`, `PR_ADDR_WD`, `[3:0]`); None when it cannot be evaluated."""
    e = expr.strip()
    m = re.fullmatch(r"\[\s*\(?(.+?)\)?\s*-\s*1\s*:\s*0\s*\]", e)
    if m:
        e = m.group(1)
    else:
        m = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", e)
        if m:
            return abs(int(m.group(1)) - int(m.group(2))) + 1
    e = e.strip("() ") if e.count("(") != e.count(")") else e
    if e == "":
        return 1
    toks = re.findall(r"[A-Za-z_]\w*|\d+|\*\*|[-+*/%()]", e)
    if "".join(toks) != re.sub(r"\s+", "", e):
        return None
    py = " ".join(str(names[t]) if t in names else t for t in toks).replace("/", "//")
    if re.search(r"[A-Za-z_]", py):
        return None
    try:
        return int(eval(py, {"__builtins__": {}}, {}))  # noqa: S307 - digits and + - * / % ** () only
    except Exception:  # noqa: BLE001
        return None


def _globals(probe: dict, overrides: dict[str, int]) -> dict[str, int]:
    vals: dict[str, int] = {}
    for _ in range(3):  # parameters may refer to earlier ones (STROBE_WIDTH = DATA_WIDTH/8)
        for k, v in probe["globals"].get("parameters", {}).items():
            n = safe_eval(v["value"], vals)
            if n is not None:
                vals[k] = n
    vals.update(overrides)
    for _ in range(3):
        for k, v in probe["globals"].get("parameters", {}).items():
            if k not in overrides:
                n = safe_eval(v["value"], vals)
                if n is not None:
                    vals[k] = n
    return vals


def axi_ports(c: VerificationContract) -> list:
    ids = {i.id for i in c.interface_contract.interfaces if i.kind.startswith("axi4")}
    names = {p for i in c.interface_contract.interfaces if i.id in ids for p in i.ports}
    return [p for p in c.interface_contract.ports if p.name in names or (not names and re.match(r"^[io]_(aw|w|b|ar|r)\w*$", p.name))]


# Tool limits observed with the real tool and recorded with their evidence: a construct the simulator rejects, and what the binding does about it
TOOL_LIMITS = {"vcs": {"vip_coverage": "observed with VCS X-2025.06: the VIP coverage component fails at run time with Error-[FCIRS] Illegal value in enum bin range (bin range values outside the 4-bit ID enums), which cannot be suppressed",
                "strong_sva": "VCS X-2025.06 rejects the strong SVA operators (s_until_with, s_eventually, ...) in simulation: Error-[SVA-SONS] Strong operator not supported"}}


def derive(probe: dict, c: VerificationContract, simulator: str = "vcs") -> dict:
    """The binding. `findings` lists what the user must know (override of a VIP constant, ties, mismatches)."""
    params = {p.name: int(p.value) for p in c.interface_contract.parameters if re.fullmatch(r"-?\d+", p.value)}
    dut_ports = axi_ports(c)
    bfm = next((b for n, b in probe["bfms"].items() if "master" in n and "driver" in n), {"ports": []})
    vip_dir = {p["name"]: p["dir"] for p in bfm["ports"]}  # master driver BFM: output = driven by the VIP master
    vip_sigs = {s["name"]: s for s in probe["interface"].get("signals", [])}
    findings: list[dict] = []
    subs: list[dict] = []
    overrides: dict[str, int] = {}
    gparams = probe["globals"].get("parameters", {})

    # first pass: which VIP constants the DUT's widths determine (a VIP width written as [NAME-1:0] or [(NAME/8)-1:0])
    mapping = []
    for p in dut_ports:
        base = PREFIX.sub("", p.name)
        v = vip_sigs.get(base)
        dut_w = safe_eval(p.width, params)
        if v is None or dut_w is None:
            mapping.append({"dut": p.name, "vip": base if v else "", "dut_width": dut_w, "status": "dut_only" if v is None else "unevaluable"})
            continue
        m = re.fullmatch(r"\[\s*([A-Za-z_]\w*)\s*-\s*1\s*:\s*0\s*\]", v["width"])
        if m and m.group(1) in gparams and dut_w and base in ("wdata", "rdata", "awaddr", "araddr"):
            overrides[m.group(1)] = dut_w
        mapping.append({"dut": p.name, "vip": base, "dut_width": dut_w, "vip_signal": v})
    vals = _globals(probe, overrides)
    out_map = []
    for m in mapping:
        if "vip_signal" not in m:
            out_map.append(m)
            continue
        vw = safe_eval(m["vip_signal"]["width"], vals)
        vip_drives = vip_dir.get(m["vip"]) == "output"
        dut_in = next(p for p in dut_ports if p.name == m["dut"]).direction.startswith("in")
        status = "matched" if vw == m["dut_width"] else "width_mismatch"
        if vip_drives != dut_in:
            status = "direction_mismatch"
        out_map.append({"dut": m["dut"], "vip": m["vip"], "direction": "vip_to_dut" if dut_in else "dut_to_vip", "dut_width": m["dut_width"],
                        "vip_width": vw, "status": status})
        if status != "matched":
            findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "error",
                             "text": f"{m['dut']} <-> VIP {m['vip']}: {status} (DUT width {m['dut_width']}, VIP width {vw})"})
    mapped_vip = {m["vip"] for m in out_map if m.get("vip")}
    vip_only = [{"vip": n, "drives": "vip" if vip_dir.get(n) == "output" else "dut"} for n in vip_sigs if n not in mapped_vip and n not in ("aclk", "aresetn")]
    ties = [x["vip"] for x in vip_only if x["drives"] == "dut"]
    for k, v in sorted(overrides.items()):
        if gparams[k]["value"].strip() != str(v):
            findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "warning",
                             "text": f"the VIP constant {k} is {gparams[k]['value'].strip()} in {probe['globals']['file']} and the approved DUT configuration needs {v}: "
                                     "a derived copy of that package with the new value is generated in the testbench tree; the VIP file is not edited (DUT-DV-020)"})
    if ties:
        findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "info",
                         "text": f"VIP inputs with no DUT port are tied to 0 in the bridge: {', '.join(ties)}"})
    san = [m["vip"] for m in out_map if m.get("status") == "matched" and m["direction"] == "vip_to_dut" and re.search(r"(valid|ready)$", m["vip"])]
    if san:
        findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "warning",
                         "text": "the bridge reads the VIP's VALID/READY outputs as 1 only when they are a defined 1 (X is seen as 0): " + ", ".join(san) + ". "
                                 "Observed with the VIP: it leaves these unknown until its first transfer, and an X on a handshake input corrupts the DUT state for the whole run."})
    unused = [x["vip"] for x in vip_only if x["drives"] == "vip"]
    if unused:
        findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "info",
                         "text": f"VIP outputs with no DUT port are left unconnected (the DUT does not have them): {', '.join(unused)}"})
    issues = probe.get("known_issues") or []
    if simulator == "vcs" and probe.get("coverage_class"):
        cc = probe["coverage_class"]
        why = TOOL_LIMITS["vcs"]["vip_coverage"]
        subs.append({"type": "include", "unit": cc["name"], "file": cc["file"], "kind": "class", "stand_in": f"vip_cfg/{cc['include']}", "reason": why})
        findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "warning",
                         "text": f"VIP class {cc['name']} ({cc['file']}) is replaced by a stand-in with the same name and the member the agent uses and no covergroup: {why}. "
                                 "The stand-in is found first through the include path; the VIP file is not edited. The VIP's coverage of its own stimulus is therefore NOT_RUN "
                                 "(DUT coverage is the testbench's own, spec-driven: DUT-DV-009)."})
    if simulator == "vcs":
        findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "warning",
                         "text": "VCS X-2025.06 reports Error-[ETMFCB] Enum type mismatch for the VIP's coverage bins (bin range 16 of the 4-bit ID enums in "
                                 "axi4_master_coverage.sv); the compile uses -suppress=ETMFCB (observed: without it the compile fails, with it it succeeds)."})
    observation = {"source": "tb_pin_monitor", "reason": ("the VIP monitor has a known defect (" + ", ".join(sorted({i['id'] for i in issues})) + "): the checker "
                                                          "observes the DUT pins with a testbench-owned, VIP-independent monitor and uses the VIP only to drive stimulus")
                   if issues else "the checker observes the DUT pins with a testbench-owned, VIP-independent monitor; the VIP drives stimulus (DUT-DV-019)"}
    if probe.get("strong_sva_units") and "strong_sva" in TOOL_LIMITS.get(simulator, {}):
        for n, u in probe["strong_sva_units"].items():
            subs.append({"type": "unit", "unit": n, "file": u["file"], "kind": u["kind"], "ports": u["ports"], "stand_in": f"vip_cfg/{n}_standin.sv", "reason": TOOL_LIMITS[simulator]["strong_sva"]})
            findings.append({"id": f"BIND-{len(findings) + 1:03d}", "severity": "warning",
                             "text": f"VIP unit {n} ({u['file']}) uses strong SVA operators: {TOOL_LIMITS[simulator]['strong_sva']}. A stand-in with the same ports and no "
                                     f"properties is generated in the testbench tree instead of the VIP file; the VIP's own protocol assertions are therefore NOT_RUN. The DUT is "
                                     "checked by the scoreboard and by assertions derived from the specification (DUT-DV-020)."})
    mode = next((m for m in probe["globals"].get("mode_macros", []) if m.endswith("_slave")), None)
    return {
        "dut_role": "slave", "vip_agent_role": "master", "agent_mode": "ACTIVE", "mode_macro": mode,
        "signal_map": out_map, "tie_to_zero": ties, "sanitized_controls": san, "unconnected_vip_outputs": unused,
        "config_overrides": overrides, "override_package": {"unit": next((n for n, u in probe["units"].items() if "globals" in n and u["kind"] == "package"), ""),
                                                           "source": probe["globals"].get("file", ""), "derived_file": "vip_cfg/axi4_globals_pkg.sv"} if overrides else None,
        "agent_cfg": {"has_coverage": 1 if any(i["id"] == "ACTIVE_NEEDS_COVERAGE" for i in issues) else 0},
        "compile_options": {"vcs": ["-suppress=ETMFCB"]} if simulator == "vcs" else {},
        "substitutions": subs, "observation": observation, "findings": findings,
        "blocking": [f for f in findings if f["severity"] == "error"],
    }
