"""Probe of an existing reusable protocol VIP (rules DUT-DV-010, 019, 020): what the VIP offers, learned from its own source.

A lexical scan (no simulator, nothing executed, the VIP is read-only): packages and their dependencies, interfaces and BFM ports, classes
by UVM role, analysis ports, config_db keys, the VIP's configuration constants and mode macros, and the minimal compile closure for the
roles the testbench needs. Every fact carries `file:line`. Known defects of the VIP found by pattern detectors are recorded in
`known_issues` so the testbench does not depend on them. The result is `schemas/vip_integration.json`."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SCHEMA_VERSION = 1
SRC_SUFFIXES = (".sv", ".svh", ".v")
ROLE_BY_BASE = {"uvm_agent": "agent", "uvm_monitor": "monitor", "uvm_driver": "driver", "uvm_sequencer": "sequencer", "uvm_sequence": "sequence",
                "uvm_sequence_item": "item", "uvm_env": "env", "uvm_test": "test", "uvm_scoreboard": "scoreboard", "uvm_subscriber": "subscriber",
                "uvm_component": "component", "uvm_object": "object"}

# defects of a VIP that a pattern can show; a testbench that depends on the behavior must not (DUT-DV-019)
DETECTORS = [
    ("ZERO_BEAT_DROPPED", re.compile(r"foreach\s*\(\s*\w+\.(wdata|rdata)\[(\w+)\]\s*\)\s*begin\s*\n?\s*if\s*\(\s*\w+\.\1\[\2\]\s*!=\s*0\s*\)", re.S),
     "the VIP's item converter keeps a data beat only when its value is not 0: a monitored burst loses its zero-valued beats, so the VIP monitor "
     "cannot be a data checker"),
    ("ACTIVE_NEEDS_COVERAGE", re.compile(r"UVM_ACTIVE\s*\)\s*begin(?:(?!\bend\b).)*?\w*_cov_h\s*\.", re.S),
     "the agent's connect phase uses its coverage component inside the active branch: that component exists only when has_coverage is set, so "
     "an active agent needs has_coverage = 1 (otherwise a null object is dereferenced)"),
]


def _lines(path: Path) -> list[str]:
    return path.read_text(errors="replace").splitlines()


def _strip(line: str) -> str:
    return re.sub(r"//.*", "", line)


def scan_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in SRC_SUFFIXES and ".git" not in p.parts)


def rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root))


def units(root: Path, files: list[Path]) -> dict[str, dict]:
    """Design units defined in the files (package, interface, module): name -> {kind, file, line, imports, includes, instances}."""
    out: dict[str, dict] = {}
    names: list[tuple[str, str, Path, int]] = []
    for f in files:
        for n, ln in enumerate(_lines(f), 1):
            m = re.match(r"\s*(?:extern\s+)?(package|interface|module)\s+(?:automatic\s+)?(\w+)", _strip(ln))
            if m and m.group(1) in ("package", "interface", "module"):
                names.append((m.group(1), m.group(2), f, n))
    known = {n for _, n, _, _ in names}
    for kind, name, f, line in names:
        text = "\n".join(_strip(x) for x in _lines(f))
        imports = sorted({m.group(1) for m in re.finditer(r"\bimport\s+(\w+)\s*::", text)} & known - {name})
        incl = re.findall(r'`include\s+"([^"]+)"', text)
        # every other design unit named in the file counts: an instance, a port type (`axi4_if intf`), a `bind` target or unit
        inst = sorted(w for w in known - {name} if re.search(rf"\b{re.escape(w)}\b", text))
        out[name] = {"kind": kind, "file": rel(root, f), "line": line, "imports": imports, "includes": incl, "instances": sorted(set(inst)),
                     "strong_sva": bool(re.search(r"\bs_(?:until_with|until|eventually|nexttime|always)\b", text))}
    return out


def closure(us: dict[str, dict], roots: list[str]) -> list[str]:
    """Units needed by the roots, in a compile order: packages first (dependencies before users), then interfaces, then modules."""
    need: set[str] = set()
    stack = [r for r in roots if r in us]
    while stack:
        n = stack.pop()
        if n in need:
            continue
        need.add(n)
        stack += [d for d in us[n]["imports"] + us[n]["instances"] if d in us]
    # a unit that binds INTO a needed unit (bind <target> <unit>) is needed as well: handled through `instances` of the file that binds
    order: list[str] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen or n not in need:
            return
        seen.add(n)
        for d in us[n]["imports"] + us[n]["instances"]:
            visit(d)
        order.append(n)
    for kind in ("package", "interface", "module"):
        for n in sorted(need, key=lambda x: (x not in roots, x)):
            if us[n]["kind"] == kind:
                visit(n)
    return order


def classes(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        for n, ln in enumerate(_lines(f), 1):
            m = re.match(r"\s*(?:virtual\s+)?class\s+(\w+)\s*(?:#\(.*?\))?\s+extends\s+(\w+)", _strip(ln))
            if m:
                out.append({"name": m.group(1), "extends": m.group(2), "role": ROLE_BY_BASE.get(m.group(2), "derived"), "file": rel(root, f), "line": n})
    base = {c["name"]: c["role"] for c in out}
    for c in out:  # a class extending a VIP class takes that class's role
        if c["role"] == "derived":
            c["role"] = base.get(c["extends"], "derived")
    return out


def analysis_ports(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        cur = ""
        for n, ln in enumerate(_lines(f), 1):
            s = _strip(ln)
            cm = re.match(r"\s*class\s+(\w+)", s)
            if cm:
                cur = cm.group(1)
            m = re.match(r"\s*uvm_analysis_port\s*#\(\s*(\w+)\s*\)\s+(\w+)\s*;", s)
            if m:
                out.append({"class": cur, "name": m.group(2), "type": m.group(1), "file": rel(root, f), "line": n})
    return out


def config_db(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        for n, ln in enumerate(_lines(f), 1):
            m = re.search(r"uvm_config_db\s*#\(\s*([^)]*?)\s*\)\s*::\s*(set|get)\s*\((.*)\)", _strip(ln))
            if m:
                keys = re.findall(r'"([^"]*)"', m.group(3))
                out.append({"type": m.group(1), "op": m.group(2), "key": keys[-1] if keys else "", "file": rel(root, f), "line": n})
    return out


def ports(root: Path, f: Path) -> list[dict]:
    """Ports of the first interface/module in a file: direction, width, name."""
    text = "\n".join(_strip(x) for x in _lines(f))
    m = re.search(r"\b(?:interface|module)\s+\w+\s*(?:#\(.*?\))?\s*\((.*?)\)\s*;", text, re.S)
    if not m:
        return []
    out = []
    for item in m.group(1).split(","):
        t = re.sub(r"\s+", " ", item.strip())
        pm = re.match(r"(input|output|inout)?\s*(?:reg|logic|wire)?\s*(\[[^\]]*\])?\s*(\w+)$", t)
        if pm:
            out.append({"dir": pm.group(1) or "input", "width": (pm.group(2) or "").replace(" ", ""), "name": pm.group(3)})
    return out


def interface_signals(root: Path, f: Path) -> list[dict]:
    out = []
    for n, ln in enumerate(_lines(f), 1):
        m = re.match(r"\s*logic\s*(\[[^\]]*\])?\s*(\w+)\s*;", _strip(ln))
        if m:
            out.append({"name": m.group(2), "width": (m.group(1) or "").replace(" ", ""), "line": n})
    return out


def globals_of(root: Path, f: Path) -> dict:
    params, macros = {}, []
    for n, ln in enumerate(_lines(f), 1):
        s = _strip(ln)
        m = re.match(r"\s*parameter\s+(?:int|bit|longint)?\s*(\w+)\s*=\s*([^;]+);", s)
        if m and m.group(1) not in params:
            params[m.group(1)] = {"value": m.group(2).strip(), "line": n}
        for k in re.findall(r"`(?:ifdef|ifndef|elsif)\s+(\w+)", s):
            macros.append(k)
    return {"file": rel(root, f), "parameters": params, "mode_macros": sorted(set(macros))}


def filelist_modes(root: Path, fl: Path) -> dict:
    """The VIP's own compile filelist: files per mode (`//@MODE=<m>` tags), incdirs, and which files are mode independent."""
    modes: dict[str, list[str]] = {"all": []}
    incdirs = []
    for ln in _lines(fl):
        s = ln.strip()
        tag = re.search(r"//@MODE=(\w+)", s)
        s = re.sub(r"^//(?=\S)", "", s) if tag else s
        s = re.sub(r"//.*", "", s).strip() if not s.startswith("+incdir+") else re.sub(r"\s*//.*", "", s)
        if not s:
            continue
        if s.startswith("+incdir+"):
            incdirs.append(str((fl.parent / s[len("+incdir+"):]).resolve().relative_to(root.resolve())) if (fl.parent / s[len("+incdir+"):]).exists() else s)
            continue
        if s.startswith(("+", "-")):
            continue
        p = rel(root, (fl.parent / s).resolve()) if (fl.parent / s).exists() else s
        modes.setdefault(tag.group(1) if tag else "all", []).append(p)
    return {"file": rel(root, fl), "modes": modes, "incdirs": incdirs}


