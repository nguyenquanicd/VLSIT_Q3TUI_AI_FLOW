/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Per-bank SRAM control/data fan-out (we/wstrb gating for SLVERR) and the
 *                one-cycle read pipeline register, which remembers the id/last/err and bank of
 *                the pending read (SRAM read latency is 1 cycle). Instance u_vlsi_sram_fanout
 *                in m_vlsi_sram_misc.
 * REQ-IDs: REQ-001, REQ-002, REQ-008, REQ-018, REQ-023, REQ-024, REQ-025, REQ-027, REQ-028,
 *          REQ-035, REQ-039, REQ-041, REQ-042, REQ-045, REQ-047, REQ-048, REQ-049, REQ-054,
 *          REQ-055, REQ-066
 */
`timescale 1ns/1ps

module m_vlsi_sram_fanout #(
  // Parameter Description: AXI/SRAM data width (PR_DATA_WD); must be a multiple of 8.
  // Parameter values: legal values are 8, 16, 32, ..., 1024; default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_DATA_WD   = 32,
  // Parameter Description: AXI byte-address width (PR_ADDR_WD), also the o_sram_addr width.
  // Parameter values: legal values are greater than $clog2(PARA_DATA_WD/8); default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ADDR_WD   = 32,
  // Parameter Description: AXI ID width (PR_ID_WD) of the ARFIFO entry id field.
  // Parameter values: legal values are 1 or more; default is 4.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ID_WD     = 4,
  // Parameter Description: number of SRAM banks (PR_NUM_BANKS); 1 = single-SRAM interface.
  // Parameter values: legal values are powers of 2, 1 or more; default is 1.
  // Parameter unit: count
  parameter logic [31:0] PARA_NUM_BANKS = 1,
  localparam logic [31:0] LPARA_STRB_WD = PARA_DATA_WD / 32'd8,
  localparam logic [31:0] LPARA_BANK_WD = (PARA_NUM_BANKS <= 32'd1) ? 32'd1
                                                                    : $clog2(PARA_NUM_BANKS)
) (
  // Clock and reset: single i_clk domain, active-low reset with asynchronous assertion.
  input  wire                                          i_clk_core,
  input  wire                                          i_rst_n_core,   // REQ-001, REQ-002

  // Issue / bank-select inputs (from m_vlsi_bank_arb).
  input  wire                                          i_w_wr_issue,
  input  wire                                          i_w_rd_issue,
  input  wire  [LPARA_BANK_WD-1:0]                     i_bank_sel_wr,
  input  wire  [LPARA_BANK_WD-1:0]                     i_bank_sel_rd,

  // Unpacked FIFO-head fields (from m_vlsi_axi_fifopack).
  input  wire  [PARA_ADDR_WD-1:0]                      i_wr_addr_field,
  input  wire  [PARA_ADDR_WD-1:0]                      i_rd_addr_field,
  input  wire  [PARA_DATA_WD-1:0]                      i_wr_wdata_field,
  input  wire  [LPARA_STRB_WD-1:0]                     i_wr_wstrb_field,
  input  wire                                          i_wr_err_field,
  input  wire                                          i_rd_last_field,
  input  wire                                          i_rd_err_field,

  // ARFIFO head entry {last, err, id, addr}; its id field is captured for the read pipeline.
  input  wire  [PARA_ADDR_WD+PARA_ID_WD+1:0]           i_arfifo_data_out,

  // SRAM outputs, one packed set per bank (bit-for-bit single SRAM when PR_NUM_BANKS = 1).
  output logic [PARA_NUM_BANKS-1:0][PARA_ADDR_WD-1:0]  o_sram_addr,
  output logic [PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]  o_sram_wdata,
  output logic [PARA_NUM_BANKS-1:0][LPARA_STRB_WD-1:0] o_sram_wstrb,
  output logic [PARA_NUM_BANKS-1:0]                    o_sram_we,
  output logic [PARA_NUM_BANKS-1:0]                    o_sram_oe,      // REQ-008, REQ-023

  // Read pipeline register outputs (to m_vlsi_bank_arb and m_vlsi_axi_fifopack).
  output logic                                         o_reg_rd_pending,
  output logic [PARA_ID_WD-1:0]                        o_reg_rd_id_d,
  output logic                                         o_reg_rd_last_d,
  output logic                                         o_reg_rd_err_d,
  output logic [LPARA_BANK_WD-1:0]                     o_reg_rd_bank_d
);

  // Elaboration checks: whole-byte data width (C1) and power-of-2 bank count (C4).
  if ((PARA_DATA_WD < 32'd8) || ((PARA_DATA_WD % 32'd8) != 32'd0)) begin : gen_chk_data_wd
    $error("m_vlsi_sram_fanout: PARA_DATA_WD must be a multiple of 8, 8 or more");
  end
  if ((PARA_NUM_BANKS < 32'd1) || ((PARA_NUM_BANKS & (PARA_NUM_BANKS - 32'd1)) != 32'd0))
  begin : gen_chk_num_banks
    $error("m_vlsi_sram_fanout: PARA_NUM_BANKS must be a power of 2, 1 or more");
  end

  // Per-bank SRAM fan-out (combinational from the FIFO heads and the issue decision).
  // - Write: only the bank selected by i_bank_sel_wr gets o_sram_we; a SLVERR beat
  //   (i_wr_err_field) blocks the write but still pops its FIFOs upstream.
  // - Read: only the bank selected by i_bank_sel_rd gets o_sram_oe, also for SLVERR beats.
  // - Strobe: WFIFO-head strobe only while that bank's o_sram_we is 1, all-zero otherwise
  //   (non-selected banks, SLVERR beats, read and idle cycles); no other byte masking.
  // - Address: full un-hashed beat address of the active access, 0 for an idle bank.
  // - Write data: full word to every bank, meaningful only while o_sram_we is 1.
  always_comb begin : p_com_sram_fanout
    o_sram_we    = '0;
    o_sram_oe    = '0;
    o_sram_wdata = '0;
    o_sram_wstrb = '0;
    o_sram_addr  = '0;
    for (int idx = 0; idx < PARA_NUM_BANKS; idx++) begin
      o_sram_we[idx]    = i_w_wr_issue & ~i_wr_err_field &
                          (i_bank_sel_wr == LPARA_BANK_WD'(idx));
      // REQ-045, REQ-027, REQ-035
      o_sram_oe[idx]    = i_w_rd_issue & (i_bank_sel_rd == LPARA_BANK_WD'(idx));
      // REQ-047, REQ-028, REQ-018
      o_sram_wdata[idx] = i_wr_wdata_field;
      // REQ-024
      o_sram_wstrb[idx] = o_sram_we[idx] ? i_wr_wstrb_field : '0;
      // REQ-025, REQ-027
      o_sram_addr[idx]  = o_sram_we[idx] ? i_wr_addr_field :
                          (o_sram_oe[idx] ? i_rd_addr_field : '0);
      // REQ-049, REQ-035
    end
  end  // REQ-066, REQ-041

  // Read pipeline register: set in the cycle after a read issue (t+1) so RFIFO is pushed with
  // the SRAM data sampled then; it clears at the end of t+1 because no read can issue while it
  // is set, which keeps one read in flight and at most one read issue every 2 cycles.
  // The id/last/err/bank of the issued beat are captured with it. All cleared on reset.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_rd_pipe
    if (!i_rst_n_core) begin
      o_reg_rd_pending <= 1'b0;
      o_reg_rd_id_d    <= '0;
      o_reg_rd_last_d  <= 1'b0;
      o_reg_rd_err_d   <= 1'b0;
      o_reg_rd_bank_d  <= '0;
    end else begin
      o_reg_rd_pending <= i_w_rd_issue;
      if (i_w_rd_issue) begin
        o_reg_rd_id_d   <= i_arfifo_data_out[PARA_ADDR_WD+PARA_ID_WD-1:PARA_ADDR_WD];
        o_reg_rd_last_d <= i_rd_last_field;
        o_reg_rd_err_d  <= i_rd_err_field;
        o_reg_rd_bank_d <= i_bank_sel_rd;
      end
    end
  end  // REQ-001, REQ-002, REQ-039, REQ-041, REQ-042, REQ-047, REQ-048, REQ-054, REQ-055

endmodule : m_vlsi_sram_fanout
