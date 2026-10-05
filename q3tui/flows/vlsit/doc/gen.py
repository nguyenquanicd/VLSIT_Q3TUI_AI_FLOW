"""`docs/specification.md` of the VLSIT flow (spec_pdf_generator.md): assembled by code from the artifacts and the RTL.
Every number and table comes from an artifact; the optional prose (introduction, architecture notes, clock / reset notes,
register map) is the only thing an LLM writes. A part whose artifact does not exist yet says so."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from q3tui.steps.vlsit import artifacts


def esc(text) -> str:
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")


def table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_none_", ""]
    return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|",
            *("| " + " | ".join(esc(c) for c in r) + " |" for r in rows), ""]


def tree(spec: dict, rtl_hierarchy: dict | None) -> list[str]:
    """Module hierarchy as a text tree: the elaborated RTL's when available, else the spec's module list."""
    lines: list[str] = []
    desc = {m["name"]: m.get("description", "") for m in spec["modules"]}
    kids = {m["name"]: m.get("instances", []) for m in spec["modules"]}

    def walk(name: str, inst: str | None, children: list, prefix: str, last: bool, root: bool, seen: tuple) -> None:
        label = f"{name}" + (f" `{inst}`" if inst and inst != name else "") + (f" — {desc[name]}" if desc.get(name) else "")
        lines.append(label if root else f"{prefix}{'└── ' if last else '├── '}{label}")
        child_prefix = "" if root else prefix + ("    " if last else "│   ")
        for i, c in enumerate(children):
            walk(c[0], c[1], c[2], child_prefix, i == len(children) - 1, False, seen)

    def from_spec(name: str, seen: tuple = ()) -> list:
        return [(c, None, from_spec(c, (*seen, name))) for c in kids.get(name, []) if c not in seen and c in kids]

    def from_rtl(node: dict) -> list:
        return [(c["module"], c["instance"], from_rtl(c)) for c in node.get("children", [])]

    top = spec["metadata"]["top_module"]
    if rtl_hierarchy:
        walk(rtl_hierarchy["module"], None, from_rtl(rtl_hierarchy), "", True, True, ())
    else:
        walk(top, None, from_spec(top), "", True, True, ())
    return ["```text", *lines, "```", ""]


_CLK = re.compile(r"(^|_)(clk|clock)(_|$)", re.I)
_RST = re.compile(r"(^|_)[a-z]?(rst|reset)(n|_n|_ni)?(_|$)", re.I)


def port_groups(ports: list) -> dict[str, list]:
    groups: dict[str, list] = {}
    for p in ports:
        base = re.sub(r"^(i|o|io)_", "", p.name)
        if _CLK.search(p.name):
            g = "Clock & Reset"
        elif _RST.search(p.name):
            g = "Clock & Reset"
        else:
            g = base.rsplit("_", 1)[0] if "_" in base else "Other"  # s_axis_tdata → s_axis
        groups.setdefault(g, []).append(p)
    singles = [g for g, ps in groups.items() if len(ps) == 1 and g not in ("Clock & Reset", "Other")]
    for g in singles:
        groups.setdefault("Other", []).extend(groups.pop(g))
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] != "Clock & Reset", kv[0] == "Other", kv[0])))


def rtl_design(engine, top: str | None):
    """(ParsedDesign | None, note): the RTL elaborated by pyslang for the top module's ports and the hierarchy."""
    from q3tui.steps.vlsit.base import layout

    lay = layout(engine)
    try:
        from q3tui.hdl.filelist import filelist_from_files, parse_filelist
        from q3tui.hdl.parse import parse_design

        fl_path = lay.rtl / "filelist.f"
        files = sorted(lay.rtl.glob("*.sv")) if lay.rtl.is_dir() else []
        if not fl_path.is_file() and not files:
            return None, "no RTL yet"
        fl = parse_filelist(fl_path, engine.project.root) if fl_path.is_file() else filelist_from_files(files)
        design = parse_design(fl, top)
        return (design if design.modules else None), ("" if design.ok else "the RTL has elaboration errors: ports may be incomplete")
    except Exception as exc:  # noqa: BLE001 - a document is never worth failing on the parser
        return None, f"RTL not parsed ({type(exc).__name__}: {exc})"


