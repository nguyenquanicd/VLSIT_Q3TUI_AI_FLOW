"""The ASIC port check (code, no LLM): what in the RTL an ASIC flow cannot take as it is.

An FPGA-minded (or simulation-minded) design carries things a standard-cell flow has no answer to: vendor primitives, FPGA
attributes, memories that Design Compiler would build from flip-flops, power-up values, simulation-only statements. Each finding
names the module, the line, the rule and the fix that belongs to it; the fixes go to the flow that owns the RTL as scoped change
requests (`asic_handoff`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str  # "error": no ASIC flow can take it; "warn": it can, but usually not as intended
    title: str
    fix: str


RULES = {r.id: r for r in (
    Rule("vendor_primitive", "error", "FPGA vendor primitive instantiated",
         "replace the primitive by behavioural RTL (the library cells are inferred by synthesis) or by the ASIC macro of the same function"),
    Rule("sim_only", "error", "simulation-only statement in synthesizable RTL",
         "remove it, or wrap it in `// synopsys translate_off` … `translate_on` (or `ifndef SYNTHESIS`)"),
    Rule("fpga_attribute", "warn", "FPGA synthesis attribute",
         "remove it: the ASIC tool ignores it, and the intent (memory style, DSP use, keep, async register) needs an ASIC equivalent or none"),
    Rule("memory_array", "warn", "memory array without a macro",
         "a memory this size becomes flip-flops in standard cells: keep it behind a small wrapper module so an SRAM / register-file "
         "macro can replace it (single module with the memory and its port logic only), or reduce the depth"),
    Rule("mem_init", "warn", "memory initialised from a file ($readmem)",
         "an ASIC memory has no contents at power-up: load it through a bus / reset sequence, or keep $readmem for simulation only"),
    Rule("initial_value", "warn", "power-up value (initial block / declaration initialiser)",
         "an ASIC register has no power-up value: give it a reset (and remove the initialiser), or make it a don't-care"),
    Rule("tristate", "warn", "tri-state / high-Z driver",
         "an internal tri-state bus is not allowed in a standard-cell flow: use a mux; only pads may be tri-state"),
    Rule("gated_clock", "warn", "clock gated or generated in logic",
         "use an integrated clock-gating cell (ICG) through a small wrapper module, or a clock-enable on the registers"),
)}

_PRIMITIVES = (r"xpm_\w+|RAMB\w*|DSP48\w*|BUFG\w*|BUFH\w*|BUFR|IBUF\w*|OBUF\w*|IOBUF\w*|IDDR\w*|ODDR\w*|ISERDES\w*|OSERDES\w*|IDELAY\w*|"
               r"FD[CPRSE]{1,3}\b|LUT[1-6]\b|LUT[1-6]_[LD]|CARRY[48]|MUXF[78]|SRL\w+|MMCM\w*|PLL\w*|BRAM_\w+|altsyncram|altpll|altera_\w+|"
               r"lpm_\w+|SB_\w+|cyclone\w*|stratix\w*")
_ATTRS = r"ram_style|rom_style|use_dsp\w*|keep\w*|dont_touch|mark_debug|async_reg|shreg_extract|srl_style|max_fanout|syn_\w+|altera_\w+|ramstyle|romstyle|full_case|parallel_case"
_PATTERNS = (
    ("vendor_primitive", re.compile(rf"^\s*(?:{_PRIMITIVES})\b\s*(?:#\s*\(|\w+\s*\()")),
    ("sim_only", re.compile(r"\$(?:display\w*|write\w*|monitor|strobe|finish|stop|random|urandom\w*|fopen|fclose|fdisplay|time|stime|realtime)\b|"
                            r"^\s*(?:force|release)\b|\bfork\b|^\s*wait\s*\(|#\s*\d+(?:\.\d+)?\s*(?:ns|ps|us)?\s*[\w(;]")),
    ("fpga_attribute", re.compile(rf"\(\*\s*(?:{_ATTRS})\b[^*]*\*\)", re.I)),
    ("mem_init", re.compile(r"\$readmem[bh]\b")),
    ("initial_value", re.compile(r"^\s*initial\b|^\s*(?:logic|reg|bit|byte|int|integer)\b[^;=()]*?[\w\]]\s*=(?!=)\s*[^;]+;")),
    ("tristate", re.compile(r"\d*'[bBhHdDoO]\s*[zZ?]+|\bz\b\s*[;:)]")),
    ("gated_clock", re.compile(r"\bassign\s+\w*(?:clk|clock)\w*\s*=\s*[^;]*[&|^?~]")),
)
_MEM = re.compile(r"^\s*(?:logic|reg|bit|byte|wire)\b(?:\s*\[[^\]]+\])*\s*(\w+)\s*\[\s*([^\]]+?)\s*\]\s*;")
_OFF = re.compile(r"(?://|/\*)\s*(?:synopsys|pragma|synthesis)\s+translate_off")
_ON = re.compile(r"(?://|/\*)\s*(?:synopsys|pragma|synthesis)\s+translate_on")
_MODULE = re.compile(r"^\s*module\s+(\w+)")


def _strip(line: str, in_block: bool) -> tuple[str, bool]:
    """The code of a line without its comments (and whether a /* … */ comment goes on)."""
    out, i = [], 0
    while i < len(line):
        if in_block:
            j = line.find("*/", i)
            if j < 0:
                return "".join(out), True
            i, in_block = j + 2, False
        elif line.startswith("/*", i):
            in_block = True
            i += 2
        elif line.startswith("//", i):
            break
        else:
            out.append(line[i])
            i += 1
    return "".join(out), in_block


def _depth(expr: str, params: dict[str, int]) -> int | None:
    """The number of words an unpacked dimension `[N]` / `[0:N-1]` / `[N-1:0]` stands for (None: not known)."""
    expr = expr.strip()
    if ":" in expr:
        a, _, b = expr.partition(":")
        x, y = _eval(a, params), _eval(b, params)
        return abs(x - y) + 1 if x is not None and y is not None else None
    return _eval(expr, params)


def _eval(expr: str, params: dict[str, int]) -> int | None:
    e = re.sub(r"\b[A-Za-z_]\w*\b", lambda m: str(params[m.group(0)]) if m.group(0) in params else m.group(0), expr.replace("'d", "").strip())
    if re.fullmatch(r"[\d\s+\-*/()]+", e):
        try:
            return int(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 - digits and + - * / ( ) only
        except Exception:  # noqa: BLE001
            return None
    return None


def scan_file(path: Path, root: Path, min_words: int = 16, waive: tuple[str, ...] = ()) -> list[dict]:
    """The findings of one RTL file: [{rule, severity, module, file, line, text, fix}]."""
    out: list[dict] = []
    module, off, in_block = path.stem, False, False
    params: dict[str, int] = {}
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    for n, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if _OFF.search(raw):
            off = True
        if _ON.search(raw):
            off = False
            continue
        code, in_block = _strip(raw, in_block)
        if off or not code.strip():
            continue
        m = _MODULE.match(code)
        if m:
            module = m.group(1)
        for pm in re.finditer(r"\b(?:parameter|localparam)\b(?:\s+\w+)?(?:\s*\[[^\]]+\])?\s+(\w+)\s*=\s*(\d+)\b", code):
            params[pm.group(1)] = int(pm.group(2))
        found: list[tuple[str, str]] = []
        for rid, rx in _PATTERNS:
            if rx.search(code):
                found.append((rid, code.strip()))
        mm = _MEM.match(re.sub(r"\(\*.*?\*\)\s*", "", code))  # (a synthesis attribute may come first)
        if mm:
            depth = _depth(mm.group(2), params)
            if depth is None or depth >= min_words:
                found.append(("memory_array", code.strip() + (f"  ({depth} words)" if depth else "  (depth is a parameter)")))
        for rid, text in found:
            if rid in waive or rid not in RULES:
                continue
            r = RULES[rid]
            out.append({"rule": rid, "severity": r.severity, "module": module, "file": rel, "line": n, "text": text[:160], "fix": r.fix})
    return out


def scan(files: list[Path], root: Path, min_words: int = 16, waive: tuple[str, ...] = ()) -> list[dict]:
    """The findings of all files, numbered P1, P2, … in file / line order."""
    found = [f for p in files for f in scan_file(p, root, min_words, waive)]
    return [{"id": f"P{i}", **f, "handed_off": None} for i, f in enumerate(found, 1)]


def handoff_text(f: dict) -> str:
    """The scoped change request for the flow that owns the RTL."""
    return f"[rtl:{f['module']}] ASIC port ({f['file']}:{f['line']}, {RULES[f['rule']].title}): `{f['text']}` — {f['fix']}"
