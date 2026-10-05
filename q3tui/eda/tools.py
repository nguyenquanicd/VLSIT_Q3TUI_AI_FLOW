"""Tools by role, described in a file (docs/spec/flows.md "Tools").

`tools.json` / `tools.yaml` (project root, or `tools.file`, or `$Q3TUI_HOME/`) — or `tools.roles:` in q3tui.yaml —
names the tools a flow uses: a role (`lint`, `synth`, `sim`, `mutate`, …) → the command line with placeholders and
how to read its log. Steps run a role with `run_role` (command and log parsing are code; the LLM gets the diagnostics
as ground truth) and stages that may run a tool themselves get `describe_for_llm`.

The older vendor adapters (`q3tui.eda`: VCS, slang, …) stay for the Q3TUI flows; this is the flexible layer.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from q3tui.core.config import Config, ToolRole
from q3tui.pipeline.base import StepFailed
from q3tui.eda.base import ToolDiagnostic, ToolEnv, ToolResult, ToolUnavailable, run_command

FILE_NAMES = ("tools.json", "tools.yaml", "tools.yml")


def tools_file(cfg: Config, project_root: Path) -> Path | None:
    if cfg.tools.file:
        p = Path(os.path.expandvars(cfg.tools.file)).expanduser()
        p = p if p.is_absolute() else project_root / p
        if not p.is_file():
            raise ToolUnavailable(f"tools file '{cfg.tools.file}' not found")
        return p
    home = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser()
    for base in (project_root, home):
        for name in FILE_NAMES:
            if (base / name).is_file():
                return base / name
    return None


def load_file(path: Path) -> dict:
    text = path.read_text()
    try:
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - a YAML / JSON syntax error: name the file (and the usual cause)
        raise ToolUnavailable(f"{path}: not readable ({str(exc).splitlines()[0]}; quote commands that contain {{placeholders}} in YAML)") from exc
    if not isinstance(data, dict):
        raise ToolUnavailable(f"{path}: a tools file is a mapping (roles: {{role: {{cmd: …}}}})")
    return data


@dataclass
class ToolSet:
    roles: dict[str, ToolRole]
    env: ToolEnv
    timeout_s: int
    source: str = ""

    def role(self, name: str) -> ToolRole:
        if name not in self.roles:
            raise ToolUnavailable(f"no tool for role '{name}': describe it in tools.json (roles: {{{name}: {{cmd: …}}}}) "
                                  f"or tools.roles in q3tui.yaml; known roles: {', '.join(self.roles) or 'none'}")
        return self.roles[name]


def load_tools(cfg: Config, project_root: Path) -> ToolSet:
    """The roles of the tools file overlaid by the inline `tools.roles`."""
    roles: dict[str, ToolRole] = {}
    source = "q3tui.yaml"
    setup, modules, init, timeout = cfg.tools.setup_script, list(cfg.tools.modules), cfg.tools.modules_init, cfg.tools.timeout_s
    path = tools_file(cfg, project_root)
    if path is None and not cfg.tools.roles:
        # nothing configured anywhere (no tools.json in the project or ~/.q3tui, no tools.roles): the Synopsys preset — the
        # flows then run on a Synopsys site without a setup step; `q3tui tools init` copies it to edit, another tool set needs
        # its own tools.json
        path = PRESET_DIR / "synopsys.json"
    if path is not None:
        data = load_file(path)
        source = str(path) if path.parent != PRESET_DIR else "built-in synopsys preset (no tools.json found; `q3tui tools init` copies it)"
        try:
            roles = {n: ToolRole.model_validate(r) for n, r in (data.get("roles") or {}).items()}
        except Exception as exc:  # noqa: BLE001
            raise ToolUnavailable(f"{path}: {exc}") from exc
        setup = setup or data.get("setup_script")
        modules = list(dict.fromkeys([*(data.get("modules") or []), *modules]))
        init = init or data.get("modules_init")
        if "timeout_s" in data and cfg.tools.timeout_s == 600:
            timeout = int(data["timeout_s"])
    roles.update(cfg.tools.roles)
    return ToolSet(roles, ToolEnv(setup, tuple(modules), init), timeout, source)


PRESET_DIR = Path(__file__).parent / "presets"


def presets() -> list[str]:
    return sorted(p.stem for p in PRESET_DIR.glob("*.json"))


def write_preset(project_root: Path, preset: str = "synopsys", force: bool = False) -> Path:
    """`tools.json` for a tool set (docs/spec/flows.md "Tools"): the project's copy to edit (modules, library paths)."""
    src = PRESET_DIR / f"{preset}.json"
    if not src.is_file():
        raise ToolUnavailable(f"no tools preset '{preset}' (presets: {', '.join(presets()) or 'none'})")
    dest = project_root / "tools.json"
    if dest.exists() and not force:
        raise ToolUnavailable(f"{dest.name} exists (use --force to overwrite)")
    dest.write_text(src.read_text())
    return dest


