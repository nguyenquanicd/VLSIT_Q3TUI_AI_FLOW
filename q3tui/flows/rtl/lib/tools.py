"""In-process MCP tools (`mcp__q3tui__*`) the RTL flow's LLM stages work with. They run the configured tool roles (never a binary by
name), cap nothing themselves (the runtime caps every tool's output) and answer in plain text the stage can reason about.

Verible: `verible_help`, `verible_check_flags`, `verible_lint`. Smoke test: `smoke_compile`, `smoke_run` (flows/rtl/smoke/)."""

from __future__ import annotations

from pathlib import Path

import anyio

from q3tui.flows.rtl.lib import verible

_STR_LIST = {"type": "array", "items": {"type": "string"}}


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def verible_tools(ts, work: Path, project_root: Path, rtl_files, flags_file: Path | None = None) -> list:
    """`rtl_files`: a callable giving the RTL files now (they change as fixes land). `flags_file`: the flags `verible_lint` uses by default."""
    from claude_agent_sdk import tool

    @tool("verible_help", "Verible's own documentation of its lint rules: rule='all' lists them, a rule name explains it and its parameters "
                          "(the exact syntax of a rule's configuration). Rule names and parameters differ between Verible versions: ask, do not remember.",
          {"type": "object", "properties": {"rule": {"type": "string", "description": "'all' or a rule name"}}})
    async def help_tool(args: dict) -> dict:
        rule = (args.get("rule") or "all").strip()
        res = await anyio.to_thread.run_sync(lambda: verible.run_lint(ts, work, [], [f"--help_rules={rule}"]))
        return _text(verible.log_text(res, 12000) or res.summary)

    @tool("verible_check_flags", "Check lint flags without any design: runs Verible with them on a small sample and says whether it accepted them "
                                 "(an unknown rule or a bad parameter is rejected with Verible's message). Findings on the sample mean nothing.",
          {"type": "object", "properties": {"flags": _STR_LIST}, "required": ["flags"]})
    async def check_tool(args: dict) -> dict:
        flags = [str(f) for f in args.get("flags") or []]
        problems = verible.validate_flags(flags)
        if problems:
            return _text("not accepted by Q3TUI's format check:\n" + "\n".join(f"- {p}" for p in problems))
        sample = work / "sample.sv"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(verible.SAMPLE)
        res = await anyio.to_thread.run_sync(lambda: verible.run_lint(ts, work, [sample], flags))
        msg = verible.problem(res, "verible_lint")
        if msg:
            return _text("Verible did not accept the flags:\n" + msg)
        return _text("accepted: Verible ran with these flags (" + str(len(verible.findings(res.diagnostics, project_root))) + " finding(s) on the sample, ignore them)")

    @tool("verible_lint", "Run Verible lint on the RTL (every file, or `files`) with `flags` (empty: the flags file of this step). Returns the findings "
                          "with the rule, the message and the source line, and the flags used.",
          {"type": "object", "properties": {"flags": _STR_LIST, "files": {**_STR_LIST, "description": "RTL file names; empty: all"}}})
    async def lint_tool(args: dict) -> dict:
        flags = [str(f) for f in args.get("flags") or []] or (verible.read_flags(flags_file) if flags_file else [])
        names = {Path(str(f)).name for f in args.get("files") or []}
        files = [f for f in rtl_files() if not names or f.name in names]
        if not files:
            return _text("no RTL file to lint")
        res = await anyio.to_thread.run_sync(lambda: verible.run_lint(ts, work, files, flags))
        msg = verible.problem(res, "verible_lint")
        if msg:
            return _text(msg)
        out = verible.show_findings(verible.findings(res.diagnostics, project_root))
        errs = verible.syntax_errors(res.diagnostics)
        return _text(("SYNTAX ERRORS:\n" + "\n".join(errs) + "\n" if errs else "") + out + "\nflags used: " + " ".join(flags))

    return [help_tool, check_tool, lint_tool]


