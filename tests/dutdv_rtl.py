"""A small stand-in RTL for tests: a top with the interface of the approved contract (outputs tied to 0). Not a model of any design."""

import json
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "axi4_sram_ctrl"


def mini_rtl(top: str = "m_vlsi_axi4_sram", param_prefix: str = "PARA_", drop_port: str | None = None, widen: dict[str, str] | None = None) -> str:
    c = json.loads((EXAMPLE / "schemas" / "verification_contract.json").read_text())["interface_contract"]
    params = [p for p in c["parameters"]]
    pl = ",\n".join(f"  parameter int {param_prefix}{p['name'].split('_', 1)[1]} = {p['value']}" for p in params)
    ports = []
    for p in c["ports"]:
        if p["name"] == drop_port:
            continue
        w = (widen or {}).get(p["name"], p["width"])
        for q in params:
            w = w.replace(q["name"], param_prefix + q["name"].split("_", 1)[1])
        rng = "" if w == "1" else (w if w.startswith("[") else f"[({w})-1:0]")
        ports.append(f"  {'input' if p['direction'].startswith('in') else 'output'} logic {rng} {p['name']}")
    outs = [p["name"] for p in c["ports"] if not p["direction"].startswith("in") and p["name"] != drop_port]
    return (f"// RTL_SENTINEL_TOKEN: test stand-in, outputs tied to 0\nmodule {top} #(\n{pl}\n) (\n" + ",\n".join(ports) + "\n);\n"
            + "".join(f"  assign {o} = '0;\n" for o in outs) + "endmodule\n")
