"""What the project settings flow works on: the project's own `pdks.json`, a scan of a folder for standard-cell libraries, the tool roles and
whether their tools are found. Code only; the steps and the views call these."""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
from pathlib import Path

from q3tui.core.project import write_json

MAX_DEPTH = 6
MAX_FILES = 5000


def state_dir(project) -> Path:
    d = project.state_dir / "project"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:32]


# -- libraries -----------------------------------------------------------------------------------------------------------------

def pdks_path(project) -> Path:
    return project.root / "pdks.json"


def load_pdks(project) -> dict:
    p = pdks_path(project)
    if not p.is_file():
        return {"pdks": []}
    try:
        data = json.loads(p.read_text())
    except ValueError as exc:
        raise ValueError(f"{p.name} is not valid JSON ({exc})") from exc
    data.setdefault("pdks", [])
    return data


def save_pdks(project, data: dict) -> Path:
    project.backup([pdks_path(project)])
    return write_json(pdks_path(project), data)


def scan(where: str) -> dict:
    """Look for `.db` files: `where` is a folder (searched below it, up to six levels) or a glob. Returns {"groups": [{"dir", "glob", "files"}…]}:
    the files grouped by the folder that holds them, the biggest group first (a technology's corners sit in one folder)."""
    where = os.path.expandvars(os.path.expanduser(where.strip()))
    if not where:
        raise ValueError("give a folder or a glob of .db files")
    files: list[str] = []
    if any(c in where for c in "*?["):
        files = [f for f in glob.glob(where, recursive=True) if f.endswith(".db")]
    elif Path(where).is_dir():
        base_depth = len(Path(where).parts)
        for root, dirs, names in os.walk(where):
            dirs[:] = [d for d in dirs if d != ".git" and len(Path(root, d).parts) - base_depth <= MAX_DEPTH]
            files += [str(Path(root, n)) for n in fnmatch.filter(names, "*.db")]
            if len(files) > MAX_FILES:
                break
    elif Path(where).is_file() and where.endswith(".db"):
        files = [where]
    else:
        raise ValueError(f"'{where}' is not a folder, a .db file or a glob that matches")
    groups: dict[str, list[str]] = {}
    for f in sorted(files):
        groups.setdefault(str(Path(f).parent), []).append(f)
    return {"groups": [{"dir": d, "glob": str(Path(d) / "*.db"), "files": fs} for d, fs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]}


def describe_scan(found: dict) -> str:
    from q3tui.flows.asic.lib import pdks

    groups = found["groups"]
    if not groups:
        return "no .db file found"
    out = []
    for g in groups[:6]:
        corners = [c for c, _ in pdks._corners(g["files"])]
        out.append(f"{g['dir']}: {len(g['files'])} .db — " + ", ".join(corners[:5]) + (" …" if len(corners) > 5 else ""))
    return "\n".join(out) + (f"\n… and {len(groups) - 6} more folder(s)" if len(groups) > 6 else "")


def add_entry(project, entry_id: str, where: str, title: str = "", node_nm: str = "", default: str = "", probe_ns: str = "", include: str = "") -> str:
    """Add (or replace) a technology in the project's pdks.json. `where`: the folder that holds the .db files of its corners, or a glob."""
    from q3tui.flows.asic.lib import pdks

    entry_id = slug(entry_id)
    if not entry_id:
        raise ValueError("a technology needs an id (letters and digits)")
    found = scan(where)
    if not found["groups"]:
        raise ValueError(f"no .db file under '{where}'")
    if any(c in where for c in "*?["):
        pattern = where
    else:
        direct = [g for g in found["groups"] if Path(g["dir"]) == Path(os.path.expandvars(os.path.expanduser(where)))]
        if direct:
            pattern = direct[0]["glob"]
        elif len(found["groups"]) == 1:
            pattern = found["groups"][0]["glob"]
        else:
            raise ValueError("the .db files are in several folders — name the one of this technology:\n" + describe_scan(found))
    entry: dict = {"id": entry_id, "title": title.strip() or entry_id, "node_nm": int(node_nm) if str(node_nm).strip() else 0, "globs": [pattern]}
    if include.strip():
        re.compile(include.strip())  # (a bad regex is the user's error now, not at every view)
        entry["include"] = include.strip()
    files = pdks.files_of(entry)
    if not files:
        raise ValueError(f"'{pattern}'{' with include ' + include if include.strip() else ''} matches no .db file")
    corners = [c for c, _ in pdks._corners(files)]
    if default.strip():
        if default.strip() not in corners:
            raise ValueError(f"default corner '{default}' is not one of: {', '.join(corners)}")
        entry["default"] = default.strip()
    if str(probe_ns).strip():
        entry["probe_ns"] = float(probe_ns)
    data = load_pdks(project)
    data["pdks"] = [e for e in data["pdks"] if e.get("id") != entry_id] + [entry]
    save_pdks(project, data)
    return f"library technology '{entry_id}': {len(files)} corner(s) ({', '.join(corners[:4])}{' …' if len(corners) > 4 else ''}) saved to pdks.json"


