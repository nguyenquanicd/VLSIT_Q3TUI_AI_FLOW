"""The requirements traceability matrix (schemas/rtm.json) — computed from the project's files, never patched in place.

Every row is rebuilt from its sources: the requirements (structured_spec.json), the `// REQ-xxx` tags in src/rtl, the
assertions parsed from src/sva (with your reviews), the selected test cases, the last simulation / mutation results (only
when they belong to the current RTL / TB / SVA) and your sign-offs. So a row can never claim more than the files show, and
`signed_off` exists only when you signed and conditions 1–5 still hold.

The six conditions (docs/spec/vlsit-flow.md): 1 RTL traced · 2 SVA traced (an assertion you confirmed) · 3 TC traced ·
4 simulation passes (no failing TC, no SVA violation) · 5 mutation score ≥ 0.85 (or `N/A`: no mutant could be made / run —
allowed by the flow's policy, never counted as evidence) · 6 you sign off (`review.sign_req`).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from q3tui.core.project import read_json, write_json
from q3tui.steps.vlsit import artifacts
from q3tui.steps.vlsit.base import layout

THRESHOLD = 0.85
REQ_RE = re.compile(r"REQ-\d{3,}")
_TAG = re.compile(r"//.*?(REQ-\d{3,}(?:\s*[,/&]\s*REQ-\d{3,})*)")
_PROP = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(assert|cover|assume)\s+property\b")


# -- sources --------------------------------------------------------------------------------------


def spec_data(engine) -> dict:
    return artifacts.read(engine.project, "structured_spec.json", {}) or {}


def requirements(engine) -> list[dict]:
    return [r for r in spec_data(engine).get("requirements", []) if isinstance(r, dict) and r.get("req_id")]


def modules(engine) -> list[dict]:
    """[{name, req_ids}] — the parse step's module mapping (`modules`, else each requirement's `rtl_modules`, else the
    RTL files)."""
    data = spec_data(engine)
    out: dict[str, set[str]] = {}
    for m in data.get("modules") or []:
        if isinstance(m, dict) and (m.get("name") or m.get("module")):
            out.setdefault(m.get("name") or m.get("module"), set()).update(m.get("req_ids") or [])
    if not out:
        for r in requirements(engine):
            for m in r.get("rtl_modules") or []:
                out.setdefault(m if isinstance(m, str) else m.get("name", ""), set()).add(r["req_id"])
        out.pop("", None)
    if not out:
        for f in sorted(layout(engine).rtl.glob("*.sv")):
            if not f.stem.endswith("_pkg"):
                out[f.stem] = set()
    return [{"name": n, "req_ids": sorted(ids)} for n, ids in out.items()]


def testplan(engine) -> list[dict]:
    """The selected test cases: {tc_id, title, req_ids, file}."""
    data = artifacts.read(engine.project, "selected_testplan.json", {}) or {}
    return [t for t in data.get("test_cases", []) if isinstance(t, dict) and t.get("tc_id") and t.get("selected", True)]


def rtl_files(engine) -> list[Path]:
    d = layout(engine).rtl
    return sorted(d.glob("*.sv")) if d.is_dir() else []


def rtl_tags(engine) -> dict[str, list[tuple[str, int]]]:
    """REQ-id -> [(file relative to the project, line)] of every `// REQ-xxx` tag in the RTL."""
    out: dict[str, list[tuple[str, int]]] = {}
    for f in rtl_files(engine):
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            m = _TAG.search(line)
            if m:
                for rid in REQ_RE.findall(m.group(1)):
                    out.setdefault(rid, []).append((engine.project.rel(f), n))
    return out


def fingerprint(engine, sva: bool = True, tb: bool = True) -> str:
    """Hash of the sources a simulation result belongs to (RTL, TB, SVA, the plan)."""
    lay = layout(engine)
    h = hashlib.sha256()
    dirs = [lay.rtl] + ([lay.tb] if tb else []) + ([lay.sva] if sva else [])
    for d in dirs:
        if d.is_dir():
            for f in sorted(p for p in d.rglob("*") if p.is_file() and p.suffix not in (".md", ".log")):
                h.update(f"{f.relative_to(d)}\0".encode() + f.read_bytes())
    plan = lay.artifact("selected_testplan.json")
    if plan.is_file():
        h.update(plan.read_bytes())
    return h.hexdigest()[:20]


# -- assertions (parsed from src/sva) -----------------------------------------------------------------


def parse_sva_text(text: str, module: str, file: str = "") -> list[dict]:
    """Properties of one SVA file: {label, module, req_ids, nl, kind, file, line}. An assertion's `// NL:` line(s) and
    `// REQ:` line sit right above it (a trailing `// REQ-xxx` on its own line counts too)."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = _PROP.match(line)
        if not m:
            continue
        label, kind = m.group(1), m.group(2)
        above: list[str] = []
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("//"):
            above.insert(0, lines[j].strip()[2:].strip())
            j -= 1
        nl_parts, reqs = [], set(REQ_RE.findall(line))
        collecting = False
        for c in above:
            if c.upper().startswith("NL:"):
                nl_parts, collecting = [c[3:].strip()], True
            elif c.upper().startswith("REQ"):
                reqs.update(REQ_RE.findall(c))
                collecting = False
            elif collecting and c:
                nl_parts.append(c)
        out.append({"label": label, "module": module, "req_ids": sorted(reqs), "nl": " ".join(nl_parts).strip(),
                    "kind": "cover" if kind == "cover" else "assert", "file": file, "line": i + 1})
    return out


