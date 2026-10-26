"""Deterministic UVM scaffold of the `uvm_vip` backend (milestone M3; rules DUT-DV-013, 014, 015, 024, 025).

Everything that follows from the frozen verification contract is generated completely: the approved-parameter package, the pins
interface with the specification's port names, the VIP-independent transaction classes, the SRAM-side agent skeleton, the scoreboard with
one check function per scoreboard rule, one covergroup per coverage goal, sequences and tests with their requirement traces, the
assertion bind point, `tb_top`, and the filelist. What needs semantic work is a `DV_REGION` stub that announces itself at run time with
`DV_TODO(<id>)`; the worklist (`worklist.py`) lists them.

The scaffold never reads RTL. The DUT is reached through `dut_wrapper(dut_pins_if pins)`: here an interface shell made from the
contract's ports (not a DUT); the integration phase replaces it (DUT-DV-026)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from q3tui.flows.dv.uvm import svgen as g
from q3tui.flows.dv.uvm.contract import PortSpec, VerificationContract

ROLES = {"@ID_WD@": r"(^|_)ID(_|$)", "@ADDR_WD@": r"ADDR", "@DATA_WD@": r"DATA"}   # width roles the templates use, found among the contract's parameters


def unsupported(c: VerificationContract) -> str | None:
    """Why the scaffold cannot be generated for this contract, None when it can. The templates are those of an AXI-style memory-mapped slave (transactions with an ID,
    an address and data words, a memory model, an AXI scoreboard): they need one width parameter each for the roles ID / ADDR / DATA. Any other IP (a sorter, a counter,
    a stream block) has none of them and needs a different testbench, which this backend does not generate yet."""
    try:
        param_roles(c)
    except ValueError as exc:
        names = ", ".join(p.name for p in c.interface_contract.parameters) or "none"
        return (f"the UVM testbench generator only supports IPs with an AXI-style memory-mapped interface so far ({exc}). The parameters of {c.metadata.top_module or 'this IP'}: {names}. "
                "For any other IP set Settings → tb → Testbench kind to plain (a plain SystemVerilog testbench).")
    return None


def param_roles(c: VerificationContract) -> dict[str, str]:
    """The contract's own parameter name for each width role of the templates (the specification names its parameters, the generator never assumes a prefix)."""
    names = [p.name for p in c.interface_contract.parameters]
    out = {}
    for token, rx in ROLES.items():
        hit = [n for n in names if re.search(rx, n.upper())]
        if len(hit) != 1:
            raise ValueError(f"the contract must have exactly one parameter for the role {token.strip('@')} (found {hit or 'none'}): the testbench widths follow the specification's names")
        out[token] = hit[0]
    return out


DIRS = ("env", "sram", "adapters", "scoreboard", "coverage", "sequences", "tests", "assertions", "interfaces", "integration")
TB_TOP = "tb_top"


def _clk_rst(c: VerificationContract) -> tuple[str, str, bool]:
    ic = c.interface_contract
    return ic.clock, ic.reset, bool(ic.reset_active_low)


def _data_ports(c: VerificationContract) -> list[PortSpec]:
    clk, rst, _ = _clk_rst(c)
    return [p for p in c.interface_contract.ports if p.name not in (clk, rst)]


def params_pkg(c: VerificationContract, hdr: str) -> str:
    lines = [f"localparam int {p.name} = {p.value};  // approved configuration (final_config.json)" for p in c.interface_contract.parameters]
    return (hdr + "package dut_tb_params_pkg;\n  " + "\n  ".join(lines) + "\n"
            "  // TB configuration, not a DUT requirement: clock period and reset length (override with +CLK_PERIOD_NS / +RESET_CYCLES)\n"
            "  localparam int CLK_PERIOD_NS_DEFAULT = 10;\n  localparam int RESET_CYCLES_DEFAULT = 5;\nendpackage\n")


def pins_if(c: VerificationContract, hdr: str) -> str:
    clk, rst, _ = _clk_rst(c)
    decl = []
    for p in _data_ports(c):
        decl.append(f"  logic {g.sv_range(p.width)} {p.name};".replace("logic  ", "logic ") + f"  // {p.direction}, {p.source}")
    ins = ", ".join(p.name for p in _data_ports(c))
    mem = {n for i in c.interface_contract.interfaces if i.kind == "sram" for n in i.ports}
    drv = [p.name for p in _data_ports(c) if p.name in mem and p.direction.startswith("in")]
    drv_cb = (f"\n  // `drv_cb` drives the memory-side inputs of the DUT from the testbench memory model\n  clocking drv_cb @(posedge {clk});\n"
              f"    default output #1;\n    output {', '.join(drv)};\n  endclocking" if drv else "")
    return (hdr + f"// The DUT-facing pins, named exactly as the specification names them (interface contract). Inputs (in) are driven by the\n"
            f"// testbench, outputs (out) by the DUT through dut_wrapper. `mon_cb` samples every pin for the testbench-owned monitors.\n"
            f"interface dut_pins_if(input logic {clk}, input logic {rst});\n  import dut_tb_params_pkg::*;\n" + "\n".join(decl)
            + f"\n  clocking mon_cb @(posedge {clk});\n    default input #1step;\n    input {ins};\n  endclocking{drv_cb}\nendinterface\n")


