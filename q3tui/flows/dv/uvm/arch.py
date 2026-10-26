"""The DUT testbench architecture (machine-readable) derived by code from the frozen verification contract (rules DUT-DV-019, 026).

`tb_architecture.json` lists the components the scaffold will contain, how each interface is driven, and the tests, scoreboard rules
and coverage goals with their contract IDs. VIP binding and RTL integration stay `pending` here: they are later phases."""

from __future__ import annotations

from q3tui.flows.dv.uvm.contract import VerificationContract

SCHEMA_VERSION = 1
# stimulus agents of the architecture: the VIP drives protocol interfaces, the TB models memories and clock/reset
AGENT_KINDS = {"vip_master": "existing protocol VIP, master side, drives the DUT slave interface", "sram_model": "testbench memory model: responder plus passive monitor",
               "tb_clock_reset": "clock and reset generator in tb_top", "none": "not driven"}


def derive(c: VerificationContract, backend_options: dict) -> dict:
    """The architecture of the testbench for this contract; `backend_options` is the validated uvm_vip option group (dict)."""
    ic = c.interface_contract
    interfaces = []
    for i in ic.interfaces:
        interfaces.append({"id": i.id, "kind": i.kind, "protocol": i.protocol, "role": i.role, "ports": i.ports, "stimulus_agent": i.stimulus_agent,
                           "vip_binding": ({"status": "pending", "family": backend_options["vip"]["kind"]}
                                           if i.stimulus_agent == "vip_master" and backend_options.get("vip") else None)})
    has_mem = any(i.kind == "sram" for i in ic.interfaces)
    smoke = next((t for t in c.tests if t.kind == "smoke" and t.milestone == "v0_1"), None)
    return {
        "schema_version": SCHEMA_VERSION,
        "ip_name": c.metadata.ip_name,
        "contract_hash": c.metadata.contract_hash,
        "dut": {"top_module": ic.top_module, "parameters": {p.name: p.value for p in ic.parameters},
                "clock": ic.clock, "reset": ic.reset, "reset_active_low": ic.reset_active_low},
        "interfaces": interfaces,
        "components": [
            {"id": "tb_top", "role": "clock/reset generation, interface instances, DUT wrapper, config_db, run_test"},
            {"id": "dut_wrapper", "role": "binds the spec-derived interfaces to the DUT ports; generated in the integration phase", "phase": "integration"},
            {"id": "env", "role": "UVM environment: agents, adapters, scoreboard, coverage"},
            {"id": "vip_adapters", "role": "one adapter per VIP-driven interface: VIP transaction to a VIP-independent transaction", "rule": "DUT-DV-019"},
            {"id": "scoreboard", "role": "spec-driven reference model" + (" (transaction-level memory model)" if has_mem else ""), "rules": [r.id for r in c.scoreboard_rules]},
            {"id": "coverage", "role": "functional coverage subscriber", "goals": [g.id for g in c.coverage]},
            {"id": "assertions", "role": "assertion bind point; properties derive from the specification only", "phase": "later"},
            {"id": "tests", "role": "base test, smoke test, test library", "tests": [t.tc_id for t in c.tests]},
        ],
        "tests": [{"tc_id": t.tc_id, "name": t.name, "kind": t.kind, "milestone": t.milestone, "requirement_ids": t.requirement_ids, "rule_ids": t.rule_ids}
                  for t in c.tests],
        "smoke_test": ({"tc_id": smoke.tc_id, "name": smoke.name, "rule_ids": smoke.rule_ids} if smoke else None),
        "build": {"simulator": backend_options["integration"]["simulator"], "top": "tb_top", "smoke_test_class": backend_options["integration"]["smoke_test"]},
        "integration": {"status": "pending", "note": "RTL is read only in the integration phase, after this architecture is frozen (DUT-DV-026)"},
    }


def check(arch: dict, c: VerificationContract) -> list[str]:
    """Problems of an architecture against its contract: it must name the contract's hash and cover every interface and the smoke test."""
    p = []
    if arch.get("contract_hash") != c.metadata.contract_hash or not c.metadata.contract_hash:
        p.append("the architecture was not derived from this frozen contract (hash differs)")
    if not arch.get("interfaces"):
        p.append("no interface: the contract names none")
    for i in arch.get("interfaces") or []:
        if i["stimulus_agent"] not in AGENT_KINDS:
            p.append(f"interface {i['id']}: stimulus agent '{i['stimulus_agent']}' is not one of {', '.join(AGENT_KINDS)}")
        if i["stimulus_agent"] == "vip_master" and not i.get("vip_binding"):
            p.append(f"interface {i['id']}: driven by a protocol VIP, and no VIP is set (Settings → tb → VIP location)")
    if not arch.get("smoke_test"):
        p.append("no deterministic smoke test (DUT-DV-015)")
    return p


def needs_vip(c: VerificationContract) -> list[str]:
    """The interfaces whose stimulus agent is an existing protocol VIP: the `tb` step cannot build the testbench for them without one."""
    return [i.id for i in c.interface_contract.interfaces if i.stimulus_agent == "vip_master"]


def render_md(arch: dict) -> str:
    lines = [f"# Testbench architecture — {arch['ip_name']}", "", f"Contract hash: `{arch['contract_hash']}` (generated by Q3TUI; do not edit)", "",
             f"DUT top (from the specification): `{arch['dut']['top_module']}`; clock `{arch['dut']['clock']}`, reset `{arch['dut']['reset']}`.", "",
             "## Interfaces", "", "| id | kind | role | driven by | VIP |", "|---|---|---|---|---|"]
    for i in arch["interfaces"]:
        lines.append(f"| {i['id']} | {i['kind']} | {i['role']} | {i['stimulus_agent']} | {(i['vip_binding'] or {}).get('status', '-')} |")
    lines += ["", "## Components", ""] + [f"- **{c['id']}**: {c['role']}" for c in arch["components"]]
    lines += ["", "## Tests", "", "| id | name | kind | milestone | requirements |", "|---|---|---|---|---|"]
    lines += [f"| {t['tc_id']} | {t['name']} | {t['kind']} | {t['milestone']} | {', '.join(t['requirement_ids'])} |" for t in arch["tests"]]
    return "\n".join(lines) + "\n"
