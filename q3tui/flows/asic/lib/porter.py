"""The ASIC porter (the LLM half of `asic_port`): it ports the RTL to what a standard-cell flow takes, as the rule file says, and
writes the port as work orders (the module, the line, the wrapper to make and the change). The RTL is the VLSIT flow's: the
orders go there through `asic_handoff`.

The code scan (`port.py`) is ground truth and runs first; the porter adds what it cannot see, under the rule ids of the rule
file's port rules. The orders of a file whose text did not change are kept from the last run."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.core.project import sha256_file, sha256_json

SECTION = re.compile(r"^## 3\. Port rules\s*$(.*?)(?=^## \d|\Z)", re.M | re.S)
RULE_ID = re.compile(r"^- \*\*`(\w+)`\*\*\s*\((error|warn)\)\s*—\s*(.+?)(?=\n- \*\*`|\Z)", re.M | re.S)


class PortChange(BaseModel):
    rule: str = Field(description="The id of a port rule of the rule file (section 3), e.g. mem_sram_wrapper; for a line the scan found, the scan's rule id")
    module: str = Field(description="The module (the RTL file name without extension) the finding is in")
    line: int = Field(description="The line of the code it is about (1-based, in src/rtl/<module>.sv)")
    text: str = Field(description="That code, shortened to one line")
    wrapper: str = Field("", description="The wrapper module to create (m_<function>), empty when the change needs none")
    fix: str = Field(description="The port, as an order to the RTL author, in one to four sentences: the wrapper's ports (name, direction, width), "
                                 "its latency or timing, where it is instantiated, or the exact change. Behaviour, ports and requirement tags stay the same.")


class PortPlan(BaseModel):
    changes: list[PortChange] = Field(default_factory=list, description="Only what the scan did not report; empty when nothing else has to be ported")
    summary: str = Field("", description="One sentence on what was ported")


def port_rules(rules_text: str) -> dict[str, dict]:
    """{id: {severity, title}} of the port rules of a rule file (the text after the id is the rule: its first words are the title)."""
    m = SECTION.search(rules_text)
    out = {}
    for rid, sev, body in RULE_ID.findall(m.group(1) if m else ""):
        title = " ".join(body.split()).split(" **Fix:**")[0]
        out[rid] = {"severity": sev, "title": title.split(". ")[0].rstrip(".")[:90]}
    return out


def resolve_rules(names: list[str], project_root: Path, step_dir: Path) -> list[tuple[str, str]]:
    """[(name, text)] of the rule files: a path relative to the project, else the step's rules/ folder."""
    out = []
    for n in names:
        for cand in (project_root / n, step_dir / "rules" / n):
            if cand.is_file():
                out.append((cand.name, cand.read_text()))
                break
        else:
            raise FileNotFoundError(f"rule file '{n}' not found (project dir or the asic_port step's rules/)")
    return out


def prompt(rules: list[tuple[str, str]], modules: list[str], files: list[str], found: list[dict], min_words: int, waive: tuple[str, ...]) -> str:
    """The stage prompt: the rules first (static), then what this run ports."""
    lines = ["# Rules", *(f"## {n}\n\n{t}" for n, t in rules),
             "\n# This run",
             f"Port these files of src/rtl/ (read each one): {', '.join(files)}",
             f"All modules of the design: {', '.join(modules)}",
             f"`min_mem_words` = {min_words}." + (f" Waived rules (never report): {', '.join(waive)}." if waive else "")]
    if found:
        lines += ["\nThe scan (ground truth) found these. Port them too: when one needs a wrapper or a change, give an order at the same "
                  "file and line (the rule id may be the scan's); the scan's generic fix is only a hint.",
                  *(f"- {f['rule']} {f['file']}:{f['line']} `{f['text']}`" for f in found)]
    return "\n".join(lines)


def file_hashes(files: list[Path], root: Path) -> dict[str, str]:
    return {(str(f.relative_to(root)) if f.is_relative_to(root) else str(f)): sha256_file(f) for f in files}


def rules_key(rules: list[tuple[str, str]], min_words: int, waive: tuple[str, ...]) -> str:
    return sha256_json({"rules": rules, "min_words": min_words, "waive": list(waive)})[:12]
