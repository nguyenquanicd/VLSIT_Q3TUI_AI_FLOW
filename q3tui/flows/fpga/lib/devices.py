"""The FPGA devices the FPGA flow can synthesize for (the project flow's FPGA view, docs/spec/project-flow.md "The FPGA"), like the ASIC flow's
library catalog: the families' own devices (built in), the parts you add (`config/fpgas.json`) and the parts your spec names, each with the
family (and so the tool) that synthesizes it, worked out from the part's name by code (lib/templates.py). The one in use is `fpga_synth`'s
`part` (saved to q3tui.yaml); without one the flow used the default part, whatever the spec said (rv32iv: its spec names an Arria 10, the
synthesis ran on a Kintex-7)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from q3tui.flows.fpga.lib import templates

PART_OPT = "pipeline.step_options.fpga_synth.part"
FILE = "fpgas.json"

XILINX7 = (("xc7k325tffg900-2", "Kintex-7 325T (KC705)"), ("xc7k160tffg676-2", "Kintex-7 160T"), ("xc7a200tsbg484-1", "Artix-7 200T"),
           ("xc7a100tcsg324-1", "Artix-7 100T (Arty A7-100)"), ("xc7a35ticsg324-1L", "Artix-7 35T (Arty A7-35)"), ("xc7z020clg400-1", "Zynq-7020 (PYNQ-Z2)"),
           ("xc7s50csga324-1", "Spartan-7 50"), ("xc7v585tffg1157-2", "Virtex-7 585T"))

# families no template here synthesizes (listed, said why): (pattern on the part name, vendor, family)
_NOT_HERE = ((r"^x[caq](ku|vu|zu|kup|vup|cku|cvu|cvp)", "Xilinx", "UltraScale / UltraScale+"), (r"^xc[vh][cmp]\d", "Xilinx", "Versal"),
             (r"^(gw1|gw2|gw5)", "Gowin", "GW"), (r"^(mpf|m2s|m2gl)", "Microchip", "PolarFire / SmartFusion"))
# part numbers in a spec: Xilinx (xc7k325tffg900-2, xczu9eg-ffvb1156-2-e), Intel (10AX115N2F40E2LG, 10AXF40GAA, 5CEBA4F23C7, 10CL025…), Lattice
_SPEC_PART = re.compile(r"\b(x[caq](?:7[aksvz]|ku|vu|zu|v[cmp])\w*(?:-\w+)*|10A[XS]\w+|1S[GXM]\w+|5C[EGS]\w+|10M\d\w+|10C[XL]\w+|EP4CE\w+|AG[FI]\w+|"
                        r"LFE5UM?5?G?-\d+F(?:-\w+)*|iCE40[A-Z]{2}\d+K\w*)\b", re.I)


def identify(part: str) -> dict:
    """{part, vendor, family, template (the family's id, or None when none here synthesizes it), device (the form its tool takes), note}."""
    p = part.strip()
    low = p.lower()
    t = templates.for_part(p)
    if t is not None:
        device = p
        if t.id == "ecp5" and low.startswith("lfe5u"):  # LFE5UM5G-85F-8BG381C → um5g-85k (nextpnr's device flag)
            m = re.match(r"lfe5u(m5g|m)?-(\d+)f", low)
            device = ({"m5g": "um5g-", "m": "um-"}.get(m.group(1) or "", "") + f"{m.group(2)}k") if m else p
        elif t.id == "ice40" and low.startswith("ice40"):  # iCE40HX8K-CT256 → hx8k
            m = re.match(r"ice40([a-z]{2}\d+k)", low)
            device = m.group(1) if m else p
        elif t.backend == "yosys":
            device = low
        return {"part": p, "vendor": t.vendor, "family": t.family, "template": t.id, "device": device, "note": ""}
    for pat, vendor, family in _NOT_HERE:
        if re.search(pat, low):
            return {"part": p, "vendor": vendor, "family": family, "template": None, "device": p, "note": f"no family here synthesizes {vendor} {family} parts yet"}
    return {"part": p, "vendor": "?", "family": "?", "template": None, "device": p, "note": "a part Q3TUI does not know: no family here synthesizes it"}


def _file(project) -> Path:
    return project.config_dir / FILE


def load(project) -> list[dict]:
    f = _file(project)
    try:
        data = json.loads(f.read_text()) if f.is_file() else {}
    except ValueError:
        data = {}
    return [p for p in data.get("parts", []) if isinstance(p, dict) and p.get("part")]


def save(project, parts: list[dict]) -> Path:
    f = _file(project)
    project.backup([f])
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"parts": parts}, indent=2) + "\n")
    return f