def remove_entry(project, entry_id: str) -> str:
    from q3tui.flows.asic.lib import pdks

    data = load_pdks(project)
    if not any(e.get("id") == entry_id for e in data["pdks"]):
        builtin = any(e["id"] == entry_id and e["source"] == "built-in" for e in pdks.entries(None))
        raise ValueError(f"'{entry_id}' is a built-in technology (it is not in this project's pdks.json); hide the built-in ones with \"only\": true there" if builtin
                         else f"no technology '{entry_id}' in this project's pdks.json")
    data["pdks"] = [e for e in data["pdks"] if e.get("id") != entry_id]
    save_pdks(project, data)
    return f"library technology '{entry_id}' removed from pdks.json"


# -- tools ----------------------------------------------------------------------------------------------------------------------

def _roles_index(project) -> tuple[dict[str, list[str]], dict[str, str], dict[str, object]]:
    """(role -> flows that use it, role -> the flow whose default it is, role -> a flow that needs it): from every flow this project can open."""
    from q3tui import flows
    from q3tui.eda import tools as toolmod

    owners: dict[str, list[str]] = {}
    defaults: dict[str, str] = {}
    flow_of: dict[str, object] = {}
    for name in flows.list_flows(project.root):
        try:
            flow = flows.load_flow(name, project.root)
        except Exception:  # noqa: BLE001 - a flow that does not load has no roles to list here
            continue
        for role in toolmod.roles_needed(flow):
            owners.setdefault(role, []).append(name)
            flow_of.setdefault(role, flow)
            if role in flow.tool_defaults:
                defaults.setdefault(role, name)
    return owners, defaults, flow_of


def tool_rows(project) -> list[dict]:
    """One row per tool role the flows (and the project) know: where it comes from, its command, modules, and whether it is found. Slow: it loads
    the modules (a shell per role)."""
    from q3tui.eda import tools as toolmod

    owners, defaults, flow_of = _roles_index(project)
    root = project.root
    base = toolmod.load_tools(project.cfg, root)
    out = []
    for role in sorted(set(owners) | set(base.roles)):
        flow = flow_of.get(role)
        tools = toolmod.load_tools(project.cfg, root, flow) if flow is not None else base
        r = tools.roles.get(role)
        if r is None:
            out.append({"role": role, "tool": "", "found": False, "modules": [], "from": "not defined", "cmd": "", "used_by": owners.get(role, []), "description": ""})
            continue
        src = ("q3tui.yaml tools.roles" if role in project.cfg.tools.roles else
               (tools.source if role in base.roles else f"flow '{defaults.get(role)}' default"))
        try:
            found = toolmod.available(tools, role)
        except Exception:  # noqa: BLE001 - a role that cannot be checked is not found
            found = False
        out.append({"role": role, "tool": toolmod.binary_of(r), "found": found, "modules": list(dict.fromkeys([*tools.env.modules, *r.modules])),
                    "from": src, "cmd": r.cmd, "own_modules": list(r.modules), "timeout_s": r.timeout_s, "used_by": owners.get(role, []), "description": r.description,
                    "parse": r.parse if isinstance(r.parse, str) else "patterns"})
    return out


