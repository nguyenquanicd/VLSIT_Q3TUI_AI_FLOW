"""Flow definitions as data (docs/spec/flows.md).

A flow is a YAML / JSON file: which step kinds run, in which order, what each depends on, which gates stop for a
human. The built-in flow is `vlsit` (q3tui/flows/vlsit/); users add their own under `<project>/flows/` or
`~/.q3tui/flows/` and select one with `pipeline.flow: <name | path>`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GATE_MODES = ("human", "auto", "auto_answer", "none")
# human: stops for your review; auto: approves itself (stops for blocking questions); auto_answer: unattended (also
# takes every default); none: no review gate (blocking questions still stop it)

BUILTIN_DIR = Path(__file__).parent


class FlowError(ValueError):
    pass


@dataclass
class StepSpec:
    id: str
    kind: str
    title: str | None = None
    label: str | None = None  # short number / tag shown before the name ("4b")
    deps: list[str] | None = None  # None: the kind's own
    gate: str | None = None  # one of GATE_MODES; None: no gate
    options: dict = field(default_factory=dict)  # handed to the step (StepDef.options); part of its staleness
    view: list[str] = field(default_factory=list)  # globs (project-relative) of extra files its Files tab shows
    passes: list[str] = field(default_factory=list)  # pass conditions, checked when the step finished (see `check_pass`)
    notes: str = ""  # markdown instructions appended to the step's LLM tasks (a markdown flow's section body)

    def fingerprint(self) -> str:
        data = [self.kind, self.deps, self.options, self.passes, self.notes]
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Flow:
    name: str
    steps: list[StepSpec]
    title: str = ""
    description: str = ""
    gap_targets: dict[str, str] = field(default_factory=dict)  # question kind -> the step that owns the answer
    sees: dict[str, list[str]] = field(default_factory=dict)  # step -> steps whose settled decisions it may receive
    tools: list[str] = field(default_factory=list)  # tool roles the flow needs (docs/spec/flows.md "Tools")
    version: int = 1
    path: Path | None = None
    session: str = "fresh"  # "flow": one persistent LLM session for the whole flow; "fresh": a session per stage
    # what a flow folder's add-on files bring (q3tui/addons.py); empty for every other kind of flow
    actions: dict = field(default_factory=dict)  # name -> addons.Action (the add-on's user actions)
    tool_defaults: dict = field(default_factory=dict)  # role -> ToolRole data (<flow folder>/tools.json)
    panels_file: Path | None = None  # <flow folder>/panels.py: the TUI panels of its step kinds

    def spec(self, step_id: str) -> StepSpec | None:
        return next((s for s in self.steps if s.id == step_id), None)

    @property
    def gates(self) -> dict[str, str]:
        """step id -> gate mode, as the flow defines them."""
        return {s.id: s.gate for s in self.steps if s.gate and s.gate != "none"}

    def to_file_dict(self) -> dict:
        """Everything a flow file holds (what `save_flow` writes; `parse_flow` reads it back)."""
        d: dict = {"name": self.name, "title": self.title, "description": self.description, "version": self.version,
                   "session": self.session, "tools": self.tools, "sees": self.sees, "gap_targets": self.gap_targets}
        d = {k: v for k, v in d.items() if v not in ("", [], {}, None)}
        steps = []
        for s in self.steps:
            e = {"id": s.id, "kind": s.kind, "title": s.title, "label": s.label, "deps": s.deps, "gate": s.gate,
                 "options": s.options, "view": s.view, "pass": s.passes, "notes": s.notes}
            steps.append({k: v for k, v in e.items() if v not in ("", [], {}, None) or (k == "deps" and v == [])})
        d["steps"] = steps
        return d

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "version": self.version, "session": self.session,
                "steps": [{"id": s.id, "kind": s.kind, "deps": s.deps, "gate": s.gate, "options": s.options, "view": s.view,
                           "pass": s.passes, "notes": s.notes} for s in self.steps]}


FLOW_EXTS = (".md", ".yaml", ".yml", ".json")
_BULLET = re.compile(r"^\s*[-*]\s+([A-Za-z_]+)\s*:\s*(.*)$")
_LIST_KEYS = {"view", "pass"}  # may repeat (one bullet per entry)


def _yaml(text: str):
    import yaml

    return yaml.safe_load(text)


def parse_markdown(text: str, path: Path | None = None) -> dict:
    """A flow as a skill-like markdown file: YAML front matter (name, title, session, tools, sees, gap_targets), free text,
    then one `## <step id>` section per step. A section starts with `- key: value` lines (kind, label, title, deps, gate,
    options, view, pass; `view` and `pass` may repeat), the rest of it is the step's notes for the LLM."""
    where = f"{path}: " if path else ""
    data: dict = {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.S)
    if m:
        data = _yaml(m.group(1)) or {}
        if not isinstance(data, dict):
            raise FlowError(f"{where}the front matter must be a mapping")
        text = text[m.end():]
    parts = re.split(r"^##[ \t]+(.+?)[ \t]*$", text, flags=re.M)
    intro, sections = parts[0].strip(), parts[1:]
    if intro and not data.get("description"):
        data["description"] = " ".join(intro.split())
    steps = []
    for sid, body in zip(sections[0::2], sections[1::2]):
        step: dict = {"id": sid.strip()}
        lines = body.strip("\n").split("\n")
        i = 0
        while i < len(lines) and (not lines[i].strip() or _BULLET.match(lines[i])):
            b = _BULLET.match(lines[i])
            if b:
                key, raw = b.group(1).lower(), b.group(2).strip()
                try:
                    value = _yaml(raw) if raw else None
                except Exception as exc:  # noqa: BLE001
                    raise FlowError(f"{where}step '{sid.strip()}': `{key}` is not readable ({exc})") from exc
                if key in _LIST_KEYS:
                    step.setdefault(key, []).extend(value if isinstance(value, list) else [raw])
                else:
                    step[key] = value
            i += 1
        notes = "\n".join(lines[i:]).strip()
        if notes:
            step["notes"] = notes
        steps.append(step)
    data["steps"] = steps
    return data


