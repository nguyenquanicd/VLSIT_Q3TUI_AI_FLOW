"""In-process MCP tools that expose the parsed design to LLM stages (mcp__q3tui__*)."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from q3tui.hdl.parse import ParsedDesign, summarize

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def design_tools(design: ParsedDesign) -> list[SdkMcpTool]:
    from claude_agent_sdk import tool

    @tool(
        "design_summary",
        "Deterministic summary of the elaborated design (pyslang): hierarchy, every module's "
        "ports/parameters, inferred clocks/resets, and parse errors. Ground truth — prefer it over guessing.",
        {},
    )
    async def design_summary(args: dict) -> dict:
        return _text(summarize(design))

    @tool(
        "module_info",
        "Parsed facts for one module: source file/line, ports (direction, type), parameters, "
        "child instances, inferred clocks/resets, always-block counts.",
        {"module": str},
    )
    async def module_info(args: dict) -> dict:
        name = args.get("module", "")
        info = design.modules.get(name)
        if info is None:
            return _text(f"unknown module '{name}'. Known: {', '.join(sorted(design.modules))}")
        return _text(json.dumps(asdict(info), indent=1))

    @tool(
        "parse_diagnostics",
        "Compiler diagnostics (errors and warnings) from parsing the design, with file:line.",
        {},
    )
    async def parse_diagnostics(args: dict) -> dict:
        if not design.diagnostics:
            return _text("no diagnostics")
        return _text("\n".join(f"{d.severity} {d.code} {d.file}:{d.line}: {d.message}" for d in design.diagnostics))

    return [design_summary, module_info, parse_diagnostics]
