"""ASIC step `asic_port`: what the RTL has that an ASIC flow cannot take (`schemas/asic_port.json`).

Deterministic first: the code scan (`lib/port.py`) is ground truth and runs always. Then the porter (one read-only LLM stage,
`lib/porter.py`, the rule file `rules/asic_port_rules.md`) does the porting work the scan cannot: memories for SRAM wrappers,
synchronizers for library cells, clock gates, … — each a work order naming the module, the wrapper and its ports. The RTL is the
VLSIT flow's, so the orders go there as change requests (`asic_handoff`) and its rtl step writes them. A change propagates as an update: only the files whose text changed are
ported again (the others keep their orders), and nothing is asked when no file, rule or option changed. The findings (rule, module, line, fix) are the review at the gate; `asic_handoff` (the panel's `h`, `q3tui act asic_handoff`, the
assistant) sends them to the flow that owns the RTL as scoped change requests (`[rtl:<module>] …`). The synthesis does not wait for
a clean result: a finding that is fine for you is waived (`options.waive`) or simply approved."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections
from q3tui.flows.asic.lib import port, porter
from q3tui.flows.asic.lib.base import PORT_JSON, AsicStep
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["SYSTEM"]


class Step(AsicStep):
    name = "asic_port"
    title = "ASIC port check"
    deps = ()
    hashed_options = ("min_mem_words", "waive", "rules", "port")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        return [self.filelist(engine), *self.rtl_files(engine)]

    def outputs(self, engine) -> list[Path]:
        p = self.artifact(engine, PORT_JSON)
        return [p] if p.is_file() else []

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        if not self.filelist(engine).is_file():
            return f"the RTL is needed first ({engine.project.rel(self.filelist(engine))}; run the vlsit flow's rtl step)"
        return None

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        files = self.rtl_files(engine)
        waive = tuple(str(x) for x in self.options.get("waive") or [])
        min_words = int(self.options.get("min_mem_words") or 16)
        found = port.scan(files, project.root, min_words, waive)
        prev = self.read(engine, PORT_JSON, {}) or {}
        notes: list[str] = []
        planned: dict[str, str] = {}
        key = ""
        orders: dict[tuple[str, int], dict] = {}
        if self.options.get("port", True):
            rules = porter.resolve_rules([str(n) for n in self.options.get("rules") or []], project.root, Path(__file__).parent)
            key = porter.rules_key(rules, min_words, waive)
            rel = {f: (str(f.relative_to(project.root)) if f.is_relative_to(project.root) else str(f)) for f in files}
            hashes = porter.file_hashes(files, project.root)
            old = prev.get("planned", {}) if prev.get("rules_key") == key else {}  # (other rules or options: everything again)
            todo = [f for f in files if old.get(rel[f]) != hashes[rel[f]]]
            same = {k for k, v in hashes.items() if old.get(k) == v}
            extra = [f for f in prev.get("findings", []) if f.get("source") == "llm" and f["file"] in same]
            orders = {(f["file"], f["line"]): f["order"] for f in prev.get("findings", [])
                      if f.get("source") == "code" and f.get("order") and f["file"] in same}  # (the porter's orders for scan findings)
            if todo:
                stage = Stage(name="asic_port", system_prompt=SYSTEM, cwd=project.root, builtin_tools=["Read", "Grep", "Glob"],
                              prompt=porter.prompt(rules, [f.stem for f in files], [rel[f] for f in todo],
                                                   [f for f in found if f["file"] in {rel[t] for t in todo}], min_words, waive),
                              output_model=porter.PortPlan,
                              deny_dirs=[project.model_dir])
                out: porter.PortPlan = (await ctx.llm(stage)).output
                known = porter.port_rules("\n".join(t for _, t in rules))
                by_module = {f.stem: f for f in todo}
                scanned = {(f["file"], f["line"]) for f in found}
                for r in out.changes:
                    at = (rel[by_module[r.module]], r.line) if r.module in by_module else None
                    if at in scanned:  # an order for a scan finding: it becomes that finding's port
                        orders[at] = {"rule": r.rule, "wrapper": r.wrapper, "fix": r.fix}
                    elif r.rule not in known:
                        notes.append(f"{r.rule}: not a port rule")
                    elif r.rule in waive:
                        notes.append(f"{r.rule}: waived")
                    elif r.module not in by_module:
                        notes.append(f"{r.rule}: unknown module '{r.module}'")
                    else:
                        extra.append({"rule": r.rule, "severity": known[r.rule]["severity"], "title": known[r.rule]["title"],
                                      "module": r.module, "file": rel[by_module[r.module]], "line": r.line, "text": r.text[:160],
                                      "wrapper": r.wrapper, "fix": r.fix, "source": "llm", "order": {"rule": r.rule, "wrapper": r.wrapper, "fix": r.fix}})
                same = set(hashes)
            planned = {k: v for k, v in hashes.items() if k in same}
        else:
            extra, orders = [], {}
        for f in found:  # (the porter's order replaces the scan's generic fix: the wrapper, its ports, where it goes)
            o = orders.get((f["file"], f["line"]))
            if o:
                f.update(order=o, scan_fix=f["fix"], fix=o["fix"], wrapper=o["wrapper"])
        found = sorted([*({**f, "source": "code"} for f in found), *extra], key=lambda f: (f["file"], f["line"]))
        found = [{**f, "id": f"P{i}"} for i, f in enumerate(found, 1)]
        sent = {(f["rule"], f["module"], f["text"]): f["handed_off"] for f in prev.get("findings", []) if f.get("handed_off")}
        for f in found:  # (a finding handed off earlier and still there: the RTL was not changed for it yet, or not enough)
            f["handed_off"] = sent.get((f["rule"], f["module"], f["text"]))
        errors = sum(f["severity"] == "error" for f in found)
        verdict = "fail" if errors else ("warn" if found else "pass")
        self.write(engine, PORT_JSON, {"status": verdict, "files": len(files), "waived": list(waive), "errors": errors,
                                       "warnings": len(found) - errors, "findings": found, "rules_key": key, "planned": planned,
                                       "dropped": notes})
        ctx.emit("log", message=f"ASIC port: {errors} error(s), {len(found) - errors} warning(s) in {len(files)} file(s) → {verdict}"
                                + (f" ({sum(f['source'] == 'llm' for f in found)} from the porter)" if self.options.get("port", True) else "")
                                + (f"; dropped: {'; '.join(notes)}" if notes else "")
                                + ("; check them, then hand them off (asic_handoff): the vlsit flow ports the RTL" if found else ""))