FOLDER_FILE = "FLOW.md"


def _front(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.S)
    if not m:
        return {}, text.strip()
    data = _yaml(m.group(1)) or {}
    if not isinstance(data, dict):
        raise FlowError("front matter must be a mapping")
    return data, text[m.end():].strip()


_SECTION = re.compile(r"<!-- (\w+) -->\n(.*?)\n<!-- /\1 -->\n?", re.S)


def skill_sections(path: Path) -> dict[str, str]:
    """The named texts of a step's skill.md (`<!-- NAME -->` … `<!-- /NAME -->`): the system prompts its LLM stages run with."""
    return {m.group(1): m.group(2) for m in _SECTION.finditer(path.read_text())} if path.is_file() else {}


def parse_folder(path: Path) -> dict:
    """A flow as a folder: `FLOW.md` (front matter: name, title, session, tools, sees, gap_targets, `steps: [ids in order]`;
    body: the description) and one folder per step: `<id>/md/skill.md` (front matter: kind, label, title, deps, gate, options,
    view, pass; body: the step's skill — named `<!-- NAME -->` sections are its stage system prompts, the rest is notes handed
    to every LLM task of the step), optional `<id>/step.py` (a class `Step(StepDef)`: the step's own
    code, used when skill.md names no kind), `<id>/md/prompt.md` (system-prompt override), `<id>/rules/` (rule files),
    `<id>/schemas/` (JSON schemas), `<id>/scripts/`."""
    root = path.parent
    data, body = _front(path.read_text())
    if body and not data.get("description"):
        data["description"] = " ".join(body.split())
    ids = data.get("steps") or sorted(p.parent.parent.name for p in root.glob("*/md/skill.md"))
    steps = []
    for sid in ids:
        f = root / sid / "md" / "skill.md"
        if not f.is_file():
            raise FlowError(f"{root}: step '{sid}' has no {sid}/md/skill.md")
        step, notes = _front(f.read_text())
        notes = _SECTION.sub("", notes).strip()  # (the named sections are the step's prompts, not notes for every task)
        step["id"] = sid
        code = root / sid / "step.py"
        if code.is_file() and not step.get("kind"):  # the step's own code (a class `Step`) is a kind of this flow
            from q3tui.steps import register_kind

            step["kind"] = f"{data.get('name') or root.name}.{sid}"
            register_kind(step["kind"], f"{code}:Step")
        if notes:
            step["notes"] = notes
        steps.append(step)
    data["steps"] = steps
    return data


