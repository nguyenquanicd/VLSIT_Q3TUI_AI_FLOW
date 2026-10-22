"""RTL integration phase of the `uvm_vip` backend (milestone M6; rules DUT-DV-004, 005, 022, 026..030).

Runs only after the verification contract and the TB architecture are frozen. It reads the released RTL with pyslang for structural and
compilation information ONLY: the top module, its ports and parameters, elaboration diagnostics, and the file list. It never reads the RTL's
behavior and never changes the contract: a difference between the approved interface contract and the RTL is a conformance finding (DUT-DV-005),
and a port that needs another name needs an approved mapping, never a silent adaptation (DUT-DV-030)."""

from __future__ import annotations

import re
from pathlib import Path

from q3tui.flows.dv.tb.uvm import binding as B
from q3tui.flows.dv.tb.uvm import svgen as g
from q3tui.flows.dv.tb.uvm.contract import VerificationContract

SCHEMA_VERSION = 1


def read_filelist(path: Path, root: Path) -> tuple[list[Path], list[Path], dict[str, str | None]]:
    """(source files, include directories, defines) of an RTL file list; relative entries are relative to `root` (the project root)."""
    files, incs, defs = [], [], {}
    for ln in path.read_text().splitlines():
        s = re.sub(r"//.*|#.*", "", ln).strip()
        for tok in s.split():
            if tok.startswith("+incdir+"):
                incs += [(root / t).resolve() for t in tok[len("+incdir+"):].split("+") if t]
            elif tok.startswith("+define+"):
                for d in tok[len("+define+"):].split("+"):
                    k, _, v = d.partition("=")
                    defs[k] = v or None
            elif tok.startswith(("-", "+")):
                continue
            else:
                files.append((root / tok).resolve())
    return files, incs, defs


