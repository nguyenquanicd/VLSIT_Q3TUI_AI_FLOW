"""ASIC step `asic_port`: what the RTL has that an ASIC flow cannot take (`schemas/asic_port.json`). Code only.

The findings (rule, module, line, fix) are the review at the gate; `asic_handoff` (the panel's `h`, `q3tui act asic_handoff`, the
assistant) sends them to the flow that owns the RTL as scoped change requests (`[rtl:<module>] …`). The synthesis does not wait for
a clean result: a finding that is fine for you is waived (`options.waive`) or simply approved."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows.asic.lib import port
from q3tui.flows.asic.lib.base import PORT_JSON, AsicStep
from q3tui.pipeline.base import StepContext


class Step(AsicStep):
    name = "asic_port"
    title = "ASIC port check"
    deps = ()
    hashed_options = ("min_mem_words", "waive")  # (staleness: these change the result)

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
        found = port.scan(files, project.root, int(self.options.get("min_mem_words") or 16), waive)
        prev = self.read(engine, PORT_JSON, {}) or {}
        sent = {(f["rule"], f["module"], f["text"]): f["handed_off"] for f in prev.get("findings", []) if f.get("handed_off")}
        for f in found:  # (a finding handed off earlier and still there: the RTL was not changed for it yet, or not enough)
            f["handed_off"] = sent.get((f["rule"], f["module"], f["text"]))
        errors = sum(f["severity"] == "error" for f in found)
        verdict = "fail" if errors else ("warn" if found else "pass")
        self.write(engine, PORT_JSON, {"status": verdict, "files": len(files), "waived": list(waive), "errors": errors,
                                       "warnings": len(found) - errors, "findings": found})
        ctx.emit("log", message=f"ASIC port: {errors} error(s), {len(found) - errors} warning(s) in {len(files)} file(s) → {verdict}"
                                + ("; review them, then hand them off (asic_handoff)" if found else ""))
