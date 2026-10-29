"""Whether a synthesis result is believable for the design (step `timing_util_report`, docs/spec/fpga-flow.md "Is the result the design?").

Targets alone pass a design the tool trimmed away: rv32iv (an RV32I core, a vector unit, 32 KB of instruction and data memory) synthesized
to 48 LUTs, 41 FFs and no block RAM, and every target was met. Its instruction memory was initialized only by a Quartus attribute
(`ram_init_file`) that Vivado ignores and its write port was tied off, so it was a ROM of zeros: Vivado removed it (Synth 8-6014) and
everything that read it folded to constants.

Deterministic first: code gathers the evidence (what the tool removed, the storage the RTL declares, the utilization per instance), and
decides whether it is worth a judgment; one read-only LLM stage judges it against the design (`SynthJudgment`)."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from q3tui.steps.common import QuestionOption

# Vivado: storage removed, sequential elements removed, outputs tied to a constant, ports nothing reads; Yosys: unused cells
_REMOVED_STORAGE = re.compile(r"\[Synth 8-6014\] Unused sequential element (\S+) was removed")
_REMOVED_SEQ = re.compile(r"\[Synth 8-3332\] Sequential element \((\S+)\) is unused and will be removed")
_CONST_PORT = re.compile(r"\[Synth 8-3917\] design (\S+) has port (\S+) driven by constant (\S+)")
_NO_LOAD = re.compile(r"\[Synth 8-7129\] Port (\S+) in module (\S+) is either unconnected or has no load")
_YOSYS_UNUSED = re.compile(r"Removed (\d+) unused cells")
# Quartus: registers whose fanout went, outputs stuck at a constant, a memory initialization file it could not read
_Q_LOST = re.compile(r"(\d+) registers? lost all (?:its|their) fanouts")
_Q_STUCK = re.compile(r"Output pins are stuck at VCC or GND|(\S+) .*?stuck at (?:VCC|GND)")
_Q_NO_INIT = re.compile(r"(?i)(?:can't|cannot|unable to) (?:find|open|read)[^\n]*?([\w./-]+\.(?:mif|hex|mem))")
# an unpacked array (a memory) in the RTL: `logic [W-1:0] mem [0:D-1];`, with an attribute in front or not
_ARRAY = re.compile(r"^[ \t]*(?:\(\*[^*]*\*\)[ \t]*)?(?:logic|reg|bit)\b[^;=()\n]*?\b(\w+)[ \t]*(\[[^\]\n]+\](?:[ \t]*\[[^\]\n]+\])*)[ \t]*;", re.M)


class JudgeCause(BaseModel):
    what: str = Field(description="What was lost and why, in one sentence")
    evidence: str = Field(description="The log line, report row or RTL line it rests on")
    owner: Literal["rtl", "spec", "constraints", "synthesis_setup", "unsure"] = Field(
        description="Whose fix it is: rtl (a module), spec (a decision of the spec, e.g. how a memory is initialized for this part), "
                    "constraints (the SDC), synthesis_setup (the flow's options: part, top, defines), unsure")
    module: str = Field("", description="rtl: the module to change (the file name without extension)")
    request: str = Field("", description="The change that would keep the logic, for its owner")


class SynthJudgment(BaseModel):
    plausible: bool = Field(description="The result is what this design should synthesize to: nothing the design needs was removed")
    summary: str = Field(description="The judgment in one or two sentences")
    expected: str = Field("", description="Roughly what the design should take (LUT / FF / block RAM / DSP), from its RTL and spec")
    causes: list[JudgeCause] = Field(default_factory=list, description="Implausible: why, one entry per cause")
    question: str = Field("", description="Implausible: the decision the user has to make to get a real result, as a question")
    options: list[QuestionOption] = Field(default_factory=list, description=(
        "Implausible: 2–4 ways to get a real result, the one you recommend first; each label a complete decision worded as the answer "
        "itself (what changes, where: the RTL, the spec, the SDC, the flow's part / tool), with its impact"))


@dataclass
class Evidence:
    removed_storage: list[str] = field(default_factory=list)  # Synth 8-6014: the element (often a memory) removed
    removed_seq: Counter = field(default_factory=Counter)  # Synth 8-3332 per instance (the first two levels)
    constant_ports: list[str] = field(default_factory=list)  # Synth 8-3917
    no_load: Counter = field(default_factory=Counter)  # Synth 8-7129 per module
    yosys_unused: int = 0
    memories: list[str] = field(default_factory=list)  # "<file>: <declaration>"
    rtl_lines: int = 0
    rtl_files: int = 0

    @property
    def removed_seq_total(self) -> int:
        return sum(self.removed_seq.values())


def gather(log_text: str, rtl_files: list[Path]) -> Evidence:
    ev = Evidence()
    ev.removed_storage = list(dict.fromkeys(_REMOVED_STORAGE.findall(log_text)))
    for m in _REMOVED_SEQ.finditer(log_text):
        ev.removed_seq[".".join(m.group(1).split("/")[:2])] += 1
    ev.constant_ports = list(dict.fromkeys(f"{d}.{p} = {v}" for d, p, v in _CONST_PORT.findall(log_text)))
    for _port, mod in _NO_LOAD.findall(log_text):
        ev.no_load[mod] += 1
    ev.yosys_unused = sum(int(n) for n in _YOSYS_UNUSED.findall(log_text))
    for n in _Q_LOST.findall(log_text):
        ev.removed_seq["(registers that lost all their fanouts)"] += int(n)
    ev.constant_ports += [m.group(1) or "output pins stuck at VCC or GND" for m in _Q_STUCK.finditer(log_text)]
    ev.removed_storage += [f"initialization file not found: {n}" for n in dict.fromkeys(_Q_NO_INIT.findall(log_text))]
    for f in rtl_files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        ev.rtl_files += 1
        ev.rtl_lines += text.count("\n")
        for m in _ARRAY.finditer(text):
            ev.memories.append(f"{f.name}: {' '.join(m.group(0).split())}")
    return ev


def suspicious(ev: Evidence, util: dict) -> list[str]:
    """Why the result needs a judgment (empty: nothing points at removed logic)."""
    why = []
    if ev.removed_storage:
        why.append(f"the tool removed storage: {', '.join(ev.removed_storage[:4])}")
    if ev.removed_seq_total >= 10:
        why.append(f"{ev.removed_seq_total} sequential elements removed")
    if ev.constant_ports:
        why.append(f"{len(ev.constant_ports)} output(s) driven by a constant")
    if ev.yosys_unused >= 100:
        why.append(f"{ev.yosys_unused} unused cells removed")
    lut = (util.get("lut") or {}).get("used")
    if ev.memories and not any((util.get(k) or {}).get("used") for k in ("bram_tile", "lut_ram", "uram", "mem_bits")):
        why.append(f"the RTL declares {len(ev.memories)} memory array(s) and the result has no block RAM or LUTRAM")
    if lut is not None and ev.rtl_lines >= 1500 and lut < ev.rtl_lines / 20:
        why.append(f"{lut} LUTs for {ev.rtl_lines} lines of RTL")
    return why


_TARGET = re.compile(r"\b(MHz|GHz|period|part|device|family|FPGA|ASIC|Quartus|Vivado|Yosys|nextpnr|Arria|Stratix|Cyclone|Agilex|MAX ?10|"
                     r"Kintex|Artix|Virtex|Spartan|Zynq|UltraScale|ECP5|iCE40|Gowin|PolarFire|xc[0-9a-z]+|10A[XS]\w*|5C\w+)\b", re.I)


def target_lines(spec_text: str, limit: int = 30) -> list[str]:
    """The spec's lines about the target: device, tool, clock (what this run should match)."""
    return [" ".join(line.split())[:300] for line in spec_text.splitlines() if _TARGET.search(line)][:limit]