def enums(root: Path, f: Path) -> dict[str, list[str]]:
    """`typedef enum ... {A = .., B = ..} name_e;` of a package file: type -> member names."""
    text = re.sub(r"//[^\n]*", "", f.read_text(errors="replace"))
    return {m.group(2): re.findall(r"\b([A-Za-z_]\w*)\s*(?:=|,|$)", re.sub(r"=\s*[\w'`]+", "=", m.group(1)).replace("\n", " ") + ",")
            for m in re.finditer(r"typedef\s+enum\s*(?:bit|logic|int)?\s*(?:\[[^\]]*\])?\s*\{(.*?)\}\s*(\w+)\s*;", text, re.S)}


def class_members(root: Path, file: str, cls: str) -> list[dict]:
    """`type name;` members of a class (first declaration lines, before the methods)."""
    out, inside = [], False
    for n, ln in enumerate(_lines(root / file), 1):
        s = _strip(ln)
        if re.match(rf"\s*class\s+{cls}\b", s):
            inside = True
            continue
        if inside and re.match(r"\s*endclass", s):
            break
        m = inside and re.match(r"\s*(?:rand\s+|randc\s+|static\s+|local\s+|protected\s+|virtual\s+)*([A-Za-z_]\w*(?:\s*#\([^)]*\))?)\s*(?:\[[^\]]*\])?\s+(\w+)\s*(?:\[[^\]]*\])*\s*;", s)
        if m:
            out.append({"type": m.group(1), "name": m.group(2), "line": n})
    return out


