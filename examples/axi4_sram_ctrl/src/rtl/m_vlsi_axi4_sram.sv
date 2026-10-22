/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Top level: instantiates and wires all sub-modules (u_vlsi_axfsm_0/1,
 *                u_vlsi_fifo_0..4, u_vlsi_arbiter, u_vlsi_sram_misc); owns all ports of the
 *                AXI4 (full) slave to synchronous-SRAM bridge. Single clock domain i_clk
 *                (100 MHz target, checked in synthesis), active-low reset i_rst_n.
 *                Instance map: u_vlsi_axfsm_0 = AW FSM, u_vlsi_axfsm_1 = AR FSM,
 *                u_vlsi_fifo_0..4 = AWFIFO, WFIFO, ARFIFO, RFIFO, BFIFO.
 * REQ-IDs: REQ-001, REQ-002, REQ-003, REQ-004, REQ-005, REQ-006, REQ-007, REQ-008, REQ-009,
 *          REQ-015, REQ-016, REQ-017, REQ-019, REQ-023, REQ-030, REQ-040, REQ-041, REQ-051,
 *          REQ-058, REQ-059, REQ-060, REQ-061, REQ-062, REQ-063, REQ-064, REQ-065, REQ-066
 */
`timescale 1ns/1ps

module m_vlsi_axi4_sram #(
  // Parameter Description: AXI and SRAM data width (PR_DATA_WD); PARA_DATA_WD/8 byte lanes.
  // Parameter values: legal values are 8, 16, 32, ..., 1024 (power of 2); default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_DATA_WD    = 32,
  // Parameter Description: AXI byte-address width and o_sram_addr width (PR_ADDR_WD);
  //   must exceed the byte offset, and with several banks hold the bank-hash fields.
  // Parameter values: legal values are greater than $clog2(PARA_DATA_WD/8); default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ADDR_WD    = 32,
  // Parameter Description: AXI ID width, AWID/BID/ARID/RID (PR_ID_WD).
  // Parameter values: legal values are 1 or more; default is 4.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ID_WD      = 4,
  // Parameter Description: AxLEN width (PR_LEN_WD); maximum burst is 2**PARA_LEN_WD beats.
  // Parameter values: legal values are 1 to 8; default is 8.
  // Parameter unit: bit
  parameter logic [31:0] PARA_LEN_WD     = 8,
  // Parameter Description: number of entries in each of the five channel FIFOs
  //   (PR_FIFO_DEPTH).
  // Parameter values: legal values are powers of 2, 2 or more; default is 8.
  // Parameter unit: count
  parameter logic [31:0] PARA_FIFO_DEPTH = 8,
  // Parameter Description: number of independent SRAM banks (PR_NUM_BANKS); 1 = single-SRAM
  //   behaviour (see m_vlsi_sram_misc for the bank hash and arbitration).
  // Parameter values: legal values are powers of 2, 1 or more; default is 1.
  // Parameter unit: count
  parameter logic [31:0] PARA_NUM_BANKS  = 1,
  localparam logic [31:0] LPARA_STRB_WD  = PARA_DATA_WD / 32'd8,
  localparam logic [31:0] LPARA_BYTE_LSB = $clog2(PARA_DATA_WD / 32'd8)
) (
  // Clock and reset: single i_clk domain (rising edge), active-low reset with asynchronous
  // assertion; deassertion is synchronized to i_clk outside the block.
  input  wire                                          i_clk,
  input  wire                                          i_rst_n,       // REQ-001, REQ-002

  // AXI write address channel (AW). The master keeps VALID and payload stable until the
  // handshake (assumed AXI4-compliant master).
  input  wire  [PARA_ADDR_WD-1:0]                      i_awaddr,
  input  wire                                          i_awvalid,
  output logic                                         o_awready,
  input  wire  [1:0]                                   i_awburst,
  input  wire  [PARA_LEN_WD-1:0]                       i_awlen,
  input  wire  [PARA_ID_WD-1:0]                        i_awid,
  input  wire  [2:0]                                   i_awsize,      // REQ-003, REQ-015

  // AXI write data channel (W). Exactly AWLEN+1 beats per AW burst in AW order, WRAP lengths
  // 2/4/8/16 and no AxBURST = 2'b11 are master obligations; i_wlast is accepted but ignored.
  input  wire  [PARA_DATA_WD-1:0]                      i_wdata,
  input  wire  [LPARA_STRB_WD-1:0]                     i_wstrb,
  input  wire                                          i_wvalid,
  output logic                                         o_wready,
  input  wire                                          i_wlast,       // REQ-004, REQ-016

  // AXI write response channel (B).
  output logic [PARA_ID_WD-1:0]                        o_bid,
  output logic [1:0]                                   o_bresp,
  output logic                                         o_bvalid,
  input  wire                                          i_bready,      // REQ-005

  // AXI read address channel (AR).
  input  wire  [PARA_ADDR_WD-1:0]                      i_araddr,
  input  wire                                          i_arvalid,
  output logic                                         o_arready,
  input  wire  [1:0]                                   i_arburst,
  input  wire  [PARA_LEN_WD-1:0]                       i_arlen,
  input  wire  [PARA_ID_WD-1:0]                        i_arid,
  input  wire  [2:0]                                   i_arsize,      // REQ-006

  // AXI read data channel (R); o_rdata always carries the full SRAM word.
  output logic [PARA_ID_WD-1:0]                        o_rid,
  output logic [PARA_DATA_WD-1:0]                      o_rdata,
  output logic [1:0]                                   o_rresp,
  output logic                                         o_rvalid,
  output logic                                         o_rlast,
  input  wire                                          i_rready,      // REQ-007

  // SRAM interface, one packed set per bank (bit-for-bit single SRAM when PR_NUM_BANKS = 1).
  // Each bank writes the strobed lanes at the end of a we cycle and returns the word
  // addressed in an oe cycle during the next cycle (one-cycle read latency).
  output logic [PARA_NUM_BANKS-1:0][PARA_ADDR_WD-1:0]  o_sram_addr,
  output logic [PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]  o_sram_wdata,
  output logic [PARA_NUM_BANKS-1:0][LPARA_STRB_WD-1:0] o_sram_wstrb,
  output logic [PARA_NUM_BANKS-1:0]                    o_sram_we,
  output logic [PARA_NUM_BANKS-1:0]                    o_sram_oe,
  input  wire  [PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]  i_sram_rdata   // REQ-008, REQ-017
);

  // Dependent constants: FIFO pointer address width (log2 of the depth, passed to every
  // m_vlsi_fifo as PARA_DEPTH) and the AW/AR and W FIFO entry widths.
  localparam logic [31:0] LPARA_FIFO_AW     = (PARA_FIFO_DEPTH <= 32'd2) ? 32'd1
                                                                         : $clog2(PARA_FIFO_DEPTH);
  localparam logic [31:0] LPARA_AW_DEPTH    = LPARA_FIFO_AW;
  localparam logic [31:0] LPARA_W_DEPTH     = LPARA_FIFO_AW;
  localparam logic [31:0] LPARA_AR_DEPTH    = LPARA_FIFO_AW;
  localparam logic [31:0] LPARA_R_DEPTH     = LPARA_FIFO_AW;
  localparam logic [31:0] LPARA_B_DEPTH     = LPARA_FIFO_AW;
  // AW/AR FIFO entries carry {last, err, id, addr}.
  localparam logic [31:0] LPARA_AX_ENTRY_WD = PARA_ADDR_WD + PARA_ID_WD + 32'd2;
  // W FIFO entries carry {wstrb, wdata}.
  localparam logic [31:0] LPARA_W_ENTRY_WD  = PARA_DATA_WD + LPARA_STRB_WD;
  // R FIFO entries carry {id, rresp, rdata, last}; B FIFO entries carry {id, bresp}.
  localparam logic [31:0] LPARA_R_ENTRY_WD  = PARA_DATA_WD + PARA_ID_WD + 32'd3;
  localparam logic [31:0] LPARA_B_ENTRY_WD  = PARA_ID_WD + 32'd2;

  // Elaboration checks C1..C8: illegal top-level parameter values stop elaboration.
  if (((PARA_DATA_WD % 32'd8) != 32'd0) ||
      ((LPARA_STRB_WD & (LPARA_STRB_WD - 32'd1)) != 32'd0)) begin : gen_chk_c1
    $error("m_vlsi_axi4_sram: C1 PARA_DATA_WD must be 8 x (power of 2)");
  end  // REQ-058
  if ((PARA_DATA_WD < 32'd8) || (PARA_DATA_WD > 32'd1024)) begin : gen_chk_c2
    $error("m_vlsi_axi4_sram: C2 PARA_DATA_WD must be within 8..1024");
  end  // REQ-059
  if ((PARA_FIFO_DEPTH < 32'd2) ||
      ((PARA_FIFO_DEPTH & (PARA_FIFO_DEPTH - 32'd1)) != 32'd0)) begin : gen_chk_c3
    $error("m_vlsi_axi4_sram: C3 PARA_FIFO_DEPTH must be a power of 2, 2 or more");
  end  // REQ-060
  if ((PARA_NUM_BANKS < 32'd1) ||
      ((PARA_NUM_BANKS & (PARA_NUM_BANKS - 32'd1)) != 32'd0)) begin : gen_chk_c4
    $error("m_vlsi_axi4_sram: C4 PARA_NUM_BANKS must be a power of 2, 1 or more");
  end  // REQ-061
  if ((PARA_NUM_BANKS > 32'd1) &&
      (PARA_ADDR_WD < (LPARA_BYTE_LSB + $clog2(PARA_NUM_BANKS)))) begin : gen_chk_c5
    $error("m_vlsi_axi4_sram: C5 PARA_ADDR_WD too small for the bank hash");
  end  // REQ-062
  if (PARA_ADDR_WD <= LPARA_BYTE_LSB) begin : gen_chk_c6
    $error("m_vlsi_axi4_sram: C6 PARA_ADDR_WD must be wider than the byte offset");
  end  // REQ-063
  if (PARA_ID_WD < 32'd1) begin : gen_chk_c7
    $error("m_vlsi_axi4_sram: C7 PARA_ID_WD must be 1 or more");
  end  // REQ-064
  if ((PARA_LEN_WD < 32'd1) || (PARA_LEN_WD > 32'd8)) begin : gen_chk_c8
    $error("m_vlsi_axi4_sram: C8 PARA_LEN_WD must be within 1..8");
  end  // REQ-065

  // Internal signal declarations (interconnect; widths derived from the parameters).
  // Write address FSM beat outputs.
  logic [PARA_ADDR_WD-1:0]      w_axaddr_wr;
  logic                         w_push_wr;
  logic [PARA_ID_WD-1:0]        w_id_wr;
  logic                         w_err_wr;
  logic                         w_awlast_wr;

  // Read address FSM beat outputs.
  logic [PARA_ADDR_WD-1:0]      w_axaddr_rd;
  logic                         w_push_rd;
  logic [PARA_ID_WD-1:0]        w_id_rd;
  logic                         w_err_rd;
  logic                         w_arlast;

  // AWFIFO.
  logic [LPARA_AX_ENTRY_WD-1:0] w_awfifo_data_out;
  logic [LPARA_AX_ENTRY_WD-1:0] w_awfifo_data_in;
  logic                         w_awfifo_empty;
  logic                         w_awfifo_full;
  logic                         w_awfifo_pop;

  // WFIFO.
  logic [LPARA_W_ENTRY_WD-1:0]  w_wfifo_data_out;
  logic [LPARA_W_ENTRY_WD-1:0]  w_wfifo_data_in;
  logic                         w_wfifo_empty;
  logic                         w_wfifo_full;
  logic                         w_wfifo_push;
  logic                         w_wfifo_pop;

  // ARFIFO.
  logic [LPARA_AX_ENTRY_WD-1:0] w_arfifo_data_out;
  logic [LPARA_AX_ENTRY_WD-1:0] w_arfifo_data_in;
  logic                         w_arfifo_empty;
  logic                         w_arfifo_full;
  logic                         w_arfifo_pop;

  // RFIFO.
  logic [LPARA_R_ENTRY_WD-1:0]  w_rfifo_data_out;
  logic [LPARA_R_ENTRY_WD-1:0]  w_rfifo_data_in;
  logic                         w_rfifo_empty;
  logic                         w_rfifo_full;
  logic                         w_rfifo_pop;
  logic                         w_rfifo_push;

  // BFIFO.
  logic [LPARA_B_ENTRY_WD-1:0]  w_bfifo_data_out;
  logic [LPARA_B_ENTRY_WD-1:0]  w_bfifo_data_in;
  logic                         w_bfifo_empty;
  logic                         w_bfifo_full;
  logic                         w_bfifo_pop;
  logic                         w_bfifo_push;

  // Arbiter requests and grants.
  logic                         w_req_write;
  logic                         w_req_read;
  logic                         w_arb_sel;
  logic                         w_arb_write_en;  // REQ-023

  // i_wlast is accepted but ignored (spec F15): the address FSM tracks the last beat of each
  // burst. It is folded into a sink so the input is consumed without affecting any logic.
  logic w_unused_wlast;
  assign w_unused_wlast = i_wlast;  // REQ-040

  // WFIFO entry: {wstrb, wdata} of the W beat.
  assign w_wfifo_data_in = {i_wstrb, i_wdata};  // REQ-030

  // Address FSMs: index 0 = AW channel, index 1 = AR channel. The two are independent, so AW
  // and AR handshakes in the same cycle are both accepted when their READYs are 1. AxREADY is
  // combinational from FSM state and the current-cycle FIFO-full flag (1 during reset).
  m_vlsi_axfsm #(
    .PARA_ADDR_WD      (PARA_ADDR_WD),
    .PARA_ID_WD        (PARA_ID_WD),
    .PARA_DATA_WD      (PARA_DATA_WD),
    .PARA_LEN_WD       (PARA_LEN_WD)
  ) u_vlsi_axfsm_0 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_axaddr          (i_awaddr),
    .i_axvalid         (i_awvalid),
    .o_axready         (o_awready),
    .i_axburst         (i_awburst),
    .i_axlen           (i_awlen),
    .i_axid            (i_awid),
    .i_axsize          (i_awsize),
    .i_fifo_not_full   (~w_awfifo_full),
    .o_axaddr_out      (w_axaddr_wr),
    .o_push_fifo       (w_push_wr),
    .o_id              (w_id_wr),
    .o_last            (w_awlast_wr),
    .o_err             (w_err_wr)
  );

  m_vlsi_axfsm #(
    .PARA_ADDR_WD      (PARA_ADDR_WD),
    .PARA_ID_WD        (PARA_ID_WD),
    .PARA_DATA_WD      (PARA_DATA_WD),
    .PARA_LEN_WD       (PARA_LEN_WD)
  ) u_vlsi_axfsm_1 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_axaddr          (i_araddr),
    .i_axvalid         (i_arvalid),
    .o_axready         (o_arready),
    .i_axburst         (i_arburst),
    .i_axlen           (i_arlen),
    .i_axid            (i_arid),
    .i_axsize          (i_arsize),
    .i_fifo_not_full   (~w_arfifo_full),
    .o_axaddr_out      (w_axaddr_rd),
    .o_push_fifo       (w_push_rd),
    .o_id              (w_id_rd),
    .o_last            (w_arlast),
    .o_err             (w_err_rd)
  );  // REQ-009, REQ-019, REQ-041, REQ-051

  // Channel FIFOs, all of depth PR_FIFO_DEPTH: index 0 = AWFIFO {last, err, id, addr},
  // 1 = WFIFO {wstrb, wdata}, 2 = ARFIFO {last, err, id, addr}, 3 = RFIFO
  // {id, rresp, rdata, last}, 4 = BFIFO {id, bresp}. All empty after reset.
  m_vlsi_fifo #(
    .PARA_DATA_WD      (LPARA_AX_ENTRY_WD),
    .PARA_DEPTH        (LPARA_AW_DEPTH)
  ) u_vlsi_fifo_0 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_data            (w_awfifo_data_in),
    .i_push            (w_push_wr),
    .i_pop             (w_awfifo_pop),
    .o_data_out        (w_awfifo_data_out),
    .o_empty           (w_awfifo_empty),
    .o_full            (w_awfifo_full)
  );

  m_vlsi_fifo #(
    .PARA_DATA_WD      (LPARA_W_ENTRY_WD),
    .PARA_DEPTH        (LPARA_W_DEPTH)
  ) u_vlsi_fifo_1 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_data            (w_wfifo_data_in),
    .i_push            (w_wfifo_push),
    .i_pop             (w_wfifo_pop),
    .o_data_out        (w_wfifo_data_out),
    .o_empty           (w_wfifo_empty),
    .o_full            (w_wfifo_full)
  );

  m_vlsi_fifo #(
    .PARA_DATA_WD      (LPARA_AX_ENTRY_WD),
    .PARA_DEPTH        (LPARA_AR_DEPTH)
  ) u_vlsi_fifo_2 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_data            (w_arfifo_data_in),
    .i_push            (w_push_rd),
    .i_pop             (w_arfifo_pop),
    .o_data_out        (w_arfifo_data_out),
    .o_empty           (w_arfifo_empty),
    .o_full            (w_arfifo_full)
  );

  m_vlsi_fifo #(
    .PARA_DATA_WD      (LPARA_R_ENTRY_WD),
    .PARA_DEPTH        (LPARA_R_DEPTH)
  ) u_vlsi_fifo_3 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_data            (w_rfifo_data_in),
    .i_push            (w_rfifo_push),
    .i_pop             (w_rfifo_pop),
    .o_data_out        (w_rfifo_data_out),
    .o_empty           (w_rfifo_empty),
    .o_full            (w_rfifo_full)
  );

  m_vlsi_fifo #(
    .PARA_DATA_WD      (LPARA_B_ENTRY_WD),
    .PARA_DEPTH        (LPARA_B_DEPTH)
  ) u_vlsi_fifo_4 (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_data            (w_bfifo_data_in),
    .i_push            (w_bfifo_push),
    .i_pop             (w_bfifo_pop),
    .o_data_out        (w_bfifo_data_out),
    .o_empty           (w_bfifo_empty),
    .o_full            (w_bfifo_full)
  );  // REQ-030, REQ-041, REQ-051, REQ-001, REQ-002

  // Shared write/read toggle arbiter (one instance for every bank configuration).
  m_vlsi_arbiter u_vlsi_arbiter (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_req_write       (w_req_write),
    .i_req_read        (w_req_read),
    .o_arb_sel         (w_arb_sel),
    .o_arb_write_en    (w_arb_write_en)
  );  // REQ-051

  // Control/datapath wrapper: FIFO pack/unpack, request/issue/bank hash, SRAM fan-out and
  // the read pipeline. The issue path from the FIFO heads to the SRAM outputs is
  // combinational within one i_clk cycle (100 MHz target, checked in synthesis).
  m_vlsi_sram_misc #(
    .PARA_DATA_WD      (PARA_DATA_WD),
    .PARA_ADDR_WD      (PARA_ADDR_WD),
    .PARA_ID_WD        (PARA_ID_WD),
    .PARA_NUM_BANKS    (PARA_NUM_BANKS)
  ) u_vlsi_sram_misc (
    .i_clk_core        (i_clk),
    .i_rst_n_core      (i_rst_n),
    .i_wvalid          (i_wvalid),
    .i_rready          (i_rready),
    .i_bready          (i_bready),
    .i_axaddr_wr       (w_axaddr_wr),
    .i_id_wr           (w_id_wr),
    .i_awlast_wr       (w_awlast_wr),
    .i_err_wr          (w_err_wr),
    .i_axaddr_rd       (w_axaddr_rd),
    .i_id_rd           (w_id_rd),
    .i_arlast          (w_arlast),
    .i_err_rd          (w_err_rd),
    .i_arb_sel         (w_arb_sel),
    .i_arb_write_en    (w_arb_write_en),
    .i_awfifo_data_out (w_awfifo_data_out),
    .i_awfifo_empty    (w_awfifo_empty),
    .i_wfifo_data_out  (w_wfifo_data_out),
    .i_wfifo_empty     (w_wfifo_empty),
    .i_wfifo_full      (w_wfifo_full),
    .i_arfifo_data_out (w_arfifo_data_out),
    .i_arfifo_empty    (w_arfifo_empty),
    .i_rfifo_data_out  (w_rfifo_data_out),
    .i_rfifo_empty     (w_rfifo_empty),
    .i_rfifo_full      (w_rfifo_full),
    .i_bfifo_data_out  (w_bfifo_data_out),
    .i_bfifo_empty     (w_bfifo_empty),
    .i_bfifo_full      (w_bfifo_full),
    .i_sram_rdata      (i_sram_rdata),
    .o_awfifo_data_in  (w_awfifo_data_in),
    .o_awfifo_pop      (w_awfifo_pop),
    .o_wfifo_push      (w_wfifo_push),
    .o_wfifo_pop       (w_wfifo_pop),
    .o_arfifo_data_in  (w_arfifo_data_in),
    .o_arfifo_pop      (w_arfifo_pop),
    .o_rfifo_data_in   (w_rfifo_data_in),
    .o_rfifo_push      (w_rfifo_push),
    .o_rfifo_pop       (w_rfifo_pop),
    .o_bfifo_data_in   (w_bfifo_data_in),
    .o_bfifo_push      (w_bfifo_push),
    .o_bfifo_pop       (w_bfifo_pop),
    .o_req_write       (w_req_write),
    .o_req_read        (w_req_read),
    .o_rid             (o_rid),
    .o_rdata           (o_rdata),
    .o_rresp           (o_rresp),
    .o_rvalid          (o_rvalid),
    .o_rlast           (o_rlast),
    .o_bid             (o_bid),
    .o_bresp           (o_bresp),
    .o_bvalid          (o_bvalid),
    .o_wready          (o_wready),
    .o_sram_addr       (o_sram_addr),
    .o_sram_wdata      (o_sram_wdata),
    .o_sram_wstrb      (o_sram_wstrb),
    .o_sram_we         (o_sram_we),
    .o_sram_oe         (o_sram_oe)
  );  // REQ-051, REQ-066, REQ-008, REQ-023

endmodule : m_vlsi_axi4_sram