def wrapper_shell(c: VerificationContract, hdr: str) -> str:
    outs = [p.name for p in _data_ports(c) if p.direction.startswith("out")]
    return (hdr + "// INTERFACE SHELL, NOT THE DUT: outputs are tied to 0 so the testbench can be compiled before the RTL is integrated.\n"
            "// A compile against this shell is a syntax and integration check, never a DUT result (DUT-DV-012). The integration phase\n"
            "// replaces this module by the real wrapper with the same name and port.\n"
            "module dut_wrapper(dut_pins_if pins);\n" + "\n".join(f"  assign pins.{o} = '0;" for o in outs) + "\nendmodule\n")


def base_svh(hdr: str) -> str:
    return hdr + '''// DV_TODO: a stub announces itself at run time and keeps the test from reporting PASS (DUT-DV-023, 024).
`define DV_TODO(ID, MSG) dut_dv_todo::announce(`"ID`", MSG);

class dut_dv_todo;
  static int count;
  static string ids[$];
  static function void announce(string id, string msg);
    count++;
    ids.push_back(id);
    `uvm_warning("DV_TODO", $sformatf("[%s] %s is not implemented", id, msg))
  endfunction
endclass
'''


def axi_txn(c: VerificationContract, hdr: str) -> str:
    return hdr + '''// VIP-independent AXI transaction: what the scoreboard and coverage see. The adapter fills it from the VIP's own transaction (DUT-DV-019).
class dut_axi_txn extends uvm_sequence_item;
  `uvm_object_utils(dut_axi_txn)
  bit                         is_write;
  bit [@ID_WD@-1:0]          id;
  bit [@ADDR_WD@-1:0]        addr;
  bit [7:0]                   len;     // beats - 1
  bit [2:0]                   size;
  bit [1:0]                   burst;
  bit [@DATA_WD@-1:0]        data[$];
  bit [(@DATA_WD@/8)-1:0]    strb[$];
  bit [1:0]                   resp[$]; // B: one entry; R: one per beat
  bit                         last_seen;
  bit                         incomplete;  // the response came before every data beat was seen / the beat count differs from len + 1
  time                        t_addr;      // time of the address handshake
  time                        t_done;      // time of the last data beat (read) or of the response (write)
  function new(string name = "dut_axi_txn");
    super.new(name);
  endfunction
  function string convert2string();
    return $sformatf("%s id=%0h addr=%0h len=%0d size=%0d burst=%0d beats=%0d", is_write ? "WR" : "RD", id, addr, len, size, burst, data.size());
  endfunction
endclass
'''


def sram_txn(c: VerificationContract, hdr: str) -> str:
    return hdr + '''// One SRAM-side access seen on the memory interface of the DUT.
class dut_sram_txn extends uvm_sequence_item;
  `uvm_object_utils(dut_sram_txn)
  int unsigned                bank;
  bit                         we;      // write enable seen in this cycle
  bit                         oe;      // read (output) enable seen in this cycle
  bit                         is_write;
  bit [@ADDR_WD@-1:0]        addr;
  bit [@DATA_WD@-1:0]        data;    // write data, or the word returned in the following cycle
  bit [(@DATA_WD@/8)-1:0]    strb;
  function new(string name = "dut_sram_txn");
    super.new(name);
  endfunction
  function string convert2string();
    return $sformatf("bank=%0d we=%0b oe=%0b addr=%0h data=%0h strb=%0h", bank, we, oe, addr, data, strb);
  endfunction
endclass
'''


