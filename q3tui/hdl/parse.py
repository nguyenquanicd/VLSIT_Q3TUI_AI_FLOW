"""Deterministic design parsing with pyslang → parsed_design.json.

Produces ground truth the LLM stages build on: module hierarchy, ports, parameters, clock
and reset candidates, and compiler diagnostics. Clock/reset detection is heuristic (edge
events in procedural blocks + naming) and marked `inferred`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pyslang as ps
from pyslang import ast, parsing, syntax

from q3tui.hdl.filelist import Filelist

_EDGE_RE = re.compile(r"@\s*\(([^)]*)\)")
_EDGE_SIG_RE = re.compile(r"\b(posedge|negedge)\s+([A-Za-z_]\w*)")
# rst_n, srst_n, arstn, i_rst_ni, hresetn, PRESETn, por_b, clr — but not burst/first.
_RESET_NAME_RE = re.compile(r"(^|_)[a-z]?(rst|reset|clr|clear|por)(_?n|_ni|_b|b)?($|_)", re.I)


@dataclass
class Port:
    name: str
    direction: str
    type: str


@dataclass
class Param:
    name: str
    value: str | None
    local: bool


@dataclass
class ClockCandidate:
    name: str
    edge: str
    inferred: bool = True


@dataclass
class ResetCandidate:
    name: str
    polarity: str  # active_low | active_high
    kind: str  # async | sync | unknown
    inferred: bool = True


@dataclass
class ModuleInfo:
    name: str
    file: str | None
    line: int | None
    kind: str  # module | interface | program
    elaborated_in: str = ""  # instance path whose parameter values are reported
    ports: list[Port] = field(default_factory=list)
    parameters: list[Param] = field(default_factory=list)
    children: list[dict] = field(default_factory=list)  # {instance, module}
    clocks: list[ClockCandidate] = field(default_factory=list)
    resets: list[ResetCandidate] = field(default_factory=list)
    always_blocks: dict[str, int] = field(default_factory=dict)


@dataclass
class Diagnostic:
    severity: str
    code: str
    file: str | None
    line: int | None
    message: str


@dataclass
class ParsedDesign:
    top: str
    ok: bool
    modules: dict[str, ModuleInfo]
    hierarchy: dict  # nested {instance, module, children[]}
    diagnostics: list[Diagnostic]
    files: list[str]
    unresolved_modules: list[str]
    comb_loops: list[dict] = field(default_factory=list)  # {signals: [path], where: [[file, line]]} (hdl/loops.py)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


def _file_line(sm: ps.SourceManager, location) -> tuple[str | None, int | None]:
    try:
        full = sm.getFullPath(location.buffer)
        name = str(full) if str(full) else str(sm.getFileName(location) or "")
        return (name or None), sm.getLineNumber(location)
    except Exception:  # noqa: BLE001
        return None, None


def _direction(d) -> str:
    return {"In": "input", "Out": "output", "InOut": "inout", "Ref": "ref"}.get(str(d).split(".")[-1], str(d))


def _scan_edges(text: str, clocks: dict, resets: dict, counts: dict, kind: str) -> None:
    counts[kind] = counts.get(kind, 0) + 1
    m = _EDGE_RE.search(text)
    if not m:
        return
    edges = _EDGE_SIG_RE.findall(m.group(1))
    if not edges:
        return
    # Conventional coding: first edge = clock, remaining edges = async resets/sets.
    names = [n for _, n in edges]
    clock_idx = next((i for i, n in enumerate(names) if not _RESET_NAME_RE.search(n)), 0)
    for i, (edge, name) in enumerate(edges):
        if i == clock_idx:
            clocks.setdefault(name, ClockCandidate(name=name, edge=edge))
        else:
            polarity = "active_low" if edge == "negedge" else "active_high"
            resets[name] = ResetCandidate(name=name, polarity=polarity, kind="async")


def _collect_scope(scope, info: ModuleInfo, sm, clocks: dict, resets: dict) -> list:
    """Walk one module body (incl. generate blocks) without descending into child instances."""
    instances = []
    for member in scope:
        kind = member.kind
        if kind == ast.SymbolKind.Instance:
            instances.append(member)
        elif kind == ast.SymbolKind.UninstantiatedDef:
            info.children.append({"instance": member.name, "module": getattr(member, "definitionName", None) or "?", "unresolved": True})
        elif kind == ast.SymbolKind.ProceduralBlock:
            pkind = str(member.procedureKind).split(".")[-1]
            _scan_edges(str(member.syntax), clocks, resets, info.always_blocks, pkind)
        elif kind in (ast.SymbolKind.GenerateBlock, ast.SymbolKind.GenerateBlockArray):
            if kind == ast.SymbolKind.GenerateBlock and getattr(member, "isUninstantiated", False):
                continue
            instances.extend(_collect_scope(member, info, sm, clocks, resets))
    return instances


def parse_design(filelist: Filelist, top: str | None = None) -> ParsedDesign:
    sm = ps.SourceManager()
    pp = parsing.PreprocessorOptions()
    pp.additionalIncludePaths = [str(d) for d in filelist.incdirs]
    pp.predefines = [f"{k}={v}" if v is not None else k for k, v in filelist.defines.items()]
    copts = ast.CompilationOptions()
    if top:
        copts.topModules = {top}
    bag = ps.Bag([pp, copts])

    sources = [str(f) for f in filelist.all_sources() if f.exists()]
    comp = ast.Compilation(bag)
    if sources:
        comp.addSyntaxTree(syntax.SyntaxTree.fromFiles(sources, sm, bag))

    diagnostics = []
    seen_diags = set()
    for d in comp.getAllDiagnostics():
        f, line = _file_line(sm, d.location)
        text = ps.DiagnosticEngine.reportAll(sm, [d]).strip()
        lines = text.splitlines()
        # diagnostics in an included file start with "in file included from …": the message is on a later line
        first = next((ln for ln in lines if re.search(r":\s*(fatal error|error|warning):", ln)), lines[0] if lines else str(d.code))
        message = re.split(r":\s*(?:fatal error|error|warning):\s*", first, maxsplit=1)[-1] if re.search(
            r":\s*(fatal error|error|warning):", first) else (first.split(": ", 2)[-1] if ": " in first else first)
        diag = Diagnostic("error" if d.isError() else "warning", str(d.code).removeprefix("DiagCode(").rstrip(")"), f, line, message)
        key = (diag.code, diag.file, diag.line, diag.message)
        if key not in seen_diags:
            seen_diags.add(key)
            diagnostics.append(diag)

    root = comp.getRoot()
    tops = list(root.topInstances)
    modules: dict[str, ModuleInfo] = {}
    unresolved: set[str] = set()

    def visit(inst, depth: int = 0) -> dict:
        body = inst.body
        name = inst.definition.name
        node = {"instance": inst.name, "module": name, "children": []}
        if name not in modules:
            f, line = _file_line(sm, inst.definition.location)
            info = ModuleInfo(name=name, file=f, line=line, kind=str(inst.definition.definitionKind).split(".")[-1].lower(), elaborated_in=inst.hierarchicalPath)
            info.ports = [Port(p.name, _direction(p.direction), str(p.type)) for p in body.portList if hasattr(p, "direction")]
            info.parameters = [Param(p.name, str(p.value) if p.value is not None else None, bool(p.isLocalParam)) for p in body.parameters]
            clocks: dict[str, ClockCandidate] = {}
            resets: dict[str, ResetCandidate] = {}
            child_insts = _collect_scope(body, info, sm, clocks, resets)
            # Sync resets: reset-named signals tested in clocked blocks but not in the event list.
            port_names = {p.name for p in info.ports}
            for pname in port_names:
                if _RESET_NAME_RE.search(pname) and pname not in resets and pname not in clocks:
                    m = _RESET_NAME_RE.search(pname)  # the reset token's own suffix (i_resetn_core), not the end of the name
                    resets[pname] = ResetCandidate(pname, "active_low" if m and m.group(3) else "active_high", "unknown")
            info.clocks, info.resets = list(clocks.values()), list(resets.values())
            info.children.extend({"instance": c.name, "module": c.definition.name} for c in child_insts)
            modules[name] = info
            for c in info.children:
                if c.get("unresolved"):
                    unresolved.add(c["module"])
        else:
            child_insts = _collect_scope(body, ModuleInfo(name, None, None, ""), sm, {}, {})
        if depth < 64:
            node["children"] = [visit(c, depth + 1) for c in child_insts]
        return node

    hierarchy = visit(tops[0]) if tops else {}
    chosen_top = tops[0].name if tops else (top or "")
    if top and tops and tops[0].name != top:
        diagnostics.append(Diagnostic("error", "TopNotFound", None, None, f"top module '{top}' not found"))
    unresolved |= {d.message.split("'")[1] for d in diagnostics if d.code == "UnknownModule" and "'" in d.message}

    comb_loops = []
    if tops and not any(d.severity == "error" for d in diagnostics):
        from q3tui.hdl.loops import find_loops

        try:
            comb_loops = [{"signals": lp.signals, "where": [list(w) for w in lp.where]} for lp in find_loops(comp, sm)]
        except Exception:  # noqa: BLE001 — an AST shape the walker does not know: no loop report, never a parse failure
            comb_loops = []
        try:
            from q3tui.hdl.loops import comb_edges

            comb_graph = comb_edges(comp, sm)
        except Exception:  # noqa: BLE001
            comb_graph = {}
    else:
        comb_graph = {}

    parsed = ParsedDesign(
        top=chosen_top,
        ok=not any(d.severity == "error" for d in diagnostics) and bool(tops),
        modules=modules,
        hierarchy=hierarchy,
        diagnostics=diagnostics,
        files=sources,
        unresolved_modules=sorted(unresolved),
        comb_loops=comb_loops,
    )
    parsed.comb_graph = comb_graph  # (not serialised: signal → signals computed from it in the same cycle)
    return parsed


def summarize(design: ParsedDesign, max_modules: int = 200) -> str:
    """Compact text view for LLM prompts."""
    lines = [f"Top: {design.top}  (parse ok: {design.ok}, {len(design.modules)} modules)"]

    def walk(node: dict, indent: int = 0) -> None:
        lines.append("  " * indent + f"- {node['instance']}: {node['module']}")
        for c in node.get("children", []):
            walk(c, indent + 1)

    if design.hierarchy:
        lines.append("Hierarchy:")
        walk(design.hierarchy, 1)
    for i, m in enumerate(design.modules.values()):
        if i >= max_modules:
            lines.append(f"... {len(design.modules) - max_modules} more modules")
            break
        lines.append(f"\nmodule {m.name}  ({m.file}:{m.line})")
        for p in m.ports:
            lines.append(f"  {p.direction:6} {p.type:24} {p.name}")
        if m.parameters:
            lines.append(f"  parameter values as elaborated in {m.elaborated_in}:")
        for prm in m.parameters:
            lines.append(f"  {'localparam' if prm.local else 'parameter'} {prm.name} = {prm.value}")
        if m.clocks:
            lines.append("  clocks: " + ", ".join(f"{c.name}({c.edge})" for c in m.clocks))
        if m.resets:
            lines.append("  resets: " + ", ".join(f"{r.name}({r.polarity},{r.kind})" for r in m.resets))
    errors = design.errors
    if errors:
        lines.append(f"\n{len(errors)} parse errors:")
        lines.extend(f"  {e.file}:{e.line}: {e.message}" for e in errors[:30])
    return "\n".join(lines)
