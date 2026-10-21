---
name: apb-csr-tool
description: What the APB-CSR-Generator makes (APB4 slave, field types, ports, CDC bridge), its limits, and how the generated module connects to the rest of the design.
---

# The APB-CSR-Generator

Python tool (https://github.com/nguyenquanicd/APB-CSR-Generator). Input: an Excel workbook (a configuration block and register rows; Q3TUI writes it from
`src/rtl/csr/csr_map.json`). Output: one SystemVerilog **APB4 slave** module, plus `m_vlsi_synch` (a 2-flop synchronizer, only used when asynchronous).

## What the module does
- APB4: SETUP / ACCESS phases, PREADY the cycle after, PSLVERR (when `i_slverr_en`) on a misaligned address (`i_paddr[1:0] != 0`), on a partial `i_pstrb` write
  (only full 4-byte writes are accepted) and on a non-secure `i_pprot[1]` access when `i_protect_en`.
- Fields: `rw` (software write, reads back), `ro` (read passes a hardware input straight through: no flop), `rwi` (software write wins over a same-cycle
  hardware write), `w1c` (software writes 1 to clear, hardware sets through `i_hw_we_*`/`i_hw_wdata_*`). `reserved` reads 0, writes ignored, no ports.
- Asynchronous = 1: registers flop on `i_reg_clk`/`i_reg_rstn`, a request/acknowledge toggle bridge crosses from `i_bus_clk`. Asynchronous = 0: one clock, no bridge.

## Ports of the generated module
Bus (always): `i_bus_clk i_bus_rstn i_psel i_penable i_paddr i_pwrite i_pwdata i_pstrb i_pprot i_protect_en i_slverr_en` -> `o_pready o_pslverr o_prdata`.
Asynchronous: also `i_reg_clk i_reg_rstn`. Per field, named `<reg>_<field>`: `ro` -> `i_<reg>_<field>` (hardware drives it); `rw` -> `o_<reg>_<field>`;
`rwi`/`w1c` -> `o_<reg>_<field>` plus `i_hw_wdata_<reg>_<field>` and `i_hw_we_<reg>_<field>` (one enable bit for the whole field).

## Connecting it (the design's side)
- Bus ports connect to the design's own APB slave ports (the top, or the module the spec names); `i_protect_en`/`i_slverr_en` come from the design's ports or are tied
  as the spec says; `o_pready/o_pslverr/o_prdata` go back to the bus.
- Clocks and resets: `i_bus_clk`/`i_bus_rstn` to the bus clock and reset; `i_reg_clk`/`i_reg_rstn` to the register clock domain.
- Per-field ports go to the logic that produces (`ro`, hardware writes) or consumes (`rw`/`rwi`/`w1c` outputs) the value. A hardware input port left tied to 0 is a decision the
  spec must support; say so in a comment.

## Limits you must respect
- APB only, 32-bit data, full-strobe writes only (no byte-masked writes), `PPROT[1]` only.
- The tool does **not** validate the map (duplicate offsets, overlapping bits, bad widths give broken RTL silently): Q3TUI checks before it runs.
- The synchronizer `m_vlsi_synch` is a generic behavioural 2-flop model, a placeholder for a verified library cell: the integrator should be told when the design is asynchronous.
- The generated file is never edited by hand or rewritten by an LLM: change the register map (or the spec) and generate again.