def roles_needed(flow) -> list[str]:
    """Tool roles the flow's steps declare (`tool_roles`) and the flow file lists under `tools:`."""
    from q3tui.steps import make_step

    out: list[str] = list(flow.tools or [])
    for spec in flow.steps:
        try:
            out += list(getattr(make_step(spec.kind), "tool_roles", ()))
        except Exception:  # noqa: BLE001
            pass
        out += list(spec.options.get("tools", []))
    return list(dict.fromkeys(out))


# -- running -------------------------------------------------------------------------------------


class _Vars(dict):
    def __missing__(self, key):
        raise KeyError(key)


def render(role: ToolRole, variables: dict[str, object]) -> list[str]:
    """The command as an argv: placeholders filled (lists expand to several arguments, values are quoted)."""
    parts = shlex.split(role.cmd)
    argv: list[str] = []
    for part in parts:
        names = re.findall(r"\{(\w+)\}", part)
        if not names:
            argv.append(part)
            continue
        for n in names:
            if n not in variables:
                raise ToolUnavailable(f"tool command needs {{{n}}} (available: {', '.join(sorted(variables)) or 'none'}): {role.cmd}")
        if part in {f"{{{n}}}" for n in names} and isinstance(variables[names[0]], (list, tuple)):
            argv.extend(str(x) for x in variables[names[0]])  # a whole argument that is a list
            continue
        argv.append(re.sub(r"\{(\w+)\}", lambda m: " ".join(map(str, v)) if isinstance(v := variables[m.group(1)], (list, tuple)) else str(v), part))
    return argv


def binary_of(role: ToolRole) -> str:
    return shlex.split(role.cmd)[0] if role.cmd.strip() else ""


def available(tools: ToolSet, name: str) -> bool:
    role = tools.roles.get(name)
    if not role:
        return False
    binary = binary_of(role)
    if "{" in binary:  # built by an earlier step (e.g. `{workdir}/simv` from the compile): it cannot be looked up now
        return True
    return tools.env.which(binary) is not None or Path(binary).is_file()


def run_role(tools: ToolSet, name: str, workdir: Path, variables: dict[str, object], log_name: str | None = None) -> ToolResult:
    """Run the tool of a role: command from the tools file, log kept in `workdir`, diagnostics parsed by code."""
    role = tools.role(name)
    argv = render(role, variables)
    cwd = Path(role.cwd.format(**{k: v for k, v in variables.items() if isinstance(v, (str, Path))})) if role.cwd else workdir
    env = ToolEnv(tools.env.setup_script, tuple(dict.fromkeys([*tools.env.modules, *role.modules])), tools.env.modules_init)
    log = workdir / (log_name or f"{name}.log")
    rc, out, secs, timed_out = run_command(argv, cwd, log, role.timeout_s or tools.timeout_s, env, role.env)
    diags = parse_log(role.parse, out)
    ok = (rc in role.ok_returncodes) and not timed_out and not any(d.severity == "error" for d in diags)
    n_err, n_warn = sum(d.severity == "error" for d in diags), sum(d.severity == "warning" for d in diags)
    return ToolResult(name, binary_of(role), ok, argv, rc, secs, str(log), diags, timed_out,
                      f"{name}: rc={rc}, {n_err} error(s), {n_warn} warning(s)")


class ToolBroken(StepFailed):
    """A tool failed without saying why in code terms (bad command line, missing license / module, wrong option): its
    problem, not the design's — the step stops at once; no LLM fix round can repair it."""


def broken(res: ToolResult, role: str) -> str | None:
    """A message when the tool itself failed: it did not finish, or exited non-zero without one parseable error."""
    if res.ok:
        return None
    if not res.timed_out and res.returncode is not None and any(d.severity == "error" for d in res.diagnostics):
        return None  # it said what is wrong with the code
    tail = ""
    if res.log_path and Path(res.log_path).is_file():
        lines = [x for x in Path(res.log_path).read_text(errors="replace").splitlines() if x.strip()]
        tail = "\n".join(lines[-8:])
    why = "timed out" if res.timed_out else f"exited with rc={res.returncode} and no error it could read"
    return (f"the `{role}` tool {why} — a problem of its command / environment, not of the design (no fix round was "
            f"spent). Command: {' '.join(res.command)[:300]}\nLog: {res.log_path}\n{tail}")


def check_broken(res: ToolResult, role: str) -> None:
    msg = broken(res, role)
    if msg:
        raise ToolBroken(msg)


def supports(tools: ToolSet, name: str, capability: str) -> bool:
    role = tools.roles.get(name)
    return bool(role) and capability in role.supports


