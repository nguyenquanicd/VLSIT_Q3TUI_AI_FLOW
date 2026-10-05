"""Combinational loops found statically (hdl/loops.py) and reported by the RTL check."""

from pathlib import Path

from q3tui.hdl.filelist import filelist_from_files
from q3tui.hdl.parse import parse_design

CHILD = """
module busy_unit(input logic i_valid, input logic i_stall, output logic o_req, output logic o_busy);
  assign o_req = i_valid && !i_stall;
  assign o_busy = o_req;
endmodule
"""


def _loops(tmp_path: Path, top_src: str, top: str = "top") -> list[dict]:
    (tmp_path / "busy_unit.sv").write_text(CHILD)
    (tmp_path / "top.sv").write_text(top_src)
    parsed = parse_design(filelist_from_files([tmp_path / "busy_unit.sv", tmp_path / "top.sv"]), top)
    assert parsed.ok, parsed.diagnostics
    return parsed.comb_loops


def test_a_loop_through_an_instance_and_the_parent_is_found(tmp_path):
    loops = _loops(tmp_path, """
module top(input logic clk, input logic v, output logic req);
  logic busy, stall;
  assign stall = busy;
  busy_unit u(.i_valid(v), .i_stall(stall), .o_req(req), .o_busy(busy));
endmodule
""")
    assert len(loops) == 1
    sig = loops[0]["signals"]
    assert {"top.stall", "top.busy", "top.u.o_req"} <= set(sig)
    assert any(f.endswith("busy_unit.sv") for f, _ in loops[0]["where"])


def test_registers_loop_variables_and_bits_of_one_vector_are_not_loops(tmp_path):
    loops = _loops(tmp_path, """
module top(input logic clk, input logic v, input logic [3:0] g, output logic req, output logic [3:0] b);
  logic busy, busy_q;
  always_ff @(posedge clk) busy_q <= busy;
  busy_unit u(.i_valid(v), .i_stall(busy_q), .o_req(req), .o_busy(busy));
  integer i;
  always_comb begin
    b[3] = g[3];
    for (i = 2; i >= 0; i = i - 1) b[i] = g[i] ^ b[i + 1];
  end
endmodule
""")
    assert loops == []
