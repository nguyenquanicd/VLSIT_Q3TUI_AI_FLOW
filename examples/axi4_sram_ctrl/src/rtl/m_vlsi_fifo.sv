/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Parameterised synchronous FIFO with extra-MSB full/empty detection. Used for
 *                the AW, W, AR, R and B channel buffers (u_vlsi_fifo_0..4); the head entry is
 *                read combinationally from storage.
 * REQ-IDs: REQ-001, REQ-002, REQ-011, REQ-012, REQ-023, REQ-029, REQ-030, REQ-031, REQ-032,
 *          REQ-041, REQ-042, REQ-050, REQ-053, REQ-060
 */
`timescale 1ns/1ps

module m_vlsi_fifo #(
  // Parameter Description: width of one FIFO entry; set by the parent per channel
  //   (AWFIFO/ARFIFO addr+id+2, WFIFO data+strb, RFIFO data+id+3, BFIFO id+2).
  // Parameter values: legal values are 1 or more; default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_DATA_WD = 32,
  // Parameter Description: log2 of the number of entries (pointer address width);
  //   the FIFO holds 2**PARA_DEPTH entries. The parent passes $clog2(PR_FIFO_DEPTH).
  // Parameter values: legal values are 1 or more (2 entries or more); default is 4.
  // Parameter unit: count
  parameter logic [31:0] PARA_DEPTH   = 4,
  localparam logic [31:0] LPARA_ADDR_WD    = PARA_DEPTH,
  localparam logic [31:0] LPARA_DEPTH_SIZE = 32'd1 << PARA_DEPTH
) (
  // Clock and reset: single i_clk domain, active-low reset with asynchronous assertion.
  input  wire                      i_clk_core,
  input  wire                      i_rst_n_core,   // REQ-001, REQ-002

  // Write port: push request and entry data.
  input  wire  [PARA_DATA_WD-1:0]  i_data,
  input  wire                      i_push,

  // Read port: pop request and combinational head entry.
  input  wire                      i_pop,
  output logic [PARA_DATA_WD-1:0]  o_data_out,     // REQ-011, REQ-012, REQ-029, REQ-030

  // Status flags from the registered pointers (current-cycle state).
  output logic                     o_empty,
  output logic                     o_full          // REQ-031, REQ-050
);

  // Elaboration check: at least 2 entries (PR_FIFO_DEPTH >= 2, power of 2 by construction).
  if (PARA_DEPTH < 32'd1) begin : gen_chk_depth
    $error("m_vlsi_fifo: PARA_DEPTH must be >= 1 (FIFO depth >= 2)");
  end  // REQ-060

  // Internal signal declarations
  // Storage array, one PARA_DATA_WD entry per slot (width set by the parent, REQ-023).
  logic [LPARA_DEPTH_SIZE-1:0][PARA_DATA_WD-1:0] reg_mem;

  // Read/write pointers with one extra MSB so they wrap modulo 2*depth.
  logic [LPARA_ADDR_WD:0] reg_wr_ptr;
  logic [LPARA_ADDR_WD:0] reg_rd_ptr;

  // Next pointer values.
  logic [LPARA_ADDR_WD:0] w_wr_ptr_nxt;
  logic [LPARA_ADDR_WD:0] w_rd_ptr_nxt;

  // Qualified push/pop: push is ignored while full, pop is ignored while empty.
  logic                   w_push_en;
  logic                   w_pop_en;

  // Push/pop qualification: a push to a full FIFO is dropped even with a same-cycle pop,
  // a pop of an empty FIFO is dropped while a same-cycle push still happens.
  assign w_push_en = i_push & ~o_full;
  assign w_pop_en  = i_pop  & ~o_empty;  // REQ-032, REQ-050

  // Next pointers: each pointer advances by one per accepted push / pop.
  assign w_wr_ptr_nxt = w_push_en ? $bits(w_wr_ptr_nxt)'(reg_wr_ptr + 1'b1) : reg_wr_ptr;
  assign w_rd_ptr_nxt = w_pop_en  ? $bits(w_rd_ptr_nxt)'(reg_rd_ptr + 1'b1) : reg_rd_ptr;  // REQ-031

  // Head entry: read combinationally from storage at the read pointer; a pushed entry is
  // visible from the cycle after the push.
  assign o_data_out = reg_mem[reg_rd_ptr[LPARA_ADDR_WD-1:0]];  // REQ-011, REQ-012, REQ-032

  // Full / empty flags: full when the MSBs differ and the lower bits are equal,
  // empty when the pointers are equal.
  assign o_full  = (reg_wr_ptr[LPARA_ADDR_WD] != reg_rd_ptr[LPARA_ADDR_WD]) &
                   (reg_wr_ptr[LPARA_ADDR_WD-1:0] == reg_rd_ptr[LPARA_ADDR_WD-1:0]);
  assign o_empty = (reg_wr_ptr == reg_rd_ptr);  // REQ-031, REQ-011, REQ-012

  // Write pointer: cleared on reset (FIFO empty), then follows the next value.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_wr_ptr
    if (!i_rst_n_core)
      reg_wr_ptr <= '0;
    else
      reg_wr_ptr <= w_wr_ptr_nxt;
  end  // REQ-001, REQ-002, REQ-031, REQ-041

  // Read pointer: cleared on reset (FIFO empty), then follows the next value.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_rd_ptr
    if (!i_rst_n_core)
      reg_rd_ptr <= '0;
    else
      reg_rd_ptr <= w_rd_ptr_nxt;
  end  // REQ-001, REQ-002, REQ-031, REQ-041

  // Storage: cleared on reset so the head reads 0 after reset; an accepted push writes the
  // entry at the write pointer. Entries keep their order, so responses and IDs stay in
  // acceptance order (the B response is visible one cycle after its push).
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_mem
    if (!i_rst_n_core)
      reg_mem <= '0;
    else if (w_push_en)
      reg_mem[reg_wr_ptr[LPARA_ADDR_WD-1:0]] <= i_data;
  end  // REQ-001, REQ-002, REQ-023, REQ-029, REQ-030, REQ-042, REQ-053

endmodule : m_vlsi_fifo
