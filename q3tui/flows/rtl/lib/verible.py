"""Verible for the RTL flow: the flags file, running lint / format through the tool roles, reading the findings.

Everything computable is code: the flags file is validated (shape, known rules), the tool runs through `run_role`
(roles `verible_lint` / `verible_format`, flows/rtl/tools.json), findings are normalised and grouped by module, and a format is
checked to have changed whitespace only. The LLM writes the flags from the rules and judges the findings; it never runs the tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

LINT_FLAGS = "verible.flags"  # in <rtl>/style_lint/: the lint flags written from the rules
FORMAT_FLAGS = "format.flags"  # the formatter's flags (indentation, line length), written from the same rules
# the flags a flags file may hold: they choose and configure rules, never what is analysed
ALLOWED_FLAGS = ("rules", "ruleset", "rules_config", "waiver_files", "rules_config_search")
_RULE_ITEM = re.compile(r"^[+-]?(?P<name>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?:=(?P<config>.*))?$")


def read_flags(path: Path) -> list[str]:
    """The flags of a file: one per line, blank lines and `#` comments skipped."""
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def split_rules(flag: str) -> list[str]:
    """The items of a `--rules=` flag (`+a,-b,line-length=length:100`): commas inside a config stay with their rule."""
    value = flag.split("=", 1)[1] if "=" in flag else ""
    items: list[str] = []
    for part in value.split(","):
        if items and not re.match(r"^[+-]?[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:=|$)", part):
            items[-1] += "," + part  # a config value with a comma of its own
        elif part:
            items.append(part)
    return items


def validate_flags(lines: list[str]) -> list[str]:
    """Problems of a flags file (empty: fine): not a `--flag[=value]`, a flag outside ALLOWED_FLAGS, a malformed rule item, a rule both turned on
    and off. Whether a rule exists and its parameters are valid is Verible's to say (`verible_check_flags` asks it)."""
    problems: list[str] = []
    switched: dict[str, str] = {}
    for ln in lines:
        m = re.match(r"^--(?P<flag>[a-z_]+)(?:=(?P<value>.*))?$", ln)
        if not m:
            problems.append(f"`{ln}` is not a flag (`--name` or `--name=value`, one per line)")
            continue
        if m["flag"] not in ALLOWED_FLAGS:
            problems.append(f"`--{m['flag']}` is not allowed here (allowed: {', '.join('--' + f for f in ALLOWED_FLAGS)})")
            continue
        if m["flag"] != "rules":
            continue
        for item in split_rules(ln):
            r = _RULE_ITEM.match(item)
            if not r:
                problems.append(f"`{item}` is not a rule item (`name`, `+name`, `-name` or `name=param:value`)")
                continue
            name = r["name"]
            sign = "off" if item.startswith("-") else "on"
            if switched.get(name, sign) != sign:
                problems.append(f"rule `{name}` is turned both on and off")
            switched[name] = sign
    return problems


def tool_args(lines: list[str]) -> list[str]:
    """The flags as arguments of the tool (a `--rules_config=` / `--waiver_files=` path is project-relative: the tool runs there)."""
    return list(lines)


@dataclass
class Finding:
    file: str
    module: str
    line: int
    rule: str
    message: str
    text: str = ""  # the source line, for the judge

    def key(self) -> str:
        return f"{self.module}:{self.line}:{self.rule}"

    def to_dict(self) -> dict:
        return {"file": self.file, "module": self.module, "line": self.line, "rule": self.rule, "message": self.message, "text": self.text}


def findings(diagnostics, project_root: Path) -> list[Finding]:
    """Normalised findings (warnings with a rule) of a lint run, in file / line order, with the source line."""
    out: list[Finding] = []
    cache: dict[str, list[str]] = {}
    for d in diagnostics:
        if d.severity != "warning" or not d.file:
            continue
        path = Path(d.file)
        path = path if path.is_absolute() else project_root / path
        lines = cache.setdefault(str(path), path.read_text(errors="replace").splitlines() if path.is_file() else [])
        n = d.line or 0
        out.append(Finding(_rel(path, project_root), path.stem, n, d.code, d.message, lines[n - 1].rstrip() if 0 < n <= len(lines) else ""))
    return sorted(out, key=lambda f: (f.file, f.line, f.rule))


def syntax_errors(diagnostics) -> list[str]:
    return [f"{d.file}:{d.line} {d.message}" if d.file else d.message for d in diagnostics if d.severity == "error"]


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def same_code(before: str, after: str) -> bool:
    """A format may change whitespace only: the two texts are equal once every whitespace character is dropped."""
    return re.sub(r"\s+", "", before) == re.sub(r"\s+", "", after)