def sva_files(engine) -> list[Path]:
    d = layout(engine).sva
    return sorted(p for p in d.glob("*_sva.sv")) if d.is_dir() else []


def parse_properties(engine) -> list[dict]:
    out = []
    for f in sva_files(engine):
        module = f.stem[: -len("_sva")]
        out += parse_sva_text(f.read_text(errors="replace"), module, engine.project.rel(f))
    return out


# -- your reviews and sign-offs (kept on reset) --------------------------------------------------------------


def reviews_path(engine) -> Path:
    return layout(engine).artifact("sva_reviews.json")


def signoffs_path(engine) -> Path:
    return layout(engine).artifact("rtm_signoffs.json")


def reviews(engine) -> dict[str, dict]:
    return read_json(reviews_path(engine), default={}) or {}


def signoffs(engine) -> dict[str, dict]:
    return read_json(signoffs_path(engine), default={}) or {}


def work_dir(engine, name: str) -> Path:
    return layout(engine).sim / name


def sim_results(engine) -> dict | None:
    """The last verification run's results, only when it belongs to the current sources."""
    data = read_json(work_dir(engine, "verify") / "results.json", default=None)
    if isinstance(data, dict) and data.get("fingerprint") == fingerprint(engine):
        return data
    return None


def mutation_results(engine) -> dict | None:
    data = read_json(work_dir(engine, "verify") / "mutation.json", default=None)
    if isinstance(data, dict) and data.get("fingerprint") == fingerprint(engine):
        return data
    return None


def vacuity(engine) -> dict | None:
    data = read_json(work_dir(engine, "sva") / "vacuity.json", default=None)
    if isinstance(data, dict) and data.get("fingerprint") == fingerprint(engine, tb=False):
        return data
    return None


def properties(engine) -> list[dict]:
    """Parsed properties + your review status + vacuity (None: not checked)."""
    revs, vac = reviews(engine), vacuity(engine)
    hits = set((vac or {}).get("hits") or []) if vac and vac.get("ran") else None
    props = parse_properties(engine)
    by_label = {p["label"]: p for p in props}
    counts: dict[str, int] = {}
    for p in props:
        counts[p["label"]] = counts.get(p["label"], 0) + 1
    for p in props:
        # identity: the label — or `module:label` when two modules use the same one (rv32im: the div and muldiv assertions share
        # names), so that a review, a table row and a sign-off are about one assertion
        p["id"] = p["label"] if counts[p["label"]] == 1 else f"{p['module']}:{p['label']}"
        r = revs.get(p["id"], {})
        p["status"] = r.get("status", "pending")
        p["note"] = r.get("note", "")
        p["vacuous"] = None
        if hits is not None:
            if p["kind"] == "cover":
                p["vacuous"] = p["label"] not in hits
            else:
                rest = p["label"][2:] if p["label"].startswith("a_") else p["label"]
                companions = [c for c in (f"c_{rest}", f"c_{rest}_trig", f"c_{rest}_ant") if c in by_label]
                if companions:
                    p["vacuous"] = not any(c in hits for c in companions)
    return props


# -- the matrix ----------------------------------------------------------------------------------------------


