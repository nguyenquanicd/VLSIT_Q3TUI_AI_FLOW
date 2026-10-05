"""Role -> adapter registry."""

from __future__ import annotations

from q3tui.core.config import Config
from q3tui.eda.base import ROLES, ToolAdapter, ToolEnv, ToolUnavailable
from q3tui.eda.slang import SlangSyntax
from q3tui.eda.synopsys import VcsSimulator


def tool_env(cfg: Config, role: str) -> ToolEnv:
    binding = getattr(cfg.tools, role)
    modules = tuple(dict.fromkeys([*cfg.tools.modules, *binding.modules]))
    return ToolEnv(cfg.tools.setup_script, modules, cfg.tools.modules_init)


def get_adapter(role: str, cfg: Config) -> ToolAdapter:
    if role not in ROLES:
        raise ValueError(f"unknown tool role '{role}' (expected one of {', '.join(ROLES)})")
    binding = getattr(cfg.tools, role)
    key = (role, binding.adapter)
    if key == ("syntax", "slang"):
        return SlangSyntax()
    if key == ("simulator", "vcs"):
        return VcsSimulator(binding, cfg.tools.timeout_s, tool_env(cfg, role))
    raise ToolUnavailable(f"no adapter '{binding.adapter}' for role '{role}' yet (see docs/spec/README.md roadmap)")