def dump_folder(data: dict, path: Path) -> None:
    import yaml

    root = path.parent
    steps = data.get("steps", [])
    head = {k: v for k, v in data.items() if k not in ("steps", "description")}
    head["steps"] = [st["id"] for st in steps]
    path.write_text("---\n" + yaml.safe_dump(head, sort_keys=False, allow_unicode=True) + "---\n" + str(data.get("description") or "") + "\n")
    keep = {st["id"] for st in steps}
    for old in root.glob("*/md/skill.md"):
        if old.parent.parent.name not in keep:
            old.unlink()
    for st in steps:
        meta = {k: v for k, v in st.items() if k not in ("id", "notes")}
        (root / st["id"] / "md").mkdir(parents=True, exist_ok=True)
        old = root / st["id"] / "md" / "skill.md"
        secs = "".join(f"\n<!-- {n} -->\n{t}\n<!-- /{n} -->\n" for n, t in skill_sections(old).items())
        (root / st["id"] / "md" / "skill.md").write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n"
                                              + (st.get("notes") or "") + ("\n" if st.get("notes") else "") + secs)


def _read(path: Path) -> dict:
    text = path.read_text()
    try:
        if path.name == FOLDER_FILE:
            return parse_folder(path)
        if path.suffix == ".md":
            return parse_markdown(text, path)
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise FlowError(f"{path}: not readable ({exc})") from exc
    if not isinstance(data, dict):
        raise FlowError(f"{path}: a flow file is a mapping with `name` and `steps`")
    return data


def parse_flow(data: dict, path: Path | None = None) -> Flow:
    where = f"{path}: " if path else ""
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise FlowError(f"{where}`steps` must be a non-empty list")
    specs: list[StepSpec] = []
    for i, raw in enumerate(steps_raw):
        if isinstance(raw, str):
            raw = {"id": raw}
        if not isinstance(raw, dict):
            raise FlowError(f"{where}step #{i + 1} must be a mapping (or a kind name)")
        unknown = set(raw) - {"id", "kind", "title", "label", "deps", "gate", "options", "view", "pass", "notes"}
        if unknown:
            raise FlowError(f"{where}step '{raw.get('id', i + 1)}': unknown key(s) {', '.join(sorted(unknown))}")
        sid = raw.get("id") or raw.get("kind")
        if not sid or not isinstance(sid, str):
            raise FlowError(f"{where}step #{i + 1} has no `id` / `kind`")
        deps = raw.get("deps")
        if deps is not None and not (isinstance(deps, list) and all(isinstance(d, str) for d in deps)):
            raise FlowError(f"{where}step '{sid}': `deps` must be a list of step ids")
        gate = raw.get("gate")
        if gate is True:
            gate = "human"
        elif gate in (False, None):
            gate = None
        if gate is not None and gate not in GATE_MODES:
            raise FlowError(f"{where}step '{sid}': gate must be one of {', '.join(GATE_MODES)}")
        opts = raw.get("options") or {}
        if not isinstance(opts, dict):
            raise FlowError(f"{where}step '{sid}': `options` must be a mapping")
        lists = {}
        for key in ("view", "pass"):
            v = raw.get(key) or []
            v = [v] if isinstance(v, str) else v
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise FlowError(f"{where}step '{sid}': `{key}` must be a string or a list of strings")
            lists[key] = v
        specs.append(StepSpec(sid, raw.get("kind") or sid, raw.get("title"), None if raw.get("label") is None else str(raw["label"]),
                              deps, gate, opts, lists["view"], lists["pass"], str(raw.get("notes") or "")))
    flow = Flow(str(data.get("name") or (path.stem if path else "custom")), specs, str(data.get("title") or ""),
                str(data.get("description") or ""),
                dict(data.get("gap_targets") or {}), {k: list(v) for k, v in (data.get("sees") or {}).items()},
                list(data.get("tools") or []), int(data.get("version") or 1), path)
    flow.session = str(data.get("session") or "fresh")
    if flow.session not in ("fresh", "flow"):
        raise FlowError(f"{where}`session` must be 'flow' or 'fresh'")
    return flow