def set_role(project, role: str, cmd: str = "", modules: str = "", timeout_s: str = "", parse: str = "", description: str = "") -> str:
    """Change a tool role of this project: the command, the modules loaded for it (besides the global ones), the time limit. The role's other
    settings (log reader, description) stay as they are now. Saved where the project keeps its roles: `tools.json` (created, with the roles the
    built-in preset gives, when the project has none: adding one role must not lose the others), else `tools.roles` of q3tui.yaml."""
    from q3tui.core.config import ToolRole
    from q3tui.eda import tools as toolmod

    _owners, _defaults, flow_of = _roles_index(project)
    tools = toolmod.load_tools(project.cfg, project.root, flow_of.get(role))
    cur = tools.roles.get(role)
    if cur is None and not cmd.strip():
        raise ValueError(f"role '{role}' is not defined: give its command")
    data = cur.model_dump(mode="json", exclude_defaults=True) if cur is not None else {}
    if cmd.strip():
        data["cmd"] = cmd.strip()
    if modules.strip() or "modules" in data:
        data["modules"] = [m for m in modules.replace(",", " ").split() if m] if modules.strip() else []
    if timeout_s.strip():
        data["timeout_s"] = int(timeout_s)
    if parse.strip():
        data["parse"] = parse.strip()
    if description.strip():
        data["description"] = description.strip()
    ToolRole.model_validate(data)  # (a bad value is the user's error now)
    cfg_file = project.cfg.tools.file
    path = toolmod.tools_file(project.cfg, project.root) if (cfg_file or any((project.root / n).is_file() for n in toolmod.FILE_NAMES)) else None
    if path is not None and path.suffix != ".json":
        raise ValueError(f"this project's tools are in {path.name}: edit it by hand (only tools.json is written here)")
    if project.cfg.tools.roles and path is None:  # roles inline in q3tui.yaml
        project.cfg.tools.roles[role] = ToolRole.model_validate(data)
        project.save_config_values({"tools": {"roles": {role: data}}})
        return f"tool role '{role}' saved to q3tui.yaml (tools.roles)"
    if path is None:
        path = project.root / "tools.json"
        seed = json.loads((toolmod.PRESET_DIR / "synopsys.json").read_text())  # the roles the project uses now (the preset), kept
        seed.pop("_comment", None)
    else:
        seed = json.loads(path.read_text())
    seed.setdefault("roles", {})[role] = data
    project.backup([path])
    write_json(path, seed)
    return f"tool role '{role}' saved to {path.name}"


def save_tool_rows(project, rows: list[dict]) -> Path:
    return write_json(state_dir(project) / "tools.json", {"roles": rows})


def load_tool_rows(project) -> list[dict] | None:
    p = state_dir(project) / "tools.json"
    return json.loads(p.read_text()).get("roles") if p.is_file() else None


# -- identifying a library folder (the LLM fills in the rest) ---------------------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402


class LibGuess(BaseModel):
    """What the identify stage returns: an entry of the catalog, from what the folder tells."""

    id: str
    title: str
    node_nm: int = 0
    foundry: str = ""
    glob: str  # one of the globs the scan found
    include: str = ""  # regex a file name must match ("" = all of the glob)
    default_corner: str = ""
    probe_ns: float = 0.0
    notes: str = ""


def _system() -> str:
    from q3tui.flows import skill_sections

    return skill_sections(Path(__file__).resolve().parents[1] / "libraries" / "md" / "skill.md")["SYSTEM"]


def _scan_text(found: dict) -> str:
    from q3tui.flows.asic.lib import pdks

    out = []
    for g in found["groups"][:8]:
        names = [Path(f).name for f in g["files"]]
        out.append(f"- glob `{g['glob']}` — {len(names)} .db file(s); corners (labels): " + ", ".join(c for c, _ in pdks._corners(g["files"])[:40])
                   + (" …" if len(names) > 40 else "") + "\n  files: " + ", ".join(names[:40]) + (" …" if len(names) > 40 else ""))
    return "\n".join(out) + (f"\n- … {len(found['groups']) - 8} more folder(s)" if len(found["groups"]) > 8 else "")


def run_sync(factory):
    """Run `factory()` (a coroutine function) to its end from sync code, also when this thread already runs an event loop."""
    import asyncio

    import anyio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(factory)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as ex:
        return ex.submit(anyio.run, factory).result()


class _Silent:
    """An emitter that says nothing (a stage run from a thread of the UI: the UI is not touched from there)."""

    def __call__(self, *_a, **_k) -> None:
        return None

    def add_cost(self, _usd: float) -> None:
        return None


def identify(ops, where: str) -> dict:
    """`identify_async` from sync code (the CLI, an assistant tool). In a thread of the TUI the stage is silent: the TUI awaits `identify_async` itself."""
    import asyncio

    try:
        asyncio.get_running_loop()
        emit = _Silent()
    except RuntimeError:
        emit = None
    return run_sync(lambda: identify_async(ops, where, emit))


