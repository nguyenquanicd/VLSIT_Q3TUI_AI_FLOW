/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Structural wrapper instantiating u_vlsi_axi_fifopack (m_vlsi_axi_fifopack, FIFO
 *                pack/unpack), u_vlsi_bank_arb (m_vlsi_bank_arb, requests, bank hash, issue and
 *                FIFO control) and u_vlsi_sram_fanout (m_vlsi_sram_fanout, SRAM fan-out and read
 *                pipeline); the split causes no change in behaviour or timing.
 * REQ-IDs: REQ-023, REQ-051
 */
`timescale 1ns/1ps

module m_vlsi_sram_misc #(
  // Parameter Description: AXI/SRAM data width (PR_DATA_WD); must be a multiple of 8.
  // Parameter values: legal values are 8, 16, 32, ..., 1024; default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_DATA_WD   = 32,
  // Parameter Description: AXI byte-address width (PR_ADDR_WD).
  // Parameter values: legal values are greater than $clog2(PARA_DATA_WD/8); default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ADDR_WD   = 32,
  // Parameter Description: AXI ID width (PR_ID_WD).
  // Parameter values: legal values are 1 or more; default is 4.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ID_WD     = 4,
  // Parameter Description: number of parallel SRAM banks (PR_NUM_BANKS); 1 = single SRAM.
  // Parameter values: legal values are powers of 2, 1 or more; default is 1.
  // Parameter unit: count
  parameter logic [31:0] PARA_NUM_BANKS = 1,
  localparam logic [31:0] LPARA_STRB_WD = PARA_DATA_WD / 32'd8,
  localparam logic [31:0] LPARA_BANK_WD = (PARA_NUM_BANKS <= 32'd1) ? 32'd1
                                                                    : $clog2(PARA_NUM_BANKS)
) (
  // Clock and reset (single i_clk domain), used by the read pipeline in m_vlsi_sram_fanout.
  input  wire                                          i_clk_core,
  input  wire                                          i_rst_n_core,

  // AXI-side handshake inputs.
  input  wire                                          i_wvalid,
  input  wire                                          i_rready,
  input  wire                                          i_bready,

  // Address-FSM beat outputs (AW/AR FIFO input packing).
  input  wire  [PARA_ADDR_WD-1:0]                      i_axaddr_wr,
  input  wire  [PARA_ID_WD-1:0]                        i_id_wr,
  input  wire                                          i_awlast_wr,
  input  wire                                          i_err_wr,
  input  wire  [PARA_ADDR_WD-1:0]                      i_axaddr_rd,
  input  wire  [PARA_ID_WD-1:0]                        i_id_rd,
  input  wire                                          i_arlast,
  input  wire                                          i_err_rd,

  // Arbiter result inputs.
  input  wire                                          i_arb_sel,
  input  wire                                          i_arb_write_en,

  // FIFO status and head-data inputs.
  input  wire  [PARA_ADDR_WD+PARA_ID_WD+1:0]           i_awfifo_data_out,
  input  wire                                          i_awfifo_empty,
  input  wire  [PARA_DATA_WD+LPARA_STRB_WD-1:0]        i_wfifo_data_out,
  input  wire                                          i_wfifo_empty,
  input  wire                                          i_wfifo_full,
  input  wire  [PARA_ADDR_WD+PARA_ID_WD+1:0]           i_arfifo_data_out,
  input  wire                                          i_arfifo_empty,
  input  wire  [PARA_DATA_WD+PARA_ID_WD+2:0]           i_rfifo_data_out,
  input  wire                                          i_rfifo_empty,
  input  wire                                          i_rfifo_full,
  input  wire  [PARA_ID_WD+1:0]                        i_bfifo_data_out,
  input  wire                                          i_bfifo_empty,
  input  wire                                          i_bfifo_full,

  // SRAM read data, one word per bank.
  input  wire  [PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]  i_sram_rdata,

  // FIFO control and input-entry outputs.
  output logic [PARA_ADDR_WD+PARA_ID_WD+1:0]           o_awfifo_data_in,
  output logic                                         o_awfifo_pop,
  output logic                                         o_wfifo_push,
  output logic                                         o_wfifo_pop,
  output logic [PARA_ADDR_WD+PARA_ID_WD+1:0]           o_arfifo_data_in,
  output logic                                         o_arfifo_pop,
  output logic [PARA_DATA_WD+PARA_ID_WD+2:0]           o_rfifo_data_in,
  output logic                                         o_rfifo_push,
  output logic                                         o_rfifo_pop,
  output logic [PARA_ID_WD+1:0]                        o_bfifo_data_in,
  output logic                                         o_bfifo_push,
  output logic                                         o_bfifo_pop,

  // Requests to the shared arbiter.
  output logic                                         o_req_write,
  output logic                                         o_req_read,

  // AXI R/B channel outputs and W ready.
  output logic [PARA_ID_WD-1:0]                        o_rid,
  output logic [PARA_DATA_WD-1:0]                      o_rdata,
  output logic [1:0]                                   o_rresp,
  output logic                                         o_rvalid,
  output logic                                         o_rlast,
  output logic [PARA_ID_WD-1:0]                        o_bid,
  output logic [1:0]                                   o_bresp,
  output logic                                         o_bvalid,
  output logic                                         o_wready,

  // SRAM outputs, one packed set per bank.
  output logic [PARA_NUM_BANKS-1:0][PARA_ADDR_WD-1:0]  o_sram_addr,
  output logic [PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]  o_sram_wdata,
  output logic [PARA_NUM_BANKS-1:0][LPARA_STRB_WD-1:0] o_sram_wstrb,
  output logic [PARA_NUM_BANKS-1:0]                    o_sram_we,
  output logic [PARA_NUM_BANKS-1:0]                    o_sram_oe        // REQ-023
);

  // Internal signal declarations (inter-submodule wiring, widths derived from the parameters).
  logic [PARA_ADDR_WD-1:0]  w_wr_addr_field;
  logic [PARA_ADDR_WD-1:0]  w_rd_addr_field;
  logic [PARA_DATA_WD-1:0]  w_wr_wdata_field;
  logic [LPARA_STRB_WD-1:0] w_wr_wstrb_field;
  logic                     w_wr_last_field;
  logic                     w_wr_err_field;
  logic                     w_rd_last_field;
  logic                     w_rd_err_field;

  logic [LPARA_BANK_WD-1:0] w_bank_sel_wr;
  logic [LPARA_BANK_WD-1:0] w_bank_sel_rd;
  logic                     w_wr_issue;
  logic                     w_rd_issue;

  logic                     w_reg_rd_pending;
  logic [PARA_ID_WD-1:0]    w_reg_rd_id_d;
  logic                     w_reg_rd_last_d;
  logic                     w_reg_rd_err_d;
  logic [LPARA_BANK_WD-1:0] w_reg_rd_bank_d;  // REQ-023

  // FIFO pack/unpack: AW/AR FIFO input packing, AW/AR/W head unpack, R/B FIFO input packing
  // (err -> SLVERR, read-bank data mux) and R/B head unpack onto the AXI outputs.
  m_vlsi_axi_fifopack #(
    .PARA_DATA_WD      (PARA_DATA_WD),
    .PARA_ADDR_WD      (PARA_ADDR_WD),
    .PARA_ID_WD        (PARA_ID_WD),
    .PARA_NUM_BANKS    (PARA_NUM_BANKS)
  ) u_vlsi_axi_fifopack (
    .i_axaddr_wr       (i_axaddr_wr),
    .i_id_wr           (i_id_wr),
    .i_awlast_wr       (i_awlast_wr),
    .i_err_wr          (i_err_wr),
    .i_axaddr_rd       (i_axaddr_rd),
    .i_id_rd           (i_id_rd),
    .i_arlast          (i_arlast),
    .i_err_rd          (i_err_rd),
    .i_awfifo_data_out (i_awfifo_data_out),
    .i_wfifo_data_out  (i_wfifo_data_out),
    .i_arfifo_data_out (i_arfifo_data_out),
    .i_rfifo_data_out  (i_rfifo_data_out),
    .i_rfifo_empty     (i_rfifo_empty),
    .i_bfifo_data_out  (i_bfifo_data_out),
    .i_bfifo_empty     (i_bfifo_empty),
    .i_sram_rdata      (i_sram_rdata),
    .i_reg_rd_bank_d   (w_reg_rd_bank_d),
    .i_reg_rd_id_d     (w_reg_rd_id_d),
    .i_reg_rd_last_d   (w_reg_rd_last_d),
    .i_reg_rd_err_d    (w_reg_rd_err_d),
    .o_awfifo_data_in  (o_awfifo_data_in),
    .o_arfifo_data_in  (o_arfifo_data_in),
    .o_rfifo_data_in   (o_rfifo_data_in),
    .o_bfifo_data_in   (o_bfifo_data_in),
    .o_wr_addr_field   (w_wr_addr_field),
    .o_rd_addr_field   (w_rd_addr_field),
    .o_wr_wdata_field  (w_wr_wdata_field),
    .o_wr_wstrb_field  (w_wr_wstrb_field),
    .o_wr_last_field   (w_wr_last_field),
    .o_wr_err_field    (w_wr_err_field),
    .o_rd_last_field   (w_rd_last_field),
    .o_rd_err_field    (w_rd_err_field),
    .o_rid             (o_rid),
    .o_rdata           (o_rdata),
    .o_rresp           (o_rresp),
    .o_rvalid          (o_rvalid),
    .o_rlast           (o_rlast),
    .o_bid             (o_bid),
    .o_bresp           (o_bresp),
    .o_bvalid          (o_bvalid)
  );  // REQ-051

  // Decision core: request generation, bank hash, issue decision, FIFO push/pop and o_wready.
  m_vlsi_bank_arb #(
    .PARA_ADDR_WD      (PARA_ADDR_WD),
    .PARA_DATA_WD      (PARA_DATA_WD),
    .PARA_NUM_BANKS    (PARA_NUM_BANKS)
  ) u_vlsi_bank_arb (
    .i_wvalid          (i_wvalid),
    .i_rready          (i_rready),
    .i_bready          (i_bready),
    .i_arb_sel         (i_arb_sel),
    .i_arb_write_en    (i_arb_write_en),
    .i_awfifo_empty    (i_awfifo_empty),
    .i_wfifo_empty     (i_wfifo_empty),
    .i_wfifo_full      (i_wfifo_full),
    .i_arfifo_empty    (i_arfifo_empty),
    .i_rfifo_empty     (i_rfifo_empty),
    .i_rfifo_full      (i_rfifo_full),
    .i_bfifo_empty     (i_bfifo_empty),
    .i_bfifo_full      (i_bfifo_full),
    .i_wr_addr_field   (w_wr_addr_field),
    .i_rd_addr_field   (w_rd_addr_field),
    .i_wr_last_field   (w_wr_last_field),
    .i_reg_rd_pending  (w_reg_rd_pending),
    .o_awfifo_pop      (o_awfifo_pop),
    .o_wfifo_push      (o_wfifo_push),
    .o_wfifo_pop       (o_wfifo_pop),
    .o_arfifo_pop      (o_arfifo_pop),
    .o_rfifo_push      (o_rfifo_push),
    .o_rfifo_pop       (o_rfifo_pop),
    .o_bfifo_push      (o_bfifo_push),
    .o_bfifo_pop       (o_bfifo_pop),
    .o_wready          (o_wready),
    .o_req_write       (o_req_write),
    .o_req_read        (o_req_read),
    .o_bank_sel_wr     (w_bank_sel_wr),
    .o_bank_sel_rd     (w_bank_sel_rd),
    .o_w_wr_issue      (w_wr_issue),
    .o_w_rd_issue      (w_rd_issue)
  );  // REQ-051

  // SRAM fan-out and the one-cycle read pipeline register (the only clocked logic here).
  m_vlsi_sram_fanout #(
    .PARA_DATA_WD      (PARA_DATA_WD),
    .PARA_ADDR_WD      (PARA_ADDR_WD),
    .PARA_ID_WD        (PARA_ID_WD),
    .PARA_NUM_BANKS    (PARA_NUM_BANKS)
  ) u_vlsi_sram_fanout (
    .i_clk_core        (i_clk_core),
    .i_rst_n_core      (i_rst_n_core),
    .i_w_wr_issue      (w_wr_issue),
    .i_w_rd_issue      (w_rd_issue),
    .i_bank_sel_wr     (w_bank_sel_wr),
    .i_bank_sel_rd     (w_bank_sel_rd),
    .i_wr_addr_field   (w_wr_addr_field),
    .i_rd_addr_field   (w_rd_addr_field),
    .i_wr_wdata_field  (w_wr_wdata_field),
    .i_wr_wstrb_field  (w_wr_wstrb_field),
    .i_wr_err_field    (w_wr_err_field),
    .i_rd_last_field   (w_rd_last_field),
    .i_rd_err_field    (w_rd_err_field),
    .i_arfifo_data_out (i_arfifo_data_out),
    .o_sram_addr       (o_sram_addr),
    .o_sram_wdata      (o_sram_wdata),
    .o_sram_wstrb      (o_sram_wstrb),
    .o_sram_we         (o_sram_we),
    .o_sram_oe         (o_sram_oe),
    .o_reg_rd_pending  (w_reg_rd_pending),
    .o_reg_rd_id_d     (w_reg_rd_id_d),
    .o_reg_rd_last_d   (w_reg_rd_last_d),
    .o_reg_rd_err_d    (w_reg_rd_err_d),
    .o_reg_rd_bank_d   (w_reg_rd_bank_d)
  );  // REQ-051

endmodule : m_vlsi_sram_misc
