"""The rule stack of the RTL flow: your `config/rtl_rules.md` in front of Q3TUI's default rules (docs/spec/rtl-flow.md "Rules").

Both are markdown. The first file wins where rules conflict (the generator's prompt says so), so yours override the default ones.
The same stack goes to the RTL generator and to the style checker."""

from __future__ import annotations

from pathlib import Path  # noqa: F401

USER_RULES = "rtl_rules.md"  # in the project's config folder; a project path wins over a shipped file of the same name


def user_file(project) -> Path:
    return project.config_dir / USER_RULES


def stack(project, defaults: list[str]) -> list[str]:
    """Rule file names, first wins: the project's `config/rtl_rules.md` (when it exists; as a project-relative path), then
    `defaults` (`options.rules`)."""
    mine = [project.rel(user_file(project))] if project is not None and user_file(project).is_file() else []
    return [*mine, *[d for d in defaults if d not in mine]]