def build(engine, prose=None) -> str:
    p = engine.project
    spec = artifacts.read(p, "structured_spec.json")
    cfg = artifacts.read(p, "final_config.json") or {}
    rtm = artifacts.read(p, "rtm.json") or {}
    vr = artifacts.read(p, "verification_report.json") or {}
    synth = artifacts.read(p, "synth_report.json") or {}
    md = spec["metadata"]
    ip, rev, top = md["ip_name"], md["spec_revision"], md["top_module"]
    today = date.today().isoformat()
    design, note = rtl_design(engine, top)
    params = cfg.get("parameters") or {s["name"]: {"value": s["default"], "default": s["default"], "type": s["type"]} for s in spec["parameters"]}
    pdesc = {s["name"]: s["description"] for s in spec["parameters"]}
    feats = cfg.get("feature_matrix") or {}
    signed = sum(1 for r in rtm.get("requirements", []) if r.get("signed_off"))
    status = "Released" if (vr.get("metadata") or {}).get("gate_5_approved") else "Draft"

    o: list[str] = ["---", f'title: "{ip} — Hardware IP Specification"', f'revision: "{rev}"', f'date: "{today}"', f'status: "{status}"', "---", "",
                    "| Field | Value |", "|---|---|", f"| IP Name | `{ip}` |", f"| Top module | `{top}` |", f"| Revision | {rev} |",
                    f"| Date | {today} |", f"| Status | {status} |", "| Tool Flow | Q3TUI VLSIT flow |", ""]
    o += ["## Quick Reference", "", "### Module Hierarchy", "", *tree(spec, design.hierarchy if design else None),
          "### Parameter Summary", "", *table(["Parameter", "Default", "Description"], [[f"`{n}`", e["default"], pdesc.get(n, "")] for n, e in params.items()]),
          "### Feature Matrix", ""]
    o += table(["Feature", "Enabled", "Mandatory", "Parameters"],
               [[f, "✓" if e["enabled"] else "✗", "yes" if e["mandatory"] else "optional", ", ".join(e.get("parameters_driving", []))] for f, e in feats.items()]) \
        if feats else ["_not configured yet_", ""]

    o += ["## 01 — Overview", "", "### 1.1 Introduction", "", (prose.introduction if prose and prose.introduction else md.get("description") or f"`{ip}`."), ""]
    o += ["### 1.2 Key Features", ""]
    enabled = [r for r in spec["requirements"] if r["category"] == "functional" and not r.get("optional")
               or (r.get("feature_id") and feats.get(r["feature_id"], {}).get("enabled"))]
    o += [f"- {esc(r['text'])} ({r['req_id']})" for r in enabled] or ["_none_"]
    o += ["", "### 1.3 Top-Level Parameters", "", *table(["Parameter", "Type", "Default", "Value", "Description"],
                                                         [[f"`{n}`", e["type"], e["default"], e["value"], pdesc.get(n, "")] for n, e in params.items()])]
    o += ["### 1.4 Module Hierarchy", "", *tree(spec, design.hierarchy if design else None)]

    o += ["## 02 — Port List", ""]
    if design and top in design.modules:
        for g, ports in port_groups(design.modules[top].ports).items():
            o += [f"### {g}", "", *table(["Port", "Direction", "Type"], [[f"`{q.name}`", q.direction, f"`{q.type}`"] for q in ports])]
    else:
        o += [f"_The RTL's ports are not available ({note or 'no RTL'})._", ""]

    o += ["## 03 — Clock & Reset", ""]
    cr = [q for q in (design.modules[top].ports if design and top in design.modules else []) if _CLK.search(q.name) or _RST.search(q.name)]
    o += table(["Signal", "Direction", "Kind"], [[f"`{q.name}`", q.direction, "clock" if _CLK.search(q.name) else "reset"] for q in cr])
    if design and top in design.modules:
        m = design.modules[top]
        o += ["Detected in the RTL: " + (", ".join(f"clock `{c.name}` ({c.edge})" for c in m.clocks) or "no clock")
              + "; " + (", ".join(f"reset `{r.name}` ({r.polarity}, {r.kind})" for r in m.resets) or "no reset") + ".", ""]
    if prose and prose.clock_reset_notes:
        o += [prose.clock_reset_notes, ""]

    o += ["## 04 — Microarchitecture", "", *table(["Module", "Function", "Requirements"],
                                                   [[f"`{m['name']}`", m.get("description", ""), len(m.get("req_ids", []))] for m in spec["modules"]])]
    if prose and prose.microarchitecture:
        o += [prose.microarchitecture, ""]
    if prose and prose.register_map:
        o += ["## 05 — Register Map", "", prose.register_map, ""]

    o += ["## 06 — Functional Description", ""]
    for i, name in enumerate(spec["module_order"][::-1], 1):
        m = next(m for m in spec["modules"] if m["name"] == name)
        rs = [r for r in spec["requirements"] if name in r["rtl_modules"]]
        o += [f"### 6.{i} `{name}`", "", m.get("description", ""), ""]
        o += [f"- **{r['req_id']}** ({r['category']}) {esc(r['text'])}" for r in rs] or ["_no requirement_"]
        o += [""]

    o += ["## 07 — Verification Summary", ""]
    sim = vr.get("sim_summary")
    if sim:
        o += ["### Simulation Results", "", *table(["Metric", "Value"], [["Total TC", sim.get("total_tc")], ["PASS", sim.get("pass")], ["FAIL", sim.get("fail")],
                                                                      ["Timeout", sim.get("timeout", 0)], ["SVA violations", sim.get("sva_violations")]])]
    else:
        o += ["_Verification has not run (no verification_report.json)._", ""]
    mut = vr.get("mutation_summary")
    if mut:
        o += ["### Mutation Testing", "", *table(["REQ-ID", "Score", "Status"], [[r.get("req_id"), r.get("mutation_score", r.get("score")), r.get("status", "")]
                                                                               for r in mut.get("per_req_id", [])]),
              f"Global kill rate (informational): {mut.get('global_score')} — threshold {mut.get('threshold')}", ""]
    summ = vr.get("rtm_summary") or {}
    total = len(rtm.get("requirements", [])) or summ.get("total_req_ids") or len(spec["requirements"])
    o += ["### Sign-Off Summary", "", *table(["Status", "Count"], [["Signed-off", signed or summ.get("signed_off", 0)], ["Pending", summ.get("pending", "n/a")],
                                                                  ["Locked", summ.get("locked", "n/a")], ["Total REQ", total]])]

    o += ["## 08 — Synthesis Report", ""]
    if synth:
        rows = [[k, synth[k]] for k in ("tool", "pdk", "top_module", "cell_count", "area_um2", "flop_count", "target_freq_mhz") if k in synth]
        for corner, c in (synth.get("corners") or {}).items():
            rows.append([f"corner {corner}", c.get("status") if isinstance(c, dict) else c])
        o += [*table(["Metric", "Value"], rows), "Synthesis gives cell count and area only; it does not establish timing closure.", ""]
    else:
        o += ["_No synthesis report._", ""]

    o += ["## 09 — Requirements Traceability", ""]
    if rtm.get("requirements"):
        o += table(["REQ-ID", "Description", "RTL", "SVA", "TC", "Sim", "Mut", "S/O"],
                   [[r["req_id"], esc(r["text"])[:80], "✓" if r.get("rtl_traced") else "✗", "✓" if r.get("sva_traced") else "✗",
                     "✓" if r.get("tc_traced") else "✗", "✓" if r.get("sim_pass") else "✗", r.get("mutation_score"), "✓" if r.get("signed_off") else "—"]
                    for r in rtm["requirements"]])
    else:
        o += table(["REQ-ID", "Description", "Category"], [[r["req_id"], esc(r["text"])[:80], r["category"]] for r in spec["requirements"]])
    return "\n".join(o).rstrip() + "\n"
