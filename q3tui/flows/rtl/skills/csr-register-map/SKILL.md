---
name: csr-register-map
description: Read the registers of a spec (offsets, fields, access types, resets) into the structured map the APB-CSR-Generator takes, without inventing anything.
---

# From the spec to a register map

The registers are generated, so the map must say exactly what the spec says. A wrong field type or offset becomes wrong RTL that no one wrote.

## Where to look
The spec's register / memory-map / CSR / SFR sections (tables of offset, name, bits, access, reset), the interface section (the bus: APB, the address
width), the clocking section (is there a register clock of its own, asynchronous to the bus clock?), and the requirements that mention registers
(`req_ids`). Take names from the spec as written, lowercased where the table is in capitals; keep a spec's field names.

## The map
- `module_name`: the module of the design's module map that is the register block, if one is (use its exact name); else `<top>_csr`.
- `parent`: the module that instantiates it: the one the module map says, else the top.
- `addr_width`: the address bits the bus carries to the block (the spec's address width; 16 when it says nothing). `data_width` is 32 (the tool's only width).
- `asynchronous`: true only when the spec says the registers are clocked by a clock asynchronous to the bus clock (the tool then adds a handshake bridge).
- Each register: `name`, `offset` (bytes, a multiple of 4, relative to the block's base), the fields with `msb`/`lsb`, `type`, `reset`, a one-line description. List
  the fields the spec gives; bits it leaves out are filled as `reserved` by code, do not list them.
- A register wider than 32 bits is two registers (low word first); say so in `summary`.

## Field types: pick by what the *hardware* does, not only the software
| Spec says | type |
|---|---|
| software reads and writes; the value only configures hardware | `rw` (hardware sees it on an output port) |
| read-only status / counter / version, driven by hardware | `ro` (hardware drives an input port) |
| software writes it, hardware may also load or update it (a start bit that hardware clears, a value hardware overwrites) | `rwi` |
| a status/interrupt flag that hardware sets and software clears by writing 1 | `w1c` |
| reserved / unused bits | not listed |
Do not use `w1c` for a plain status bit software never clears, nor `rw` for a bit the hardware must change. A *write-only trigger / self-clearing pulse*
or a *read-to-clear* bit is not a type the tool has: use the closest (`rwi` with hardware clearing the bit) and say so in `summary`, or ask.

## Questions, not guesses
Ask (kind `spec_gap`, with a default) when: two registers share an offset, a field's access type is missing or contradicts the behaviour described, a reset value
is not given, the register clock relationship is unclear. A reasonable default is fine; an invented register is not.

## Check it
Call `csr_check` with your map: it lists what is wrong (overlap, duplicate offset, misaligned, a reset too wide) and, when the map is sound, the ports the
generated module will have. Fix problems and check again before you answer.