def sram_agent(c: VerificationContract, hdr: str) -> str:
    sram = next((p for p in _data_ports(c) if "sram" in p.name and p.name.endswith("_we")), None)
    mon = g.region("sram.monitor", "`DV_TODO(sram.monitor, \"SRAM monitor: build one dut_sram_txn per access from the pins\")")
    resp = g.region("sram.responder", "`DV_TODO(sram.responder, \"SRAM model: memory array, byte-lane writes, the read data one cycle after a read enable\")")
    return hdr + f'''// SRAM side of the DUT: a testbench memory model (responder) and a passive monitor. Behavior comes from the specification's SRAM
// obligations (interface section); the regions below are semantic work (see the worklist).
class dut_sram_monitor extends uvm_monitor;
  `uvm_component_utils(dut_sram_monitor)
  virtual dut_pins_if vif;
  uvm_analysis_port #(dut_sram_txn) ap;
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    ap = new("ap", this);
    if (!uvm_config_db#(virtual dut_pins_if)::get(this, "", "pins_vif", vif)) `uvm_fatal("DUT_SRAM", "pins_vif is not set")
  endfunction
  task run_phase(uvm_phase phase);
{g.region("sram.monitor", "`DV_TODO(sram.monitor, \"SRAM monitor: build one dut_sram_txn per access from the pins and write it to ap\")", "    ").rstrip()}
  endtask
endclass

class dut_sram_responder extends uvm_component;
  `uvm_component_utils(dut_sram_responder)
  virtual dut_pins_if vif;
  bit [7:0] mem[bit [@ADDR_WD@-1:0]];   // byte-addressed memory of the model; a byte that was never written reads as 0 (a testbench choice, not a DUT requirement)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    if (!uvm_config_db#(virtual dut_pins_if)::get(this, "", "pins_vif", vif)) `uvm_fatal("DUT_SRAM", "pins_vif is not set")
  endfunction
  task run_phase(uvm_phase phase);
{g.region("sram.responder", "`DV_TODO(sram.responder, \"SRAM model: memory array, byte-lane writes, the read data one cycle after a read enable\")", "    ").rstrip()}
  endtask
endclass

class dut_sram_agent extends uvm_agent;
  `uvm_component_utils(dut_sram_agent)
  dut_sram_monitor   mon;
  dut_sram_responder rsp;
  uvm_analysis_port #(dut_sram_txn) ap;
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    mon = dut_sram_monitor::type_id::create("mon", this);
    rsp = dut_sram_responder::type_id::create("rsp", this);
    ap = new("ap", this);
  endfunction
  function void connect_phase(uvm_phase phase);
    mon.ap.connect(ap);
  endfunction
endclass
'''


def adapters(c: VerificationContract, arch: dict, hdr: str) -> str:
    """Adapter stub, used while no VIP is bound: `send` announces itself (DV_TODO)."""
    fam = next((i["vip_binding"]["family"] for i in arch["interfaces"] if i.get("vip_binding")), "unbound")
    body = g.region("adapter.axi", "`DV_TODO(adapter.axi, \"AXI adapter: drive the transaction through the VIP master (bound to the VIP in milestone M4)\")", "    ")
    return hdr + f'''// One adapter per VIP-driven interface: the only place that knows the VIP's own classes (DUT-DV-019). VIP family: {fam}.
class dut_axi_adapter extends uvm_component;
  `uvm_component_utils(dut_axi_adapter)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  task send(dut_axi_txn t);
{body.rstrip()}
  endtask
endclass
'''