def spec_parts(project) -> list[str]:
    """The part numbers your spec names (by their form: code, not a guess)."""
    out: list[str] = []
    for doc in project.spec_documents():
        if doc.suffix not in (".md", ".txt"):
            continue
        out += [m.group(1) for m in _SPEC_PART.finditer(doc.read_text(errors="replace"))]
    return list(dict.fromkeys(out))


def in_use(project) -> dict:
    """The device fpga_synth uses here: {template (its family's id, None: none synthesizes it), part, device, set (False: nothing chosen: the
    default part), note}."""
    opts = project.cfg.pipeline.step_options.get("fpga_synth") or {}
    tpl, device, note = templates.resolve(opts)
    return {"template": tpl.id if tpl else None, "part": str(opts.get("part") or ""), "device": device, "set": bool(opts.get("part") or opts.get("template")),
            "note": note}


def entries(project) -> list[dict]:
    """Every device listed: built in (the templates' devices), yours (config/fpgas.json), the spec's; each {part, title, template, vendor,
    family, source, supported, device, note}. One row per part (yours win over a built-in one; the spec's mark a part you have)."""
    rows: dict[str, dict] = {}

    def add(part: str, title: str, source: str) -> None:
        info = identify(part)
        tpl = info["template"]
        key = part.lower()
        prev = rows.get(key)
        sources = sorted({*(prev["sources"] if prev else []), source})
        rows[key] = {"part": part, "title": title or (prev or {}).get("title") or f"{info['vendor']} {info['family']}", "template": tpl,
                     "vendor": info["vendor"], "family": info["family"], "device": info["device"], "source": "yours" if "yours" in sources else sources[0],
                     "sources": sources, "supported": bool(tpl), "note": info["note"] if not tpl else ""}

    for part, title in XILINX7:
        add(part, title, "built-in")
    for t in templates.TEMPLATES.values():
        if t.backend == "yosys":
            for dev, pkg in t.devices.items():
                add(dev, f"{t.vendor} {t.family} {dev} ({pkg})", "built-in")
        elif t.backend == "quartus":
            add(t.part, f"{t.vendor} {t.family}", "built-in")
    for p in load(project):
        add(str(p["part"]), str(p.get("title") or ""), "yours")
    for part in spec_parts(project):
        add(part, "", "spec")
    return list(rows.values())


def problems(project) -> list[str]:
    """What does not fit: nothing chosen while the spec names a part, the spec's part not in use, the spec's part no template synthesizes."""
    use = in_use(project)
    out = []
    named = spec_parts(project)
    if named and use["device"].lower() not in {identify(p)["device"].lower() for p in named}:
        what = "the default " if not use["set"] else ""
        why = ", as nothing is chosen" if not use["set"] else ""
        fam = templates.TEMPLATES[use["template"]].label if use["template"] else "no family here"
        out.append(f"the spec names {', '.join(named[:3])}; the FPGA flow uses {what}{use['device']} ({fam}){why}: its numbers are for another part")
    for p in named:
        info = identify(p)
        if not info["template"]:
            out.append(f"{p} ({info['vendor']} {info['family']}): {info['note']}")
    if use["note"]:
        out.append(use["note"])
    return out