async def identify_async(ops, where: str, emit=None) -> dict:
    """Look in `where` (a folder or glob) and fill in a catalog entry: code scans for the .db files, a read-only LLM stage reads the folder's own
    documentation and names and proposes the id, name, node, which files are cell corners, the default corner and the probe period, and code
    checks every claim against the files. Returns {id, folder (the glob), title, node_nm, include, default, probe_ns, foundry, notes, warnings}."""
    from q3tui.flows.asic.lib import pdks
    from q3tui.llm import runtime

    found = scan(where)
    if not found["groups"]:
        raise ValueError(f"no .db file under '{where}'")
    base = Path(os.path.expandvars(os.path.expanduser(where.strip())))
    root = base if base.is_dir() else Path(found["groups"][0]["dir"])
    if any(c in where for c in "*?["):  # a glob: read around the first match
        root = Path(found["groups"][0]["dir"])
    stage = runtime.Stage(name="lib_identify", system_prompt=_system(), cwd=root, builtin_tools=["Read", "Grep", "Glob"], output_model=LibGuess, max_turns=14,
                          prompt=f"## Folder given\n`{where}`\n\n## What code found (ground truth)\n{_scan_text(found)}\n\nFill in the catalog entry for the standard-cell libraries here.")
    emit = emit or ops.engine.bus.scoped("libraries")
    emit("log", message=f"reading {where} to identify the library (an LLM stage, read only)…")
    guess: LibGuess = (await runtime.run_stage(stage, ops.project.cfg, emit)).output
    warnings: list[str] = []
    glob_ = guess.glob if guess.glob in {g["glob"] for g in found["groups"]} else found["groups"][0]["glob"]
    if glob_ != guess.glob:
        warnings.append(f"the glob '{guess.glob}' is not one the scan found: using {glob_}")
    entry = {"globs": [glob_]}
    include = guess.include.strip()
    if include:
        try:
            entry["include"] = include
            if not pdks.files_of(entry):
                warnings.append(f"include '{include}' matches no file: dropped")
                entry.pop("include")
                include = ""
        except re.error:
            warnings.append(f"include '{include}' is not a valid regex: dropped")
            entry.pop("include", None)
            include = ""
    corners = [c for c, _ in pdks._corners(pdks.files_of(entry))]
    default = guess.default_corner.strip()
    if default and default not in corners:
        warnings.append(f"default corner '{default}' is not one of the corners: dropped")
        default = ""
    return {"id": slug(guess.id) or slug(root.name), "folder": glob_, "title": guess.title.strip(), "node_nm": str(guess.node_nm or ""), "include": include,
            "default": default, "probe_ns": str(guess.probe_ns or ""), "foundry": guess.foundry, "notes": guess.notes.strip(), "warnings": warnings, "corners": corners}


# -- adding a tool: code finds the executables, the LLM fills in the role (the user gives only a path) ----------------------------------------------------

class ToolGuess(BaseModel):
    """What the tool-identify stage returns: a role's setup for the tool at the path given."""

    role: str
    cmd: str
    parse: str = "generic"
    modules: list[str] = []
    description: str = ""
    notes: str = ""


def tool_scan(where: str) -> dict:
    """The executables at `where`: a file, or a folder (its `bin` folders, up to four levels down; a folder with executables itself too). Returns
    {"root": folder to read, "executables": [paths]}."""
    where = os.path.expandvars(os.path.expanduser(where.strip()))
    if not where:
        raise ValueError("give the path of a tool: its binary or its install folder")
    p = Path(where)
    if p.is_file():
        if not os.access(p, os.X_OK):
            raise ValueError(f"'{where}' is not executable")
        return {"root": str(p.parent), "executables": [str(p)]}
    if not p.is_dir():
        raise ValueError(f"'{where}' does not exist")
    found: list[str] = []
    base = len(p.parts)
    for root, dirs, names in os.walk(p):
        dirs[:] = [d for d in dirs if d != ".git" and len(Path(root, d).parts) - base < 4]
        if Path(root).name == "bin" or root == str(p):
            found += [str(Path(root, n)) for n in sorted(names) if os.access(Path(root, n), os.X_OK) and Path(root, n).is_file()]
        if len(found) > 400:
            break
    if not found:
        raise ValueError(f"no executable under '{where}' (looked in bin folders, four levels down)")
    return {"root": str(p), "executables": found}