def describe_for_llm(tools: ToolSet, names: list[str] | None = None) -> str:
    """What a stage may be told about the tools (role, what it does, the command), never a vendor binary by name
    in the prompt's static part."""
    lines = []
    for n in names or list(tools.roles):
        r = tools.roles.get(n)
        if r is None:
            lines.append(f"- {n}: not configured")
            continue
        extra = f" (supports: {', '.join(r.supports)})" if r.supports else ""
        lines.append(f"- {n}{extra}: `{r.cmd}`" + (f" — {r.description}" if r.description else ""))
    return "\n".join(lines)


# -- log parsers -----------------------------------------------------------------------------------

_LOC = r"(?P<file>[^\s:]+\.\w+):(?P<line>\d+)(?::\d+)?"
_VERILATOR = re.compile(rf"^%(?P<sev>Error|Warning)(?:-(?P<code>[A-Z0-9_]+))?:\s*(?:{_LOC}:\s*)?(?P<msg>.*)$")
_IVERILOG = re.compile(rf"^{_LOC}:\s*(?P<sev>error|warning|sorry):\s*(?P<msg>.*)$")
_YOSYS = re.compile(r"^(?P<sev>ERROR|Warning|WARNING):\s*(?P<msg>.*)$")
_DC = re.compile(rf"^(?P<sev>Error|Warning):\s*(?:{_LOC}:\s*)?(?P<msg>.*?)(?:\s*\((?P<code>[A-Z]{{2,6}}-\d+)\))?\.?\s*$")
_SIM_FAIL = re.compile(r"(?:^|\s)(?P<sev>ERROR|FATAL|FAIL(?:ED)?)\b[:\s]*(?P<msg>.*)$")


def _sev(word: str) -> str:
    return "error" if word.lower() in ("error", "fatal", "sorry", "fail", "failed") else "warning"


def parse_verilator(text: str) -> list[ToolDiagnostic]:
    out = []
    for line in text.splitlines():
        m = _VERILATOR.match(line)
        if m:
            out.append(ToolDiagnostic(_sev(m["sev"]), m["code"] or m["sev"].upper(), m["msg"][:500], m["file"], int(m["line"]) if m["line"] else None))
    return out


def parse_iverilog(text: str) -> list[ToolDiagnostic]:
    out = []
    for line in text.splitlines():
        m = _IVERILOG.match(line)
        if m:
            out.append(ToolDiagnostic(_sev(m["sev"]), m["sev"].upper(), m["msg"][:500], m["file"], int(m["line"])))
            continue
        if line.startswith(("ERROR:", "FATAL:")) or "$fatal" in line.lower() and "called" in line.lower():
            out.append(ToolDiagnostic("error", "RUNTIME", line[:500]))
    return out


def parse_yosys(text: str) -> list[ToolDiagnostic]:
    out = []
    for line in text.splitlines():
        m = _YOSYS.match(line)
        if m:
            out.append(ToolDiagnostic(_sev(m["sev"]), m["sev"].upper(), m["msg"][:500]))
    return out


def parse_dc(text: str) -> list[ToolDiagnostic]:
    """Synopsys Design Compiler / dc_shell: `Error: file:line: message (VER-294).`, `Warning: … (OPT-1006)`."""
    out = []
    for line in text.splitlines():
        m = _DC.match(line)
        if m:
            out.append(ToolDiagnostic(_sev(m["sev"]), m["code"] or m["sev"].upper(), m["msg"][:500], m["file"], int(m["line"]) if m["line"] else None))
    return out


def parse_vcs(text: str) -> list[ToolDiagnostic]:
    from q3tui.eda.synopsys import parse_vcs_log

    return parse_vcs_log(text)


def parse_generic(text: str, patterns: dict | None = None) -> list[ToolDiagnostic]:
    pats = {"error": [r"\berror\b", r"\bfatal\b"], "warning": [r"\bwarning\b"], **(patterns or {})}
    compiled = {sev: [re.compile(p, re.I) for p in ps] for sev, ps in pats.items()}
    out = []
    for line in text.splitlines():
        for sev in ("error", "warning"):
            if any(p.search(line) for p in compiled.get(sev, [])):
                out.append(ToolDiagnostic(sev, sev.upper(), line.strip()[:500]))
                break
    return out


PARSERS = {"vcs": parse_vcs, "dc": parse_dc, "verilator": parse_verilator, "iverilog": parse_iverilog, "yosys": parse_yosys}


def parse_log(parse: str | dict, text: str) -> list[ToolDiagnostic]:
    if isinstance(parse, dict):
        return parse_generic(text, parse)
    if parse in PARSERS:
        return PARSERS[parse](text)
    if parse in ("generic", "", None):
        return parse_generic(text)
    raise ToolUnavailable(f"unknown log parser '{parse}' (vcs, dc, verilator, yosys, iverilog, generic, or {{error: [...], warning: [...]}})")