def prompt(*, part: str, top: str, tool: str, clocks: list[dict], util: dict, timing: dict, ev: Evidence, why: list[str], hier: str,
           spec: str, targets: list[str], rtl: list[str]) -> str:
    rows = "\n".join(f"- {k.upper()}: {u.get('used')} / {u.get('available')} ({u.get('pct')}%)" for k, u in util.items())
    parts = ["# Task: is this synthesis result the design?", "",
             f"Part {part}, tool {tool}, top `{top}`, out-of-context synthesis (the block alone: logic that reaches no output of `{top}` is "
             "removed); SDC clocks: " + (", ".join(f"{c.get('name')} {c.get('period_ns')} ns" for c in clocks) or "none"), "",
             "## What the spec says about the target (device, tool, clock): does this run match it?",
             *(f"- {t}" for t in targets), *([] if targets else ["(nothing found)"]), "",
             "## Utilization (ground truth)", rows, f"- setup slack WNS {timing.get('wns_ns')} ns", "",
             "## Why code asks", *(f"- {w}" for w in why), "",
             "## What the tool removed (from its log)",
             f"- storage removed (Synth 8-6014): {', '.join(ev.removed_storage[:20]) or 'none'}",
             f"- sequential elements removed (Synth 8-3332), per instance: "
             + (", ".join(f"{k} ×{n}" for k, n in ev.removed_seq.most_common(20)) or "none"),
             f"- outputs driven by a constant (Synth 8-3917): {', '.join(ev.constant_ports[:20]) or 'none'}",
             f"- ports nothing reads (Synth 8-7129), per module: " + (", ".join(f"{k} ×{n}" for k, n in ev.no_load.most_common(15)) or "none"),
             *([f"- unused cells removed (Yosys): {ev.yosys_unused}"] if ev.yosys_unused else []), "",
             f"## Memories the RTL declares ({len(ev.memories)})", *(f"- {m}" for m in ev.memories[:30]), "",
             f"## The RTL ({ev.rtl_files} files, {ev.rtl_lines} lines; read them)", ", ".join(rtl[:60]), ""]
    if hier.strip():
        parts += ["## Utilization per instance (report_utilization -hierarchical, start)", "```", hier.strip()[:6000], "```", ""]
    if spec.strip():
        parts += ["## The specification (start)", spec.strip()[:6000], ""]
    return "\n".join(parts)