def dims(type_text: str) -> list[int]:
    """Packed dimensions of a pyslang type string (`logic[0:0][31:0]` -> [1, 32]; `logic` -> [])."""
    return [abs(int(a) - int(b)) + 1 for a, b in re.findall(r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]", type_text)]


def total(d: list[int]) -> int:
    n = 1
    for x in d:
        n *= x
    return n


def read_interface(files: list[Path], top: str, incdirs: list[Path], defines: dict, overrides: dict[str, str]) -> dict:
    """The top module's ports and parameters and the diagnostics of elaborating the file set (pyslang). Nothing else of the RTL is read."""
    import pyslang as ps
    from pyslang import ast, parsing, syntax

    sm = ps.SourceManager()
    pp = parsing.PreprocessorOptions()
    pp.additionalIncludePaths = [str(d) for d in incdirs]
    pp.predefines = [f"{k}={v}" if v is not None else k for k, v in defines.items()]
    co = ast.CompilationOptions()
    co.topModules = {top}
    co.paramOverrides = [f"{k}={v}" for k, v in overrides.items()]
    bag = ps.Bag([pp, co])
    comp = ast.Compilation(bag)
    srcs = [str(f) for f in files if f.exists()]
    if srcs:
        comp.addSyntaxTree(syntax.SyntaxTree.fromFiles(srcs, sm, bag))
    diags = []
    for d in comp.getAllDiagnostics():
        if d.isError():
            loc = sm.getFileName(d.location) if d.location else ""
            diags.append({"severity": "error", "code": str(d.code), "file": Path(loc).name, "text": ps.DiagnosticEngine.reportAll(sm, [d]).strip().splitlines()[-3:][0][:200]})
    tops = [i for i in comp.getRoot().topInstances if i.definition.name == top]
    if not tops:
        return {"found": False, "ports": [], "parameters": [], "diagnostics": diags}
    body = tops[0].body
    ports = [{"name": p.name, "direction": str(p.direction).split(".")[-1].lower(), "type": str(p.type)} for p in body.portList if hasattr(p, "direction")]
    params = [{"name": p.name, "value": str(p.value) if p.value is not None else None, "local": bool(p.isLocalParam)} for p in body.parameters]
    return {"found": True, "ports": ports, "parameters": params, "diagnostics": diags}


def eval_dims(width: str, params: dict[str, int]) -> list[int] | None:
    """Packed dimensions of a width as the specification writes it (`1`, `PR_X`, `[A-1:0][B/8-1:0]`); None when it cannot be evaluated."""
    groups = re.findall(r"\[[^\]]+\]", width)
    if not groups:
        n = B.safe_eval(width, params)
        return None if n is None else ([] if n == 1 else [n])
    out = [B.safe_eval(x, params) for x in groups]
    return None if None in out else out


def conformance(c: VerificationContract, rtl: dict, top_name: str, param_map: dict[str, str] | None = None, port_map: dict[str, str] | None = None,
                spec_names: dict[str, str] | None = None) -> list[dict]:
    """Differences between the approved interface contract and the RTL interface. Errors block; nothing here ever edits the contract.
    `spec_names`: renames the specification itself lists (`Original name`); they are suggestions for the user to approve, never applied."""
    param_map, port_map, spec_names = param_map or {}, port_map or {}, spec_names or {}
    out: list[dict] = []
    add = lambda sev, code, text: out.append({"id": f"CONF-{len(out) + 1:03d}", "severity": sev, "code": code, "text": text})  # noqa: E731
    ic = c.interface_contract
    if ic.top_module and top_name != ic.top_module:
        add("error", "top_name", f"the integration top '{top_name}' differs from the top module of the specification '{ic.top_module}'")
    if not rtl["found"]:
        add("error", "top_missing", f"the top module '{top_name}' was not found in the RTL files")
        return out
    for d in rtl["diagnostics"]:
        add("error", "elaboration", f"{d['file']}: {d['text']}")
    params = {p.name: int(p.value) for p in ic.parameters if re.fullmatch(r"-?\d+", p.value)}
    names = {p["name"] for p in rtl["parameters"] if not p["local"]}
    for p in ic.parameters:
        if p.name not in names and param_map.get(p.name) not in names:
            hint = (f"; the specification lists the original name {spec_names[p.name]}"
                    + (" and the RTL has a parameter of that name" if spec_names[p.name] in names else "")) if p.name in spec_names else ""
            add("error", "parameter_missing", f"approved parameter {p.name} has no parameter of that name in the RTL top{hint}: a mismatch between the specification and "
                                              "the RTL. It is not adapted silently; approve a mapping with integration.param_map or fix the RTL / the specification (DUT-DV-005, 030)")
    by = {port_map.get(x["name"], x["name"]): x for x in rtl["ports"]}
    claimed = set()
    for p in ic.ports:
        r = by.get(p.name)
        if r is None:
            add("error", "port_missing", f"port {p.name} of the interface contract is not a port of the RTL top")
            continue
        claimed.add(p.name)
        want_dir = "in" if p.direction.startswith("in") else "out"
        have_dir = "in" if r["direction"].startswith("in") else "out"
        if want_dir != have_dir:
            add("error", "direction", f"port {p.name}: the contract says {p.direction}, the RTL has {r['direction']}")
        cd = eval_dims(p.width, params)
        rd = dims(r["type"])
        if cd is None:
            add("warning", "width_unchecked", f"port {p.name}: the width '{p.width}' of the contract cannot be evaluated")
        elif total(rd) != total(cd):
            add("error", "width", f"port {p.name}: the contract width is {total(cd)} bit(s) ({p.width}), the RTL type {r['type']} has {total(rd)}")
        elif rd != cd:
            add("warning", "width_shape", f"port {p.name}: the same number of bits, but another packed shape (contract {cd}, RTL {rd})")
    known = {port_map.get(p.name, p.name) for p in ic.ports}
    for r in rtl["ports"]:
        if r["name"] not in known:
            add("error" if r["direction"].startswith("in") else "warning", "port_extra",
                f"the RTL top has a port {r['name']} ({r['direction']}) that the interface contract does not define")
    return out


def wrapper(c: VerificationContract, rtl: dict, top_name: str, hdr: str, param_map: dict[str, str] | None = None, port_map: dict[str, str] | None = None) -> str:
    """`dut_wrapper(dut_pins_if pins)`: the RTL top with the approved parameters and the contract's pins, by name. Wires only."""
    param_map, port_map = param_map or {}, port_map or {}
    pmap = {p.name: param_map.get(p.name, p.name) for p in c.interface_contract.parameters}
    rnames = {p["name"] for p in rtl["parameters"] if not p["local"]}
    over = [f".{pmap[p.name]}({p.name})" for p in c.interface_contract.parameters if pmap[p.name] in rnames]
    rev = {v: k for k, v in port_map.items()}
    conn = [f".{r['name']}(pins.{rev.get(r['name'], r['name'])})" for r in rtl["ports"]]
    inst = f"  {top_name} " + (f"#(\n    " + ",\n    ".join(over) + "\n  ) " if over else "") + "u_dut (\n    " + ",\n    ".join(conn) + "\n  );\n"
    return (hdr + "// The real DUT wrapper (integration phase): the released RTL top with the approved parameters, connected by name to the pins of the\n"
            "// interface contract. Wires only: no behavior. The only file where the DUT instance name is used (DUT-DV-028).\n"
            "module dut_wrapper(dut_pins_if pins);\n  import dut_tb_params_pkg::*;\n" + inst + "endmodule\n")


def dut_filelist(tb_filelist: str, rtl_paths: list[str]) -> str:
    """The testbench file list with the real wrapper and the RTL in place of the interface shell."""
    out = []
    for ln in tb_filelist.splitlines():
        if ln.strip() == "integration/dut_wrapper_shell.sv":
            out += rtl_paths
            out.append("integration/dut_wrapper.sv")
        else:
            out.append(ln)
    return "\n".join(out).replace("Testbench plus the interface shell (not a DUT).", "Testbench plus the released RTL as the DUT (integration phase).") + "\n"