def pin_monitor(c: VerificationContract, hdr: str) -> str:
    """VIP-independent AXI4 monitor on the DUT pins: one dut_axi_txn per burst. It follows the AXI4 channel handshakes only (a transfer happens
    where VALID and READY are both 1); it does not assume any DUT behavior beyond that, so the checker does not depend on the VIP monitor."""
    names = {re.sub(r"^[io]_", "", p.name): p.name for p in _data_ports(c)}
    P = lambda sig: f"vif.mon_cb.{names[sig]}" if sig in names else "'0"  # noqa: E731
    return hdr + f'''class dut_axi_pin_monitor extends uvm_monitor;
  `uvm_component_utils(dut_axi_pin_monitor)
  virtual dut_pins_if vif;
  uvm_analysis_port #(dut_axi_txn) ap;
  dut_axi_txn aw_q[$];   // accepted write addresses, oldest first
  dut_axi_txn ar_q[$];   // accepted read addresses, oldest first
  bit [@DATA_WD@-1:0]     spare_data[$];  // write beats that arrived before their address
  bit [(@DATA_WD@/8)-1:0] spare_strb[$];
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    ap = new("ap", this);
    if (!uvm_config_db#(virtual dut_pins_if)::get(this, "", "pins_vif", vif)) `uvm_fatal("DUT_AXI_MON", "pins_vif is not set")
  endfunction
  // the burst that takes the next write beat: the oldest accepted write whose beats are not complete
  function int next_write_burst();
    foreach (aw_q[i]) if (aw_q[i].data.size() < aw_q[i].len + 1) return i;
    return -1;
  endfunction
  function void give_write_beat(bit [@DATA_WD@-1:0] d, bit [(@DATA_WD@/8)-1:0] s);
    int i = next_write_burst();
    if (i < 0) begin
      spare_data.push_back(d);
      spare_strb.push_back(s);
    end else begin
      aw_q[i].data.push_back(d);
      aw_q[i].strb.push_back(s);
    end
  endfunction
  task run_phase(uvm_phase phase);
    forever begin
      @(vif.mon_cb);
      if (vif.{rst_name(c)} !== {rst_idle(c)}) begin  // reset: transactions in flight are discarded
        aw_q.delete(); ar_q.delete(); spare_data.delete(); spare_strb.delete();
        continue;
      end
      if ({P("awvalid")} === 1'b1 && {P("awready")} === 1'b1) begin
        dut_axi_txn t = dut_axi_txn::type_id::create("wr");
        t.is_write = 1; t.id = {P("awid")}; t.addr = {P("awaddr")}; t.len = {P("awlen")}; t.size = {P("awsize")}; t.burst = {P("awburst")};
        t.t_addr = $time;
        aw_q.push_back(t);
        while (spare_data.size() != 0 && next_write_burst() >= 0) begin
          give_write_beat(spare_data.pop_front(), spare_strb.pop_front());
        end
      end
      if ({P("wvalid")} === 1'b1 && {P("wready")} === 1'b1) give_write_beat({P("wdata")}, {P("wstrb")});
      if ({P("bvalid")} === 1'b1 && {P("bready")} === 1'b1) begin
        foreach (aw_q[i]) if (aw_q[i].id == {P("bid")}) begin
          dut_axi_txn t = aw_q[i];
          aw_q.delete(i);
          t.resp.push_back({P("bresp")});
          t.incomplete = (t.data.size() != t.len + 1);
          t.t_done = $time;
          ap.write(t);
          break;
        end
      end
      if ({P("arvalid")} === 1'b1 && {P("arready")} === 1'b1) begin
        dut_axi_txn t = dut_axi_txn::type_id::create("rd");
        t.is_write = 0; t.id = {P("arid")}; t.addr = {P("araddr")}; t.len = {P("arlen")}; t.size = {P("arsize")}; t.burst = {P("arburst")};
        t.t_addr = $time;
        ar_q.push_back(t);
      end
      if ({P("rvalid")} === 1'b1 && {P("rready")} === 1'b1) begin
        foreach (ar_q[i]) if (ar_q[i].id == {P("rid")}) begin
          dut_axi_txn t = ar_q[i];
          t.data.push_back({P("rdata")});
          t.resp.push_back({P("rresp")});
          if ({P("rlast")} === 1'b1) begin
            ar_q.delete(i);
            t.last_seen = 1;
            t.incomplete = (t.data.size() != t.len + 1);
            t.t_done = $time;
            ap.write(t);
          end
          break;
        end
      end
    end
  endtask
endclass
'''


def rst_name(c: VerificationContract) -> str:
    return c.interface_contract.reset


def rst_idle(c: VerificationContract) -> str:
    return "1'b1" if c.interface_contract.reset_active_low else "1'b0"


def rule_uses_sram(r, c: VerificationContract) -> bool:
    """A rule about the memory-side interface also gets a hook that receives the observed SRAM accesses."""
    reqs = {x.req_id: x.text for x in c.requirements}
    text = " ".join([r.description, r.source, *[reqs.get(i, "") for i in r.requirement_ids]]).lower()
    return "sram" in text


def scoreboard(c: VerificationContract, hdr: str) -> str:
    fns, calls, scalls = [], [], []
    for r in c.scoreboard_rules:
        rid = r.id
        fns.append(f"  // {rid} | {', '.join(r.requirement_ids)} | {r.source}\n  // {g.sv_str(r.description)}\n  function void check_{g.lc(rid)}(dut_axi_txn t);\n"
                   + g.region(rid, f"`DV_TODO({rid}, \"{g.sv_str(r.description[:80])}\")", "    ").rstrip() + "\n  endfunction\n")
        calls.append(f"    if (enabled.exists(\"{rid}\")) check_{g.lc(rid)}(t);")
        if rule_uses_sram(r, c):
            fns.append(f"  // {rid} (memory side): called for every SRAM access the monitor observes\n  function void sram_{g.lc(rid)}(dut_sram_txn s);\n"
                       + g.region(f"{rid}.sram", f"`DV_TODO({rid}.sram, \"{g.sv_str(r.description[:70])} (memory side)\")", "    ").rstrip() + "\n  endfunction\n")
            scalls.append(f"    if (enabled.exists(\"{rid}\")) sram_{g.lc(rid)}(s);")
    return hdr + f'''`uvm_analysis_imp_decl(_axi)
`uvm_analysis_imp_decl(_sram)

// Spec-driven reference model and checker. Each rule below names its requirements and its specification source (DUT-DV-008); none is
// derived from the RTL. A rule that is still a stub announces itself and the test cannot report PASS (DUT-DV-023).
class dut_scoreboard extends uvm_scoreboard;
  `uvm_component_utils(dut_scoreboard)
  uvm_analysis_imp_axi  #(dut_axi_txn,  dut_scoreboard) axi_imp;
  uvm_analysis_imp_sram #(dut_sram_txn, dut_scoreboard) sram_imp;
  int unsigned checks;       // every rule that makes a check increments this
  int unsigned mismatches;
  bit enabled[string];       // the rules a test relies on (set from the test's rule list): a rule that is not enabled is not run
  bit [7:0] exp_mem[bit [@ADDR_WD@-1:0]];   // expected memory contents, byte addressed, built from the observed writes by the rules
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    axi_imp = new("axi_imp", this);
    sram_imp = new("sram_imp", this);
  endfunction
  function void enable(string rule_id);
    enabled[rule_id] = 1;
  endfunction
  function void write_axi(dut_axi_txn t);
{chr(10).join(calls)}
  endfunction
  function void write_sram(dut_sram_txn s);
{chr(10).join(scalls) if scalls else "    // no rule observes the memory side"}
  endfunction
  function void mismatch(string rule, string msg);
    mismatches++;
    `uvm_error(rule, msg)
  endfunction
{chr(10).join(fns)}endclass
'''