def smoke_tools(ts, project_root: Path, work: Path, rtl_files, tb: Path) -> list:
    """`smoke_compile` / `smoke_run`: compile the testbench with the RTL and run it, as the flow will. The writer of the testbench never reads
    the RTL; these tools are how it learns whether its testbench fits the design (a compile error names what differs)."""
    from claude_agent_sdk import tool

    from q3tui.flows.rtl.lib import smoke

    @tool("smoke_compile", "Compile your testbench (rtl/smoke/tb_smoke.sv) together with the design. Returns the compile errors (file:line message), or 'compiled'.", {})
    async def compile_tool(args: dict) -> dict:
        if not tb.is_file():
            return _text(f"{tb.name} does not exist yet: write it first")
        res = await anyio.to_thread.run_sync(lambda: smoke.compile_tb(ts, project_root, work, rtl_files(), tb))
        errs = smoke.compile_errors(res)
        return _text("compiled" if res.ok and not errs else "compile errors:\n" + "\n".join(errs[:40]))

    @tool("smoke_run", "Compile (if needed) and run your testbench. Returns the SMOKE_RESULT lines and the verdict of the run.", {})
    async def run_tool(args: dict) -> dict:
        if not tb.is_file():
            return _text(f"{tb.name} does not exist yet: write it first")
        res = await anyio.to_thread.run_sync(lambda: smoke.compile_tb(ts, project_root, work, rtl_files(), tb))
        errs = smoke.compile_errors(res)
        if errs or not res.ok:
            return _text("compile errors:\n" + "\n".join(errs[:40]))
        out = await anyio.to_thread.run_sync(lambda: smoke.run_tb(ts, project_root, work))
        return _text(smoke.show_outcome(out))

    return [compile_tool, run_tool]


def csr_tools(ts, tool: Path | None, work: Path) -> list:
    """`csr_check`: validate a register map and, when the generator is installed, really generate it in a scratch folder and report the ports of the
    module it makes (the exact names the integrator will connect to)."""
    import shutil

    from claude_agent_sdk import tool as sdk_tool

    from q3tui.flows.rtl.lib import csr
    from q3tui.flows.rtl.lib.models import CsrMap

    @sdk_tool("csr_check", "Check a register map (the structure of the answer you will give): the problems of the map (overlapping fields, duplicate or misaligned "
                           "offsets, …) or, when it is sound, the ports of the module the APB-CSR-Generator makes from it. Fix every problem before you answer.",
              {"type": "object", "properties": {"map": {"type": "object", "description": "module_name, addr_width, asynchronous, registers[{name, offset, fields[{name, msb, lsb, type, reset}]}]"}},
               "required": ["map"]})
    async def check_tool(args: dict) -> dict:
        try:
            m = CsrMap.model_validate(args.get("map") or {})
        except Exception as exc:  # noqa: BLE001 - what the LLM got wrong in the shape
            return _text("the map is not in the expected structure:\n" + str(exc)[:1500])
        csr.complete(m)
        problems = csr.validate(m)
        if problems:
            return _text("problems:\n" + "\n".join(f"- {p}" for p in problems))
        if tool is None:
            return _text("the map is valid (the generator is not installed here: its ports could not be listed)")
        scratch = work / "check"
        shutil.rmtree(scratch, ignore_errors=True)
        wb = csr.write_workbook(m, scratch / "in.xlsx")
        res = await anyio.to_thread.run_sync(lambda: csr.run_tool(ts, tool, wb, scratch))
        sv = scratch / "RTL" / f"{m.module_name}.sv"
        if not sv.is_file():
            return _text("the generator did not produce the module:\n" + (Path(res.log_path).read_text(errors="replace")[-1500:] if res.log_path else res.summary))
        return _text("the map is valid and generates:\n" + csr.port_summary(sv.read_text(), m.module_name))

    return [check_tool]
