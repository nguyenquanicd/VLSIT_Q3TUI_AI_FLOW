# Intent

## Overview

A simple APB (AMBA APB) slave interface block. It sits on an APB bus as a completer and
gives an APB requester read and write access to a single internal register. Address and
data widths are design-time parameters.

## Features

1. F1: APB slave (completer) interface following the APB protocol (setup phase, access phase).
2. F2: One software-visible read/write data register.
3. F3: Write: an APB write transfer updates the register.
4. F4: Read: an APB read transfer returns the register contents.
5. F5: Parameterised data width and address width.

## Parameters

| Name       | Range | Default | Meaning                |
|------------|-------|---------|------------------------|
| DATA_WIDTH | tbd   | 32      | APB data bus width     |
| ADDR_WIDTH | tbd   | tbd     | APB address bus width  |

## Clocks and Resets

- Clock frequency: single APB clock (pclk), frequency not constrained
- Clock domains: one (pclk)
- Reset: presetn, active low, asynchronous assert; register resets to 0

## Interfaces

Standard APB slave port set: pclk, presetn, psel, penable, pwrite,
paddr[ADDR_WIDTH-1:0], pwdata[DATA_WIDTH-1:0], prdata[DATA_WIDTH-1:0], pready.

## Functional Behaviour

- Zero-wait-state slave: pready is held high (no stalls).
- Write transfer: register <= pwdata at the end of the access phase.
- Read transfer: prdata drives the register value during the access phase.

## Registers and Configuration

One register at offset 0: DATA, read/write, DATA_WIDTH bits, reset value 0.
