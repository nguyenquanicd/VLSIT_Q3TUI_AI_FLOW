/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : Combinational FIFO pack/unpack, err->SLVERR substitution, R/B output
 *                unpacking, read-bank rdata mux into RFIFO. Instance u_vlsi_axi_fifopack in
 *                m_vlsi_sram_misc: packs the AW/AR FIFO inputs, unpacks the AW/AR/W FIFO
 *                heads, packs the RFIFO/BFIFO inputs and drives the AXI R/B outputs.
 * REQ-IDs: REQ-010, REQ-011, REQ-012, REQ-013, REQ-014, REQ-023, REQ-024, REQ-027, REQ-028,
 *          REQ-029, REQ-030, REQ-042, REQ-045, REQ-046, REQ-048
 */
`timescale 1ns/1ps

module m_vlsi_axi_fifopack #(
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
  // Parameter Description: number of SRAM banks (PR_NUM_BANKS) for the read-data mux.
  // Parameter values: legal values are powers of 2, 1 or more; default is 1.
  // Parameter unit: count
  parameter logic [31:0] PARA_NUM_BANKS = 1,
  localparam logic [31:0] LPARA_STRB_WD = PARA_DATA_WD / 32'd8,
  localparam logic [31:0] LPARA_BANK_WD = (PARA_NUM_BANKS <= 32'd1) ? 32'd1
                                                                    : $clog2(PARA_NUM_BANKS)
) (
  // Address-FSM beat outputs (AW/AR FIFO input packing).
  input  wire  [PARA_ADDR_WD-1:0]                     i_axaddr_wr,
  input  wire  [PARA_ID_WD-1:0]                       i_id_wr,
  input  wire                                         i_awlast_wr,
  input  wire                                         i_err_wr,
  input  wire  [PARA_ADDR_WD-1:0]                     i_axaddr_rd,
  input  wire  [PARA_ID_WD-1:0]                       i_id_rd,
  input  wire                                         i_arlast,
  input  wire                                         i_err_rd,

  // FIFO head entries (to unpack) and R/B FIFO empty flags.
  input  wire  [PARA_ADDR_WD+PARA_ID_WD+1:0]          i_awfifo_data_out,
  input  wire  [PARA_DATA_WD+LPARA_STRB_WD-1:0]       i_wfifo_data_out,
  input  wire  [PARA_ADDR_WD+PARA_ID_WD+1:0]          i_arfifo_data_out,
  input  wire  [PARA_DATA_WD+PARA_ID_WD+2:0]          i_rfifo_data_out,
  input  wire                                         i_rfifo_empty,
  input  wire  [PARA_ID_WD+1:0]                       i_bfifo_data_out,
  input  wire                                         i_bfifo_empty,

  // SRAM read data (one word per bank) and the bank of the pending read.
  input  wire  [PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0] i_sram_rdata,
  input  wire  [LPARA_BANK_WD-1:0]                    i_reg_rd_bank_d,

  // Read-response fields latched by the read pipeline register (m_vlsi_sram_fanout).
  input  wire  [PARA_ID_WD-1:0]                       i_reg_rd_id_d,
  input  wire                                         i_reg_rd_last_d,
  input  wire                                         i_reg_rd_err_d,

  // FIFO input entries.
  output logic [PARA_ADDR_WD+PARA_ID_WD+1:0]          o_awfifo_data_in,
  output logic [PARA_ADDR_WD+PARA_ID_WD+1:0]          o_arfifo_data_in,
  output logic [PARA_DATA_WD+PARA_ID_WD+2:0]          o_rfifo_data_in,
  output logic [PARA_ID_WD+1:0]                       o_bfifo_data_in,  // REQ-030, REQ-023

  // Unpacked head fields (to m_vlsi_bank_arb / m_vlsi_sram_fanout).
  output logic [PARA_ADDR_WD-1:0]                     o_wr_addr_field,
  output logic [PARA_ADDR_WD-1:0]                     o_rd_addr_field,
  output logic [PARA_DATA_WD-1:0]                     o_wr_wdata_field,
  output logic [LPARA_STRB_WD-1:0]                    o_wr_wstrb_field,
  output logic                                        o_wr_last_field,
  output logic                                        o_wr_err_field,
  output logic                                        o_rd_last_field,
  output logic                                        o_rd_err_field,

  // AXI R and B channel outputs.
  output logic [PARA_ID_WD-1:0]                       o_rid,
  output logic [PARA_DATA_WD-1:0]                     o_rdata,
  output logic [1:0]                                  o_rresp,
  output logic                                        o_rvalid,
  output logic                                        o_rlast,
  output logic [PARA_ID_WD-1:0]                       o_bid,
  output logic [1:0]                                  o_bresp,
  output logic                                        o_bvalid
);

  // Module-local type declarations
  // AXI response encodings; EXOKAY (2'b01) and DECERR (2'b11) are never generated.
  typedef enum logic [1:0] {
    ENUM_RESP_OKAY   = 2'b00,
    ENUM_RESP_SLVERR = 2'b10
  } resp_type_def;

  // Internal signal declarations
  // AWID of the AWFIFO head, carried into the burst's BFIFO entry.
  logic [PARA_ID_WD-1:0] w_wr_id_field;

  // AW/AR FIFO packing: one {last, err, id, addr} entry per beat from each address FSM.
  assign o_awfifo_data_in = {i_awlast_wr, i_err_wr, i_id_wr, i_axaddr_wr};
  assign o_arfifo_data_in = {i_arlast, i_err_rd, i_id_rd, i_axaddr_rd};  // REQ-029, REQ-030

  // AWFIFO head unpack: address, ID, error and last flag of the next write beat.
  assign o_wr_addr_field = i_awfifo_data_out[PARA_ADDR_WD-1:0];
  assign w_wr_id_field   = i_awfifo_data_out[PARA_ADDR_WD+PARA_ID_WD-1:PARA_ADDR_WD];
  assign o_wr_err_field  = i_awfifo_data_out[PARA_ADDR_WD+PARA_ID_WD];
  assign o_wr_last_field = i_awfifo_data_out[PARA_ADDR_WD+PARA_ID_WD+1];  // REQ-045, REQ-027

  // ARFIFO head unpack: address, error and last flag of the next read beat.
  assign o_rd_addr_field = i_arfifo_data_out[PARA_ADDR_WD-1:0];
  assign o_rd_err_field  = i_arfifo_data_out[PARA_ADDR_WD+PARA_ID_WD];
  assign o_rd_last_field = i_arfifo_data_out[PARA_ADDR_WD+PARA_ID_WD+1];  // REQ-028

  // WFIFO head unpack: full data word and byte strobes of the next W beat (pushed as
  // {wstrb, wdata} on the W handshake, paired with AW beats in FIFO order).
  assign o_wr_wdata_field = i_wfifo_data_out[PARA_DATA_WD-1:0];
  assign o_wr_wstrb_field = i_wfifo_data_out[PARA_DATA_WD+LPARA_STRB_WD-1:PARA_DATA_WD];
  // REQ-010, REQ-024, REQ-045

  // RFIFO entry, pushed in the cycle after the read issue: {ARID, RRESP, SRAM word of the
  // read bank, last}. RRESP is SLVERR for a beat of an erroneous burst, OKAY otherwise; the
  // full SRAM word is always returned.
  assign o_rfifo_data_in = {
    i_reg_rd_id_d,
    (i_reg_rd_err_d ? ENUM_RESP_SLVERR : ENUM_RESP_OKAY),
    i_sram_rdata[i_reg_rd_bank_d],
    i_reg_rd_last_d
  };  // REQ-048, REQ-028, REQ-013, REQ-024, REQ-029

  // BFIFO entry, pushed on the issue of a burst's last write beat: {AWID, BRESP}, with
  // BRESP = SLVERR for an erroneous burst, OKAY otherwise.
  assign o_bfifo_data_in = {w_wr_id_field, (o_wr_err_field ? ENUM_RESP_SLVERR : ENUM_RESP_OKAY)};
  // REQ-046, REQ-027, REQ-013, REQ-029, REQ-014

  // AXI R output: RFIFO head fields; RVALID = RFIFO not empty. The head is stable until
  // popped, and reads 0 after reset because the FIFO storage is cleared.
  assign o_rid    = i_rfifo_data_out[PARA_DATA_WD+PARA_ID_WD+2:PARA_DATA_WD+3];
  assign o_rdata  = i_rfifo_data_out[PARA_DATA_WD:1];
  assign o_rresp  = i_rfifo_data_out[PARA_DATA_WD+2:PARA_DATA_WD+1];
  assign o_rlast  = i_rfifo_data_out[0];
  assign o_rvalid = ~i_rfifo_empty;  // REQ-012, REQ-014, REQ-042

  // AXI B output: BFIFO head fields; BVALID = BFIFO not empty.
  assign o_bid    = i_bfifo_data_out[PARA_ID_WD+1:2];
  assign o_bresp  = i_bfifo_data_out[1:0];
  assign o_bvalid = ~i_bfifo_empty;  // REQ-011, REQ-042

endmodule : m_vlsi_axi_fifopack
