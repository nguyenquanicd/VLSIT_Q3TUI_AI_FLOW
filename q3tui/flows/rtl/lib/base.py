"""Shared pieces of the RTL flow's own steps (rules, format, style_lint, smoke): where the RTL and the style / smoke folders are,
the tool environment, the rule text. Questions / answers come from the VLSIT step base (one file per step in schemas/questions/)."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import read_json, write_json
from q3tui.flows.rtl.lib import csr, rules, verible
from q3tui.steps.vlsit.base import VlsitStep, rules_text


class RtlFlowStep(VlsitStep):
    """Base of the RTL flow's steps. The RTL is the `rtl` step's result (`rtl/`: its `options.rtl_dir`); the style files go to
    `<rtl>/style_lint/`, the smoke test to `<rtl>/smoke/`."""

    def rtl_dir(self, engine) -> Path:
        return engine.by_name["rtl"].lay(engine).rtl

    def rtl_files(self, engine) -> list[Path]:
        """The RTL this flow writes (the files the style checks apply to): not the modules the `csr` step provides (generated code)."""
        own = csr.read_provided(self.rtl_dir(engine))
        return [f for f in verible.rtl_files(self.rtl_dir(engine)) if f.stem not in own]

    def design_files(self, engine) -> list[Path]:
        """The whole design in compile order (the `rtl` step's filelist: the provided modules included); every RTL file when there is none yet."""
        rtl = self.rtl_dir(engine)
        fl = rtl / "filelist.f"
        root = engine.project.root
        if fl.is_file():
            files = [root / ln.strip() for ln in fl.read_text().splitlines() if ln.strip() and not ln.strip().startswith(("//", "+", "-"))]
            files = [f for f in files if f.is_file()]
            if files:
                return files
        return verible.rtl_files(rtl)

    def style_dir(self, engine) -> Path:
        return self.rtl_dir(engine) / "style_lint"

    def smoke_dir(self, engine) -> Path:
        return self.rtl_dir(engine) / "smoke"

    def tools(self, engine):
        from q3tui.eda import tools as toolmod

        return toolmod.load_tools(engine.project.cfg, engine.project.root, engine.flow)

    def workdir(self, engine) -> Path:
        d = engine.project.state_dir / "rtl_flow" / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def tool_problem(self, engine, *roles: str) -> str | None:
        """Why a tool role cannot run (not configured, binary not found), None when it can. A step waits with this message
        (`missing_input`): a check whose tool is missing is not passed."""
        from q3tui.eda.base import ToolUnavailable
        from q3tui.eda.tools import available, binary_of

        try:
            ts = self.tools(engine)
        except ToolUnavailable as exc:
            return str(exc)
        for role in roles:
            if role not in ts.roles:
                return (f"no tool for role '{role}': add roles.{role} to tools.json or tools.roles "
                        f"(the flow's default for it is flows/rtl/tools.json)")
            if not available(ts, role):
                return f"the tool '{binary_of(ts.roles[role])}' (role '{role}') was not found (check tools.json, modules / setup_script)"
        return None

    def rule_files(self, engine) -> list[str]:
        """The rule stack (first wins): your `rules/rtl_rules.md` when it exists, then the `rtl` step's own rule files."""
        return rules.stack(engine.project.root, list(engine.by_name["rtl"].options.get("rules") or []))

    def rule_text(self, engine) -> str:
        return rules_text(self.rule_files(engine), engine.project.root)

    def rule_paths(self, engine) -> list[Path]:
        """The rule files on disk (for staleness): the project's, else the shipped one."""
        from q3tui.steps.vlsit.base import rule_dirs

        out = []
        for n in self.rule_files(engine):
            for cand in [engine.project.root / n, *[d / n for d in rule_dirs()]]:
                if cand.is_file():
                    out.append(cand)
                    break
        return out

    def read_json(self, path: Path, default=None):
        return read_json(path, default)

    def write_json(self, path: Path, data) -> Path:
        return write_json(path, data)