def build_rtm(engine) -> dict:
    """The complete rtm.json, rebuilt from the sources."""
    project = engine.project
    spec = spec_data(engine)
    meta_in = spec.get("metadata", {})
    old = dict((artifacts.read(project, "rtm.json", {}) or {}).get("metadata", {}))
    # the gates' own record wins over the file (a reset of verify deletes rtm.json, which sva shares: the flags must not go with it)
    for kind, flag in (("vlsit_sva", "gate_3"), ("vlsit_verify", "gate_5")):
        step = next((s for s in engine.steps if s.kind == kind), None)
        rec = engine.state.gates.get(step.name) if step else None
        if rec is not None and rec.status == "approved" and not old.get(f"{flag}_approved"):
            old[f"{flag}_approved"], old[f"{flag}_at"] = True, rec.approved_at
    props = properties(engine)
    tcs = testplan(engine)
    tags = rtl_tags(engine)
    sim = sim_results(engine)
    mut = mutation_results(engine)
    sign = signoffs(engine)
    rows = []
    for r in requirements(engine):
        rid = r["req_id"]
        mine = [p for p in props if rid in p["req_ids"] and p["kind"] == "assert"]
        confirmed = [p for p in mine if p["status"] == "confirmed"]
        tc_ids = [t["tc_id"] for t in tcs if rid in (t.get("req_ids") or [])]
        row = {"req_id": rid, "text": r.get("text", ""), "category": r.get("category", "functional")}
        if r.get("feature_id"):
            row["feature_id"] = r["feature_id"]
        where = tags.get(rid, [])
        row.update(rtl_traced=bool(where), rtl_files=sorted({f for f, _ in where}), rtl_lines=[n for _, n in where],
                   sva_traced=bool(confirmed), sva_assertions=[p["id"] for p in confirmed],
                   sva_files=sorted({p["file"] for p in confirmed}), tc_traced=bool(tc_ids), tc_ids=tc_ids)
        results = (sim or {}).get("tests", {})
        tc_res = {t: results[t]["status"] for t in tc_ids if t in results}
        labels = {p["label"] for p in props if rid in p["req_ids"]}
        viol = sum(1 for t in tc_ids for v in (results.get(t, {}).get("sva_violations") or []) if v in labels)
        row.update(sim_tc_results=tc_res, sva_violation_count=viol,
                   sim_pass=bool(tc_ids) and len(tc_res) == len(tc_ids) and all(s == "pass" for s in tc_res.values()) and viol == 0)
        score, detail, note = "pending", {"total": 0, "killed": 0, "survived": 0, "compile_error": 0}, ""
        if mut is not None:
            per = (mut.get("per_req") or {}).get(rid)
            if per:
                detail = {k: int(per.get(k, 0)) for k in detail}
                ran = detail["total"] - detail["compile_error"]
                score = round(detail["killed"] / ran, 4) if ran > 0 else "N/A"
            elif mut.get("ran"):
                score = "N/A"
                note = "no mutant could be made on this requirement's RTL"
            else:
                score, note = "N/A", mut.get("reason", "mutation testing was not run")
        row.update(mutation_score=score, mutation_detail=detail)
        if note:
            row["mutation_note"] = note
        reasons = lock_reasons(row)
        s = sign.get(rid, {})
        signed = bool(s.get("signed")) and not reasons
        row.update(signed_off=signed, sign_off_note=s.get("note") if s.get("signed") else None,
                   sign_off_at=s.get("at") if signed else None,
                   status="signed_off" if signed else ("locked" if reasons else "pending"), locked_reasons=reasons)
        if s.get("signed") and reasons:
            row["sign_off_note"] = f"sign-off withdrawn — {'; '.join(reasons)} (was: {s.get('note') or 'no note'})"
        rows.append(row)
    return {
        "metadata": {"ip_name": meta_in.get("ip_name", ""), "spec_revision": str(meta_in.get("spec_revision", "")),
                     "gate_3_approved": bool(old.get("gate_3_approved", False)), "gate_3_at": old.get("gate_3_at"),
                     "gate_5_approved": bool(old.get("gate_5_approved", False)), "gate_5_at": old.get("gate_5_at"),
                     "last_updated": artifacts.now()},
        "requirements": rows,
        "gap_report": gap_report(rows),
    }