def coverage(c: VerificationContract, hdr: str) -> str:
    groups, news, regs = [], [], []
    for k in c.coverage:
        n = g.lc(k.id)
        reqs = ", ".join(k.requirement_ids or []) or ", ".join(k.objective_ids)
        groups.append(f"  bit hit_{n};\n  // {k.id} | {reqs}\n  // {g.sv_str(k.description)}\n  covergroup cg_{n};\n    option.name = \"{k.id}\";\n    option.per_instance = 1;\n"
                      f"    cp_hit: coverpoint hit_{n} {{ bins hit = {{1}}; }}\n  endgroup\n")
        news.append(f"    cg_{n} = new();")
        regs.append(g.region(k.id, f"`DV_TODO({k.id}, \"sample cg_{n} and replace its placeholder bins: {g.sv_str(k.description[:70])}\")", "    ").rstrip())
    return hdr + f'''// Functional coverage; every goal traces to requirements or objectives of the contract (DUT-DV-009). Bins and sampling are semantic work.
class dut_cov extends uvm_subscriber #(dut_axi_txn);
  `uvm_component_utils(dut_cov)
{chr(10).join(groups)}  function new(string name, uvm_component parent);
    super.new(name, parent);
{chr(10).join(news)}
  endfunction
  function void write(dut_axi_txn t);
{chr(10).join(regs)}
  endfunction
endclass
'''


def env(c: VerificationContract, hdr: str) -> str:
    return hdr + '''class dut_env_cfg extends uvm_object;
  `uvm_object_utils(dut_env_cfg)
  bit has_scoreboard = 1;
  bit has_coverage   = 0;  // coverage regions are semantic work: switch on when they are implemented (no coverage is claimed before)
  function new(string name = "dut_env_cfg");
    super.new(name);
  endfunction
endclass

// The VIP is driven through dut_axi_adapter; the checker observes the DUT pins with the testbench-owned dut_axi_pin_monitor.
class dut_env extends uvm_env;
  `uvm_component_utils(dut_env)
  dut_env_cfg        cfg;
  dut_sram_agent     sram;
  dut_axi_adapter    axi_adapter;
  dut_axi_pin_monitor axi_mon;
  dut_scoreboard     scb;
  dut_cov            cov;
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    if (!uvm_config_db#(dut_env_cfg)::get(this, "", "cfg", cfg)) cfg = dut_env_cfg::type_id::create("cfg");
    sram = dut_sram_agent::type_id::create("sram", this);
    axi_adapter = dut_axi_adapter::type_id::create("axi_adapter", this);
    axi_mon = dut_axi_pin_monitor::type_id::create("axi_mon", this);
    if (cfg.has_scoreboard) scb = dut_scoreboard::type_id::create("scb", this);
    if (cfg.has_coverage) cov = dut_cov::type_id::create("cov", this);
  endfunction
  function void connect_phase(uvm_phase phase);
    if (scb != null) begin
      axi_mon.ap.connect(scb.axi_imp);
      sram.ap.connect(scb.sram_imp);
    end
    if (cov != null) axi_mon.ap.connect(cov.analysis_export);
  endfunction
endclass
'''