def detect_issues(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        text = f.read_text(errors="replace")
        for iid, rx, msg in DETECTORS:
            for m in rx.finditer(text):
                out.append({"id": iid, "file": rel(root, f), "line": text[:m.start()].count("\n") + 1, "text": msg})
    return out


def pick(cls: list[dict], role: str, contains: str) -> str:
    c = [x for x in cls if x["role"] == role and contains in x["name"]]
    return sorted(c, key=lambda x: len(x["name"]))[0]["name"] if c else ""


def probe(vip_dir: Path, kind: str = "custom", revision: str | None = None, roots: list[str] | None = None, path_expr: str | None = None) -> dict:
    """The integration facts of a VIP directory. `roots`: design units the testbench needs (default: the master agent BFM and package).
    `path_expr`: how the VIP location is written in the options (an environment-variable expression keeps the artifact portable); the
    absolute location of this machine is never stored."""
    root = vip_dir.resolve()
    files = scan_files(root)
    srcs = [f for f in files if "test" not in f.relative_to(root).parts]  # the VIP's own tests are not part of the testbench
    us = units(root, srcs)
    cls = classes(root, srcs)
    roots = roots or [n for n in us if re.search(r"master.*(agent_bfm|pkg)$|globals", n) and "test" not in n and "seq" not in n] + [n for n in us if n.endswith("master_seq_pkg")]
    order = closure(us, roots)
    gfile = next((root / us[n]["file"] for n in us if "globals" in n and us[n]["kind"] == "package"), None)
    ifile = next((root / u["file"] for n, u in us.items() if u["kind"] == "interface" and re.search(r"axi\w*_if$|_if$", n)), None)
    fls = [filelist_modes(root, p) for p in root.rglob("*compile*.f")]
    bfms = {n: {"file": u["file"], "kind": u["kind"], "ports": ports(root, root / u["file"])} for n, u in us.items() if "bfm" in n and u["kind"] == "interface"}
    docs = sorted(rel(root, p) for p in root.rglob("*") if p.is_file() and p.suffix in (".md", ".pdf") and "doc" in p.relative_to(root).parts[:1] + (p.name,))
    lic = next((rel(root, p) for p in root.iterdir() if p.name.upper().startswith("LICENSE")), "")
    g = globals_of(root, gfile) if gfile else {}
    by_name = {c["name"]: c for c in cls}
    cov = next((c for c in cls if "master" in c["name"] and "coverage" in c["name"] and c["role"] == "subscriber"), None)
    roles = {"agent": pick(cls, "agent", "master"), "agent_config": next((c["name"] for c in cls if "master" in c["name"] and c["name"].endswith("agent_config")), ""),
             "item": next((c["name"] for c in cls if "master" in c["name"] and c["role"] == "item"), ""),
             "write_sequencer": next((c["name"] for c in cls if "master" in c["name"] and "write" in c["name"] and c["role"] == "sequencer"), ""),
             "read_sequencer": next((c["name"] for c in cls if "master" in c["name"] and "read" in c["name"] and c["role"] == "sequencer"), ""),
             "monitor_proxy": next((c["name"] for c in cls if "master" in c["name"] and "monitor" in c["name"] and c["role"] == "component"), ""),
             "driver_proxy": next((c["name"] for c in cls if "master" in c["name"] and "driver" in c["name"] and c["role"] in ("component", "driver")), "")}
    agent_members = class_members(root, by_name[roles["agent"]]["file"], roles["agent"]) if roles["agent"] in by_name else []
    members = {k: next((m["name"] for m in agent_members if m["type"] == roles[k]), "") for k in ("agent_config", "write_sequencer", "read_sequencer")}
    item_fields = [m["name"] for m in class_members(root, by_name[roles["item"]]["file"], roles["item"])] if roles["item"] in by_name else []
    return {
        "schema_version": SCHEMA_VERSION,
        "vip": {"path": path_expr or str(root), "kind": kind, "revision": revision or "", "license_file": lic, "docs": docs,
                "fingerprint": hashlib.sha256("".join(f"{rel(root, f)}:{hashlib.sha256(f.read_bytes()).hexdigest()}" for f in files).encode()).hexdigest()},
        "units": {n: u for n, u in us.items()},
        "compile": {"roots": roots, "order": [{"unit": n, "file": us[n]["file"], "kind": us[n]["kind"]} for n in order],
                    "vip_filelists": fls, "incdirs": sorted({str(Path(us[n]["file"]).parent) for n in order})},
        "globals": g,
        "interface": ({"file": rel(root, ifile), "name": next(n for n, u in us.items() if u["file"] == rel(root, ifile) and u["kind"] == "interface"),
                       "signals": interface_signals(root, ifile)} if ifile else {}),
        "bfms": bfms,
        "strong_sva_units": {n: {"file": us[n]["file"], "kind": us[n]["kind"], "ports": ports(root, root / us[n]["file"])} for n in order if us[n]["strong_sva"]},
        "classes": cls,
        "analysis_ports": analysis_ports(root, srcs),
        "config_db": config_db(root, srcs),
        "coverage_class": ({"name": cov["name"], "file": cov["file"], "include": Path(cov["file"]).name,
                            "members": [m for m in class_members(root, cov["file"], cov["name"]) if m["type"] == roles["agent_config"]],
                            "item": roles["item"]} if cov else None),
        "roles": roles, "agent_members": members, "item_fields": item_fields,
        "enums": enums(root, gfile) if gfile else {},
        "known_issues": detect_issues(root, srcs),
    }


def conformance(p: dict) -> list[str]:
    """Problems that make a probe unusable: the VIP must offer an agent, a config, an item, a sequencer, and a compile closure."""
    out = []
    for k in ("agent", "agent_config", "item", "write_sequencer", "read_sequencer"):
        if not p["roles"].get(k):
            out.append(f"the probe found no {k.replace('_', ' ')} class in the VIP")
    if not p["compile"]["order"]:
        out.append("the probe found no compile closure for the requested roots")
    for k in ("agent_config", "write_sequencer", "read_sequencer"):
        if not p["agent_members"].get(k):
            out.append(f"the agent class has no member of the {k.replace('_', ' ')} type")
    need = ["awid", "awaddr", "awlen", "awsize", "awburst", "wdata", "wstrb", "arid", "araddr", "arlen", "arsize", "arburst", "tx_type", "transfer_type", "rdata"]
    miss = [f for f in need if f not in p["item_fields"]]
    if miss:
        out.append("the VIP item class lacks the field(s) " + ", ".join(miss))
    need_enum = {"transfer_type_e": ["NON_OUTSTANDING_WRITE", "NON_OUTSTANDING_READ"], "tx_type_e": ["WRITE", "READ"]}
    for t, names in need_enum.items():
        gone = [n for n in names if n not in p["enums"].get(t, [])]
        if gone:
            out.append(f"the VIP enum {t} lacks {', '.join(gone)}")
    if not p["interface"]:
        out.append("the probe found no interface of the VIP")
    return out