STEP_KEYS = ("view", "pass", "notes")


def apply_overrides(flow: Flow, overrides: dict[str, dict]) -> None:
    """q3tui.yaml `pipeline.step_flow` on top of the flow file's view / pass / notes (an empty value clears)."""
    for sid, kv in (overrides or {}).items():
        spec = flow.spec(sid)
        if spec is None or not isinstance(kv, dict):
            continue
        for key, value in kv.items():
            if key == "notes":
                spec.notes = str(value or "")
            elif key in ("view", "pass"):
                lst = [value] if isinstance(value, str) else list(value or [])
                setattr(spec, "view" if key == "view" else "passes", [str(x) for x in lst if str(x).strip()])


def check_pass(passes: list[str], root: Path) -> list[str]:
    """The pass conditions that do not hold (empty: all hold). One per line, paths relative to the project:
    `exists <glob>` (a match), `contains <file> <regex>`, `run <command>` (exit 0, in the project folder)."""
    out: list[str] = []
    for cond in passes:
        verb, _, rest = cond.strip().partition(" ")
        rest = rest.strip()
        ok = False
        if verb == "exists":
            ok = any(root.glob(rest))
        elif verb == "contains":
            name, _, rx = rest.partition(" ")
            try:
                ok = bool(re.search(rx.strip(), (root / name).read_text(errors="replace"), re.M))
            except (OSError, re.error):
                ok = False
        elif verb == "run":
            try:
                ok = subprocess.run(rest, shell=True, cwd=root, capture_output=True, timeout=600).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        else:
            out.append(f"{cond}  (unknown condition: use exists / contains / run)")
            continue
        if not ok:
            out.append(cond)
    return out


def dump_markdown(data: dict) -> str:
    import yaml

    steps = data.get("steps", [])
    head = {k: v for k, v in data.items() if k not in ("steps", "description")}
    out = ["---", yaml.safe_dump(head, sort_keys=False, allow_unicode=True).rstrip(), "---"]
    if data.get("description"):
        out += [str(data["description"]), ""]
    for st in steps:
        out += [f"## {st['id']}"]
        for key in ("kind", "label", "title", "deps", "gate", "options"):
            if key in st:
                out.append(f"- {key}: {json.dumps(st[key], ensure_ascii=False)}")
        for key in ("view", "pass"):
            out += [f"- {key}: {x}" for x in st.get(key, [])]
        out += ["", *([st["notes"], ""] if st.get("notes") else [])]
    return "\n".join(out).rstrip() + "\n"


