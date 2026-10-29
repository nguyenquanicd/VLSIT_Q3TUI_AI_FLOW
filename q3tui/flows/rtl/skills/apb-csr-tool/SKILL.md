---
name: apb-csr-tool
description: What the APB-CSR-Generator makes (APB4 slave, field types, ports, CDC bridge), its limits, and how the generated module connects to the rest of the design.
---

# The APB-CSR-Generator

Python tool (https://github.com/nguyenquanicd/APB-CSR-Generator). Input: an Excel workbook (a configuration block and register rows; Q3TUI writes it from
`src/rtl/csr/csr_map.json`). Output: one SystemVerilog **APB4 slave** module, plus a 2-flop synchronizer model (only used when asynchronous):
`m_csr_sync` from version 2.0 of the tool, `m_vlsi_synch` before. Version 2.0 renamed the bus, clock and reset ports (below), not the behaviour.

## What the module does
- APB4: SETUP / ACCESS phases, PREADY the cycle after, PSLVERR (when `i_slverr_en`) on a misaligned address (`PADDR[1:0] != 0`), on a partial `PSTRB` write
  (only full 4-byte writes are accepted) and on a non-secure `PPROT[1]` access when `i_protect_en`.
- Fields: `rw` (software write, reads back), `ro` (read passes a hardware input straight through: no flop), `rwi` (software write wins over a same-cycle
  hardware write), `w1c` (software writes 1 to clear, hardware sets through `i_hw_we_*`/`i_hw_wdata_*`). `reserved` reads 0, writes ignored, no ports.
- Asynchronous = 1: registers flop on the register clock, a request/acknowledge toggle bridge crosses from the APB clock. Asynchronous = 0: one clock, no bridge.

## Ports of the generated module
The csr step lists the generated module's real ports (`csr_report.json` `ports`, `q3tui act csr_show`): use those names. They depend on the tool's version:

| | version 2.x | version 1.x |
|---|---|---|
| APB clock / reset | `i_clk_apb i_rst_n_apb` | `i_bus_clk i_bus_rstn` |
| register clock / reset (asynchronous only) | `i_clk_reg i_rst_n_reg` | `i_reg_clk i_reg_rstn` |
| APB inputs | `i_apb_psel i_apb_penable i_apb_pwrite i_apb_paddr i_apb_pwdata i_apb_pstrb i_apb_pprot` | `i_psel i_penable i_pwrite i_paddr i_pwdata i_pstrb i_pprot` |
| APB outputs | `o_apb_pready o_apb_pslverr o_apb_prdata` | `o_pready o_pslverr o_prdata` |

Both: `i_protect_en i_slverr_en`. Per field, named `<reg>_<field>`: `ro` -> `i_<reg>_<field>` (hardware drives it); `rw` -> `o_<reg>_<field>`;
`rwi`/`w1c` -> `o_<reg>_<field>` plus `i_hw_wdata_<reg>_<field>` and `i_hw_we_<reg>_<field>` (one enable bit for the whole field).

## Connecting it (the design's side)
- Bus ports connect to the design's own APB slave ports (the top, or the module the spec names); `i_protect_en`/`i_slverr_en` come from the design's ports or are tied
  as the spec says; the PREADY / PSLVERR / PRDATA outputs go back to the bus.
- Clocks and resets: the APB clock and reset ports to the bus clock and reset; the register clock and reset ports to the register clock domain.
- Per-field ports go to the logic that produces (`ro`, hardware writes) or consumes (`rw`/`rwi`/`w1c` outputs) the value. A hardware input port left tied to 0 is a decision the
  spec must support; say so in a comment.

## Limits you must respect
- APB only, 32-bit data, full-strobe writes only (no byte-masked writes), `PPROT[1]` only.
- The tool does **not** validate the map (duplicate offsets, overlapping bits, bad widths give broken RTL silently): Q3TUI checks before it runs.
- The synchronizer model (`m_csr_sync` / `m_vlsi_synch`) is a generic behavioural 2-flop model, a placeholder for a verified library cell: the integrator should be told when the design is asynchronous.
- The generated file is never edited by hand or rewritten by an LLM: change the register map (or the spec) and generate again.