def sequences(c: VerificationContract, hdr: str) -> str:
    smoke = next((t for t in c.tests if t.kind == "smoke" and t.milestone == "v0_1"), None)
    body = g.region("seq.smoke", f"`DV_TODO(seq.smoke, \"smoke sequence {smoke.tc_id if smoke else ''}: {g.sv_str(smoke.description[:90]) if smoke else ''}\")", "    ")
    return hdr + f'''class dut_base_seq extends uvm_sequence #(uvm_sequence_item);
  `uvm_object_utils(dut_base_seq)
  function new(string name = "dut_base_seq");
    super.new(name);
  endfunction
endclass

// {smoke.tc_id if smoke else 'no smoke test'} | {', '.join(smoke.requirement_ids) if smoke else ''}
// {g.sv_str(smoke.description) if smoke else ''}
class dut_smoke_vseq extends dut_base_seq;
  `uvm_object_utils(dut_smoke_vseq)
  dut_axi_adapter adapter;   // set by the test: the only way the sequence reaches the VIP
  function new(string name = "dut_smoke_vseq");
    super.new(name);
  endfunction
  task body();
{body.rstrip()}
  endtask
endclass
'''


def tests(c: VerificationContract, opts: dict, hdr: str) -> str:
    smoke_cls = opts["integration"]["smoke_test"]
    out = [hdr + f'''// Base test: reset wait, watchdog and result reporting. `VLSIT_DUT_TB_RESULT` and `TC_RESULT` are printed from the report phase; a test is
// PASS only with zero UVM_ERROR / UVM_FATAL, no scoreboard mismatch, no DV_TODO announced and at least one check made (DUT-DV-023).
// The watchdog (+DV_TIMEOUT_NS, default 1000000 ns) ends a run that makes no progress with a UVM_FATAL: a hang is a FAIL, never a PASS.
class dut_base_test extends uvm_test;
  `uvm_component_utils(dut_base_test)
  dut_env env;
  string  tc_id = "";
  string  rules[$];                  // scoreboard rules this test relies on (contract: rule_ids)
  virtual dut_pins_if vif;
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    env = dut_env::type_id::create("env", this);
    if (!uvm_config_db#(virtual dut_pins_if)::get(this, "", "pins_vif", vif)) `uvm_fatal("DUT_TEST", "pins_vif is not set")
  endfunction
  function void end_of_elaboration_phase(uvm_phase phase);
    int unsigned t_ns = 1000000;
    void'($value$plusargs("DV_TIMEOUT_NS=%d", t_ns));
    uvm_top.set_timeout(t_ns * 1ns, 0);
    if (env.scb != null) foreach (rules[i]) env.scb.enable(rules[i]);
  endfunction
  task wait_reset_release();   // the reset is asserted after a few idle cycles (tb_top): wait for that, then for its release
    @(posedge vif.{c.interface_contract.clock} iff vif.{c.interface_contract.reset} === {"1'b0" if c.interface_contract.reset_active_low else "1'b1"});
    @(posedge vif.{c.interface_contract.clock} iff vif.{c.interface_contract.reset} === {"1'b1" if c.interface_contract.reset_active_low else "1'b0"});
    repeat (2) @(posedge vif.{c.interface_contract.clock});
  endtask
  function void report_phase(uvm_phase phase);
    uvm_report_server srv = uvm_report_server::get_server();
    int unsigned mism = (env != null && env.scb != null) ? env.scb.mismatches : 0;
    int unsigned chk  = (env != null && env.scb != null) ? env.scb.checks : 0;
    string status = (srv.get_severity_count(UVM_ERROR) + srv.get_severity_count(UVM_FATAL) + mism) != 0 ? "FAIL"
                  : (dut_dv_todo::count != 0 || chk == 0) ? "INCOMPLETE" : "PASS";  // no check made = no evidence
    `uvm_info("DUT_RESULT", $sformatf("VLSIT_DUT_TB_RESULT test=%s tc=%s status=%s checks=%0d mismatches=%0d todo=%0d", get_type_name(), tc_id, status, chk, mism, dut_dv_todo::count), UVM_NONE)
    if (tc_id != "") `uvm_info("DUT_RESULT", $sformatf("TC_RESULT %s %s", tc_id, status), UVM_NONE)
  endfunction
endclass
''']
    for t in c.tests:
        smoke = t.kind == "smoke" and t.milestone == "v0_1"
        cls = smoke_cls if smoke else f"dut_tc_{int(t.tc_id.split('-')[-1]):03d}_{g.lc(t.name)}_test"
        run = ("    dut_smoke_vseq seq = dut_smoke_vseq::type_id::create(\"seq\");\n    phase.raise_objection(this);\n    wait_reset_release();\n"
               "    seq.adapter = env.axi_adapter;\n    seq.start(null);\n    phase.drop_objection(this);\n" if smoke else
               g.region(f"test.{t.tc_id}", f"`DV_TODO(test.{t.tc_id}, \"{t.tc_id} {g.sv_str(t.name)} is not implemented (milestone: {t.milestone})\")", "    "))
        rules = "".join(f'    rules.push_back("{r}");\n' for r in t.rule_ids)
        out.append(f'''// {t.tc_id} | {', '.join(t.requirement_ids) or ', '.join(t.objective_ids)} | {t.kind}, {t.milestone}
class {cls} extends dut_base_test;
  `uvm_component_utils({cls})
  function new(string name, uvm_component parent);
    super.new(name, parent);
    tc_id = "{t.tc_id}";
{rules}  endfunction
  task run_phase(uvm_phase phase);
{run.rstrip()}
  endtask
endclass
''')
    return "\n".join(out)


