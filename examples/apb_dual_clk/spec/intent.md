# Intent

## Overview

An APB-controlled event counter with two asynchronous clock domains. Software on the APB side (`pclk`) enables
and starts a counter that runs in a separate core clock domain (`clk_core`), reads its count and status back, and
gets an interrupt when the count reaches a programmed limit. The two clocks are asynchronous to each other and each
domain has its own asynchronous reset. Version A: every clock-domain and reset-domain crossing is specified
explicitly (correct by construction).

## Features

1. F1: APB4 slave (completer) in the `pclk` domain, zero wait states (pready held high).
2. F2: CTRL register (RW): ENABLE, START (self-clearing pulse request), CLEAR (self-clearing pulse request).
3. F3: LIMIT register (RW, 32 bits): the count at which the counter stops and `done` is set.
4. F4: STATUS register (RO): RUNNING, DONE.
5. F5: COUNT register (RO, 32 bits): the current counter value, readable at any time.
6. F6: A 32-bit counter in the `clk_core` domain: counts `clk_core` cycles while running; stops at LIMIT and sets DONE.
7. F7: Interrupt output `o_irq` in the `pclk` domain, level, high while DONE is set and IRQ_EN is set; cleared by CLEAR.
8. F8: IRQ_EN bit in CTRL (RW).

## Parameters

| Name       | Range     | Default | Meaning               |
|------------|-----------|---------|-----------------------|
| ADDR_WIDTH | 8..32     | 8       | APB address bus width |
| CNT_WIDTH  | 8..32     | 32      | Counter / LIMIT / COUNT width |

## Clocks and Resets

- Two clock domains, asynchronous to each other: `pclk` (APB side, frequency not constrained) and `clk_core`
  (counter side, frequency not constrained).
- Two resets, asynchronous assert, each in its own domain: `presetn` (active low, `pclk` domain) and `core_rstn`
  (active low, `clk_core` domain). They are independent: either can be asserted without the other.
- Reset de-assertion must be synchronized to its own domain's clock (a reset synchronizer per domain).
- All registers reset to 0.

## Clock Domain Crossings (all of them, no others)

- `pclk` -> `clk_core`, quasi-static control bits ENABLE: 2-flop synchronizer in `clk_core`.
- `pclk` -> `clk_core`, LIMIT (multi-bit, quasi-static, software changes it only while the counter is not running):
  captured in `clk_core` only after the synchronized ENABLE is seen; documented as quasi-static (a CDC waiver case).
- `pclk` -> `clk_core`, START and CLEAR request pulses: toggle synchronizer (toggle in `pclk`, 2-flop sync and
  edge detect in `clk_core`).
- `clk_core` -> `pclk`, RUNNING and DONE: 2-flop synchronizers in `pclk`.
- `clk_core` -> `pclk`, COUNT (multi-bit, changes every cycle): the counter value is Gray-coded in `clk_core`,
  passed through a 2-flop synchronizer per bit in `pclk` and decoded in `pclk`.

## Interfaces

APB slave: pclk, presetn, psel, penable, pwrite, paddr[ADDR_WIDTH-1:0], pwdata[31:0], prdata[31:0], pready.
Core: clk_core, core_rstn. Output: o_irq (level, pclk domain).

## Functional Behaviour

- START while ENABLE is set and the counter is idle: counter clears and counts up each `clk_core` cycle (RUNNING = 1).
- When the counter reaches LIMIT it stops (RUNNING = 0) and DONE = 1.
- CLEAR: counter and DONE cleared (any state).
- START while DONE is set is ignored until CLEAR.
- If ENABLE is cleared while running the counter stops (RUNNING = 0), DONE unchanged.

## Registers and Configuration

| Offset | Name   | Access | Fields |
|--------|--------|--------|--------|
| 0x00   | CTRL   | RW     | [0] ENABLE, [1] START (W1 pulse, reads 0), [2] CLEAR (W1 pulse, reads 0), [3] IRQ_EN |
| 0x04   | LIMIT  | RW     | [CNT_WIDTH-1:0] LIMIT, reset 0 |
| 0x08   | STATUS | RO     | [0] RUNNING, [1] DONE |
| 0x0C   | COUNT  | RO     | [CNT_WIDTH-1:0] current count (Gray-coded across the crossing, binary at the register) |
