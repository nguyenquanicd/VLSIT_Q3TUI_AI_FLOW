"""Skills of the RTL flow: markdown playbooks an LLM stage reads when it needs them (docs/spec/rtl-flow.md "Skills").

A skill is a folder `<name>/SKILL.md` with a front matter (`name`, `description`) and the instructions. The stage's prompt lists the
skills with their one-line descriptions and paths; the LLM reads the ones its task calls for (progressive disclosure: the full text is
not in every prompt) and decides how to use them. Q3TUI does not script the steps of a skill: it gives the tools, confines the files
and checks the result by code.

Search order, the first folder holding a name wins: the project's `config/skills/`, the user's `~/.q3tui/skills/`, the flow folder's
`skills/` (a project flow copy), then the built-in ones. So a project replaces or adds a skill by dropping a folder in `skills/`."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

BUILTIN = Path(__file__).parents[1] / "skills"


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md


def search_dirs(config_dir: Path, flow_path: Path | None = None) -> list[Path]:
    home = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser()
    flow_dir = flow_path.parent / "skills" if flow_path and flow_path.name == "FLOW.md" else None
    out = [config_dir / "skills", home / "skills", *([flow_dir] if flow_dir else []), BUILTIN]
    return list(dict.fromkeys(d for d in out if d.is_dir()))


def _front(text: str) -> dict:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.S)
    if not m:
        return {}
    import yaml

    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def discover(config_dir: Path, flow_path: Path | None = None, names: list[str] | None = None) -> list[Skill]:
    """The skills found (the first folder wins per name), restricted to `names` when given, in `names` order."""
    found: dict[str, Skill] = {}
    for d in search_dirs(config_dir, flow_path):
        for f in sorted(d.glob("*/SKILL.md")):
            meta = _front(f.read_text())
            name = str(meta.get("name") or f.parent.name)
            found.setdefault(name, Skill(name, " ".join(str(meta.get("description") or "").split()), f))
    if names is None:
        return sorted(found.values(), key=lambda s: s.name)
    return [found[n] for n in names if n in found]


def index(skills: list[Skill], project_root: Path) -> str:
    """The prompt section naming the skills (the LLM reads the SKILL.md it needs)."""
    if not skills:
        return ""

    def show(p: Path) -> str:
        try:
            return str(p.relative_to(project_root))
        except ValueError:
            return str(p)

    lines = ["## Skills", "Playbooks you may read (Read the SKILL.md) when the task calls for them; follow your own judgment on how to use them. "
             "A skill gives know-how, not a fixed script."]
    lines += [f"- **{s.name}** — {s.description} (`{show(s.path)}`)" for s in skills]
    return "\n".join(lines)


def add_dirs(skills: list[Skill]) -> list[Path]:
    """Read-only folders to add to the stage so it can read the skills (those outside the project)."""
    return list(dict.fromkeys(s.path.parent.parent for s in skills))
