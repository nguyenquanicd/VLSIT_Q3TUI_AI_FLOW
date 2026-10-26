"""Semantic implementation of the scaffold's DV regions by an agent (milestone M5; rules DUT-DV-002, 003, 008, 013, 019, 024).

The agent returns the body of each region as data; code splices it into the region, marks the owner `agent` (so it is distinguishable from the
scaffold and kept on regeneration), and checks it. The agent reads the specification and the generated testbench, never the RTL. Only the regions
on the smoke path are implemented here (the SRAM model, the smoke sequence and the scoreboard rules the smoke test relies on); the other regions stay
stubs that announce themselves."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.flows.dv.uvm import svgen as g
from q3tui.flows.dv.uvm import worklist as wl
from q3tui.flows.dv.uvm.contract import VerificationContract
from q3tui.steps.common import Question

FORBIDDEN = [(re.compile(r"DV_TODO"), "DV_TODO must be removed from an implemented region"),
             (re.compile(r"\$(finish|fatal|display|write|stop)\b"), "$finish/$fatal/$display/$write/$stop are not allowed (use uvm_info/uvm_error)"),
             (re.compile(r"(?<![\w$])#\s*[\d(]"), "delay controls are not allowed (sample and drive through the clocking blocks)"),
             (re.compile(r"\$u?random\b"), "$random is not allowed (randomize through UVM)"),
             (re.compile(r"\b(force|release)\b"), "force/release are not allowed"),
             (re.compile(r"\b(u_dut|dut)\s*\."), "hierarchical references into the DUT are not allowed (DUT-DV-028)"),
             (re.compile(r"\btb_top\s*\."), "hierarchical references into tb_top are not allowed")]


class RegionImpl(BaseModel):
    id: str = Field(description="The region id exactly as listed in the task (e.g. SBR-001, sram.responder, seq.smoke)")
    body: str = Field(description="SystemVerilog statements of the region body: no markers, no DV_TODO, no module/class declarations")
    note: str = Field("", description="One line: what it implements and from which requirement(s)")


class SemanticResult(BaseModel):
    regions: list[RegionImpl]
    summary: str = ""
    questions: list[Question] = Field(default_factory=list, description="Only questions about the specification (spec_gap); usually none")


def smoke_region_ids(c: VerificationContract) -> list[str]:
    """Regions on the smoke path: the SRAM model, the smoke sequence and the scoreboard rules the smoke test relies on."""
    from q3tui.flows.dv.uvm.scaffold import rule_uses_sram

    smoke = next((t for t in c.tests if t.kind == "smoke" and t.milestone == "v0_1"), None)
    rules = {r.id: r for r in c.scoreboard_rules}
    ids = []
    for rid in (smoke.rule_ids if smoke else []):
        ids.append(rid)
        if rid in rules and rule_uses_sram(rules[rid], c):
            ids.append(f"{rid}.sram")
    return ["sram.monitor", "sram.responder", "seq.smoke", *ids]


def units(c: VerificationContract, scope: str = "smoke", items: list[dict] | None = None) -> list[dict]:
    """The agent's units of work. `smoke`: the SRAM model and the checker of the smoke path. `all`: also the remaining scoreboard rules and the directed
    tests, in groups of at most 6 tests. Coverage and assertions are not part of it. `items` (the worklist) limits the ids to regions that exist."""
    from q3tui.flows.dv.uvm.scaffold import rule_uses_sram

    ids = smoke_region_ids(c)
    out = [{"name": "sram", "regions": [i for i in ids if i.startswith("sram.")]},
           {"name": "checker", "regions": [i for i in ids if not i.startswith("sram.")]}]
    if scope == "all":
        have = {i["id"] for i in items} if items is not None else None
        rules = []
        for r in c.scoreboard_rules:
            if r.id not in ids:
                rules += [r.id, f"{r.id}.sram"] if rule_uses_sram(r, c) else [r.id]
        tests = [f"test.{t.tc_id}" for t in c.tests if not (t.kind == "smoke" and t.milestone == "v0_1")]
        ok = lambda x: have is None or x in have  # noqa: E731
        out.append({"name": "rules", "regions": [i for i in rules if ok(i)]})
        for n, k in (("tests_a", 0), ("tests_b", 6)):
            out.append({"name": n, "regions": [i for i in tests[k:k + 6] if ok(i)]})
    return [u for u in out if u["regions"]]


def mark(text: str, ids: list[str]) -> str:
    """The file text with the target regions flagged for the prompt."""
    for rid in ids:
        text = text.replace(f"// DV_REGION({rid}) BEGIN", f"// >>> IMPLEMENT THIS REGION: {rid}\n    // DV_REGION({rid}) BEGIN", 1)
    return text


def unit_context(tb_dir: Path, c: VerificationContract, unit: dict, items: list[dict]) -> dict:
    """What the agent needs for a unit, as data (also the basis of its input hash)."""
    mine = [i for i in items if i["id"] in unit["regions"]]
    reqs = {r.req_id: r for r in c.requirements}
    need: list[str] = []
    for i in mine:
        for r in i.get("requirements", []):
            if r not in need:
                need.append(r)
    smoke = next((t for t in c.tests if t.kind == "smoke" and t.milestone == "v0_1"), None)
    files = sorted({i["file"] for i in mine})
    tests = [{"tc_id": t.tc_id, "name": t.name, "kind": t.kind, "description": t.description, "rules": t.rule_ids, "requirements": t.requirement_ids}
             for t in c.tests if f"test.{t.tc_id}" in unit["regions"]]
    extra = ("adapters/dut_axi_adapter.svh", "sequences/dut_seq_lib.svh", "tests/dut_test_lib.svh") if tests else ()
    return {"regions": [{k: i.get(k) for k in ("id", "kind", "file", "line", "owner", "requirements", "source", "text")} for i in mine],
            "files": {f: (tb_dir / f).read_text() for f in files},
            "requirements": {r: {"text": reqs[r].text, "status": reqs[r].status, "origin": reqs[r].origin_class, "reason": reqs[r].status_reason} for r in need if r in reqs},
            "smoke": ({"tc_id": smoke.tc_id, "description": smoke.description, "rules": smoke.rule_ids} if smoke else None),
            "support": {f: (tb_dir / f).read_text() for f in ("interfaces/dut_pins_if.sv", "env/dut_txn.svh", *extra) if (tb_dir / f).is_file() and f not in files},
            **({"tests": tests} if tests else {})}


def input_hash(ctx: dict) -> str:
    keep = {"regions": [{k: v for k, v in r.items() if k not in ("line", "owner")} for r in ctx["regions"]], "requirements": ctx["requirements"], "smoke": ctx["smoke"],
            "support": ctx["support"], **({"tests": ctx["tests"]} if ctx.get("tests") else {})}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:20]


def prompt(ctx: dict, decisions: list[str], previous: dict[str, str], problems: list[str], feedback: list[str]) -> str:
    parts = ["# Task: implement the regions below (code only inside the regions; the rest of each file is fixed)"]
    for r in ctx["regions"]:
        parts.append(f"\n## Region `{r['id']}` ({r['kind']}) in `{r['file']}`" + (f"\nRequirements: {', '.join(r['requirements'])}" if r.get("requirements") else "")
                     + (f"\nSpecification source: {r['source']}" if r.get("source") else "") + (f"\nIntent: {r['text']}" if r.get("text") else "")
                     + (f"\nCurrent implementation (update it, keep what is still right):\n```systemverilog\n{previous[r['id']]}\n```" if r["id"] in previous else ""))
    parts.append("\n## Requirements (approved; the specification is the only source of expected behavior)\n"
                 + "\n".join(f"- {k} [{v['status']}{', ' + v['origin'] if v['origin'] != 'NORMATIVE' else ''}]: {v['text']}" + (f" (decision: {v['reason']})" if v["reason"] else "")
                             for k, v in ctx["requirements"].items()))
    for t in ctx.get("tests", []):
        parts.append(f"\n## Test {t['tc_id']} ({t['kind']}): {t['name']}\nRequirements: {', '.join(t['requirements'])}; scoreboard rules enabled by the class: {', '.join(t['rules'])}\n{t['description']}")
    if ctx["smoke"]:
        parts.append(f"\n## The smoke test {ctx['smoke']['tc_id']}\n{ctx['smoke']['description']}")
    if decisions:
        parts.append("\n## Decisions the user already made (apply them; do not ask again)\n" + "\n".join(f"- {d}" for d in decisions))
    for f, t in ctx["support"].items():
        parts.append(f"\n## `{f}` (fixed; read-only)\n```systemverilog\n{t}\n```")
    for f, t in ctx["files"].items():
        parts.append(f"\n## `{f}` (the regions to implement are flagged)\n```systemverilog\n{mark(t, [r['id'] for r in ctx['regions']])}\n```")
    if feedback:
        parts.append("\n## Change requests\n" + "\n".join(f"- {x}" for x in feedback))
    if problems:
        parts.append("\n## Problems found by Q3TUI's checks and by the compiler (fix every one; the compiler output is ground truth)\n" + "\n".join(f"- {p}" for p in problems))
    parts.append("\nReturn one entry per region listed above, with the complete body of the region.")
    return "\n".join(parts)


def region_bodies(tb_dir: Path, items: list[dict], ids: list[str]) -> dict[str, str]:
    """The current body text of regions (without the markers)."""
    out = {}
    for it in items:
        if it["id"] in ids and it["owner"] != "scaffold":
            m = g.regions((tb_dir / it["file"]).read_text()).get(it["id"])
            if m:
                out[it["id"]] = "\n".join(m[1].splitlines()[1:-1])
    return out


def check_body(rid: str, body: str) -> list[str]:
    out = []
    if not body.strip():
        out.append(f"{rid}: the body is empty")
    for rx, msg in FORBIDDEN:
        if rx.search(re.sub(r"//[^\n]*", "", body)):
            out.append(f"{rid}: {msg}")
    return out


def apply(tb_dir: Path, items: list[dict], impls: list[RegionImpl], allowed: list[str], vip_names: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Splice the agent's bodies into their regions. Returns (problems, ids written). A region owned by the user is never touched."""
    by_id = {i["id"]: i for i in items}
    problems: list[str] = []
    written: list[str] = []
    for impl in impls:
        it = by_id.get(impl.id)
        if impl.id not in allowed or it is None:
            problems.append(f"{impl.id}: not a region of this task")
            continue
        if it["owner"] == "user":
            problems.append(f"{impl.id}: owned by the user: not changed")
            continue
        bad = check_body(impl.id, impl.body)
        for n in sorted(vip_names or ()):
            if re.search(rf"\b{re.escape(n)}\b", re.sub(r"//[^\n]*", "", impl.body)):
                bad.append(f"{impl.id}: references the VIP name {n} (DUT-DV-019: only the adapter may)")
        if bad:
            problems += bad
            continue
        path = tb_dir / it["file"]
        text = path.read_text()
        m = g.regions(text)[impl.id][1]
        indent = re.match(r"([ \t]*)", m).group(1)
        body = "\n".join((indent + ln if ln.strip() else "") for ln in impl.body.strip("\n").split("\n"))
        new = f"{indent}// DV_REGION({impl.id}) BEGIN agent\n{body}\n{indent}// DV_REGION({impl.id}) END\n"
        path.write_text(text.replace(m, new, 1))
        written.append(impl.id)
    missing = [i for i in allowed if i not in {x.id for x in impls}]
    problems += [f"{i}: no implementation returned" for i in missing]
    return problems, written
