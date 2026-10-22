/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Request generation, bank XOR hash, grant/issue decision, FIFO push/pop
 *                control and o_wready. Instance u_vlsi_bank_arb in m_vlsi_sram_misc; the SRAM
 *                fan-out only acts on its issue and bank-select outputs. Purely
 *                combinational, so every decision is made in the issue cycle.
 * REQ-IDs: REQ-010, REQ-011, REQ-012, REQ-014, REQ-018, REQ-023, REQ-027, REQ-034, REQ-035,
 *          REQ-036, REQ-037, REQ-038, REQ-039, REQ-045, REQ-046, REQ-047, REQ-048, REQ-050,
 *          REQ-052, REQ-053, REQ-054, REQ-055, REQ-056, REQ-066
 */
`timescale 1ns/1ps

module m_vlsi_bank_arb #(
  // Parameter Description: AXI byte-address width (PR_ADDR_WD); with several banks it must be
  //   at least LPARA_BYTE_LSB + LPARA_BANK_WD so both hash fields exist.
  // Parameter values: legal values are greater than $clog2(PARA_DATA_WD/8); default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ADDR_WD   = 32,
  // Parameter Description: AXI/SRAM data width (PR_DATA_WD); sets the byte offset of the hash.
  // Parameter values: legal values are 8, 16, 32, ..., 1024; default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_DATA_WD   = 32,
  // Parameter Description: number of parallel SRAM banks (PR_NUM_BANKS). 1 = single-SRAM
  //   behaviour (bank select is a constant 0 and the original grant expressions are used).
  // Parameter values: legal values are powers of 2, 1 or more; default is 1.
  // Parameter unit: count
  parameter logic [31:0] PARA_NUM_BANKS = 1,
  localparam logic [31:0] LPARA_BANK_WD  = (PARA_NUM_BANKS <= 32'd1) ? 32'd1
                                                                     : $clog2(PARA_NUM_BANKS),
  localparam logic [31:0] LPARA_BYTE_LSB = $clog2(PARA_DATA_WD / 32'd8)
) (
  // AXI-side handshake inputs.
  input  wire                      i_wvalid,
  input  wire                      i_rready,
  input  wire                      i_bready,

  // Arbiter result inputs from the shared arbiter instance u_vlsi_arbiter.
  input  wire                      i_arb_sel,
  input  wire                      i_arb_write_en,

  // FIFO status inputs (current-cycle flags from the registered pointers).
  input  wire                      i_awfifo_empty,
  input  wire                      i_wfifo_empty,
  input  wire                      i_wfifo_full,
  input  wire                      i_arfifo_empty,
  input  wire                      i_rfifo_empty,
  input  wire                      i_rfifo_full,
  input  wire                      i_bfifo_empty,
  input  wire                      i_bfifo_full,

  // Unpacked FIFO-head fields (from m_vlsi_axi_fifopack).
  input  wire  [PARA_ADDR_WD-1:0]  i_wr_addr_field,
  input  wire  [PARA_ADDR_WD-1:0]  i_rd_addr_field,
  input  wire                      i_wr_last_field,   // REQ-023

  // Read-pending status (from the read pipeline register in m_vlsi_sram_fanout).
  input  wire                      i_reg_rd_pending,

  // FIFO control outputs.
  output logic                     o_awfifo_pop,
  output logic                     o_wfifo_push,
  output logic                     o_wfifo_pop,
  output logic                     o_arfifo_pop,
  output logic                     o_rfifo_push,
  output logic                     o_rfifo_pop,
  output logic                     o_bfifo_push,
  output logic                     o_bfifo_pop,
  output logic                     o_wready,

  // Request outputs to the shared arbiter (fed every cycle in every bank configuration).
  output logic                     o_req_write,
  output logic                     o_req_read,        // REQ-034

  // Bank-select and issue outputs (consumed by m_vlsi_sram_fanout).
  output logic [LPARA_BANK_WD-1:0] o_bank_sel_wr,
  output logic [LPARA_BANK_WD-1:0] o_bank_sel_rd,
  output logic                     o_w_wr_issue,
  output logic                     o_w_rd_issue
);

  // Elaboration checks: bank count is a power of 2 (C4), and with several banks the address is
  // wide enough for both hash fields (C5).
  if ((PARA_NUM_BANKS < 32'd1) || ((PARA_NUM_BANKS & (PARA_NUM_BANKS - 32'd1)) != 32'd0))
  begin : gen_chk_num_banks
    $error("m_vlsi_bank_arb: PARA_NUM_BANKS must be a power of 2, 1 or more");
  end
  if ((PARA_NUM_BANKS > 32'd1) && (PARA_ADDR_WD < (LPARA_BYTE_LSB + LPARA_BANK_WD)))
  begin : gen_chk_addr_wd
    $error("m_vlsi_bank_arb: PARA_ADDR_WD too small for the bank hash");
  end

  // Request generation: a write needs both AW and W heads; a burst's last beat is held off
  // while BFIFO is full (a same-cycle BFIFO pop does not release it) so its BRESP always has
  // room. A read needs an ARFIFO head, RFIFO room and no read in flight, which limits reads to
  // one in flight and at most one issue every 2 cycles. Writes can issue every cycle.
  assign o_req_write = ~i_awfifo_empty & ~i_wfifo_empty & (~i_wr_last_field | ~i_bfifo_full);
  // REQ-038, REQ-050, REQ-056, REQ-052
  assign o_req_read  = ~i_arfifo_empty & ~i_rfifo_full & ~i_reg_rd_pending;
  // REQ-039, REQ-050, REQ-055

  // Bank selection and issue decision.
  // PARA_NUM_BANKS = 1 is its own generate branch that reuses the ORIGINAL mutual-exclusion
  // grant expressions verbatim. An earlier single-formula version, hand-proven equivalent at
  // one bank, corrupted data in regression (alternating_rw_burst_len, id_reuse_pending,
  // read_modify_write_hazard, zero_delay_flood); the cause was never found, so the original
  // text is kept for the default configuration.
  // PARA_NUM_BANKS > 1: the bank index is the low index bits above the byte offset XORed with
  // the top address bits, so strides equal to the bank count do not pin one bank. A write and
  // a read only arbitrate when they hash to the same bank; otherwise both issue in the cycle.
  if (PARA_NUM_BANKS <= 32'd1) begin : gen_single_bank
    // The head addresses are not needed with one bank (bank select is constant 0); fold them
    // into a sink so the inputs are consumed.
    logic w_unused_addr;
    assign w_unused_addr = ^{i_wr_addr_field, i_rd_addr_field};

    assign o_bank_sel_wr = '0;
    assign o_bank_sel_rd = '0;
    assign o_w_wr_issue  = i_arb_write_en;
    assign o_w_rd_issue  = ~i_arb_write_en & i_arb_sel;
    // REQ-037, REQ-018
  end else begin : gen_multi_bank
    logic w_bank_conflict;

    assign o_bank_sel_wr = i_wr_addr_field[LPARA_BYTE_LSB +: LPARA_BANK_WD] ^
                           i_wr_addr_field[PARA_ADDR_WD-1 -: LPARA_BANK_WD];
    assign o_bank_sel_rd = i_rd_addr_field[LPARA_BYTE_LSB +: LPARA_BANK_WD] ^
                           i_rd_addr_field[PARA_ADDR_WD-1 -: LPARA_BANK_WD];
    // REQ-035
    assign w_bank_conflict = o_req_write & o_req_read & (o_bank_sel_wr == o_bank_sel_rd);
    assign o_w_wr_issue    = o_req_write & (w_bank_conflict ? i_arb_write_en : 1'b1);
    assign o_w_rd_issue    = o_req_read  & (w_bank_conflict ? i_arb_sel      : 1'b1);
    // REQ-036, REQ-018
  end  // REQ-066

  // Address/data FIFO pops: an issued write pops AWFIFO and WFIFO together (strict FIFO
  // pairing, also for SLVERR beats); an issued read pops ARFIFO.
  assign o_awfifo_pop = o_w_wr_issue;
  assign o_wfifo_pop  = o_w_wr_issue;
  assign o_arfifo_pop = o_w_rd_issue;  // REQ-045, REQ-047, REQ-027, REQ-052

  // R channel: the read pipeline pushes RFIFO in the cycle after the read issue (t+1), so
  // o_rvalid follows in t+2; RFIFO pops only on the R handshake.
  assign o_rfifo_push = i_reg_rd_pending;
  assign o_rfifo_pop  = i_rready & ~i_rfifo_empty;  // REQ-048, REQ-054, REQ-012, REQ-014, REQ-050

  // B channel: one BFIFO push per burst, on the issue of its last beat (also for SLVERR
  // bursts), so o_bvalid follows in the next cycle; BFIFO pops only on the B handshake.
  assign o_bfifo_push = o_w_wr_issue & i_wr_last_field;
  assign o_bfifo_pop  = i_bready & ~i_bfifo_empty;  // REQ-046, REQ-053, REQ-011, REQ-014, REQ-027

  // W channel: o_wready is NOT(WFIFO full) and does not depend on i_wvalid; a W beat is
  // pushed on the handshake, independent of the AW channel.
  assign o_wready     = ~i_wfifo_full;
  assign o_wfifo_push = i_wvalid & o_wready;  // REQ-010, REQ-050

endmodule : m_vlsi_bank_arb