def rtl_files(rtl_dir: Path) -> list[Path]:
    """The RTL sources in compile order when there is a filelist, else by name."""
    fl = rtl_dir / "filelist.f"
    if fl.is_file():
        names = [Path(ln.strip()).name for ln in fl.read_text().splitlines() if ln.strip() and not ln.strip().startswith(("//", "+", "-"))]
        ordered = [rtl_dir / n for n in names if (rtl_dir / n).is_file()]
        rest = [p for p in sorted(rtl_dir.glob("*.sv")) + sorted(rtl_dir.glob("*.v")) if p not in ordered]
        return [*ordered, *rest]
    return sorted(rtl_dir.glob("*.sv")) + sorted(rtl_dir.glob("*.v")) if rtl_dir.is_dir() else []


SAMPLE = """module Sample_Mod (input logic clk, input logic rst_n, output logic [3:0] q);
  parameter Width = 4;
  logic [3:0] cnt;
  always_ff @(posedge clk) if (!rst_n) cnt <= '0; else cnt <= cnt + 1;
  assign q = cnt;
endmodule
"""


def run_lint(ts, work: Path, files: list[Path], flags: list[str]):
    """Verible lint through the role `verible_lint` (exit code 1 = findings: a result)."""
    from q3tui.eda.tools import run_role

    return run_role(ts, "verible_lint", work, {"args": list(flags), "files": [str(f) for f in files]}, log_name="verible_lint.log")


def run_format(ts, work: Path, files: list[Path], flags: list[str]):
    """Verible format, in place, through the role `verible_format`."""
    from q3tui.eda.tools import run_role

    args = ["--inplace", *[f for f in flags if f != "--inplace"]]
    return run_role(ts, "verible_format", work, {"args": args, "files": [str(f) for f in files]}, log_name="verible_format.log")


def log_text(res, limit: int = 6000) -> str:
    """The tool's own output (its log without the `$ command` line), the end of it when long."""
    if not res.log_path or not Path(res.log_path).is_file():
        return ""
    lines = Path(res.log_path).read_text(errors="replace").splitlines()
    text = "\n".join(lines[1:] if lines and lines[0].startswith("$ ") else lines)
    return text if len(text) <= limit else "…\n" + text[-limit:]


def show_findings(found: list[Finding], limit: int = 150) -> str:
    """Findings as the LLM reads them: counts per rule, then `module:line [rule] message | source line`."""
    if not found:
        return "no findings"
    per: dict[str, int] = {}
    for f in found:
        per[f.rule] = per.get(f.rule, 0) + 1
    head = f"{len(found)} finding(s): " + ", ".join(f"{r} ×{n}" for r, n in sorted(per.items(), key=lambda x: -x[1]))
    rows = [f"{f.module}:{f.line} [{f.rule}] {f.message} | {f.text.strip()[:100]}" for f in found[:limit]]
    return "\n".join([head, *rows, *([f"… {len(found) - limit} more"] if len(found) > limit else [])])


def problem(res, role: str) -> str | None:
    """Why a Verible run is the tool's problem, not the design's (None: it ran). The tool's own refusal (unknown rule, parameter out of range, an
    unknown flag) has no file and may come with exit code 1, which for lint also means "findings": it must not read as a clean result."""
    from q3tui.eda.tools import broken

    tool = [d.message for d in res.diagnostics if d.severity == "error" and not d.file]
    if tool:
        return f"{role}: Verible refused its flags: " + "; ".join(dict.fromkeys(tool))[:600]
    return broken(res, role)


def check(res, role: str) -> None:
    from q3tui.eda.tools import ToolBroken

    msg = problem(res, role)
    if msg:
        raise ToolBroken(msg)


_RULE_HEAD = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*)\s{2,}(\S.*)$")
_PARAM = re.compile(r"\*\s*`(\w+)`\s*Default:\s*`([^`]*)`")


def rule_catalog(help_text: str, width: int = 140) -> str:
    """A compact catalog of the installed Verible's lint rules from `--help_rules=all` (a rule starts at column 0: `name  description`; then
    `Parameters:` with `* \`param\` Default: \`value\``): `- name — description [param=default, …]` (on by default: `*`). The LLM reads this instead
    of asking for every rule's help one by one. Empty when the text is not in that shape."""
    rules: list[dict] = []
    for line in help_text.splitlines():
        m = _RULE_HEAD.match(line)
        if m:
            rules.append({"name": m.group(1), "text": m.group(2).strip(), "params": [], "on": False})
        elif rules:
            if (p := _PARAM.search(line)):
                rules[-1]["params"].append(f"{p.group(1)}={p.group(2) or '\"\"'}")
            elif "Enabled by default: true" in line:
                rules[-1]["on"] = True
    out = []
    for r in rules:
        text = r["text"] if len(r["text"]) <= width else r["text"][: width - 1].rstrip() + "…"
        out.append(f"- {r['name']}{'*' if r['on'] else ''} — {text}" + (f" [{', '.join(r['params'])}]" if r["params"] else ""))
    return "\n".join(out)
