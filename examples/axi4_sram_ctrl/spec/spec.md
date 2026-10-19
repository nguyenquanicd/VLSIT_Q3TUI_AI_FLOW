# AXI4 SRAM Controller — Block Specification

Top module: `m_vlsi_axi4_sram`

<!-- section: overview -->
## 1. Overview

`m_vlsi_axi4_sram` is an AXI4 (full) slave that lets an AXI4 master read and write external or on-chip synchronous SRAM. It sits between one AXI4 master (or interconnect slave port) and one or more SRAM banks. The AXI side has the five independent channels AW, W, B, AR and R. AxSIZE sets the beat size; INCR, WRAP and FIXED bursts of 1 to 256 beats are supported. The SRAM side is a simple per-bank interface: write enable, output enable, byte address, write data, byte write strobes and read data. The SRAM has a one-cycle read latency.

Each AXI channel is buffered by its own synchronous FIFO. These FIFOs separate AXI handshaking from SRAM access timing. Two address FSMs split every AW and AR burst into single-beat SRAM requests. A round-robin arbiter shares the SRAM between reads and writes. When the optional multi-bank mode is enabled (`PARA_NUM_BANKS` > 1), an address hash routes each beat to a bank. A write and a read to different banks then proceed in the same cycle. Typical uses are on-chip scratch memory behind an AXI4 interconnect, a memory model in AXI verification environments, and a bridge to SRAM macros with per-byte write enables. The controller returns `SLVERR` for misaligned or oversized requests. It does not support exclusive or locked accesses.

<!-- section: features -->
## 2. Key Features

