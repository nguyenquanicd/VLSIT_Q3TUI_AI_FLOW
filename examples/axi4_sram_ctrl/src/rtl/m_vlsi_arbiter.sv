/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Shared round-robin (toggle) arbiter between the write and read SRAM requests.
 *                One shared instance (u_vlsi_arbiter in m_vlsi_axi4_sram) serves every bank
 *                configuration; the toggle flips once per cycle in which any request is granted.
 * REQ-IDs: REQ-001, REQ-002, REQ-033, REQ-034, REQ-041, REQ-066
 */
`timescale 1ns/1ps

module m_vlsi_arbiter (
  // Clock and reset: single i_clk domain, active-low reset with asynchronous assertion.
  input  wire   i_clk_core,
  input  wire   i_rst_n_core,     // REQ-001, REQ-002

  // Request inputs from the request-generation logic (m_vlsi_bank_arb).
  input  wire   i_req_write,
  input  wire   i_req_read,

  // Grant outputs: o_arb_sel = read granted, o_arb_write_en = write granted.
  output logic  o_arb_sel,
  output logic  o_arb_write_en    // REQ-033
);

  // Internal signal declarations
  logic  reg_arb_toggle;
  logic  w_arb_toggle_nxt;

  // Grant decision: a lone request is always granted; when both sides request,
  // the toggle selects write (0) or read (1). Purely combinational, one gate level,
  // so the issue path stays within one i_clk cycle at the 100 MHz target.
  assign o_arb_sel      = i_req_read  & (~i_req_write |  reg_arb_toggle);
  assign o_arb_write_en = i_req_write & (~i_req_read  | ~reg_arb_toggle);  // REQ-033, REQ-066

  // Toggle next state: invert once at the end of every cycle with at least one grant
  // (write or read), in every bank configuration; otherwise hold.
  assign w_arb_toggle_nxt = (o_arb_write_en | o_arb_sel) ? ~reg_arb_toggle : reg_arb_toggle;  // REQ-034

  // Toggle state: asynchronously cleared to 0 on reset so the first contended grant goes
  // to write; updated on the rising edge of i_clk_core only.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_arb_toggle
    if (!i_rst_n_core)
      reg_arb_toggle <= 1'b0;
    else
      reg_arb_toggle <= w_arb_toggle_nxt;
  end  // REQ-001, REQ-002, REQ-034, REQ-041

endmodule : m_vlsi_arbiter