def save_flow(flow: Flow, project_root: Path) -> Path:
    """Write a flow into the project's `flows/` (a project file of the same name wins over the user's and built-in one, so a
    built-in flow is forked, not changed). A project file keeps its format. Returns the file."""
    problems = validate(flow)
    if problems:
        raise FlowError("; ".join(problems))
    own = flow.path if flow.path and flow.path.is_file() and (project_root / "flows") in flow.path.parents else None
    path = own or (project_root / "flows" / f"{flow.name}.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = flow.to_file_dict()
    if path.name == FOLDER_FILE:
        dump_folder(data, path)
    elif path.suffix == ".md":
        path.write_text(dump_markdown(data))
    elif path.suffix == ".json":
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        import yaml

        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    flow.path = path
    return path


def search_dirs(project_root: Path | None) -> list[Path]:
    home = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser()
    return [d for d in ([project_root / "flows"] if project_root else []) + [home / "flows", BUILTIN_DIR]]


def find_flow(ref: str, project_root: Path | None = None) -> Path:
    """`ref`: a file path (relative to the project) or the name of a flow file in flows/ (project, user, built-in)."""
    if ref.endswith(FLOW_EXTS) or "/" in ref:
        for base in ([project_root] if project_root else []) + [Path.cwd()]:
            p = (base / ref).expanduser()
            if p.is_file():
                return p
        raise FlowError(f"flow file '{ref}' not found")
    for d in search_dirs(project_root):
        if (d / ref / FOLDER_FILE).is_file():
            return d / ref / FOLDER_FILE
        for ext in FLOW_EXTS:
            if (d / f"{ref}{ext}").is_file():
                return d / f"{ref}{ext}"
    raise FlowError(f"unknown flow '{ref}' (known: {', '.join(list_flows(project_root)) or 'none'})")


def list_flows(project_root: Path | None = None) -> dict[str, Path]:
    """name -> file; the project's flows win over the user's, which win over the built-in ones."""
    out: dict[str, Path] = {}
    for d in reversed(search_dirs(project_root)):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if (p / FOLDER_FILE).is_file():
                    out[p.name] = p / FOLDER_FILE
                elif p.suffix in FLOW_EXTS:
                    out[p.stem] = p
    return out


def load_flow(ref: str, project_root: Path | None = None) -> Flow:
    path = find_flow(ref, project_root)
    flow = parse_flow(_read(path), path)
    if path.name == FOLDER_FILE:  # a flow folder may bring an add-on (addon.py, tools.json, panels.py)
        from q3tui import addons

        addons.attach(flow)
    return flow


def validate(flow: Flow, known_kinds: set[str] | None = None) -> list[str]:
    """Problems that make a flow unrunnable (empty: fine): duplicate ids, unknown kinds / deps, cycles."""
    from q3tui.steps import KINDS

    kinds = known_kinds if known_kinds is not None else set(KINDS)
    problems: list[str] = []
    ids = [s.id for s in flow.steps]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"step id '{dup}' appears more than once")
    for s in flow.steps:
        if s.kind not in kinds:
            problems.append(f"step '{s.id}': unknown kind '{s.kind}' (kinds: {', '.join(sorted(kinds))})")
        for d in s.deps or []:
            if d not in ids:
                problems.append(f"step '{s.id}': depends on unknown step '{d}'")
    for k, target in flow.gap_targets.items():
        if target not in ids:
            problems.append(f"gap_targets.{k}: unknown step '{target}'")
    if not problems and len(_order(flow.steps, lambda s: s.deps or [])) != len(flow.steps):
        problems.append("the dependencies form a cycle")
    return problems


def _order(specs: list[StepSpec], deps_of) -> list[StepSpec]:
    """Stable topological order: a step keeps its place unless a dependency comes later in the file."""
    done: list[StepSpec] = []
    left = list(specs)
    names = {s.id for s in specs}
    while left:
        for s in left:
            if all(d in {x.id for x in done} or d not in names for d in deps_of(s)):
                done.append(s)
                left.remove(s)
                break
        else:
            break  # a cycle
    return done


def build_steps(flow: Flow) -> list:
    """Instantiate the flow's step kinds (execution order = dependency order, file order where free)."""
    from q3tui.steps import make_step

    problems = validate(flow)
    if problems:
        raise FlowError(f"flow '{flow.name}': " + "; ".join(problems))
    steps = []
    for spec in _order(flow.steps, lambda s: s.deps or []):
        step = make_step(spec.kind)
        step.name = spec.id
        if spec.deps is not None:
            step.deps = tuple(spec.deps)
        if spec.title:
            step.title = spec.title
        step.options = dict(spec.options)
        steps.append(step)
    return steps


def describe(flow: Flow) -> str:
    """The dependency graph as text (`q3tui flow check`)."""
    from q3tui.steps import make_step

    lines = [f"flow {flow.name}" + (f" — {flow.title}" if flow.title else "")]
    for spec in _order(flow.steps, lambda s: s.deps or []):
        try:
            deps = spec.deps if spec.deps is not None else list(make_step(spec.kind).deps)
        except Exception:  # noqa: BLE001
            deps = spec.deps or []
        gate = f"  [gate: {spec.gate}]" if spec.gate and spec.gate != "none" else ""
        extra = f"  [pass: {len(spec.passes)}]" if spec.passes else ""
        lines.append(f"  {spec.id:<12} {spec.kind:<14} ← {', '.join(deps) or '—'}{gate}{extra}")
    return "\n".join(lines)