| ID | Feature | Mandatory / Optional |
|---|---|---|
| F01 | AXI4 (full) slave interface with independent read and write channels (AW, W, B, AR, R). | Mandatory |
| F02 | Burst types FIXED (2'b00), INCR (2'b01) and WRAP (2'b10). WRAP follows the AXI4 wrap-boundary window. | Mandatory |
| F03 | Burst length AxLEN = 0 (single beat) to 255 (256 beats); the maximum is set by `PARA_LEN_WD`. | Mandatory |
| F04 | Data, address and ID widths are configurable (`PARA_DATA_WD`, `PARA_ADDR_WD`, `PARA_ID_WD`). | Mandatory |
| F05 | Narrow transfers: AxSIZE selects 2**AxSIZE bytes per beat (1 to `PARA_DATA_WD/8`), and the INCR/WRAP address stride follows 2**AxSIZE. | Mandatory |
| F06 | Byte strobes: `i_wstrb` (one bit per byte lane) passes through unchanged to the SRAM as `o_sram_wstrb` while `o_sram_we` is asserted for that bank; at all other times `o_sram_wstrb` is 0. | Mandatory |
| F07 | Error response: `BRESP`/`RRESP` = `SLVERR` when a burst's start address is not aligned to 2**AxSIZE, or when AxSIZE is larger than the bus width. All other accesses return `OKAY`. A SLVERR write beat does not modify the SRAM. | Mandatory |
| F08 | The AXI ID is carried from AW to B and from AR to R. Responses come back in the order the address channel accepted them; nothing is reordered. | Mandatory |
| F09 | Synchronous FIFO buffering on all five channels (AWFIFO, WFIFO, ARFIFO, RFIFO, BFIFO), all of depth `PARA_FIFO_DEPTH`. READY signals are derived from FIFO occupancy. | Mandatory |
| F10 | Round-robin (toggle) arbitration between write and read SRAM requests, using one shared arbiter. | Mandatory |
| F11 | Multi-bank SRAM: `PARA_NUM_BANKS` banks, with XOR-hash bank selection. A write and a read to different banks are issued in the same cycle. | Optional (`PARA_NUM_BANKS` > 1) |
| F12 | No lost write responses: the write for a burst's last beat is not granted while BFIFO is full. | Mandatory |
| F13 | Only one SRAM read in flight at a time (system-wide), so read throughput is at most 0.5 beat per cycle. | Mandatory (characteristic) |
| F14 | Write throughput up to 1 beat per cycle. | Mandatory |
| F15 | `i_wlast` is accepted but ignored; the address FSM tracks the last beat. | Mandatory |

<!-- section: parameters -->
## 3. Parameters

The parameter names are those of the released RTL (the specification used `PR_*` names before 2026-10-19; the verification owner renamed them to `PARA_*` to match the RTL). Each one is listed once below.

| Parameter | SV type | Default | Legal range | Meaning | Features affected | Locked |
|---|---|---|---|---|---|---|
| `PARA_DATA_WD` | `int` | 32 | 8, 16, 32, …, 1024 (power of 2, multiple of 8). *Assumption:* AXI4 data-width range. | AXI and SRAM data width in bits. `PARA_DATA_WD/8` = number of byte lanes = width of `i_wstrb`/`o_sram_wstrb`. | F04, F05, F06, F07 | no |
| `PARA_ADDR_WD` | `int` | 32 | See C5 and C6. *Assumption:* ≤ 64. | AXI byte-address width and `o_sram_addr` width. | F04, F11 | no |
| `PARA_ID_WD` | `int` | 4 | ≥ 1 | AXI ID width (AWID/BID/ARID/RID). | F04, F08 | no |
| `PARA_LEN_WD` | `int` | 8 | 1 … 8. *Assumption:* 8 is the only AXI4-compliant value. | Width of `i_awlen`/`i_arlen`. The maximum burst is 2**`PARA_LEN_WD` beats (256 at the default). | F03 | no |
| `PARA_FIFO_DEPTH` | `int` | 8 | Power of 2, ≥ 2 | Number of entries in each of the 5 FIFOs (all share this depth). | F09 | no |
| `PARA_NUM_BANKS` | `int` | 1 | Power of 2, ≥ 1 | Number of independent SRAM banks. 1 = single-SRAM behaviour. | F11 | no |

Derived quantities used in this document (not top-level parameters):
- `STRB_WD = PARA_DATA_WD/8`
- `BYTE_LSB = $clog2(PARA_DATA_WD/8)`
- `MAX_SIZE = $clog2(PARA_DATA_WD/8)`
- `BANK_WD = (PARA_NUM_BANKS <= 1) ? 1 : $clog2(PARA_NUM_BANKS)`

### Elaboration constraints

- **C1:** `PARA_DATA_WD % 8 == 0 && ((PARA_DATA_WD/8) & (PARA_DATA_WD/8 - 1)) == 0`
- **C2:** `PARA_DATA_WD >= 8 && PARA_DATA_WD <= 1024` (*Assumption*)
- **C3:** `PARA_FIFO_DEPTH >= 2 && (PARA_FIFO_DEPTH & (PARA_FIFO_DEPTH - 1)) == 0`
- **C4:** `PARA_NUM_BANKS >= 1 && (PARA_NUM_BANKS & (PARA_NUM_BANKS - 1)) == 0`
- **C5:** `PARA_NUM_BANKS > 1 -> PARA_ADDR_WD >= BYTE_LSB + $clog2(PARA_NUM_BANKS)`
- **C6:** `PARA_ADDR_WD > BYTE_LSB`
- **C7:** `PARA_ID_WD >= 1`
- **C8:** `PARA_LEN_WD >= 1 && PARA_LEN_WD <= 8` (*Assumption*)

<!-- section: interface -->
## 4. Interface

| | |
|---|---|
| **Top module** | `m_vlsi_axi4_sram` |
| **Clock** | `i_clk`, rising edge; target frequency 100 MHz (10 ns period), checked in synthesis |
| **Reset** | `i_rst_n`, active low, asynchronous assertion (*Assumption:* deassertion synchronized to `i_clk` outside the block) |

### Clock and reset

- A single clock domain `i_clk` is used. All sequential state shall change only on the rising edge of `i_clk` or on assertion of `i_rst_n`.
- The target frequency of `i_clk` is 100 MHz (10 ns period). The design, including its combinational paths (see the Timing and Constraints section), shall meet this target, and it is checked in synthesis.
- While `i_rst_n` = 0 the controller shall hold the reset state described in §5.1. *Assumption:* reset is asynchronous active-low. The documents do not name clock or reset; the names are taken from the user's RTL.

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `i_clk` | in | 1 | – | – | System clock, 100 MHz target; all logic is clocked on its rising edge |
| `i_rst_n` | in | 1 | asynchronous assert, `i_clk`-synchronous deassert | – | Active-low reset |

### AXI write address channel (AW)

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `i_awaddr` | in | `PARA_ADDR_WD` | i_clk | – | Write burst start byte address |
| `i_awvalid` | in | 1 | i_clk | – | Write address valid |
| `o_awready` | out | 1 | i_clk | 1 (*Assumption*, see §5.1) | Write address ready. Combinational: write FSM in `S_IDLE` AND AWFIFO not full. |
| `i_awburst` | in | 2 | i_clk | – | Burst type: 00 = FIXED, 01 = INCR, 10 = WRAP; reserved 11 is addressed like WRAP |
| `i_awlen` | in | `PARA_LEN_WD` | i_clk | – | Burst length − 1 (number of beats − 1) |
| `i_awid` | in | `PARA_ID_WD` | i_clk | – | Write transaction ID |
| `i_awsize` | in | 3 | i_clk | – | Bytes per beat = 2**`i_awsize`; values > `MAX_SIZE` are an error (F07) |

### AXI write data channel (W)

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `i_wdata` | in | `PARA_DATA_WD` | i_clk | – | Write data |
| `i_wstrb` | in | `PARA_DATA_WD/8` | i_clk | – | Write strobe, one bit per byte lane. Only asserted lanes are written to SRAM. |
| `i_wvalid` | in | 1 | i_clk | – | Write data valid |
| `o_wready` | out | 1 | i_clk | 1 (*Assumption*) | Write data ready = WFIFO not full |
| `i_wlast` | in | 1 | i_clk | – | Last write beat. Present but not used internally. |

### AXI write response channel (B)

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `o_bid` | out | `PARA_ID_WD` | i_clk | 0 (*Assumption*) | Write response ID (the burst's AWID) |
| `o_bresp` | out | 2 | i_clk | 0 (*Assumption*) | 2'b00 `OKAY`, or 2'b10 `SLVERR` if the burst's start address was misaligned to 2**AWSIZE or AWSIZE exceeded the bus width |
| `o_bvalid` | out | 1 | i_clk | 0 | Write response valid = BFIFO not empty |
| `i_bready` | in | 1 | i_clk | – | Write response ready |

### AXI read address channel (AR)

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `i_araddr` | in | `PARA_ADDR_WD` | i_clk | – | Read burst start byte address |
| `i_arvalid` | in | 1 | i_clk | – | Read address valid |
| `o_arready` | out | 1 | i_clk | 1 (*Assumption*) | Read address ready. Combinational: read FSM in `S_IDLE` AND ARFIFO not full. |
| `i_arburst` | in | 2 | i_clk | – | Burst type (same encoding as `i_awburst`) |
| `i_arlen` | in | `PARA_LEN_WD` | i_clk | – | Burst length − 1 |
| `i_arid` | in | `PARA_ID_WD` | i_clk | – | Read transaction ID |
| `i_arsize` | in | 3 | i_clk | – | Bytes per beat = 2**`i_arsize`; same validity rule as `i_awsize` |

### AXI read data channel (R)

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `o_rid` | out | `PARA_ID_WD` | i_clk | 0 (*Assumption*) | Read response ID (the burst's ARID) |
| `o_rdata` | out | `PARA_DATA_WD` | i_clk | 0 (*Assumption*) | Read data (always the full SRAM word) |
| `o_rresp` | out | 2 | i_clk | 0 (*Assumption*) | 2'b00 `OKAY`, or 2'b10 `SLVERR` if this beat's burst had a misaligned start address or an oversized ARSIZE |
| `o_rvalid` | out | 1 | i_clk | 0 | Read data valid = RFIFO not empty |
| `o_rlast` | out | 1 | i_clk | 0 (*Assumption*) | Last beat of the read burst |
| `i_rready` | in | 1 | i_clk | – | Read data ready |

### SRAM interface (one set per bank, `k` = 0 … `PARA_NUM_BANKS`−1)

Each port is a packed array `[PARA_NUM_BANKS-1:0]` of the per-bank width. With the default `PARA_NUM_BANKS` = 1 it is bit-for-bit the single-SRAM interface.

| Port | Dir | Width | Clock | Reset value | Description |
|---|---|---|---|---|---|
| `o_sram_addr` | out | `[PARA_NUM_BANKS-1:0][PARA_ADDR_WD-1:0]` | i_clk | 0 | Per-bank SRAM byte address: the full, un-hashed AXI beat address. *Assumption:* 0 when the bank has neither `o_sram_we[k]` nor `o_sram_oe[k]` asserted. |
| `o_sram_wdata` | out | `[PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]` | i_clk | 0 (*Assumption*) | Per-bank write data; valid only while `o_sram_we[k]` = 1, don't-care otherwise |
| `o_sram_wstrb` | out | `[PARA_NUM_BANKS-1:0][PARA_DATA_WD/8-1:0]` | i_clk | 0 | Per-bank, per-byte write strobe. Pass-through of `i_wstrb` while `o_sram_we[k]` = 1. Shall be all-zero whenever `o_sram_we[k]` = 0, including SLVERR beats and non-selected banks. |
| `i_sram_rdata` | in | `[PARA_NUM_BANKS-1:0][PARA_DATA_WD-1:0]` | i_clk | – | Per-bank read data, sampled in the cycle after `o_sram_oe[k]` |
| `o_sram_we` | out | `[PARA_NUM_BANKS-1:0]` | i_clk | 0 | Per-bank write enable, active high |
| `o_sram_oe` | out | `[PARA_NUM_BANKS-1:0]` | i_clk | 0 | Per-bank output (read) enable, active high, asserted in the read-issue cycle (also for SLVERR read beats) |

### Bus protocol rules

- **B1:** A transfer on any AXI channel happens at a rising edge of `i_clk` where VALID and READY are both 1.
- **B2:** `o_awready` shall be 1 exactly when the write FSM is in `S_IDLE` and AWFIFO is not full; `o_arready` likewise for the read FSM and ARFIFO. Both use the current-cycle FIFO-full flag (a pop in the same cycle does not raise READY). Neither shall depend combinationally on the VALID input. READY may drop without a handshake (e.g. FSM leaves `S_IDLE`).
- **B3:** `o_wready` shall equal NOT(WFIFO full) on the current-cycle flag and shall not depend on `i_wvalid`. A W beat is pushed into WFIFO when `i_wvalid & o_wready`. W beats may be accepted before their AW burst.
- **B4:** `o_bvalid` shall equal NOT(BFIFO empty). `o_bid`/`o_bresp` shall be the BFIFO head entry. Once `o_bvalid` is 1, it and the payload stay stable until the cycle where `i_bready` = 1, when the entry is popped.
- **B5:** `o_rvalid` shall equal NOT(RFIFO empty). `o_rid`/`o_rdata`/`o_rresp`/`o_rlast` shall be the RFIFO head entry and stay stable until popped by `i_rready` = 1.
- **B6:** Response encodings: `OKAY` = 2'b00, `SLVERR` = 2'b10. `EXOKAY` and `DECERR` shall never be generated.
- **B7:** Exactly one B response per accepted AW burst, and exactly AxLEN+1 R beats per accepted AR burst, with `o_rlast` = 1 only on the last one. Responses on each channel come back in address-acceptance order, whatever the ID.
- **B8:** Master obligations (AXI4-compliant master):
  - VALID and payload stay stable until the handshake;
  - exactly AWLEN+1 W beats per AW burst, in AW order, with no write-data interleaving;
  - WRAP lengths of 2, 4, 8 or 16 beats only;
  - AxBURST = 2'b11 is reserved and should not be used. If it is used, the controller shall address the burst like WRAP and respond `OKAY` (unless misaligned or oversized).

  Behaviour on violation: see the Error Handling section.
- **B9:** SRAM obligations, per bank `k`:
  - At the rising edge ending a cycle where `o_sram_we[k]` = 1, the bank shall write `o_sram_wdata[k]` into the byte lanes selected by `o_sram_wstrb[k]` at word `o_sram_addr[k][PARA_ADDR_WD-1:BYTE_LSB]`. Unselected bytes are unchanged.
  - In a cycle where `o_sram_oe[k]` = 1, the bank shall capture `o_sram_addr[k]` and present that word on `i_sram_rdata[k]` throughout the following cycle (one-cycle read latency).
  - *Assumption:* the SRAM ignores `o_sram_addr[k][BYTE_LSB-1:0]`.
  - Each bank shall hold the full addressable range it is expected to serve; banking adds no capacity.
- **B10:** In any cycle, `o_sram_we[k]` and `o_sram_oe[k]` shall not both be 1 for the same bank `k`.

<!-- section: functional -->
## 5. Functional Description

### 5.1 Reset

While `i_rst_n` = 0, and after reset until the first transaction:
- both address FSMs shall be in `S_IDLE` with beat counter = 0 and latched error = 0;
- all five FIFOs shall be empty (read and write pointers = 0);
- the arbiter toggle `arb_toggle` shall be 0, so the first contended grant goes to write;
- `reg_rd_pending` = 0.

The outputs shall therefore be:
- `o_awready` = `o_arready` = `o_wready` = 1 (combinational, also while `i_rst_n` = 0);
- `o_bvalid` = `o_rvalid` = 0;
- `o_sram_we` = `o_sram_oe` = 0 for every bank;
- `o_sram_addr` = 0;
- `o_sram_wstrb` = 0 for every bank (because `o_sram_we` = 0).

*Assumption:* `o_bid`, `o_bresp`, `o_rid`, `o_rdata`, `o_rresp`, `o_rlast` and `o_sram_wdata` read 0 after reset (FIFO storage cleared). The documents do not state reset values. A transaction in progress when reset asserts is discarded; no response is produced for it.

### 5.2 Address FSMs (`u_vlsi_axfsm_0` for AW, `u_vlsi_axfsm_1` for AR)

Two independent, identical instances of `m_vlsi_axfsm`, each with two states:

| State | Behaviour |
|---|---|
| `S_IDLE` | Waits for the AxVALID & AxREADY handshake. AxREADY = (state == `S_IDLE`) AND (its FIFO not full). |
| `S_ADDR` | Emits burst beats: pushes one `{last, err, id, addr}` entry per cycle into AWFIFO/ARFIFO while the counter is non-zero and the FIFO is not full. |

**Handshake (cycle c0):** in the handshake cycle the FSM shall:
- capture `i_axaddr`, `i_axlen`, `i_axburst`, `i_axid` and `i_axsize`;
- load the beat counter with `i_axlen + 1`;
- compute and latch `err` (§5.3) and the WRAP window (§5.4);
- move to `S_ADDR` at the end of c0.

The beat counter shall be `PARA_LEN_WD + 1` bits wide so that AxLEN = 255 loads 256 without overflow. It only decrements while non-zero, so it never wraps.

**Beat emission (cycles c1 …):** in every `S_ADDR` cycle where counter ≠ 0 and the FIFO is not full (current-cycle flag), the FSM shall:
- push `{last, err, id, addr}` at the end of the cycle, where `addr` is the current beat address, `id` the captured ID and `err` the latched error;
- set `last` = 1 exactly when counter == 1;
- decrement the counter;
- update the address per §5.4.

If the FIFO is full, the FSM shall stall with no push and no counter or address change. A pop of the same FIFO in that cycle does not allow a push in that cycle. The first beat is pushed no earlier than c1.

**Return to idle:** in the first `S_ADDR` cycle where counter == 0, the FSM shall push nothing and return to `S_IDLE` at the end of that cycle. With no FIFO back-pressure, a burst with AxLEN = L therefore:
- pushes in c1 … c(L+1);
- holds AxREADY = 0 in c1 … c(L+2);
- can accept the next burst in c(L+3).

### 5.3 Alignment / oversize error detection

At the handshake, with `size_bytes = 2**i_axsize`, the FSM shall set `err` = 1 if:
1. `i_axsize > MAX_SIZE` (more bytes per beat requested than the bus carries); or
2. `(i_axaddr & (size_bytes − 1)) != 0` (start address not a multiple of `size_bytes`).

`err` shall be latched for the whole burst and attached to every beat. INCR and WRAP beats after the first stay aligned because the address always advances by a multiple of `size_bytes`. An erroneous burst shall still generate all AxLEN+1 beats at a 2**AxSIZE stride. Only `BRESP`/`RRESP` and the SRAM write enable/strobe are affected (§5.7, §5.8, §5.9 and the Error Handling section). Reads of an erroneous burst still access the SRAM.

### 5.4 Burst address generation

The registered transfer size is `size_bytes = 2**reg_axsize`, not a fixed `PARA_DATA_WD/8`. After each pushed beat the next address shall be:

| Burst | Encoding | Next address |
|---|---|---|
| FIXED | 2'b00 | unchanged |
| INCR | 2'b01 | `addr + size_bytes` |
| WRAP | 2'b10 | `addr + size_bytes`, unless that equals the window's upper boundary `wrap_lower + size_bytes*burst_len`; then `wrap_lower` |
| reserved | 2'b11 | same as WRAP |

`burst_len = AxLEN + 1`. Both of these are computed from the incoming `i_axaddr`/`i_axlen`/`i_axsize` at the handshake and latched:
- `wrap_lower = start_addr & ~(size_bytes*burst_len − 1)`
- `wrap_mask = size_bytes*burst_len − 1`

Example: a 4-beat, 32-bit (AxSIZE = 2) WRAP burst starting at 0x88 shall visit 0x88, 0x8C, 0x80, 0x84; 0x90 and 0x94 shall not be touched. Address arithmetic is modulo 2**`PARA_ADDR_WD`. *Assumption:* no 4 KB boundary check. AxBURST = 2'b11 shall be addressed exactly like WRAP and shall not by itself cause `SLVERR`; its response is `OKAY` unless the alignment/oversize check of §5.3 fails.

### 5.5 Write data path (W → WFIFO)

`{i_wstrb, i_wdata}` shall be pushed into WFIFO when `i_wvalid & o_wready`, with `o_wready = ~wfifo_full`. W beats are paired with AWFIFO beats strictly in FIFO order: the n-th W beat goes with the n-th AW beat entry. `i_wlast` shall be ignored.

### 5.6 Request generation and arbitration

Requests, evaluated every cycle on the FIFO heads and current-cycle flags:
- `req_write = ~awfifo_empty & ~wfifo_empty & (~awfifo_head.last | ~bfifo_full)`. A burst's last beat is held off while BFIFO is full, so a completed write always has room to post its BRESP. A BFIFO pop in the same cycle does not release the hold.
- `req_read = ~arfifo_empty & ~rfifo_full & ~reg_rd_pending`

Arbiter `u_vlsi_arbiter` (one round-robin toggle shared by all banks; there is no per-bank arbiter):

| Condition | Result |
|---|---|
| Write only | Write granted |
| Read only | Read granted |
| Both, `arb_toggle` = 0 | Write granted |
| Both, `arb_toggle` = 1 | Read granted |

`arb_toggle` (1 bit) shall invert at the end of every cycle in which at least one request is granted (write or read), so neither side starves. It inverts at most once per cycle. The arbiter is fed `req_write`/`req_read` every cycle in every bank configuration, so with `PARA_NUM_BANKS` > 1 the toggle also inverts in cycles where a write and a read both issue to different banks (no conflict).

**`PARA_NUM_BANKS` = 1:** `w_wr_issue` = arbiter write grant; `w_rd_issue` = arbiter read grant. Write and read are mutually exclusive in a cycle.

**`PARA_NUM_BANKS` > 1:**
- `bank_sel_wr`/`bank_sel_rd = addr[BYTE_LSB +: BANK_WD] ^ addr[PARA_ADDR_WD-1 -: BANK_WD]`, computed on the AWFIFO head and the ARFIFO head every cycle.
- `bank_conflict = req_write & req_read & (bank_sel_wr == bank_sel_rd)`.
- `w_wr_issue = req_write & (bank_conflict ? arbiter_write_grant : 1)`; `w_rd_issue = req_read & (bank_conflict ? arbiter_read_grant : 1)`.
- Without a conflict, a write and a read shall both issue in the same cycle to their different banks.
- Each FIFO entry is one beat, so a burst spanning several banks is routed beat by beat.
- With `PARA_NUM_BANKS` = 1 every hash is 0 and the behaviour shall be identical to the single-bank case.

### 5.7 SRAM write

In a cycle where `w_wr_issue` = 1:
- AWFIFO and WFIFO shall both be popped at the end of the cycle;
- for bank `k = bank_sel_wr` (0 when single-bank): `o_sram_addr[k]` = AWFIFO head address, `o_sram_wdata[k]` = WFIFO head data;
- `o_sram_we[k]` = 1 and `o_sram_wstrb[k]` = WFIFO head strobe, unless the AWFIFO head `err` = 1. A SLVERR beat still pops both FIFOs and does its response bookkeeping, but `o_sram_we[k]` = 0 and `o_sram_wstrb[k]` = 0, so the SRAM is not modified;
- if the AWFIFO head `last` = 1, `{id, err ? SLVERR : OKAY}` shall be pushed into BFIFO at the end of the cycle.

In every cycle, `o_sram_wstrb[k]` shall be 0 for every bank whose `o_sram_we[k]` = 0 (non-selected banks, SLVERR beats, read-only and idle cycles). The controller does no other byte masking: WSTRB passes straight through on write cycles, and bytes not selected by WSTRB are left unchanged by the SRAM.

### 5.8 SRAM read and read pipeline

In a cycle where `w_rd_issue` = 1 (cycle t):
- `o_sram_oe[k]` = 1 and `o_sram_addr[k]` = ARFIFO head address, for `k = bank_sel_rd`;
- ARFIFO shall be popped at the end of t;
- `reg_rd_pending` shall be set, and the head's `id`, `last`, `err` and the bank index captured (`reg_rd_id_d`, `reg_rd_last_d`, `reg_rd_err_d`, `reg_rd_bank_d`).

In cycle t+1:
- `{id, err ? SLVERR : OKAY, i_sram_rdata[reg_rd_bank_d], last}` shall be pushed into RFIFO, and `reg_rd_pending` clears at the end of t+1;
- this push always succeeds: issue in t required RFIFO not full, and no other RFIFO push can occur before it;
- because `req_read` is gated by `~reg_rd_pending`, no read shall issue in t+1.

Only one read is in flight system-wide, even with several banks. A read beat with `err` = 1 shall be issued like any other: `o_sram_oe[k]` is asserted, and the SRAM word read is returned on `o_rdata` with `RRESP` = SLVERR.

### 5.9 Responses

- **B:** BFIFO entries `{id, bresp}` appear on `o_bid`/`o_bresp` with `o_bvalid = ~bfifo_empty`; popped when `i_bready & o_bvalid`.
- **R:** RFIFO entries `{id, rresp, rdata, last}` appear on `o_rid`/`o_rresp`/`o_rdata`/`o_rlast` with `o_rvalid = ~rfifo_empty`; popped when `i_rready & o_rvalid`.

### 5.10 ID handling and ordering

- The AXI ID shall be preserved end to end. AWID is captured at the AW handshake, carried in every AWFIFO entry and written to BFIFO with the last beat. ARID is captured at the AR handshake, carried in every ARFIFO entry and written to RFIFO with each beat.
- The controller shall not reorder anything: B responses come back in AW acceptance order and R bursts in AR acceptance order, regardless of ID values (ID reuse is allowed).
- *Assumption:* no ordering is enforced between the write and read paths. A read issued after a write to the same address on the other channel may observe old data unless the master waits for BRESP.

### 5.11 Narrow transfers

- AxSIZE only changes the address stride, the WRAP window and the error check.
- The SRAM always sees a full `PARA_DATA_WD` word; WSTRB narrows a write to the requested lane(s).
- Reads always return the full word; only the lanes at the requested address are meaningful to the master (AXI4 narrow-transfer model).

### 5.12 FIFOs

- Each FIFO is synchronous with `PARA_FIFO_DEPTH` entries and read/write pointers of `$clog2(PARA_FIFO_DEPTH)+1` bits that wrap modulo 2·`PARA_FIFO_DEPTH`. Full = MSBs differ and the lower bits are equal; empty = pointers are equal. Both flags come from the registered pointers (current-cycle state).
- Push while full is ignored, even if a pop happens in the same cycle. Pop while empty is ignored; a push into an empty FIFO in the same cycle is still performed. Push and pop together on a FIFO that is neither full nor empty both take effect, and occupancy is unchanged. The design never issues a push to a full FIFO or a pop from an empty one (see Error Handling).
- The head entry is output combinationally from storage. A pushed entry is visible at the output from the cycle after the push.

| FIFO | Width | Content |
|---|---|---|
| AWFIFO | `PARA_ADDR_WD + PARA_ID_WD + 2` | `{last, err, id, addr}` |
| WFIFO | `PARA_DATA_WD + PARA_DATA_WD/8` | `{wstrb, wdata}` |
| ARFIFO | `PARA_ADDR_WD + PARA_ID_WD + 2` | `{last, err, id, addr}` |
| RFIFO | `PARA_DATA_WD + PARA_ID_WD + 2 + 1` | `{id, rresp, rdata, last}` |
| BFIFO | `PARA_ID_WD + 2` | `{id, bresp}` |

At the defaults the widths are 38, 36, 38, 39 and 6 bits.

### 5.13 Corner cases

- AxLEN = 255 (256 beats) shall be handled completely, with no counter overflow.
- A W burst may arrive entirely before its AW, or the other way round; the write issues once both FIFO heads are present.
- With BFIFO full and `i_bready` low, non-last write beats continue to issue; the last beat waits until BFIFO has room.
- With RFIFO full, no read shall issue.
- With continuous back-to-back reads and RFIFO never full, reads issue every 2 cycles.
- In multi-bank mode, a write+read to the same bank is decided by the shared toggle; to different banks, both proceed in the same cycle and the toggle still inverts.
- A WRAP burst starting at the window's lower boundary visits addresses in increasing order with no wrap.
- A FIXED burst writes or reads the same address on every beat.
- AW and AR handshakes in the same cycle are independent and both accepted if their READYs are 1.

<!-- section: microarchitecture -->
## 6. Microarchitecture

The structure below is prescribed by the user's documents and shall be kept exactly as given. Module names are as in the documents. The instance names are the VLSIT names prescribed by REQ-051 (user decision; they replace the earlier names `u_axfsm_wr`/`u_axfsm_rd`, `u_awfifo` … `u_bfifo`, `u_arbiter`, `u_sram_misc`, `u_axi_fifopack`, `u_bank_arb`, `u_sram_fanout`). The hierarchy check expects exactly these instance names. In total there are 7 sub-module types under the top.

| Module | Function |
|---|---|
| `m_vlsi_axi4_sram` | Top level: instantiates and wires all sub-modules; owns all ports. |
| `m_vlsi_axfsm` | AXI address FSM: AW/AR handshake; burst address generation (INCR/WRAP/FIXED) sized by AxSIZE; alignment/oversize error detection; pushes `{last, err, id, addr}` into its FIFO. |
| `m_vlsi_fifo` | Parameterised synchronous FIFO with extra-MSB full/empty detection. |
| `m_vlsi_arbiter` | Round-robin toggle arbiter between write and read. With `PARA_NUM_BANKS` > 1 it only breaks ties when the pending write and read hash to the same bank. |
| `m_vlsi_sram_misc` | Structural wrapper only: instantiates the three sub-modules below, keeping its original external port list. |
| `m_vlsi_axi_fifopack` | Purely combinational. Packs AW/AR/W FIFO inputs and unpacks their fields; packs RFIFO/BFIFO inputs, including the err→SLVERR substitution; unpacks the R/B outputs. Muxes the read-selected bank's `i_sram_rdata` into RFIFO, reading the per-bank array directly. |
| `m_vlsi_bank_arb` | Request generation, address-to-bank XOR hash, grant/issue decision, FIFO push/pop control, `o_wready`. |
| `m_vlsi_sram_fanout` | Per-bank SRAM control/data fan-out (gates `we` for SLVERR beats) and the one-cycle read pipeline register (`reg_rd_pending` with id/last/err/bank). |

Hierarchy (instance : module — role):

```
m_vlsi_axi4_sram
├── u_vlsi_axfsm_0        : m_vlsi_axfsm        — write address FSM (AW)
├── u_vlsi_axfsm_1        : m_vlsi_axfsm        — read address FSM (AR)
├── u_vlsi_fifo_0         : m_vlsi_fifo         — AWFIFO
├── u_vlsi_fifo_1         : m_vlsi_fifo         — WFIFO
├── u_vlsi_fifo_2         : m_vlsi_fifo         — ARFIFO
├── u_vlsi_fifo_3         : m_vlsi_fifo         — RFIFO
├── u_vlsi_fifo_4         : m_vlsi_fifo         — BFIFO
├── u_vlsi_arbiter        : m_vlsi_arbiter      — shared write/read arbiter
└── u_vlsi_sram_misc      : m_vlsi_sram_misc    — structural wrapper
    ├── u_vlsi_axi_fifopack : m_vlsi_axi_fifopack
    ├── u_vlsi_bank_arb     : m_vlsi_bank_arb
    └── u_vlsi_sram_fanout  : m_vlsi_sram_fanout
```

Structural requirements carried over from the documents:
- The `PARA_NUM_BANKS` ≤ 1 case shall be its own `generate` branch. It reuses the original (pre-bank-hashing) grant expressions verbatim (`w_wr_issue` = arbiter write grant, `w_rd_issue` = arbiter read grant), rather than a single parameterised formula that reduces to them. Per revision 1.6 this branch lives in `m_vlsi_bank_arb`.
- `PARA_NUM_BANKS` > 1 uses the bank-hash-aware branch of §5.6.
- The split of `m_vlsi_sram_misc` into three sub-modules shall cause no change in behaviour or timing.

<!-- section: timing -->
## 7. Timing and Constraints

All values are in `i_clk` cycles. They follow from the FSM, FIFO and pipeline behaviour in §5; c0 is the handshake cycle.

| Item | Value |
|---|---|
| AW/AR handshake → first beat entry visible at FIFO head | 2 cycles (pushed at end of c1, head in c2) |
| AW handshake → first SRAM write (`o_sram_we`), W data already in WFIFO, no contention | c2 |
| W handshake → beat available for issue | next cycle |
| Last write issue (cycle t) → `o_bvalid` | t+1 |
| Single-beat write: AW handshake → `o_bvalid` (minimum) | c3 |
| AR handshake → `o_sram_oe` (minimum) | c2 |
| Read issue (cycle t) → RFIFO push | t+1 (SRAM data sampled in t+1) |
| Read issue (cycle t) → `o_rvalid` | t+2 |
| AR handshake → first `o_rvalid` (minimum) | c4 |
| Read issue rate | at most 1 per 2 cycles (0.5 beat/cycle), independent of `PARA_NUM_BANKS` |
| Write issue rate | up to 1 beat per cycle |
| Beat generation per address FSM | 1 beat per cycle while its FIFO is not full |
| Minimum interval between two AW (or AR) handshakes | AxLEN + 3 cycles (burst of AxLEN = L: pushes c1…c(L+1), idle transition c(L+2), ready in c(L+3)) |
| SRAM read latency required | exactly 1 cycle (`oe` in t, data in t+1) |

Combinational paths: `o_awready`/`o_arready` depend on FSM state and FIFO-full; `o_wready` depends on WFIFO-full. None depend on the input VALID. The SRAM control and data outputs are combinational from the FIFO heads and arbitration in the issue cycle.

### Synthesis constraint

- **Target clock:** `i_clk` = 100 MHz (10 ns period). The design shall meet this target at the default parameters, and this shall be checked in synthesis.
- The cycle behaviour above is fixed: the combinational issue path (FIFO head → request generation, bank hash and arbitration → SRAM control/data outputs) and the READY paths shall fit within one 10 ns cycle, with no extra pipeline stages added.
- I/O delays relative to `i_clk` toward the AXI master and the SRAM banks are not given by the documents. *Assumption:* I/O timing budgets are set by the integrating environment.

<!-- section: errors -->
## 8. Error Handling

**Detected error (the only one):** a misaligned start address (`AxADDR & (2**AxSIZE − 1) != 0`) or an oversized `AxSIZE > log2(PARA_DATA_WD/8)`. This is checked once per burst at the handshake.

- **Write burst with error:** every beat shall pop AWFIFO and WFIFO normally. `o_sram_we` and `o_sram_wstrb` stay 0 for those beats, so the SRAM is not modified. Exactly one B response is posted, with `BRESP` = `SLVERR` (2'b10) and the burst's AWID.
- **Read burst with error:** all AxLEN+1 beats are returned, each with `RRESP` = `SLVERR`, the burst's ARID, and `o_rlast` on the final beat. The SRAM is still read (`o_sram_oe` asserted for every beat), and the word read is returned on `o_rdata`; its content is meaningless to the master.
- After an error the controller shall carry on with no lasting effect. No sticky state, interrupt or recovery action is needed, and later aligned bursts return `OKAY`.

**Not detected / not modelled** (a valid, aligned access is always `OKAY`):
- address decode errors (`DECERR`), ECC, exclusive access (`EXOKAY`), locked transfers;
- WRAP bursts whose length is not 2, 4, 8 or 16 beats. *Assumption:* no error is reported and the address sequence is undefined;
- AxBURST = 2'b11 (reserved): addressed exactly like WRAP and answered `OKAY` (or `SLVERR` only if the alignment/oversize check fails);
- INCR bursts that cross a 4 KB boundary or overflow the address width. *Assumption:* no error; the address wraps modulo 2**`PARA_ADDR_WD`;
- W beat count not equal to AWLEN+1, or WLAST misplaced. W/AW pairing is pure FIFO order, so later bursts become misaligned. The master must not do this;
- WSTRB lanes that do not match AxSIZE/address. They are passed through as given.

**Overflow protection:** FIFO pushes and pops shall never overflow or underflow.
- Address FSMs push only when their FIFO is not full, and W is accepted only when WFIFO is not full.
- The last write beat is held off while BFIFO is full.
- A read issues only when RFIFO is not full and no read is pending.
- B and R pops happen only on handshake.

<!-- section: notes -->
## 9. Notes and Open Items

### Out of scope (no requirement)
- AXI4 signals not in the port list: AxLOCK, AxCACHE, AxPROT, AxQOS, AxREGION, AxUSER, WUSER/BUSER/RUSER. Also exclusive access and locked transfers.
- Read or write reordering by ID, and more than one outstanding SRAM read.
- Using banking to increase capacity: each bank receives the full un-hashed address, so banking only gives access parallelism.
- Multiple clock domains and low-power features.

### Rationale and history carried over from the user documents
- **v1.2:** the beat counter was widened by one bit, fixing silent loss of 256-beat bursts. The write grant now checks BFIFO room on a last beat, fixing BRESP loss.
- **v1.3:** added AxSIZE, WSTRB, SLVERR and multi-bank support.
- **v1.4:** WRAP addressing fixed. It previously behaved like INCR, e.g. 0x88, 0x8C, 0x90, 0x94. The testbench reference model `next_addr()` was fixed the same way, and regression test `wrap_window_spec_check_axi_sram_test` was added.
- **v1.5:** `o_sram_we` is suppressed for SLVERR writes. Multi-bank directed tests `multibank_concurrency_check_{2,4}banks_axi_sram_test` were added (`make run_multibank`; one compile per bank count via `PARA_NUM_BANKS`). The 0.5 beat/cycle read cap was documented.
- **v1.6:** `m_vlsi_sram_misc` was split into `m_vlsi_axi_fifopack`, `m_vlsi_bank_arb` and `m_vlsi_sram_fanout`, with no change in behaviour or timing.
- **Why the separate `PARA_NUM_BANKS` = 1 `generate` branch:** an earlier single-formula version was hand-proven equivalent at one bank. Even so, it corrupted data repeatably in `alternating_rw_burst_len`, `id_reuse_pending`, `read_modify_write_hazard` and `zero_delay_flood`. The cause was never found; it was removed by duplicating the original expressions.
- **Validation status per the documents:** the full 51-test regression passes at `PARA_NUM_BANKS` = 1, and both `run_multibank` configurations (2 and 4 banks) pass. `reset_mid_burst_axi_sram_test` is kept out of the default regression because the VIP master stops issuing after a reset mid-write.
- The `architecture.html` footer notes that the Tier-2 schematic `sram_misc_dp.html` still shows the pre-split internals. This is documentation only.
- **Clock target (user decision):** `i_clk` targets 100 MHz, checked in synthesis (see the Timing and Constraints section).

### TBDs
- Any reset synchronizer outside the block.
