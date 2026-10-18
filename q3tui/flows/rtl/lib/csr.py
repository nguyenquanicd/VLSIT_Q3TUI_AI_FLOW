"""The APB-CSR-Generator (https://github.com/nguyenquanicd/APB-CSR-Generator) for the RTL flow: validation of a register map, the Excel workbook
it reads, running it through the tool role `csr_gen`, reading what it generated.

The tool turns a workbook (`Table - Configuration` + `REGISTER` rows) into an APB4 slave with address decode, SLVERR / PREADY handshaking, per-field
rw / ro / rwi / w1c semantics and an optional clock-domain-crossing bridge. It validates nothing (no overlap, no duplicate offset check), so the
checks are here, before it runs. The tool is not shipped with Q3TUI (it carries no license): it is found by `options.tool_dir`, `$CSR_GEN_HOME`,
`~/.q3tui/tools/APB-CSR-Generator` or `<project>/tools/APB-CSR-Generator` (`q3tui act csr_tool_install` clones it)."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from q3tui.flows.rtl.lib.models import CsrField, CsrMap

REPO = "https://github.com/nguyenquanicd/APB-CSR-Generator.git"
SCRIPT = "CSR_Generation.py"
SYNC_MODEL = "m_vlsi_synch"  # the CDC synchronizer the generated module instantiates when Asynchronous=1
SHEET = "CSR"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VOLATILE = re.compile(r"^//\s*(Generator|Date and time):.*$", re.M)


# -- where the tool is ------------------------------------------------------------------------------------------------

def tool_dirs(project_root: Path, option: str | None = None) -> list[Path]:
    home = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser()
    cands = [option, os.environ.get("CSR_GEN_HOME"), home / "tools" / "APB-CSR-Generator", project_root / "tools" / "APB-CSR-Generator"]
    out = []
    for c in cands:
        if c:
            p = Path(os.path.expandvars(str(c))).expanduser()
            out.append(p if p.is_absolute() else project_root / p)
    return out


def find_tool(project_root: Path, option: str | None = None) -> Path | None:
    return next((d for d in tool_dirs(project_root, option) if (d / SCRIPT).is_file() and (d / "models" / f"{SYNC_MODEL}.sv").is_file()), None)


# -- the register map -------------------------------------------------------------------------------------------------

def complete(m: CsrMap) -> CsrMap:
    """The map with every bit of every register covered: a gap becomes a `reserved` ro field (the tool wants the whole register described; a
    `reserved` field has no ports). Fields are ordered high to low."""
    for r in m.registers:
        r.fields.sort(key=lambda f: -f.msb)
        have = [(f.msb, f.lsb) for f in r.fields]
        top = m.data_width - 1
        gaps = []
        for b in range(top, -1, -1):
            if not any(lo <= b <= hi for hi, lo in have):
                if gaps and gaps[-1][1] == b + 1:
                    gaps[-1][1] = b
                else:
                    gaps.append([b, b])
        for hi, lo in gaps:
            r.fields.append(CsrField(name="reserved", msb=hi, lsb=lo, type="ro", description="Reserved. Read-as-zero, write ignored."))
        r.fields.sort(key=lambda f: -f.msb)
    return m


def validate(m: CsrMap) -> list[str]:
    """What is wrong with a map (empty: fine), in words the author of the map can act on."""
    p: list[str] = []
    if not m.has_registers:
        return p
    if not _IDENT.match(m.module_name or ""):
        p.append(f"module_name `{m.module_name}` is not an identifier")
    if m.data_width != 32:
        p.append(f"data_width is {m.data_width}: the tool supports 32 only")
    if not 4 <= m.addr_width <= 32:
        p.append(f"addr_width {m.addr_width} is outside 4..32")
    if not m.registers:
        p.append("the map has no register")
    names, offsets = set(), {}
    for r in m.registers:
        if not _IDENT.match(r.name):
            p.append(f"register name `{r.name}` is not an identifier")
        if r.name.lower() in names:
            p.append(f"register `{r.name}` appears twice (names are compared without case: the tool builds ports from them)")
        names.add(r.name.lower())
        if r.offset % 4:
            p.append(f"register `{r.name}` offset {hex(r.offset)} is not a multiple of 4 (the tool answers SLVERR to a misaligned address)")
        if r.offset < 0 or r.offset >= (1 << m.addr_width):
            p.append(f"register `{r.name}` offset {hex(r.offset)} does not fit in {m.addr_width} address bits")
        if r.offset in offsets:
            p.append(f"registers `{offsets[r.offset]}` and `{r.name}` share offset {hex(r.offset)}")
        offsets[r.offset] = r.name
        covered = [False] * m.data_width
        fnames = set()
        for f in r.fields:
            where = f"register `{r.name}` field `{f.name}`"
            if not _IDENT.match(f.name):
                p.append(f"{where}: not an identifier")
            if f.name != "reserved" and f.name in fnames:
                p.append(f"register `{r.name}` has two fields named `{f.name}`")
            fnames.add(f.name)
            if not (0 <= f.lsb <= f.msb < m.data_width):
                p.append(f"{where}: bits [{f.msb}:{f.lsb}] are not inside [{m.data_width - 1}:0]")
                continue
            if f.name == "reserved" and f.type != "ro":
                p.append(f"{where}: a `reserved` field is `ro` (it has no ports and reads 0)")
            if f.reset >= (1 << (f.msb - f.lsb + 1)):
                p.append(f"{where}: reset value {hex(f.reset)} does not fit in {f.msb - f.lsb + 1} bit(s)")
            for b in range(f.lsb, f.msb + 1):
                if covered[b]:
                    p.append(f"{where}: bit {b} is also in another field of `{r.name}`")
                    break
                covered[b] = True
    return p


def reset_value(r) -> int:
    return sum(f.reset << f.lsb for f in r.fields)


def write_workbook(m: CsrMap, path: Path, sheet: str = SHEET) -> Path:
    """The workbook in the layout CSR_Generation.py reads (see its CSR_FORMAT_PROPOSAL.xlsx): configuration block, then a header row, then a row per
    register (`REGISTER`, `OFFSET`, reset) followed by one row per field (`BIT NAME`, `FIELD WIDTH` `[msb:lsb]`, `BIT TYPE`, reset, description)."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    def row(n: int, *values) -> None:
        for c, v in enumerate(values, 1):
            if v is not None:
                ws.cell(row=n, column=c, value=v)

    row(1, "Table - Configuration")
    row(2, "Config", "Value", "Description")
    for n, (k, v) in enumerate((("Module Name", m.module_name), ("Protocol", "APB"), ("Data Width", m.data_width), ("Address Width", m.addr_width),
                                ("Write Strobe", 4), ("Asynchronous", 1 if m.asynchronous else 0)), 3):
        row(n, k, v)
    row(14, "Table - Register Definition")
    row(15, "REGISTER", "OFFSET", "BIT NAME", "FIELD WIDTH", "BIT TYPE", "RESET VALUE", "DESCRIPTION")
    n = 16
    for r in m.registers:
        row(n, r.name, f"0x{r.offset:04X}", None, None, None, hex(reset_value(r)), r.description or None)
        n += 1
        for f in r.fields:
            row(n, None, None, f.name, f"[{f.msb}]" if f.msb == f.lsb else f"[{f.msb}:{f.lsb}]", f.type, hex(f.reset), f.description or None)
            n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def run_tool(ts, tool: Path, workbook: Path, workdir: Path, sheet: str = SHEET):
    """Run the generator through the role `csr_gen` in `workdir` (it writes `RTL/` and `Debug/` into its working directory)."""
    from q3tui.eda.tools import run_role

    shutil.rmtree(workdir / "RTL", ignore_errors=True)
    shutil.rmtree(workdir / "Debug", ignore_errors=True)
    return run_role(ts, "csr_gen", workdir, {"python": sys.executable, "tool": tool, "workbook": workbook, "sheet": sheet,
                                             "workdir": workdir, "project": workdir}, log_name="csr_gen.log")


