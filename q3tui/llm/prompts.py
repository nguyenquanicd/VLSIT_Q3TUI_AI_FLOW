"""Customisable prompts (docs/spec/flows.md "Prompts").

Every LLM stage goes through `StepContext.llm`, which calls `apply` here: a markdown file named after the **step id** (all its
stages) or the exact **stage name** (`rtl_fifo`) in `<project>/prompts/` or `~/.q3tui/prompts/` changes the stage's system
prompt — appended to it (default) or replacing it (`mode: replace` in the front matter). Project files win over the user's.
The built-in prompt of each stage is recorded in `.q3tui/prompts/<step>.md` when it runs, so it can be read before editing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

MODES = ("append", "replace")


def dirs(root: Path, flow_dir: Path | None = None) -> list[Path]:
    """Project prompts/, the flow folder's prompts/ (a folder flow), then the user's."""
    home = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser()
    return [root / "prompts", *([flow_dir / "prompts"] if flow_dir else []), home / "prompts"]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def find(root: Path, target: str, flow_dir: Path | None = None) -> Path | None:
    for d in dirs(root, flow_dir):
        p = d / f"{_safe(target)}.md"
        if p.is_file():
            return p
    return None


def read(root: Path, target: str, flow_dir: Path | None = None) -> tuple[str, str]:
    """(mode, text) of a target's override ("append", "" when there is none)."""
    p = find(root, target, flow_dir)
    if p is None:
        return "append", ""
    text = p.read_text()
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.S)
    mode = "append"
    if m:
        mm = re.search(r"^mode:\s*(\w+)", m.group(1), re.M)
        mode = mm.group(1) if mm and mm.group(1) in MODES else "append"
        text = text[m.end():]
    return mode, text.strip()


def write(root: Path, target: str, text: str, mode: str = "append") -> Path | None:
    """Save (or, with empty text, delete) the project's override of a target."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    path = root / "prompts" / f"{_safe(target)}.md"
    if not text.strip():
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nmode: {mode}\n---\n{text.strip()}\n")
    return path


def skill_overrides(stage, step: str, flow_dir: Path | None, builtin_dir: Path | None) -> None:
    """A flow folder that is a copy of the built-in one: a section of its `<step>/md/skill.md` that differs from the built-in text
    replaces that text in the stage's system prompt (the step code reads the built-in skill; the copy customises it)."""
    from q3tui.flows import skill_sections

    if not flow_dir or not builtin_dir or flow_dir.resolve() == builtin_dir.resolve():
        return
    mine, base = (skill_sections(d / step / "md" / "skill.md") for d in (flow_dir, builtin_dir))
    for name, text in mine.items():
        if name in base and base[name] != text and base[name] in stage.system_prompt:
            stage.system_prompt = stage.system_prompt.replace(base[name], text)


def apply(stage, root: Path, step: str, state_dir: Path, flow_dir: Path | None = None, builtin_dir: Path | None = None) -> None:
    """Record the stage's built-in system prompt, then change it by the step's and the stage's override files."""
    record(state_dir, step, stage.name, stage.system_prompt)
    skill_overrides(stage, step, flow_dir, builtin_dir)
    for target in dict.fromkeys((step, stage.name)):
        mode, text = read(root, target, flow_dir)
        if text:
            stage.system_prompt = text if mode == "replace" else f"{stage.system_prompt}\n\n## Your instructions ({target})\n\n{text}"


def record(state_dir: Path, step: str, stage: str, prompt: str) -> None:
    p = state_dir / "prompts" / f"{_safe(step)}.md"
    try:
        old = p.read_text() if p.is_file() else ""
    except OSError:
        return
    sections = {m.group(1): m.group(2) for m in re.finditer(r"^## (\S+)\n\n(.*?)(?=^## \S+\n\n|\Z)", old, re.S | re.M)}
    if sections.get(stage, "").strip() == prompt.strip():
        return
    sections[stage] = prompt.strip() + "\n\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"## {k}\n\n{v}" for k, v in sections.items()))


def builtin(state_dir: Path, step: str) -> str:
    """The recorded built-in prompts of a step's stages (empty until it ran once)."""
    p = state_dir / "prompts" / f"{_safe(step)}.md"
    return p.read_text() if p.is_file() else ""


def targets(root: Path, state_dir: Path, steps: list[str]) -> list[str]:
    """Step ids, plus every other name that has a recorded stage prompt or an override file."""
    out = list(steps)
    for d in dirs(root):
        if d.is_dir():
            out += [p.stem for p in sorted(d.glob("*.md"))]
    return list(dict.fromkeys(out))
