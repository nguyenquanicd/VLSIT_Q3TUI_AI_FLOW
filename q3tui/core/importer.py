"""Bring your own project: sort a folder (or a file) into what the flow's steps take — by code, no LLM.

Kinds: `spec` (documents → spec/), `rtl` (design sources), `tb` (testbench environment, tests and their docs), `sva`
(assertion files). Everything else (scripts, Makefiles, filelists, images, logs) is listed as not imported. The folder's
names decide first (rtl/ src/ hdl/ · tb/ sim/ test(s)/ verif/ env/ · sva/ assert*/ formal/); for HDL files no folder
names, the content does (bind / assert property → sva; classes, programs, $finish, UVM/VMM/SVT → tb; else rtl).
`design_facts` gives the imported RTL's module hierarchy (pyslang, regex fallback) as ground truth for the steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from q3tui.core.project import SPEC_DOC_EXTS

HDL_EXTS = {".sv", ".v", ".svh", ".vh"}
KINDS = ("spec", "rtl", "tb", "sva")
_SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__", ".q3tui", "node_modules", "csrc", "simv.daidir", "work", "obj_dir"}
_RTL_DIRS = {"rtl", "src", "hdl", "design", "hw", "verilog", "systemverilog"}
_TB_DIRS = {"tb", "tbs", "sim", "simulation", "test", "tests", "testcases", "verif", "verification", "env", "dv", "testbench", "uvm", "vip"}
_SVA_DIRS = {"sva", "assert", "assertions", "props", "properties", "formal", "fv"}
_DOC_DIRS = {"doc", "docs", "spec", "specs", "specification", "documentation"}
_TB_TEXT = re.compile(r"^\s*(?:virtual\s+)?class\s+\w+|^\s*program\b|\$finish\b|\buvm_|\bvmm_|\bsvt_|`include\s+\"(?:uvm|vmm|svt)", re.M)
_SVA_TEXT = re.compile(r"^\s*bind\s+\w+|\bassert\s+property\b|\bassume\s+property\b|\bcover\s+property\b", re.M)
_MODULE = re.compile(r"^\s*(module|interface|package|program)\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)", re.M)
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


@dataclass
class ImportPlan:
    root: Path
    files: dict[str, list[Path]] = field(default_factory=lambda: {k: [] for k in KINDS})
    skipped: list[Path] = field(default_factory=list)  # not imported (scripts, filelists, images, …)

    def rel(self, f: Path) -> str:
        try:
            return str(f.relative_to(self.root))
        except ValueError:
            return f.name


def _dirs(f: Path, root: Path) -> list[str]:
    try:
        parts = f.relative_to(root).parts[:-1]
    except ValueError:
        parts = ()
    return [p.lower() for p in parts]


def classify(f: Path, root: Path) -> str | None:
    """The kind of one file (None: not imported)."""
    ext = f.suffix.lower()
    dirs = _dirs(f, root)
    if ext in HDL_EXTS:
        for d in reversed(dirs):  # the innermost folder that says something decides (sim/vcs/env → tb)
            if d in _SVA_DIRS:
                return "sva"
            if d in _TB_DIRS:
                return "tb"
            if d in _RTL_DIRS:
                return "rtl"
        text = _COMMENT.sub("", f.read_text(errors="replace"))
        if _SVA_TEXT.search(text) and not re.search(r"^\s*always", text, re.M):
            return "sva"
        if _TB_TEXT.search(text) or re.match(r"(tb_|test_|tc_)|.*(_tb|_test)$", f.stem.lower()):
            return "tb"
        return "rtl" if _MODULE.search(text) else None
    if ext in SPEC_DOC_EXTS:
        if any(d in _TB_DIRS for d in dirs):
            return "tb"  # (a test environment's README describes the tests, not the design)
        if any(d in _RTL_DIRS | _SVA_DIRS for d in dirs) and not any(d in _DOC_DIRS for d in dirs):
            return None
        return "spec"
    return None


def scan(path: Path) -> ImportPlan:
    """Sort a folder (or classify one file) into kinds."""
    path = path.resolve()
    if path.is_file():
        plan = ImportPlan(path.parent)
        kind = classify(path, path.parent)
        (plan.files[kind] if kind else plan.skipped).append(path)
        return plan
    plan = ImportPlan(path)
    for f in sorted(path.rglob("*")):
        if not f.is_file() or any(p in _SKIP_DIRS or p.startswith(".") for p in f.relative_to(path).parts):
            continue
        kind = classify(f, path)
        (plan.files[kind] if kind else plan.skipped).append(f)
    return plan


def flat_names(files: list[Path], root: Path) -> dict[Path, str]:
    """Target names for copying into one flat folder: the file name, prefixed with its folder when two files clash."""
    seen: dict[str, int] = {}
    for f in files:
        seen[f.name] = seen.get(f.name, 0) + 1
    out = {}
    for f in files:
        if seen[f.name] == 1:
            out[f] = f.name
        else:
            try:
                parts = f.relative_to(root).parts
            except ValueError:
                parts = (f.name,)
            out[f] = "_".join(parts[-2:]) if len(parts) > 1 else f.name
    return out


# -- the imported RTL as facts ------------------------------------------------------------------------


def _regex_facts(files: list[Path]) -> dict[str, dict]:
    mods: dict[str, dict] = {}
    texts = {f: _COMMENT.sub("", f.read_text(errors="replace")) for f in files}
    for f, text in texts.items():
        for kind, name in _MODULE.findall(text):
            if kind in ("module", "package", "interface"):
                mods[name] = {"file": f.name, "kind": kind, "ports": [], "instances": []}
    for f, text in texts.items():
        for m in _MODULE.finditer(text):
            name = m.group(2)
            if name not in mods:
                continue
            end = text.find("endmodule", m.end())
            body = text[m.end(): end if end > 0 else len(text)]
            mods[name]["instances"] = [c for c in mods if c != name and re.search(rf"^\s*{re.escape(c)}\s*(#|\w)", body, re.M)]
    return mods


def design_facts(files: list[Path]) -> dict:
    """{top, modules: {name: {file, kind, ports: [[dir, name]], instances: [module]}}} of RTL files."""
    files = [f for f in files if f.suffix.lower() in (".sv", ".v")]
    if not files:
        return {"top": None, "modules": {}}
    mods: dict[str, dict] = {}
    try:
        from q3tui.hdl.filelist import filelist_from_files
        from q3tui.hdl.parse import parse_design

        design = parse_design(filelist_from_files(files))
        for name, info in design.modules.items():
            mods[name] = {"file": Path(info.file).name if info.file else "", "kind": info.kind,
                          "ports": [[p.direction, p.name] for p in info.ports],
                          "instances": list(dict.fromkeys(c["module"] for c in info.children))}
    except Exception:  # noqa: BLE001 - pyslang missing or the files do not elaborate: names and instances by regex
        mods = {}
    regex = _regex_facts(files)
    for name, m in regex.items():  # (packages and uninstantiated modules pyslang does not report as definitions)
        mods.setdefault(name, m)
    known = set(mods)
    for m in mods.values():
        m["instances"] = [i for i in m["instances"] if i in known]
    inst = {i for m in mods.values() for i in m["instances"]}
    roots = [n for n, m in mods.items() if n not in inst and m["kind"] == "module"]
    # the top: the root that instantiates the most (a lone helper that nobody uses is not the design's top)
    top = max(roots, key=lambda n: (len(_closure(mods, n)), n), default=None)
    return {"top": top, "modules": mods}


def _closure(mods: dict, name: str, seen: set | None = None) -> set:
    seen = seen if seen is not None else set()
    for c in mods.get(name, {}).get("instances", []):
        if c not in seen:
            seen.add(c)
            _closure(mods, c, seen)
    return seen


def facts_block(facts: dict) -> str:
    """The imported design as prompt text (names, top, hierarchy, ports)."""
    lines = [f"Top: `{facts.get('top')}`"]
    for name, m in (facts.get("modules") or {}).items():
        ports = ", ".join(f"{d} {n}" for d, n in m.get("ports", [])[:40])
        lines.append(f"- `{name}` ({m.get('kind', 'module')}, {m.get('file', '')})"
                     + (f" instantiates {', '.join(m['instances'])}" if m.get("instances") else "")
                     + (f"; ports: {ports}" if ports else ""))
    return "\n".join(lines)
