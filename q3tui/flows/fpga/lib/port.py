"""The FPGA port (step `fpga_port`, docs/spec/fpga-flow.md "FPGA port"): what the RTL needs to synthesize on the chosen part, and the porting the
FPGA flow does itself.

Code: the scan (`scan`: the memory initialization files the RTL names and whether the part's tool can read them, another vendor's primitives),
the memory images (`convert`: a `$readmemh` text, a binary or a MIF into the file the RTL names, in `src/fpga/`), the setup of the synthesis
(`set_synth_option`: top, defines, synthesis arguments, checked). The LLM (the porter, one stage with the tools below) decides what to do with
them: which image a memory boots with, what the RTL has to change (work orders for the flow that owns the RTL), what you have to decide
(questions). rv32iv: its memories are initialized by Quartus' `ram_init_file = "m_imem.mif"`, and there was no .mif: Quartus Pro stopped
(error 20059); on Vivado the attribute is ignored and the memory was removed."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.flows.fpga.lib.templates import Template
from q3tui.steps.common import Question

PORT_JSON = "fpga_port.json"  # schemas/: the scan, what the porter did, the orders handed to the RTL's flow
PART_CHANGED = "[part changed]"  # (fpga_synth's notice that the part is not the one ported for: scanned again, the porter only for what that finds)
INIT_SUFFIXES = (".mif", ".hex", ".mem", ".txt", ".dat", ".coe", ".bin")

_NAMED = re.compile(r'(ram_init_file\s*=\s*|\$readmem[hb]\s*\(\s*)"([^"]+)"')
_MODULE = re.compile(r"^\s*module\s+(\w+)", re.M)
# another vendor's primitives (an instance of one: `RAMB36E1 #(` / `altsyncram u_ram (`)
_PRIMS = {
    "Xilinx": r"RAMB(?:18|36)E[12]|BUFG\w*|BUFR|IBUFG?DS|OBUFDS|MMCME[234]_\w+|PLLE[234]_\w+|DSP48E[12]|SRL(?:16|C32)E|[IO]DDR\w*|xpm_\w+",
    "Intel": r"altsyncram|altpll|altera_pll|altdpram|altddio_\w+|scfifo|dcfifo|lpm_\w+|(?:cyclonev|arriav|twentynm|fourteennm)_\w+",
    "Lattice": r"DP16KD|PDPW16KD|EHXPLLL|MULT18X18D|ALU54B|SB_RAM40_4K\w*|SB_PLL40_\w+|SB_IO|SB_GB\w*|SB_SPRAM256KA",
}


@dataclass
class Finding:
    rule: str  # init_missing | init_ignored | foreign_primitive
    file: str  # relative to the project
    line: int
    module: str
    code: str
    note: str  # what it means on this part's tool

    def to_dict(self) -> dict:
        return asdict(self)


def init_dirs(project, rtl_files: list[Path]) -> list[Path]:
    """Where an initialization file the RTL names is found: beside the RTL, then src/fpga/ (the port's)."""
    return [*dict.fromkeys(f.parent for f in rtl_files), project.fpga_src_dir]


def named_inits(rtl_files: list[Path], t: Template) -> list[tuple[Path, int, str, bool]]:
    """(file, line, the name the RTL gives, whether this part's tool reads it): `ram_init_file` is Quartus' (Vivado and Yosys ignore it),
    `$readmemh` / `$readmemb` every tool's."""
    out = []
    for f in rtl_files:
        text = f.read_text(errors="replace")
        for m in _NAMED.finditer(text):
            reads = m.group(1).startswith("$") or t.backend == "quartus"
            out.append((f, text.count("\n", 0, m.start()) + 1, m.group(2), reads))
    return out


def find_init(name: str, dirs: list[Path]) -> Path | None:
    return next((d / name for d in dirs if (d / name).is_file()), None) or next((d / Path(name).name for d in dirs if (d / Path(name).name).is_file()), None)


def scan(project, rtl_files: list[Path], t: Template) -> list[Finding]:
    """What the part's tool cannot take as the RTL is (ground truth for the porter and the synthesis)."""
    out: list[Finding] = []
    dirs = init_dirs(project, rtl_files)
    texts = {f: f.read_text(errors="replace") for f in rtl_files}

    def module_at(f: Path, line: int) -> str:
        mods = [(texts[f].count("\n", 0, m.start()) + 1, m.group(1)) for m in _MODULE.finditer(texts[f])]
        return next((n for ln, n in reversed(mods) if ln <= line), f.stem)

    for f, line, name, reads in named_inits(rtl_files, t):
        code = texts[f].splitlines()[line - 1].strip()
        if not reads:
            out.append(Finding("init_ignored", project.rel(f), line, module_at(f, line), code,
                               f"{t.tool} ignores Quartus' ram_init_file: the memory starts empty, and one nothing writes is removed with what reads it"))
        elif find_init(name, dirs) is None:
            out.append(Finding("init_missing", project.rel(f), line, module_at(f, line), code,
                               f"{name} is neither beside the RTL nor in {project.rel(project.fpga_src_dir)}/: "
                               + ("Quartus Pro stops (error 20059), Standard leaves the memory empty" if t.backend == "quartus" else "the memory starts empty")))
    for vendor, pat in _PRIMS.items():
        if vendor == t.vendor:
            continue
        rx = re.compile(rf"^\s*({pat})\s*(?:#\s*\(|\w+\s*\()", re.M)
        for f, text in texts.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                out.append(Finding("foreign_primitive", project.rel(f), line, module_at(f, line), text.splitlines()[line - 1].strip(),
                                   f"{m.group(1)} is {'an' if vendor[0] in 'AEIOU' else 'a'} {vendor} primitive: {t.tool} for {t.family} has no such cell"))
    return out


def show(found: list[Finding]) -> str:
    return "\n".join(f"- [{x.rule}] {x.file}:{x.line} ({x.module}) `{x.code[:160]}` — {x.note}" for x in found) or "(nothing)"


def stage_inits(project, rtl_files: list[Path], t: Template, work: Path) -> list[str]:
    """Copy the initialization files the tool reads into its work folder (Quartus Pro reads them there, not on the search path; Vivado and
    Yosys resolve a relative `$readmemh` path from where they run). Returns the names it could not find."""
    dirs, missing = init_dirs(project, rtl_files), []
    for _f, _line, name, reads in named_inits(rtl_files, t):
        if not reads:
            continue
        src = find_init(name, dirs)
        if src is None:
            missing.append(name)
            continue
        dest = work / (name if not Path(name).is_absolute() and ".." not in Path(name).parts else Path(name).name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    return list(dict.fromkeys(missing))


# -- memory images -------------------------------------------------------------------------------------------------

def read_image(path: Path, width: int | None = None) -> dict[int, int]:
    """{address: word} of an image: a `$readmemh` text (`@addr`, `//` and `/* */` comments, x / z as 0), a Quartus MIF, or a binary
    (little-endian words of `width` bits)."""
    if path.suffix.lower() == ".bin":
        if not width or width % 8:
            raise ValueError("a binary image needs the word width in bits (a multiple of 8)")
        data, n = path.read_bytes(), width // 8
        return {i // n: int.from_bytes(data[i:i + n].ljust(n, b"\0"), "little") for i in range(0, len(data), n)}
    text = path.read_text(errors="replace")
    if re.search(r"^\s*CONTENT\s+BEGIN", text, re.M | re.I):
        return _read_mif(text)
    text = re.sub(r"/\*.*?\*/", " ", re.sub(r"//[^\n]*", " ", text), flags=re.S)
    words, addr = {}, 0
    for tok in text.split():
        if tok.startswith("@"):
            addr = int(tok[1:], 16)
            continue
        words[addr] = int(re.sub(r"[xXzZ]", "0", tok.replace("_", "")), 16)
        addr += 1
    return words


def _read_mif(text: str) -> dict[int, int]:
    body = re.search(r"CONTENT\s+BEGIN(.*?)END\s*;", text, re.S | re.I)
    radix = (re.search(r"DATA_RADIX\s*=\s*(\w+)", text, re.I) or [None, "HEX"])[1].upper()
    base = {"HEX": 16, "BIN": 2, "DEC": 10, "UNS": 10, "OCT": 8}.get(radix, 16)
    out: dict[int, int] = {}
    for m in re.finditer(r"(\[\s*\w+\s*\.\.\s*\w+\s*\]|\w+)\s*:\s*([^;]+);", body.group(1) if body else ""):
        a, vals = m.group(1), m.group(2).split()
        lo, hi = (int(x, 16) for x in a.strip("[] ").split("..")) if a.startswith("[") else (int(a, 16), int(a, 16))
        for i, addr in enumerate(range(lo, hi + 1)):
            out[addr] = int(vals[i % len(vals)], base)
    return out


def to_mif(words: dict[int, int], width: int, depth: int, source: str = "") -> str:
    digits = (width + 3) // 4
    lines = [f"-- generated by Q3TUI (fpga_port){' from ' + source if source else ''}", f"WIDTH={width};", f"DEPTH={depth};",
             "ADDRESS_RADIX=HEX;", "DATA_RADIX=HEX;", "CONTENT BEGIN"]
    addr = 0
    for a in sorted(words):
        if a > addr:  # (the addresses the image leaves out: zero)
            lines.append(f"\t[{addr:X}..{a - 1:X}] : {0:0{digits}X};" if a - 1 > addr else f"\t{addr:X} : {0:0{digits}X};")
        lines.append(f"\t{a:X} : {words[a]:0{digits}X};")
        addr = a + 1
    if addr < depth:
        lines.append(f"\t[{addr:X}..{depth - 1:X}] : {0:0{digits}X};" if depth - 1 > addr else f"\t{addr:X} : {0:0{digits}X};")
    return "\n".join([*lines, "END;"]) + "\n"


def to_readmemh(words: dict[int, int], width: int) -> str:
    digits, out, nxt = (width + 3) // 4, [], 0
    for a in sorted(words):
        if a != nxt:
            out.append(f"@{a:X}")
        out.append(f"{words[a]:0{digits}X}")
        nxt = a + 1
    return "\n".join(out) + "\n"


def convert(source: Path, target: str, depth: int | None, width: int | None, label: str = "") -> tuple[str, str]:
    """(the text of `target`, what it holds): a MIF for a `.mif` target (Quartus), else a `$readmemh` text. The words must fit the memory."""
    words = read_image(source, width)
    if not words:
        raise ValueError(f"{label or source.name} holds no words")
    width = int(width or max(4, max(words.values()).bit_length()))
    depth = int(depth or max(words) + 1)
    if max(words) >= depth:
        raise ValueError(f"{label or source.name} has a word at address {max(words)}: deeper than the memory ({depth} words)")
    wide = [a for a, w in words.items() if w.bit_length() > width]
    if wide:
        raise ValueError(f"{label or source.name}: the word at address {wide[0]} is wider than {width} bits")
    text = to_mif(words, width, depth, label) if target.lower().endswith(".mif") else to_readmemh(words, width)
    return text, f"{len(words)} word(s) of {width} bits, depth {depth}" + (" (the rest 0)" if len(words) < depth else "")


# -- the synthesis setup the porter may change ----------------------------------------------------------------------

_SAFE_TCL = re.compile(r"^set_(?:global|instance)_assignment(?:\s+[^\s\[\];$\\]+)+$")  # (Quartus: an assignment, nothing evaluated)
_SAFE_FLAG = re.compile(r"^-[A-Za-z_]\w*(?:\s+[^\s\[\];$\\-][^\s\[\];$\\]*)?$")  # (Vivado / Yosys: a flag of the synthesis command, one value)


def set_synth_option(engine, key: str, value: str) -> str:
    """Change fpga_synth's `top` / `defines` (added) / `synth_args` (added), checked; saved to q3tui.yaml (the Settings dialog shows it)."""
    from q3tui.core import settings
    from q3tui.flows.fpga.lib import templates

    step = engine.by_name.get("fpga_synth")
    if step is None:
        raise ValueError("this flow has no fpga_synth step")
    lines = [x.strip() for x in str(value).splitlines() if x.strip()]
    if key == "top":
        mods = {m for f in step.rtl_files(engine) for m in _MODULE.findall(f.read_text(errors="replace"))}
        if len(lines) != 1 or lines[0] not in mods:
            raise ValueError(f"top: {value!r} is not a module of the RTL")
        new = lines[0]
    elif key == "defines":
        new = dict(step.options.get("defines") or {})
        for ln in lines:
            name, _, val = ln.partition("=")
            if not re.fullmatch(r"[A-Za-z_]\w*", name.strip()) or re.search(r"[\s\[\];$\\\"]", val.strip()):
                raise ValueError(f"defines: {ln!r} is not NAME or NAME=VALUE")
            new[name.strip()] = val.strip() or None
    elif key == "synth_args":
        t, _d, _n = templates.resolve(step.options)
        rx = _SAFE_TCL if t is not None and t.backend == "quartus" else _SAFE_FLAG
        bad = [ln for ln in lines if not rx.match(ln)]
        if bad:
            raise ValueError(f"synth_args: {bad[0]!r} is not " + ("a Quartus assignment (set_global_assignment -name NAME VALUE)"
                                                                  if rx is _SAFE_TCL else "a flag of the synthesis command (-flag [value])"))
        new = [*(step.options.get("synth_args") or []), *(ln for ln in lines if ln not in (step.options.get("synth_args") or []))]
    else:
        raise ValueError(f"{key}: the port changes top, defines or synth_args (the part is yours: ask)")
    cfg = engine.project.cfg
    cfg.pipeline.step_options.setdefault("fpga_synth", {})[key] = new
    step.options[key] = new
    engine.project.save_config_values(settings.nested({f"pipeline.step_options.fpga_synth.{key}": new}))
    return f"fpga_synth {key} = {new} (saved to q3tui.yaml)"


# -- images in the project (for the prompt) --------------------------------------------------------------------------

def images(project, wanted: list[str], limit: int = 40, budget: int = 20000) -> list[str]:
    """The memory images the project has (hex / mif / mem / bin …, not the internal state, not the RTL), the ones whose name has a word of
    `wanted` (the files the RTL names: m_imem.mif → imem) first; each with its words and width when it is a text image."""
    import os

    words = {w.lower() for n in wanted for w in re.split(r"[^A-Za-z0-9]+", Path(n).stem) if len(w) > 2}
    skip = {project.state_dir.resolve(), project.fpga_dir.resolve()}
    found: list[Path] = []
    seen = 0
    for d, dirs, files in os.walk(project.root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and (Path(d) / x).resolve() not in skip]
        for n in files:
            seen += 1
            if n.lower().endswith(INIT_SUFFIXES):
                found.append(Path(d) / n)
        if seen > budget:
            break
    found.sort(key=lambda p: (not any(w in p.name.lower() for w in words), project.rel(p)))
    out = []
    for p in found[:limit]:
        desc = ""
        if p.stat().st_size < 5_000_000 and p.suffix.lower() != ".bin":
            try:
                w = read_image(p)
                desc = f" — {len(w)} word(s), {max(w.values()).bit_length() if w else 0} bit(s) used" if w else ""
            except (ValueError, OSError):
                desc = ""
        out.append(f"- {project.rel(p)}{desc}")
    if len(found) > limit:
        out.append(f"- … and {len(found) - limit} more (Glob for them)")
    return out


# -- the porter's answer ----------------------------------------------------------------------------------------------

class PortOrder(BaseModel):
    module: str = Field(description="The RTL module to change (its file name without extension)")
    change: str = Field(description="What to change in it for this part, concretely; behaviour, ports and requirement tags stay")
    why: str = Field(description="The finding, the tool's error or the judgment it answers")


class PortResult(BaseModel):
    summary: str = Field(description="What you found and did, in one to three sentences")
    done: list[str] = Field(default_factory=list, description="What you changed here (the images you made, the synthesis settings), one line each")
    orders: list[PortOrder] = Field(default_factory=list, description="RTL changes this part needs, for the flow that owns the RTL")
    questions: list[Question] = Field(default_factory=list, description="Decisions that are the user's (which program a memory boots with when the "
                                                                        "spec does not say, another part, a spec change), each with 2-4 options")


def tools(engine, project, t: Template, rtl_files, done: list[str]) -> list:
    """The porter's in-process tools (`mcp__q3tui__*`): `mem_image` (an image into the file the RTL names, in src/fpga/), `synth_setting`
    (fpga_synth's top / defines / synth_args), `port_scan` (the scan again, after a change)."""
    import anyio
    from claude_agent_sdk import tool

    def text(s: str) -> dict:
        return {"content": [{"type": "text", "text": s}]}

    @tool("mem_image", "Make the memory initialization file the RTL names (target, e.g. m_imem.mif) in src/fpga/ from an image of the project "
          "(source: a $readmemh .hex / .mem, a .mif or a .bin, relative to the project). depth / width: the RTL array's words and bits "
          "(read the module). A .mif target is written as a Quartus MIF, others as $readmemh text.",
          {"source": str, "target": str, "depth": int, "width": int})
    async def mem_image(args: dict) -> dict:
        try:
            src = (project.root / str(args.get("source") or "")).resolve()
            if not src.is_relative_to(project.root.resolve()) or src.is_relative_to(project.state_dir.resolve()) or not src.is_file():
                return text(f"no image {args.get('source')!r} in the project")
            target = str(args.get("target") or "")
            if not target or Path(target).name != target or not target.lower().endswith(INIT_SUFFIXES[:-1]):
                return text(f"target {target!r}: the file name the RTL gives (m_imem.mif, rom.hex, …), no folder")
            body, what = await anyio.to_thread.run_sync(lambda: convert(src, target, args.get("depth"), args.get("width"), project.rel(src)))
        except ValueError as exc:
            return text(f"not written: {exc}")
        dest = project.fpga_src_dir / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        project.backup([dest])
        dest.write_text(body)
        done.append(f"{project.rel(dest)} ← {project.rel(src)}: {what}")
        return text(f"wrote {project.rel(dest)}: {what}")

    @tool("synth_setting", "Change the synthesis setup of fpga_synth: key top (the top module), defines (NAME or NAME=VALUE per line, added) "
          "or synth_args (per line, added: Quartus `set_global_assignment -name NAME VALUE`; Vivado / Yosys a flag of the synthesis command). "
          "The part is the user's: ask instead.", {"key": str, "value": str})
    async def synth_setting(args: dict) -> dict:
        try:
            msg = set_synth_option(engine, str(args.get("key") or ""), str(args.get("value") or ""))
        except ValueError as exc:
            return text(f"not changed: {exc}")
        done.append(msg)
        return text(msg)

    @tool("port_scan", "The scan again (what the part's tool cannot take as the RTL and src/fpga/ are now): check your changes with it.", {})
    async def port_scan(args: dict) -> dict:
        return text(show(scan(project, rtl_files(), t)))

    return [mem_image, synth_setting, port_scan]


def hand_to_flow(ctx, flow_name: str, text: str) -> str:
    """A change request for the `rtl` step of the flow that owns the RTL; returns the flow's name."""
    from q3tui import flows
    from q3tui.pipeline.base import StepFailed
    from q3tui.pipeline.engine import Engine

    owner = flows.load_flow(flow_name, ctx.project.root)
    if owner.name == ctx.engine.flow.name:
        raise StepFailed("handoff_flow is this flow itself: the RTL is the rtl flow's")
    ctx.engine.save()  # (the other flow's engine reads and writes the same pipeline.json)
    other = Engine(ctx.project, flow=owner)
    if "rtl" not in other.by_name:
        raise StepFailed(f"flow '{owner.name}' has no step 'rtl' to hand the RTL change to")
    other.request_change("rtl", text)
    ctx.engine.reload()
    return owner.name