def stable(text: str) -> str:
    """The generated text without the lines that change on every run (user name, date): what decides whether the module changed."""
    return _VOLATILE.sub("", text)


_PORT = re.compile(r"^\s*(input|output|inout)\s*(\[[^\]]+\])?\s*(\w+)\s*,?\s*$", re.M)


def ports(sv: str) -> list[tuple[str, str, str]]:
    """(direction, width, name) of the ports of a generated module, in order."""
    head = sv.split(");", 1)[0]
    return [(d, (w or "").replace(" ", ""), n) for d, w, n in _PORT.findall(head)]


def port_summary(sv: str, module: str) -> str:
    """The generated module's ports as the integrator reads them: APB bus, clocks/resets, then the register ports grouped by register."""
    ps = ports(sv)
    bus = [n for _, _, n in ps if n.startswith(("i_bus_", "i_p", "o_p", "i_protect", "i_slverr", "i_reg_"))]
    rest = [(d, w, n) for d, w, n in ps if n not in bus]
    lines = [f"module {module}", "bus / clocks: " + ", ".join(f"{n}" for n in bus), "register ports:"]
    lines += [f"  {d}{' ' + w if w else ''} {n}" for d, w, n in rest]
    return "\n".join(lines)


def instantiation_hint(sv: str, module: str, inst: str | None = None) -> str:
    ps = ports(sv)
    inst = inst or "u_" + re.sub(r"^m_", "", module)
    body = ",\n".join(f"  .{n}()" for _, _, n in ps)
    return f"{module} {inst} (\n{body}\n);"


# -- modules the tool provides (the `rtl` step does not write, rule-check or fix them) ------------------------------------

PROVIDED = "csr/provided.json"  # in the RTL folder: {module: {kind, parent, instances, req_ids}}


def provided_file(rtl_dir: Path) -> Path:
    return rtl_dir / PROVIDED


def read_provided(rtl_dir: Path) -> dict[str, dict]:
    from q3tui.core.project import read_json

    data = read_json(provided_file(rtl_dir), {}) or {}
    return data if isinstance(data, dict) else {}


_MENTIONS = re.compile(r"\b(registers?|CSRs?|SFRs?|register map|memory[- ]mapped|address map)\b", re.I)


def mentions_registers(text: str) -> bool:
    """A cheap first look: a spec that never says register / CSR / SFR / memory-mapped has no register map to generate (no LLM needed)."""
    return bool(_MENTIONS.search(text))