def lock_reasons(row: dict) -> list[str]:
    """Why a REQ cannot be signed off yet (conditions 1–5)."""
    out = []
    if not row["rtl_traced"]:
        out.append("no RTL block is tagged with it (1)")
    if not row["sva_traced"]:
        out.append("no assertion you confirmed at Gate 3b (2)")
    if not row["tc_traced"]:
        out.append("no selected test case covers it (3)")
    if row["tc_traced"] and not row["sim_pass"]:
        out.append("its simulation does not pass (4)" if row["sim_tc_results"] else "it has not been simulated (4)")
    s = row["mutation_score"]
    if s == "pending":
        out.append("mutation testing has not run (5)")
    elif isinstance(s, (int, float)) and s < THRESHOLD:
        out.append(f"mutation score {s:.2f} < {THRESHOLD} (5)")
    return out


def gap_report(rows: list[dict]) -> dict:
    return {
        "req_ids_no_rtl": [r["req_id"] for r in rows if not r["rtl_traced"]],
        "req_ids_no_sva": [r["req_id"] for r in rows if not r["sva_traced"]],
        "req_ids_no_tc": [r["req_id"] for r in rows if not r["tc_traced"]],
        "req_ids_sim_fail": [r["req_id"] for r in rows if r["tc_traced"] and not r["sim_pass"]],
        "req_ids_low_mut": [r["req_id"] for r in rows if isinstance(r["mutation_score"], (int, float)) and r["mutation_score"] < THRESHOLD],
        "req_ids_unsigned": [r["req_id"] for r in rows if not r["signed_off"]],
    }


def rtm_summary(rows: list[dict]) -> dict:
    total = len(rows)
    signed = sum(r["signed_off"] for r in rows)
    pending = sum(r["status"] == "pending" for r in rows)
    gr = gap_report(rows)
    return {"total_req_ids": total, "signed_off": signed, "pending": pending, "locked": total - signed - pending,
            "sign_off_pct": round(100 * signed / total, 1) if total else 0.0,
            "gap_report": {"no_rtl": gr["req_ids_no_rtl"], "no_sva": gr["req_ids_no_sva"], "no_tc": gr["req_ids_no_tc"],
                           "sim_fail": gr["req_ids_sim_fail"], "low_mut": gr["req_ids_low_mut"]}}


def render_dashboard(rtm: dict) -> str:
    rows = rtm["requirements"]
    s = rtm_summary(rows)
    lines = [f"# RTM dashboard — {rtm['metadata'].get('ip_name', '')}", "",
             f"{s['signed_off']}/{s['total_req_ids']} signed off · {s['pending']} ready to sign · {s['locked']} locked", "",
             "| REQ | status | RTL | SVA | TC | sim | mutation | why locked |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ms = r["mutation_score"]
        ms = f"{ms:.2f}" if isinstance(ms, float) else str(ms)
        lines.append(f"| {r['req_id']} | {r['status']} | {'✔' if r['rtl_traced'] else '·'} | {'✔' if r['sva_traced'] else '·'} | "
                     f"{'✔' if r['tc_traced'] else '·'} | {'✔' if r['sim_pass'] else '·'} | {ms} | {'; '.join(r['locked_reasons'])} |")
    return "\n".join(lines) + "\n"


def write_rtm(engine) -> dict:
    """Rebuild and write schemas/rtm.json (+ the dashboard, + the report's rtm_summary when a report exists); the recorded
    hashes of the steps that list these files are refreshed (writing them is not a user edit)."""
    rtm = build_rtm(engine)
    artifacts.write(engine.project, "rtm.json", rtm)
    lay = layout(engine)
    (lay.schemas / "RTM.md").write_text(render_dashboard(rtm))
    rep = artifacts.read(engine.project, "verification_report.json")
    if isinstance(rep, dict):
        rep["rtm_summary"] = rtm_summary(rtm["requirements"])
        write_json(artifacts.path(engine.project, "verification_report.json"), rep)
    rehash(engine, [artifacts.path(engine.project, "rtm.json"), lay.schemas / "RTM.md",
                    artifacts.path(engine.project, "verification_report.json")])
    return rtm


def rehash(engine, paths: list[Path]) -> None:
    """Files the VLSIT steps rewrite outside their run (reviews, sign-offs): their recorded hashes follow."""
    from q3tui.core.project import sha256_file

    changed = False
    for step in engine.steps:
        if step.kind not in ("vlsit_sva", "vlsit_verify"):
            continue
        rec = engine.state.steps.get(step.name)
        if rec is None:
            continue
        for p in paths:
            rel = engine.project.rel(p)
            if rel in rec.outputs and p.is_file():
                rec.outputs[rel] = sha256_file(p)
                changed = True
    if changed:
        engine.save()
