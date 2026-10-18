# ASIC port rules

> **Purpose:** what an RTL written for simulation or an FPGA has to change before a standard-cell flow (Design Compiler, Formality, place and route) can take it. The `asic_port` step ports `src/rtl/` by these rules: it writes the work orders (what changes, which wrapper, which ports); the flow that owns the RTL writes the new code (`asic_handoff`). Behaviour, ports and requirement tags never change in a fix.
> **How to read it:** each rule has an id, a severity (`error`: no ASIC flow takes it; `warn`: it takes it, usually not as intended) and the fix. **Code rules** are found by a deterministic scan, ahead of the porter: do not port them again. **Port rules** are the porting work the scan cannot do; an order needs a file, a line and the code it is about.

## 1. Wrapper convention

A structure that a library or a macro implements goes behind a **wrapper module**: one small module that holds the structure and nothing else, named `m_<function>` (the VLSIT naming), with the same ports whatever implements it. The RTL keeps a behavioural body in the wrapper (so simulation and synthesis still work); the ASIC flow replaces the body, or the whole module, with the macro or the library cell. Never instantiate a macro or a library cell by its vendor name inside functional RTL.

## 2. Code rules (already found by the scan: do not port again)

| id | severity | what |
|---|---|---|
| `vendor_primitive` | error | FPGA vendor primitive instantiated (BUFG, RAMB, DSP48, LUT, SB_*, xpm_*, altsyncram, …) |
| `sim_only` | error | `$display`, `#delay`, `force`, `fork`, … outside `translate_off` |
| `fpga_attribute` | warn | `ram_style`, `use_dsp`, `keep`, `async_reg`, `syn_*`, … |
| `memory_array` | warn | an unpacked array of at least `min_mem_words` words |
| `mem_init` | warn | `$readmemh` / `$readmemb` |
| `initial_value` | warn | `initial` block or declaration initialiser |
| `tristate` | warn | `'z` driver |
| `gated_clock` | warn | `assign gclk = clk & en;` |

## 3. Port rules

- **`mem_sram_wrapper`** (warn) — A memory the scan cannot see as an array, or a memory spread over several arrays or a generate loop: a register file, a FIFO storage, a ROM or a lookup table of at least `min_mem_words` words, an array behind a packed vector. Also a memory the scan reports (`memory_array`) that sits in a module with other logic. **Fix:** move the storage into a wrapper module (`m_<name>_sram` / `m_<name>_rf` / `m_<name>_rom`) with synchronous ports (address, data in, data out, write enable, optional byte enables, clock) and no other logic; the macro (SRAM, register file, ROM) replaces its body. State the width, depth, number of ports and the read latency the wrapper must keep. A memory below `min_mem_words` stays flip-flops: not a finding.
- **`sync_lib_cell`** (warn) — A clock-domain crossing synchronizer written as plain registers: a two- or three-flop chain on a single bit, a gray-code pointer synchronizer, a pulse or handshake synchronizer, a reset synchronizer (asynchronous assert, synchronous release). **Fix:** one wrapper module per kind (`m_sync_bit`, `m_sync_reset`, …) whose body the library's synchronizer cell replaces, so that placement keeps the flops together and the CDC and timing tools can recognise it; remove any FPGA `async_reg` attribute. A synchronizer that is already behind such a wrapper is not a finding.
- **`clock_gate_icg`** (warn) — A gated clock the scan did not see: a clock gated in an `always` block, a latch-based gate, a clock enable that drives a clock pin. **Fix:** an integrated clock-gating cell behind a wrapper (`m_clk_gate`) with `i_clk`, `i_en`, `i_dft_scan_en` (or the test enable the project names) and `o_gclk`; or a clock-enable on the registers when the saving does not matter.
- **`clock_mux_divider`** (warn) — A clock mux, a clock divider or a generated clock built from logic or from a counter that drives clock pins. **Fix:** a glitch-free clock-mux cell or a divider cell behind a wrapper (`m_clk_mux`, `m_clk_div`); clocks leave the wrapper on dedicated nets.
- **`pll_macro_wrapper`** (warn) — Behaviour written to stand for a PLL / MMCM / DLL: a clock multiplied or phase-shifted in a model, an `ifdef FPGA` clocking block. **Fix:** a wrapper (`m_pll`) with the reference clock, the lock output and the divider settings as ports; the PLL macro replaces its body.
- **`pad_wrapper`** (warn) — A bidirectional or tri-state port, a pad, an I/O buffer or a level shifter written in the core. **Fix:** a pad wrapper at the top (`io_` ports only there); the core sees separate `i_` / `o_` / `o_oe` signals.
- **`latch_inferred`** (warn) — A latch inferred by an incomplete `if` / `case` in combinational code, not written on purpose. **Fix:** assign every output in every branch (a default before the `case`); a deliberate latch goes behind a wrapper cell.
- **`async_reset_style`** (warn) — A mix of reset styles in one clock domain, a reset that is also data, or an asynchronous reset deasserted without a synchronizer. **Fix:** one reset style per domain, the deassertion through the reset synchronizer wrapper (`sync_lib_cell`).
- **`fpga_only_branch`** (warn) — Code under `ifdef FPGA` / `XILINX` / `ALTERA` / `ECP5` (or a generate on a target parameter) that the ASIC build would take or skip differently. **Fix:** the ASIC branch is the default; the FPGA branch stays only for the FPGA flow's build.

## 4. What is not ported

- Anything the code rules cover (section 2): the scan's findings carry their own fix.
- Plain registers, counters, FSMs, adders, multipliers, comparators, muxes: synthesis maps them to library cells without help.
- A structure already behind a wrapper of section 1.
- A memory below `min_mem_words` words.
- Testbench, SVA and model code: the porter never reads them.
- Style, naming or lint remarks: the VLSIT flow's rtl step owns them.

## 5. Work orders

Each order: the rule id from section 3, the module (the file name without extension), the line of the code it is about, that code (shortened), the severity above, the wrapper module to create, and the port in one to four sentences: the wrapper's ports (name, direction, width), its latency and where it is instantiated. An order is complete when the RTL author can write it without asking. Fewer, better orders; nothing to port is a valid answer.
