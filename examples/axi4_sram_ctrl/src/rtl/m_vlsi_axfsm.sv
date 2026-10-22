/*
 * Project      : AXI4 SRAM Controller (axi4_sram_ctrl)
 * Author       : Trthinh (Ethan), Thang Luong (superzeldalink) (user)
 * AI Flow      : VLSIT RTL Generator AI Automation Flow
 * AI Flow Page : https://github.com/nguyenquanicd/VLSIT_RTL_Generator_AI_Model
 * Date         : 2026-10-07
 * Description  : AXI address FSM: handshake, burst address generation sized by AxSIZE,
 *                alignment/oversize error detection, pushes {last, err, id, addr} into its
 *                FIFO. One instance serves AW (u_vlsi_axfsm_0) and one serves AR
 *                (u_vlsi_axfsm_1); FIXED/INCR/WRAP (reserved 2'b11 as WRAP) addressing.
 * REQ-IDs: REQ-001, REQ-002, REQ-009, REQ-014, REQ-019, REQ-020, REQ-021, REQ-022, REQ-023,
 *          REQ-024, REQ-026, REQ-029, REQ-040, REQ-041, REQ-043, REQ-044, REQ-050, REQ-052,
 *          REQ-057, REQ-066
 */
`timescale 1ns/1ps

module m_vlsi_axfsm #(
  // Parameter Description: AXI byte-address width (PR_ADDR_WD); must exceed LPARA_MAX_SIZE.
  // Parameter values: legal values are greater than $clog2(PARA_DATA_WD/8); default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ADDR_WD = 32,
  // Parameter Description: AXI ID width (PR_ID_WD).
  // Parameter values: legal values are 1 or more; default is 4.
  // Parameter unit: bit
  parameter logic [31:0] PARA_ID_WD   = 4,
  // Parameter Description: AXI data width (PR_DATA_WD); sets the largest legal AxSIZE.
  // Parameter values: legal values are 8, 16, 32, ..., 1024; default is 32.
  // Parameter unit: bit
  parameter logic [31:0] PARA_DATA_WD = 32,
  // Parameter Description: AxLEN width (PR_LEN_WD); the beat counter is one bit wider.
  // Parameter values: legal values are 1 to 8; default is 8.
  // Parameter unit: bit
  parameter logic [31:0] PARA_LEN_WD  = 8,
  localparam logic [31:0] LPARA_SIZE_WD     = 3,
  localparam logic [31:0] LPARA_MAX_SIZE    = $clog2(PARA_DATA_WD / 32'd8),
  localparam logic [31:0] LPARA_LEN_LOG2_WD = $clog2(PARA_LEN_WD + 32'd1)
) (
  // Clock and reset: single i_clk domain, active-low reset with asynchronous assertion.
  input  wire                      i_clk_core,
  input  wire                      i_rst_n_core,     // REQ-001, REQ-002

  // AXI address channel (AW or AR, per instance); the two instances are independent.
  input  wire  [PARA_ADDR_WD-1:0]  i_axaddr,
  input  wire                      i_axvalid,
  output logic                     o_axready,
  input  wire  [1:0]               i_axburst,
  input  wire  [PARA_LEN_WD-1:0]   i_axlen,
  input  wire  [PARA_ID_WD-1:0]    i_axid,
  input  wire  [LPARA_SIZE_WD-1:0] i_axsize,         // REQ-019, REQ-023

  // FIFO status for READY generation and beat emission (current-cycle flag).
  input  wire                      i_fifo_not_full,

  // Beat outputs to the address FIFO.
  output logic [PARA_ADDR_WD-1:0]  o_axaddr_out,
  output logic                     o_push_fifo,
  output logic [PARA_ID_WD-1:0]    o_id,
  output logic                     o_last,
  output logic                     o_err             // REQ-029, REQ-040
);

  // Module-local type declarations
  // Address-FSM states: wait for the handshake in IDLE, emit beats in ADDR.
  // Both 1-bit encodings are used; the case default recovers to ENUM_ST_IDLE.
  typedef enum logic [0:0] {
    ENUM_ST_IDLE = 1'b0,
    ENUM_ST_ADDR = 1'b1
  } ax_state_type_def;

  // AXI burst-type encodings (AxBURST); the reserved 2'b11 falls into the WRAP branch.
  typedef enum logic [1:0] {
    ENUM_BURST_FIXED = 2'b00,
    ENUM_BURST_INCR  = 2'b01,
    ENUM_BURST_WRAP  = 2'b10
  } burst_type_def;

  // Elaboration checks: AxLEN width 1..8 (C8), whole-byte data width (C1), and an address
  // wider than the byte offset (C6).
  if ((PARA_LEN_WD < 32'd1) || (PARA_LEN_WD > 32'd8)) begin : gen_chk_len_wd
    $error("m_vlsi_axfsm: PARA_LEN_WD must be within 1..8");
  end
  if ((PARA_DATA_WD < 32'd8) || ((PARA_DATA_WD % 32'd8) != 32'd0)) begin : gen_chk_data_wd
    $error("m_vlsi_axfsm: PARA_DATA_WD must be a multiple of 8, 8 or more");
  end
  if (PARA_ADDR_WD <= LPARA_MAX_SIZE) begin : gen_chk_addr_wd
    $error("m_vlsi_axfsm: PARA_ADDR_WD must be wider than the byte offset");
  end

  // Internal signal declarations
  // FSM state.
  ax_state_type_def           reg_fsm_ax_current_state;
  ax_state_type_def           w_fsm_ax_nxt_state;

  // Beat counter, one bit wider than AxLEN so AxLEN = 2**PARA_LEN_WD-1 loads 2**PARA_LEN_WD.
  logic [PARA_LEN_WD:0]       reg_cnt_addr;
  logic                       w_cnt_addr_done;

  // Burst parameters captured at the handshake.
  logic [1:0]                 reg_axburst;
  logic [PARA_ADDR_WD-1:0]    reg_axaddr;
  logic [PARA_ID_WD-1:0]      reg_id;
  logic [LPARA_SIZE_WD-1:0]   reg_axsize;
  logic                       reg_err;

  // Per-beat byte stride 2**reg_axsize.
  logic [PARA_ADDR_WD-1:0]    w_size_bytes;

  // Alignment / oversize check on the incoming request.
  logic                       w_err_calc;
  logic [PARA_ADDR_WD-1:0]    w_axsize_in_bytes;
  logic [PARA_ADDR_WD-1:0]    w_axsize_in_mask;

  // Next-address candidates.
  logic [PARA_ADDR_WD-1:0]    w_addr_fixed;
  logic [PARA_ADDR_WD-1:0]    w_addr_incr;
  logic [PARA_ADDR_WD-1:0]    w_addr_wrap;
  logic [PARA_ADDR_WD-1:0]    w_addr_calc;

  // WRAP window: AXI4 WRAP lengths are 2/4/8/16, so size_bytes*burst_len is a power of 2
  // and the wrap boundary is a mask. log2(AxLEN+1) is the index of AxLEN's top set bit + 1.
  logic [LPARA_LEN_LOG2_WD-1:0] w_wrap_len_log2;
  logic [PARA_ADDR_WD-1:0]    w_wrap_window_in;
  logic [PARA_ADDR_WD-1:0]    reg_wrap_mask;
  logic [PARA_ADDR_WD-1:0]    reg_wrap_lower;
  logic [PARA_ADDR_WD-1:0]    w_wrap_upper;

  // Handshake and beat-emit strobes.
  logic                       w_hs;
  logic                       w_beat_fire;

  // Handshake: accepted only in IDLE with AxREADY high. Beats are emitted only in ADDR while
  // beats remain and the FIFO is not full (current-cycle flag), so the first beat is pushed
  // in the cycle after the handshake (c1) and one beat per cycle afterwards.
  assign w_hs        = (reg_fsm_ax_current_state == ENUM_ST_IDLE) & i_axvalid & o_axready;
  assign w_beat_fire = (reg_fsm_ax_current_state == ENUM_ST_ADDR) & ~w_cnt_addr_done &
                       i_fifo_not_full;  // REQ-044, REQ-050, REQ-052, REQ-057

  // Beat counter done flag.
  assign w_cnt_addr_done = (reg_cnt_addr == '0);  // REQ-022, REQ-044

  // Per-beat stride 2**reg_axsize as a same-width shift.
  assign w_size_bytes = PARA_ADDR_WD'(1) << reg_axsize;  // REQ-020, REQ-024

  // Alignment / oversize error of the incoming request: AxSIZE larger than the bus, or a start
  // address not a multiple of 2**AxSIZE. Later INCR/WRAP beats stay aligned automatically,
  // so one check at the handshake is enough.
  assign w_axsize_in_bytes = PARA_ADDR_WD'(1) << i_axsize;
  assign w_axsize_in_mask  = $bits(w_axsize_in_mask)'(w_axsize_in_bytes - 1'b1);
  assign w_err_calc        = (i_axsize > LPARA_SIZE_WD'(LPARA_MAX_SIZE)) |
                             ((i_axaddr & w_axsize_in_mask) != '0);  // REQ-026

  // WRAP window size of the incoming request: size_bytes * burst_len.
  assign w_wrap_window_in = w_axsize_in_bytes << w_wrap_len_log2;  // REQ-021
  assign w_wrap_upper     = $bits(w_wrap_upper)'(reg_wrap_lower + reg_wrap_mask + 1'b1);

  // Next-address candidates: FIXED keeps the address, INCR adds the stride (modulo
  // 2**PARA_ADDR_WD, no 4 KB check), WRAP adds the stride unless that lands on the upper
  // window boundary, then it returns to the lower boundary. Reserved 2'b11 is addressed
  // exactly like WRAP.
  assign w_addr_fixed = reg_axaddr;
  assign w_addr_incr  = $bits(w_addr_incr)'(reg_axaddr + w_size_bytes);
  assign w_addr_wrap  = (w_addr_incr == w_wrap_upper) ? reg_wrap_lower : w_addr_incr;
  assign w_addr_calc  = (reg_axburst == ENUM_BURST_FIXED) ? w_addr_fixed :
                        (reg_axburst == ENUM_BURST_INCR)  ? w_addr_incr  :
                        w_addr_wrap;  // REQ-020, REQ-021, REQ-024

  // AxREADY: combinational from FSM state and the current-cycle FIFO-full flag only; it does
  // not depend on AxVALID, and a same-cycle pop does not raise it. It is 1 during reset.
  assign o_axready = (reg_fsm_ax_current_state == ENUM_ST_IDLE) & i_fifo_not_full;
  // REQ-009, REQ-019, REQ-041, REQ-057, REQ-066

  // Beat outputs: registered address/id/err of the current beat; last on the final beat
  // (counter == 1), so the downstream B/R path sees exactly one last per burst.
  assign o_axaddr_out = reg_axaddr;
  assign o_push_fifo  = w_beat_fire;
  assign o_id         = reg_id;
  assign o_last       = w_beat_fire & (reg_cnt_addr == {{PARA_LEN_WD{1'b0}}, 1'b1});
  assign o_err        = reg_err;  // REQ-014, REQ-029, REQ-040, REQ-044, REQ-026

  // FSM next state: IDLE -> ADDR on the handshake; ADDR -> IDLE in the first cycle with no
  // beats left (no push in that cycle), so the next burst is accepted in c(L+3).
  always_comb begin : p_com_fsm_ax
    w_fsm_ax_nxt_state = reg_fsm_ax_current_state;
    case (reg_fsm_ax_current_state)
      ENUM_ST_IDLE: w_fsm_ax_nxt_state = w_hs ? ENUM_ST_ADDR : ENUM_ST_IDLE;
      ENUM_ST_ADDR: w_fsm_ax_nxt_state = w_cnt_addr_done ? ENUM_ST_IDLE : ENUM_ST_ADDR;
      default:      w_fsm_ax_nxt_state = ENUM_ST_IDLE;
    endcase
  end  // REQ-043, REQ-044, REQ-057

  // WRAP length: log2(AxLEN+1) from the index of AxLEN's highest set bit. Valid only for
  // legal WRAP lengths (2/4/8/16); other lengths are a master violation (undefined sequence).
  always_comb begin : p_com_wrap_len
    w_wrap_len_log2 = '0;
    for (int idx = 0; idx < PARA_LEN_WD; idx++) begin
      if (i_axlen[idx]) w_wrap_len_log2 = LPARA_LEN_LOG2_WD'(idx + 1);
    end
  end  // REQ-021

  // FSM state register: reset to IDLE.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_fsm_ax
    if (!i_rst_n_core)
      reg_fsm_ax_current_state <= ENUM_ST_IDLE;
    else
      reg_fsm_ax_current_state <= w_fsm_ax_nxt_state;
  end  // REQ-001, REQ-002, REQ-041, REQ-043

  // Beat counter: loads AxLEN+1 at the handshake and decrements once per emitted beat;
  // it never decrements below 0 (no wrap) and holds while the FIFO is full.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_cnt_addr
    if (!i_rst_n_core)
      reg_cnt_addr <= '0;
    else if (w_hs)
      reg_cnt_addr <= $bits(reg_cnt_addr)'({1'b0, i_axlen} + 1'b1);
    else if (w_beat_fire)
      reg_cnt_addr <= $bits(reg_cnt_addr)'(reg_cnt_addr - 1'b1);
  end  // REQ-022, REQ-041, REQ-043, REQ-044

  // Burst type: captured at the handshake (reset value INCR).
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_axburst
    if (!i_rst_n_core)
      reg_axburst <= ENUM_BURST_INCR;
    else if (w_hs)
      reg_axburst <= i_axburst;
  end  // REQ-043

  // AxSIZE: captured at the handshake (reset value = full bus width).
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_axsize
    if (!i_rst_n_core)
      reg_axsize <= LPARA_SIZE_WD'(LPARA_MAX_SIZE);
    else if (w_hs)
      reg_axsize <= i_axsize;
  end  // REQ-043, REQ-024

  // Error: computed at the handshake and latched for the whole burst; reloaded on every
  // handshake, so there is no sticky state. Cleared on reset.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_err
    if (!i_rst_n_core)
      reg_err <= 1'b0;
    else if (w_hs)
      reg_err <= w_err_calc;
  end  // REQ-026, REQ-041, REQ-043

  // WRAP window: mask and lower boundary latched at the handshake from the incoming request.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_wrap
    if (!i_rst_n_core) begin
      reg_wrap_mask  <= '0;
      reg_wrap_lower <= '0;
    end else if (w_hs) begin
      reg_wrap_mask  <= $bits(reg_wrap_mask)'(w_wrap_window_in - 1'b1);
      reg_wrap_lower <= i_axaddr & ~($bits(reg_wrap_lower)'(w_wrap_window_in - 1'b1));
    end
  end  // REQ-021, REQ-043

  // Beat address: start address at the handshake, next address after each emitted beat;
  // unchanged while the FIFO is full.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_axaddr
    if (!i_rst_n_core)
      reg_axaddr <= '0;
    else if (w_hs)
      reg_axaddr <= i_axaddr;
    else if (w_beat_fire)
      reg_axaddr <= w_addr_calc;
  end  // REQ-020, REQ-021, REQ-043, REQ-044

  // ID: captured at the handshake and attached to every beat of the burst.
  always_ff @(posedge i_clk_core, negedge i_rst_n_core) begin : p_ff_id
    if (!i_rst_n_core)
      reg_id <= '0;
    else if (w_hs)
      reg_id <= i_axid;
  end  // REQ-029, REQ-043

endmodule : m_vlsi_axfsm