def assertions(c: VerificationContract, hdr: str) -> str:
    return hdr + '''// Assertion bind point: properties derive from the specification only (never from RTL). None yet: later milestone.
module dut_assertions(dut_pins_if pins);
  // DV_REGION(assertions) BEGIN scaffold
  // DV_TODO(assertions, no property implemented yet)
  // DV_REGION(assertions) END
endmodule
'''


def package(c: VerificationContract, hdr: str) -> str:
    files = ["dut_dv_base.svh", "env/dut_txn.svh", "sram/dut_sram_agent.svh", "adapters/dut_axi_adapter.svh", "env/dut_axi_pin_monitor.svh", "scoreboard/dut_scoreboard.svh",
             "coverage/dut_cov.svh", "env/dut_env.svh", "sequences/dut_seq_lib.svh", "tests/dut_test_lib.svh"]
    return hdr + "package dut_dv_pkg;\n  import uvm_pkg::*;\n  import dut_tb_params_pkg::*;\n  `include \"uvm_macros.svh\"\n" + "".join(f'  `include "{f}"\n' for f in files) + "endpackage\n"


def tb_top(c: VerificationContract, hdr: str, vip_block: str = "") -> str:
    clk, rst, low = _clk_rst(c)
    on, off = ("1'b0", "1'b1") if low else ("1'b1", "1'b0")
    vip_block = vip_block.replace("{CLK}", clk).replace("{RST}", rst)
    return hdr + f'''module {TB_TOP};
  import uvm_pkg::*;
  import dut_tb_params_pkg::*;
  import dut_dv_pkg::*;
  `include "uvm_macros.svh"
  logic {clk};
  logic {rst};
  int unsigned clk_period_ns = CLK_PERIOD_NS_DEFAULT;
  int unsigned reset_cycles  = RESET_CYCLES_DEFAULT;

  dut_pins_if pins(.{clk}({clk}), .{rst}({rst}));
  dut_wrapper u_dut(.pins(pins));
  dut_assertions u_assertions(.pins(pins));
{vip_block}
  initial begin
    void'($value$plusargs("CLK_PERIOD_NS=%d", clk_period_ns));
    {clk} = 1'b0;
    forever #(clk_period_ns / 2.0) {clk} = ~{clk};
  end

  // Reset: idle for a few cycles first, then asserted for `reset_cycles`, then released: a protocol VIP and the tests wait for the reset ASSERTION
  // event, so reset must not already be active at time 0. Changes happen on the falling edge (synchronous to the clock, no race).
  initial begin
    void'($value$plusargs("RESET_CYCLES=%d", reset_cycles));
    {rst} = {off};
    repeat (3) @(negedge {clk});
    {rst} = {on};
    repeat (reset_cycles) @(negedge {clk});
    {rst} = {off};
  end

  initial begin
    uvm_config_db#(virtual dut_pins_if)::set(null, "*", "pins_vif", pins);
    run_test();
  end
endmodule
'''


def filelist(c: VerificationContract, vip_lines: list[str] | None = None) -> str:
    dirs = [f"+incdir+./{d}".rstrip("/") for d in ("", "env", "sram", "adapters", "scoreboard", "coverage", "sequences", "tests")]
    return ("// Generated by Q3TUI (uvm_vip), paths relative to this file. Testbench plus the interface shell (not a DUT).\n"
            "// VIP files are added in milestone M4, the real DUT wrapper and RTL in milestone M6 (integration phase).\n"
            + "\n".join(dirs) + "\n"
            + "\n".join(vip_lines or []) + ("\n" if vip_lines else "")
            + "dut_tb_params_pkg.sv\ninterfaces/dut_pins_if.sv\ndut_dv_pkg.sv\nassertions/dut_assertions.sv\nintegration/dut_wrapper_shell.sv\n"
            + ("integration/dut_axi_bridge.sv\n" if vip_lines else "") + "tb_top.sv\n")


