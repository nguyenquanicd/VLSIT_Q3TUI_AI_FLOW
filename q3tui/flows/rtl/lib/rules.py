"""The rule stack of the RTL flow: your `rules/rtl_rules.md` in front of Q3TUI's default rules (docs/spec/rtl-flow.md "Rules").

Both are markdown. The first file wins where rules conflict (the generator's prompt says so), so yours override the default ones.
The same stack goes to the RTL generator and to the style checker."""

from __future__ import annotations

from pathlib import Path

USER_RULES = "rules/rtl_rules.md"  # in the project; the project path wins over a shipped file of the same name


def user_file(project_root: Path) -> Path:
    return project_root / USER_RULES


def stack(project_root: Path | None, defaults: list[str]) -> list[str]:
    """Rule file names, first wins: the project's `rules/rtl_rules.md` (when it exists), then `defaults` (`options.rules`)."""
    mine = [USER_RULES] if project_root is not None and user_file(project_root).is_file() else []
    return [*mine, *[d for d in defaults if d not in mine]]
