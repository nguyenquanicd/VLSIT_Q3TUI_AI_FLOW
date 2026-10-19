"""The agent worklist of the scaffold: every `DV_REGION` that is still a stub (DUT-DV-013, 024). Code lists them with their requirement
context so the agent opens only the region it must implement."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows.vlsit.tb.uvm import svgen as g
from q3tui.flows.vlsit.tb.uvm.contract import VerificationContract

KINDS = (("SBR-", "scoreboard_rule"), ("COV-", "coverage_goal"), ("test.", "test"), ("seq.", "sequence"), ("sram.", "sram_model"),
         ("adapter.", "adapter"), ("assertions", "assertion"))


def kind_of(rid: str) -> str:
    return next((k for p, k in KINDS if rid.startswith(p)), "other")


def context(rid: str, c: VerificationContract) -> dict:
    side = rid.endswith(".sram")
    rid = rid[:-5] if side else rid
    if rid.startswith("SBR-"):
        r = next((x for x in c.scoreboard_rules if x.id == rid), None)
        return {"requirements": r.requirement_ids, "source": r.source, "text": r.description + (" [memory-side hook: called for every SRAM access observed]" if side else "")} if r else {}
    if rid.startswith("COV-"):
        k = next((x for x in c.coverage if x.id == rid), None)
        return {"requirements": k.requirement_ids, "objectives": k.objective_ids, "text": k.description} if k else {}
    if rid.startswith("test."):
        t = next((x for x in c.tests if x.tc_id == rid[5:]), None)
        return {"requirements": t.requirement_ids, "text": t.description or t.name} if t else {}
    if rid.startswith("seq."):
        t = next((x for x in c.tests if x.kind == "smoke"), None)
        return {"requirements": t.requirement_ids, "rules": t.rule_ids, "text": t.description} if t else {}
    return {}


def scan(tb_dir: Path, c: VerificationContract) -> list[dict]:
    """Every region of the testbench: id, file, line, owner, whether a DV_TODO is still in it, and its context."""
    items = []
    for f in sorted(tb_dir.rglob("*")):
        if not f.is_file() or f.suffix not in (".sv", ".svh"):
            continue
        text = f.read_text(errors="replace")
        for m in g.REGION_RE.finditer(text):
            rid, owner, body = m.group(2), m.group(3), m.group(4)
            items.append({"id": rid, "kind": kind_of(rid), "file": str(f.relative_to(tb_dir)), "line": text[:m.start()].count("\n") + 1,
                          "owner": owner, "open": "DV_TODO(" in body, **context(rid, c)})
    return items


def summary(items: list[dict]) -> dict:
    return {"regions": len(items), "open": sum(i["open"] for i in items), "agent_or_user_owned": sum(i["owner"] != "scaffold" for i in items),
            "implemented": sum(not i["open"] for i in items)}