def generate(c: VerificationContract, arch: dict, opts: dict, project: str, author: str, day: str, vip: dict | None = None) -> dict[str, str]:
    """Every scaffold file (path relative to the TB directory) -> text. Deterministic for the same contract."""
    h = c.metadata.contract_hash
    H = lambda desc: g.file_header(project, author, day, desc, h)  # noqa: E731
    from q3tui.flows.dv.uvm import vipgen

    files = {
        "dut_tb_params_pkg.sv": params_pkg(c, H("Approved parameters of the DUT configuration.")),
        "interfaces/dut_pins_if.sv": pins_if(c, H("DUT pins interface.")),
        "integration/dut_wrapper_shell.sv": wrapper_shell(c, H("Interface shell for the pre-DUT compile.")),
        "dut_dv_base.svh": base_svh(H("DV_TODO announcement.")),
        "env/dut_txn.svh": axi_txn(c, H("AXI transaction (VIP independent).")) + "\n" + sram_txn(c, ""),
        "sram/dut_sram_agent.svh": sram_agent(c, H("SRAM side agent.")),
        "adapters/dut_axi_adapter.svh": adapters(c, arch, H("VIP adapter.")),
        "scoreboard/dut_scoreboard.svh": scoreboard(c, H("Spec-driven scoreboard.")),
        "coverage/dut_cov.svh": coverage(c, H("Functional coverage.")),
        "env/dut_env.svh": env(c, H("Environment.")),
        "sequences/dut_seq_lib.svh": sequences(c, H("Sequences.")),
        "tests/dut_test_lib.svh": tests(c, opts, H("Tests.")),
        "assertions/dut_assertions.sv": assertions(c, H("Assertion bind point.")),
        "dut_dv_pkg.sv": package(c, H("Package of the DUT testbench.")),
        "env/dut_axi_pin_monitor.svh": pin_monitor(c, H("AXI4 monitor on the DUT pins (VIP independent).")),
        f"{TB_TOP}.sv": tb_top(c, H("Testbench top.")),
        "filelist_tb.f": filelist(c),
    }
    if vip:  # the existing VIP is bound: derived constants, bridge, adapter, tb_top and filelist with the VIP
        probe, binding = vip["probe"], vip["binding"]
        files["adapters/dut_axi_adapter.svh"] = vipgen.adapter(probe, binding, H("VIP adapter."))
        files["integration/dut_axi_bridge.sv"] = vipgen.bridge(c, probe, binding, H("Bridge between the VIP interface and the DUT pins."))
        files[f"{TB_TOP}.sv"] = tb_top(c, H("Testbench top."), vipgen.tb_top_block(probe, vip["dir"]))
        files["filelist_tb.f"] = filelist(c, vipgen.filelist_lines(probe, binding, vip["root"]))
        for sub in binding.get("substitutions", []):
            files[sub["stand_in"]] = (vipgen.standin(sub, H("Stand-in for a VIP unit the simulator cannot compile.")) if sub.get("type") == "unit"
                                      else vipgen.standin_coverage(sub, probe, H("Stand-in for a VIP class that does not run on the simulator.")))
        if binding.get("override_package"):
            files["vip_cfg/axi4_globals_pkg.sv"] = vipgen.derived_globals(vip["dir"], probe, binding, H("Derived VIP configuration."))
    roles = param_roles(c)
    for k, t in files.items():
        for token, name in roles.items():
            t = t.replace(token, name)
        files[k] = t
    return files


def write(tb_dir, files: dict[str, str], recorded: dict[str, str]) -> dict:
    """Write the scaffold without ever losing engineer or agent work (DUT-DV-014). `recorded`: file -> SHA-256 of what was written last.
    A file that is still as written is replaced. A file that was changed: if only inside DV_REGION blocks, the regenerated file keeps
    those regions (owner agent/user are kept verbatim); if it was changed outside them, it is left alone and the new scaffold is put
    beside it as `<file>.scaffold_new`. Returns {written, merged: {file: [region ids]}, kept_edited, unchanged, sha}."""
    import hashlib

    out = {"written": [], "merged": {}, "kept_edited": [], "unchanged": [], "sha": {}}
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731
    for rel, new in files.items():
        path = tb_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = path.read_text()
            if old == new:
                out["unchanged"].append(rel)
                out["sha"][rel] = sha(new)
                continue
            merged, kept = g.merge(new, old)  # regions taken over by the agent or the engineer survive even when the file is as written
            if recorded.get(rel) != sha(old) and g.outside(old) != g.outside(new):  # changed outside the regions (or not ours)
                path.with_name(path.name + ".scaffold_new").write_text(new)
                out["kept_edited"].append(rel)
                out["sha"][rel] = recorded.get(rel, "")
                continue
            new = merged
            if kept:
                out["merged"][rel] = kept
            if new == old:
                out["unchanged"].append(rel)
                out["sha"][rel] = sha(new)
                continue
        path.write_text(new)
        out["written"].append(rel)
        out["sha"][rel] = sha(new)
    return out
