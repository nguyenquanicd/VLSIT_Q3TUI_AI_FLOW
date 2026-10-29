"""Add-on of the RTL flow (q3tui/addons.py): settings of its steps and the user actions around the rules and the formatter. Core exposes the
actions as assistant tools, `q3tui act …` and the TUI's `/act …`."""

from __future__ import annotations

from q3tui.flows.rtl.lib import rules, verible

RULES_SKELETON = """# My RTL rules

These rules are read before Q3TUI's default rules (`rtl_rule_default.md`): where they differ, **yours win**. A rule with the same id as a
default rule replaces it; a new id adds a rule. The same rules go to the RTL generator, to Verible (as flags, written by the `rules` step) and to the
style judge. Write one rule per bullet, `- **ID** text`, in plain words: say what, and where it matters (naming, layout, structure).

<!-- Examples (remove the comment marks to use them):
- **NAM-07** Every RTL name, prefix and suffix included, is at most 40 characters long.
- **FMT-01** Indent with 4 spaces, never tabs. A line is at most 120 characters long.
- **USR-01** Every module starts with the header template of the company (name, description, owner).
-->
"""


def register(api) -> None:
    api.setting("style_lint", "max_rounds", "Max style fix rounds", "int",
                "Rounds of rtl → format → style_lint (the judge hands violations back to rtl) before the step fails. Empty: pipeline.max_fix_iterations.",
                nullable=True)
    api.setting("smoke", "max_rounds", "Max smoke fix rounds", "int",
                "Rounds of smoke failure → triage → fix → format → style_lint → smoke before the step fails. Empty: pipeline.max_fix_iterations.",
                nullable=True)

    api.setting("rtl", "keep_generated", "Never edit generated RTL", "bool",
                "Off (default): lint findings in the generated CSR module are fixed in place (name and ports kept); the fix stays "
                "until the register map changes. On: they are only reported (the module stays as the APB-CSR-Generator made it).")
    api.setting("csr", "mode", "CSR generation", "choice", "auto: generate the registers with the APB-CSR-Generator when the spec has software-visible registers; on: always; off: never.",
                (("auto", "auto — when the spec has registers"), ("on", "on — always"), ("off", "off — never")))
    api.setting("csr", "tool_dir", "APB-CSR-Generator folder", "text", "Empty: $CSR_GEN_HOME, ~/.q3tui/tools/APB-CSR-Generator, <project>/tools/APB-CSR-Generator "
                "(q3tui act csr_tool_install clones it).", nullable=True)

    @api.action("csr_tool_install", "Clone the APB-CSR-Generator (https://github.com/nguyenquanicd/APB-CSR-Generator) into ~/.q3tui/tools/APB-CSR-Generator, where the csr step "
                "looks for it. It has no license file: Q3TUI does not ship it.")
    def csr_tool_install(ops) -> str:
        import os
        import subprocess
        from pathlib import Path

        from q3tui.flows.rtl.lib import csr
        from q3tui.pipeline.engine import EngineError

        from q3tui.eda import tools as toolmod

        role = toolmod.load_tools(ops.project.cfg, ops.project.root).roles.get("csr_gen")
        found = csr.find_tool(ops.project.root, None, (role.env or {}).get("CSR_GEN_HOME") if role else None)
        if found:
            return ops._log("csr", f"the APB-CSR-Generator is already at {found}")
        dest = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser() / "tools" / "APB-CSR-Generator"
        if dest.exists():
            raise EngineError(f"{dest} exists but is not the generator (no {', '.join(csr.missing(dest))}): remove it or set options.tool_dir")
        dest.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["git", "clone", "--depth", "1", csr.REPO, str(dest)], capture_output=True, text=True, timeout=300)
        if res.returncode:
            raise EngineError("git clone failed: " + (res.stderr or res.stdout).strip()[-400:])
        return ops._log("csr", f"the APB-CSR-Generator is installed at {dest}")

    @api.action("csr_show", "Show the register map the csr step drafted (src/rtl/csr/csr_map.json: registers, offsets, fields) and the ports of the module the APB-CSR-Generator made from it.")
    def csr_show(ops) -> str:
        from q3tui.core.project import read_json

        eng = ops.engine
        step = next((s for s in eng.steps if s.name == "csr"), None)
        if step is None:
            return "this flow has no csr step"
        f = step._files(eng)
        m = read_json(f["map"], None)
        if not m:
            return "no register map yet: run the csr step"
        if not m.get("has_registers", True):
            return "the spec has no software-visible registers: nothing generated"
        lines = [f"{m['module_name']} (parent {m.get('parent') or 'top'}): addr {m['addr_width']} bits, {'asynchronous' if m.get('asynchronous') else 'single clock'}"]
        for r in m.get("registers", []):
            lines.append(f"  0x{r['offset']:04X} {r['name']}: " + ", ".join(f"{fl['name']}[{fl['msb']}:{fl['lsb']}] {fl['type']}" for fl in r["fields"]))
        rep = read_json(f["report"], {}) or {}
        if rep.get("ports"):
            lines += ["", rep["ports"]]
        return "\n".join(lines)

    @api.action("rtl_rules_init", "Create config/rtl_rules.md, your own RTL rules (markdown, one `- **ID** text` rule per bullet), from a skeleton. They are read "
                "before Q3TUI's default rules and win where they differ; the rules step turns them into Verible flags and checks them for contradictions.")
    def rtl_rules_init(ops) -> str:
        from q3tui.pipeline.engine import EngineError

        f = rules.user_file(ops.project)
        if f.exists():
            raise EngineError(f"{ops.project.rel(f)} exists already")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(RULES_SKELETON)
        return ops._log("rules", f"{ops.project.rel(f)} created: write your rules in it, then run (the rules step reads it)")

    @api.action("style_show", "Show the rule stack in use, the Verible flags written from it and the result of the last style check and smoke test.")
    def style_show(ops) -> str:
        from q3tui.core.project import read_json

        eng = ops.engine
        style = next((s for s in eng.steps if s.name == "style_lint"), None)
        if style is None:
            return "this flow has no style_lint step"
        d = style.style_dir(eng)
        lines = ["rules (first wins): " + ", ".join(style.rule_files(eng)), ""]
        for name in (verible.LINT_FLAGS, verible.FORMAT_FLAGS):
            lines.append(f"{name}: " + (" ".join(verible.read_flags(d / name)) or "(none yet: run the rules step)"))
        rep = read_json(d / "style_report.json", {}) or {}
        if rep:
            lines += ["", f"style check: {rep.get('status')} — {rep.get('findings')} finding(s), {rep.get('waived_findings')} accepted, round {rep.get('rounds')}"]
        smoke = read_json(style.smoke_dir(eng) / "smoke_report.json", {}) or {}
        if smoke:
            lines.append(f"smoke test: {smoke.get('status')} ({len((smoke.get('run') or {}).get('results') or {})} behaviour(s), tools {smoke.get('tool')})")
        return "\n".join(lines)

    @api.action("format_now", "Format the RTL with Verible now (whitespace only; a backup is kept; a file you edited is formatted too, since you ask, and stays "
                "marked as yours).", {"files": "RTL file names separated by commas; empty: all"}, optional=("files",))
    def format_now(ops, files: str = "") -> str:
        from q3tui.pipeline.engine import EngineError

        eng = ops.engine
        step = next((s for s in eng.steps if s.name == "format"), None)
        if step is None:
            raise EngineError("this flow has no format step")
        problem = step.tool_problem(eng, "verible_format")
        if problem:
            raise EngineError(problem)
        every = step.rtl_files(eng)
        wanted = {n.strip() for n in files.split(",") if n.strip()}
        targets = [f for f in every if not wanted or f.name in wanted or f.stem in wanted]
        if not targets:
            raise EngineError("no RTL file to format" + (f" named {files}" if wanted else ""))
        changed, rejected, syntax = step.apply(eng, targets, step.hand_edited(eng, every))
        return ops._log("format", f"format: {len(changed)} file(s) formatted" + (f"; code would change in {', '.join(rejected)} (left as it was)" if rejected else "")
                        + (f"; syntax errors in {', '.join(syntax)}" if syntax else ""))