def modules_available(timeout: float = 20.0) -> list[str]:
    """The environment modules (`module avail -t`), empty when there is no module system here."""
    import subprocess

    init = os.environ.get("MODULESHOME")
    inits = [Path(init) / "init" / "bash"] if init else []
    inits.append(Path("/usr/share/modules/init/bash"))
    for f in inits:
        if f.is_file():
            try:
                r = subprocess.run(["bash", "-c", f"source {f} && module avail -t 2>&1"], capture_output=True, text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                return []
            return sorted({ln.strip().rstrip("/") for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("/") and ":" not in ln})
    return []


def role_contracts(project) -> list[dict]:
    """The roles that exist: name, command (the placeholders the steps pass are in it), log reader, modules, description."""
    from q3tui.eda import tools as toolmod

    owners, _defaults, flow_of = _roles_index(project)
    base = toolmod.load_tools(project.cfg, project.root)
    out = []
    for role in sorted(set(owners) | set(base.roles)):
        r = (toolmod.load_tools(project.cfg, project.root, flow_of.get(role)) if role in flow_of else base).roles.get(role)
        if r is not None:
            out.append({"role": role, "cmd": r.cmd, "parse": r.parse if isinstance(r.parse, str) else "patterns", "modules": list(r.modules), "description": r.description,
                        "used_by": owners.get(role, []), "placeholders": sorted(set(re.findall(r"\{(\w+)\}", r.cmd)))})
    return out


def _tool_system() -> str:
    from q3tui.flows import skill_sections

    return skill_sections(Path(__file__).resolve().parents[1] / "tools" / "md" / "skill.md")["SYSTEM"]


def identify_tool(ops, where: str) -> dict:
    """`identify_tool_async` from sync code (silent in a thread of the TUI, which awaits the async one itself)."""
    import asyncio

    try:
        asyncio.get_running_loop()
        emit = _Silent()
    except RuntimeError:
        emit = None
    return run_sync(lambda: identify_tool_async(ops, where, emit))


async def identify_tool_async(ops, where: str, emit=None) -> dict:
    """Work out which role the tool at `where` serves and how to run it: code finds the executables, a read-only LLM stage (it may read the tool's folder)
    proposes the role, command, log reader and modules, and code checks them. Returns {role, cmd, parse, modules, description, notes, warnings, executables}."""
    from q3tui.eda import tools as toolmod
    from q3tui.llm import runtime

    found = tool_scan(where)
    roles = role_contracts(ops.project)
    mods = modules_available()
    readers = sorted({*toolmod.PARSERS, "generic"})
    role_text = "\n".join(f"- `{r['role']}` (used by: {', '.join(r['used_by']) or '–'}): `{r['cmd']}` — placeholders {', '.join('{' + x + '}' for x in r['placeholders']) or 'none'}; log reader `{r['parse']}`; "
                           f"{r['description']}" for r in roles)
    exes = "\n".join(f"- {e}" for e in found["executables"][:80]) + (f"\n- … {len(found['executables']) - 80} more" if len(found["executables"]) > 80 else "")
    stage = runtime.Stage(name="tool_identify", system_prompt=_tool_system(), cwd=Path(found["root"]), builtin_tools=["Read", "Grep", "Glob"], output_model=ToolGuess, max_turns=14,
                          prompt=f"## Path given\n`{where}`\n\n## Executables found (ground truth)\n{exes}\n\n## Roles that exist\n{role_text}\n\n"
                                 f"## Log readers you may name\n{', '.join(readers)}\n\n## Environment modules\n" + (", ".join(mods[:400]) if mods else "(none: no module system here)")
                                 + "\n\nSet up this tool for the role it serves.")
    emit = emit or ops.engine.bus.scoped("tools")
    emit("log", message=f"reading {where} to identify the tool (an LLM stage, read only)…")
    guess: ToolGuess = (await runtime.run_stage(stage, ops.project.cfg, emit)).output
    warnings: list[str] = []
    known = {r["role"]: r for r in roles}
    role = slug(guess.role) or "tool"
    cmd = guess.cmd.strip()
    if not cmd:
        raise ValueError("the stage gave no command")
    if role in known:
        allowed = set(known[role]["placeholders"])
        extra = sorted(set(re.findall(r"\{(\w+)\}", cmd)) - allowed)
        if extra:
            warnings.append(f"the command uses {', '.join('{' + x + '}' for x in extra)}, which the steps do not pass to '{role}' (they pass: {', '.join(sorted(allowed)) or 'nothing'}): check it")
    else:
        warnings.append(f"'{role}' is a new role: no step uses it until a flow asks for it")
    first = cmd.split()[0]
    if "{" not in first and not (first in {Path(e).name for e in found["executables"]} or first in found["executables"]):
        warnings.append(f"the command starts with '{first}', which is not among the executables found")
    parse = guess.parse.strip() or "generic"
    if parse not in readers:
        warnings.append(f"log reader '{parse}' does not exist: using generic")
        parse = "generic"
    if parse == "generic" and role in known and known[role]["parse"] == "patterns":
        parse = ""  # the role reads its log with its own error / warning patterns: "generic" would lose them, so the role keeps them
    modules = [m for m in guess.modules if m in mods] if mods else []
    dropped = [m for m in guess.modules if m not in modules]
    if dropped:
        warnings.append(f"module(s) not available here, dropped: {', '.join(dropped)}")
    return {"role": role, "cmd": cmd, "parse": parse, "modules": modules, "description": guess.description.strip(), "notes": guess.notes.strip(), "warnings": warnings,
            "executables": found["executables"][:10]}
