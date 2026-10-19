"""M10 role sessions (docs/spec/team.md): every LLM task of a role goes to that role's one persistent Claude session.

Four roles — architect, modeler, designer, verifier — each with one session that lives across tasks, runs and
restarts (`.q3tui/team/roles.json`) until `q3tui team reset`. A task is a new message in it:

- init once: the first task of a role starts its session with the project brief (the charter is the system prompt);
- the system prompt (the charter) and the tool list (the union of the role's tools) stay the same from task to task,
  so the prompt cache keeps working; a tool outside the current task answers that it is not available, built-in
  tools the task did not ask for are denied by the hook (runtime._path_guard);
- instructions and large blocks the session already has are replaced by a reference (dedupe);
- a task that starts while the role's main session is busy forks it (parallel units): the fork knows everything
  the role knew, and its summary goes to the main session with its next task;
- after Claude compacts the session, the next task carries the project brief again.

Independence is unchanged: every task keeps its own deny_dirs / write_dirs / write_files.
Reviewers, delegated sub-agents and the per-side investigators stay fresh sessions (`role_of` → None).
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from q3tui.llm import runtime
from q3tui.llm.runtime import Stage, StageResult
from q3tui.core.project import read_json, write_json

ROLES = ("architect", "modeler", "designer", "verifier", "reviewer", "flow")
# offered to every task of a role (the task's own list is enforced): only roles whose tasks write files get write tools —
# an offered tool a task may not use is a tool the model reaches for
ROLE_BUILTINS = {"architect": ["Read", "Grep", "Glob"], "modeler": ["Read", "Grep", "Glob", "Write", "Edit"],
                 "designer": ["Read", "Grep", "Glob", "Write", "Edit"], "verifier": ["Read", "Grep", "Glob"],
                 "reviewer": ["Read", "Grep", "Glob"], "flow": ["Read", "Grep", "Glob", "Write", "Edit"]}
_FRESH = re.compile(r"review|_section_|^arch_block_|_group_|^triage_(rtl|tb|model)$|_init$")


# unit-level work (one module's RTL, one block's model, one unit's plan / testbench): its own small session, resumed
# for that unit's fixes (the steps keep those per module / piece) — not the role's one session, which on rv32im grew
# to hundreds of k tokens that every stimulus turn re-read (team.unit_sessions: own)
UNIT_LEVEL = re.compile(r"^(unit_|rtl_(?!review)|model_|tb_(stimulus|checker)$|vplan_)")


def unit_level(stage_name: str) -> bool:
    return bool(UNIT_LEVEL.match(stage_name))


def role_of(stage_name: str) -> str | None:
    """The role a task belongs to (None: an independent, fresh session)."""
    if stage_name.startswith("rtl_review_"):  # the RTL reviewer: its own session, never the designer's
        return "reviewer"
    if _FRESH.search(stage_name):
        return None
    name = stage_name.removeprefix("unit_")
    if name.startswith(("spec_", "req_", "arch_")):
        return "architect"
    if name.startswith("model_"):
        return "modeler"
    if name.startswith("rtl_"):
        return "designer"
    if name.startswith(("vplan", "tb_")) or name == "triage":
        return "verifier"
    return None


CHARTER_COMMON = """
You are a persistent member of a chip design team run by Q3TUI. You keep this one session for the whole
project: tasks arrive as messages starting with "# Task:", each with its own instructions and output format —
follow them exactly for that task. What you learned in earlier tasks stays valid unless a task says it changed.

- Files are the truth, not your memory: read a file again before you change it when it may have changed since.
- The tools of a task are the ones its instructions name; others answer "not available in this task".
- Independence: the team members never read each other's work — you only see what a task lets you see (the hook
  enforces it). Never try to reach other folders.
- Keep short notes of your decisions, conventions and open issues in your NOTES.md (path in the project brief):
  update it with the `notes` tool (available in every task; one `section` per block / topic) when you decide something a
  later task must remember.
- A bug report is a claim, not an order: fix what is really yours; if your work already meets the requirement,
  change nothing and say why, citing the requirement."""

CHARTER_FLOW = """
You are the engineer of a whole RTL design flow run by Q3TUI (spec → requirements → configuration → RTL →
testbench → assertions → verification → documents), like one long Claude Code conversation: you keep this one
session for the entire flow, and what you learned in earlier tasks stays valid unless a task says it changed. Tasks
arrive as messages starting with "# Task:", each with its own instructions and output format — follow them exactly.

- Files are the truth, not your memory: read a file again before you change it when it may have changed since.
- The tools of a task are the ones its instructions name; others answer "not available in this task".
- Keep short notes of your decisions, conventions and open issues in your NOTES.md (path in the project brief):
  update it with the `notes` tool when you decide something a later task must remember.
- A bug report is a claim, not an order: fix what is really yours; if your work already meets the requirement,
  change nothing and say why, citing the requirement."""

CHARTERS = {
    "flow": CHARTER_FLOW,
    "architect": "You are the architect: you write the specification, the requirements (REQ-nnn) and the architecture "
                 "with its block contracts, and you decide what a requirement means when the others disagree."
                 + CHARTER_COMMON,
    "modeler": "You are the modeler: you write the Python golden model — one untimed class per block, with "
               "self-tests — from the spec, the requirements and the block contracts. You never see the RTL or the "
               "testbench." + CHARTER_COMMON,
    "designer": "You are the RTL designer: you write synthesizable SystemVerilog, one module per block, from the spec, "
                "the requirements and the block contracts, and you debug your RTL on bug reports (simulate, waves, "
                "probe). You never see the golden model or the testbench." + CHARTER_COMMON,
    "reviewer": "You are the RTL reviewer: you review each RTL module against the coding rules and the design "
                "requirements it implements, and report violations — you never write the RTL (the designer does), "
                "and you never see the golden model or the testbench. Hold every module to the same bar; remember "
                "what you flagged before and whether it was fixed." + CHARTER_COMMON,
    "verifier": "You are the verifier: you plan the verification, write the testbenches (the top and every block), "
                "run the simulations and debug failures first: your own testbench bugs you fix, RTL or model bugs "
                "you report to their owners in terms of the requirements. You never see the RTL or the model code."
                + CHARTER_COMMON,
}


COMPACT_KEEP = ("Keep: the design decisions and conventions you follow, each unit's / module's status and its open "
                "issues, what you changed and why, and what is still to do. Drop tool output, file contents and "
                "simulation logs (files are on disk; re-read what you need).")


def notes_path(project, role: str) -> Path:
    if role == "reviewer":  # (its own notes, next to the RTL it reviews)
        return project.rtl_dir / "REVIEW_NOTES.md"
    if role == "flow":
        return project.schemas_dir / "NOTES.md"
    return {"architect": project.arch_dir, "modeler": project.model_dir, "designer": project.rtl_dir,
            "verifier": project.tb_dir}[role] / "NOTES.md"


def role_denied(project, role: str) -> list[Path]:
    """What a role never reads (its init turn; every task adds its own deny_dirs)."""
    if role == "flow":  # one engineer, no independence between its tasks (each task still carries its own deny_dirs)
        return [project.state_dir]
    other = {"architect": [project.model_dir, project.rtl_dir, project.tb_dir],
             "modeler": [project.rtl_dir, project.tb_dir],
             "designer": [project.model_dir, project.tb_dir],
             "reviewer": [project.model_dir, project.tb_dir],
             "verifier": [project.rtl_dir, project.model_dir]}[role]
    return [*other, project.state_dir]


def brief(project, role: str) -> str:
    rel = project.rel
    if role == "flow":
        return (f"# Project brief\n\nProject `{project.root.name}` at {project.root}. Spec: {rel(project.spec_dir)}/; JSON artifacts: "
                f"{rel(project.schemas_dir)}/; RTL / SVA / testbench: {rel(project.src_dir)}/{{rtl,sva,tb}}/; documents: "
                f"{rel(project.docs_dir)}/ (each may not exist yet).\nYour notes: {rel(notes_path(project, role))}"
                + _notes_text(project, role))
    own = {"architect": f"{rel(project.spec_dir)}/, {rel(project.req_dir)}/, {rel(project.arch_dir)}/",
           "modeler": f"{rel(project.model_dir)}/", "designer": f"{rel(project.rtl_dir)}/",
           "reviewer": f"{rel(project.rtl_dir)}/ (reading and reviewing only)",
           "verifier": f"{rel(project.tb_dir)}/"}[role]
    never = ", ".join(f"{rel(d)}/" for d in role_denied(project, role))
    return (f"# Project brief\n\nProject `{project.root.name}` at {project.root}. The spec is in {rel(project.spec_dir)}/, "
            f"the requirements in {rel(project.req_dir)}/req.json, the architecture and block contracts in "
            f"{rel(project.arch_dir)}/architecture.json (each may not exist yet).\nYour work lives in {own}; "
            f"you never read {never}.\nYour notes: {rel(notes_path(project, role))}" + _notes_text(project, role))


def _notes_text(project, role: str, limit: int = 4000) -> str:
    """The role's notes, inline (reading them would cost turns a short task does not have)."""
    path = notes_path(project, role)
    text = path.read_text(errors="replace").strip() if path.is_file() else ""
    if not text:
        return " (none yet)."
    return ":\n\n" + (text if len(text) <= limit else text[:limit] + "\n… (the rest is in the file)")


@dataclass
class RoleState:
    session: str | None = None
    tasks: int = 0
    forks: int = 0
    compactions: int = 0
    cost_usd: float = 0.0
    sent: list[str] = field(default_factory=list)  # hashes of the blocks the main session has (dedupe)
    pending: list[str] = field(default_factory=list)  # summaries of finished forks, for the next main task
    needs_brief: bool = False
    totals: dict | None = None  # the main session's running totals as the SDK reports them (costs are the difference)
    rotations: int = 0  # sessions closed for size (team.max_context)
    idle_at: str | None = None  # the main session's last message after its last finished task: forks branch there


class Team:
    """The role sessions of one project (one per process: `team(project)`)."""

    def __init__(self, project):
        self.project = project
        self.path = project.state_dir / "team" / "roles.json"
        saved = read_json(self.path, default={}) or {}
        self.roles = {r: RoleState(**{k: v for k, v in (saved.get(r) or {}).items() if k in RoleState.__annotations__})
                      for r in ROLES}
        for st in self.roles.values():  # a restarted process: the session's totals so far are not counted again
            runtime.seed_totals(st.session, st.totals)
        self.busy: dict[str, bool] = dict.fromkeys(ROLES, False)
        self.tools: dict[str, dict[str, object]] = {r: {} for r in ROLES}  # name -> latest SdkMcpTool of the role

    def save(self) -> None:
        write_json(self.path, {r: dataclasses.asdict(s) for r, s in self.roles.items()})

    def reset(self, role: str | None = None) -> list[str]:
        names = [role] if role else list(ROLES)
        for r in names:
            self.roles[r] = RoleState()
        self.save()
        return names

    # -- a task -------------------------------------------------------------------------------------------------

    async def run(self, stage: Stage, role: str, cfg, emit) -> StageResult:
        st = self.roles[role]
        # init once: the role's first task starts its session with the project brief (no separate init turn — one
        # with write tools in view started implementing "while getting to know the project")
        target = stage.resume or st.session
        on_main = target == st.session
        # one task at a time on the role's session: parallel tasks queue (a fork would inherit an unfinished task and
        # rewrite the whole session to the cache); only a task started from inside a task of the same role forks
        nested = role in _ACTIVE.get()
        # team.fork: parallel tasks (independent units of one level) fork the session at its last idle point — they know
        # everything the role knew then, never an unfinished task; without a known idle point they queue
        # (the flow's one engineer works one task at a time, like one conversation: parallel tasks queue)
        if on_main and not nested and (role == "flow" or not cfg.team.fork or st.idle_at is None):
            while self.busy[role]:
                await anyio.sleep(0.2)
        fork = on_main and self.busy[role]
        main = on_main and not fork
        sent = set(st.sent) if on_main else set()
        parts = []
        if main and st.pending:
            parts.append("Meanwhile, these tasks of yours were done elsewhere (their files are on disk):\n"
                         + "\n".join(f"- {p}" for p in st.pending))
        if on_main and (st.needs_brief or st.session is None):
            parts.append(brief(self.project, role))
            sent = set()
        parts.append(f"# Task: {stage.name}")
        parts.append(self._dedupe(f"## Instructions for this task\n\n{stage.system_prompt}", sent, cfg, whole=True,
                                  ref=f"## Instructions for this task\n\n(the same as for your earlier `{_kind(stage.name)}` "
                                      f"tasks, above)"))
        # reading is always allowed (a denied Read is a wasted turn), inside the role's area: its hard limits hold
        # for every task, also those built without file tools (the verifier's triage never opens rtl/ or model/)
        deny = list(dict.fromkeys([*stage.deny_dirs, *role_denied(self.project, role)]))
        can = list(dict.fromkeys([*stage.builtin_tools, "Read", "Grep", "Glob"]))
        # a task built without file tools (a judge answering from its prompt) may read here: turns for that
        extra = 6 if stage.max_turns and not stage.builtin_tools else 0
        stage = dataclasses.replace(stage, deny_dirs=deny, max_turns=(stage.max_turns + extra) if stage.max_turns else None)
        writes = [t for t in can if t in runtime.WRITE_TOOLS]
        parts.append(f"Tools for this task: {', '.join(can)}" + ("" if writes else " (read only: change no files except "
                                                                                    "your NOTES.md)") + ", notes, submit.")
        note = runtime.access_note(stage).strip()
        if note:
            parts.append(note)
        # (the flow's one engineer gets every task's prompt in full: a task's facts — a failing test's log, a diagnostic —
        # are what it must read, and a stub "as sent to you earlier" for text it saw in another context hid exactly that)
        parts.append(stage.prompt if role == "flow" else self._dedupe(stage.prompt, sent, cfg))
        model = stage.output_model
        if model is not None:  # the result goes through the one `submit` tool: the tool list stays the same
            schema = json.dumps(runtime.output_schema(model), separators=(",", ":"))
            parts.append(self._dedupe(
                f"## Result\n\nWhen the task is done, call `submit` with `result` = a JSON object of this schema (this "
                f"replaces any StructuredOutput the instructions mention):\n```json\n{schema}\n```", sent, cfg, whole=True,
                ref=f"## Result\n\nCall `submit` with the result (the schema of your earlier `{_kind(stage.name)}` tasks, above)."))
        else:
            parts.append("## Result\n\nWhen the task is done, answer in text (no `submit`).")
        box: dict = {}
        notes = notes_path(self.project, role)
        task = dataclasses.replace(
            stage, system_prompt=CHARTERS[role], prompt="\n\n".join(parts), role=role, resume=target, fork=fork,
            fork_at=st.idle_at if (fork and not nested) else None,
            task_tools=can, builtin_tools=list(ROLE_BUILTINS[role]), sdk_tools=self._union(role, stage, box, model),
            write_dirs=[*stage.write_dirs, notes] if stage.write_dirs is not None else None,
            write_files=[*stage.write_files, notes] if stage.write_files is not None else None,
            on_compact=(lambda: self._compacted(role)) if main else None, notes_file=notes, submit_tool=True)
        if main:
            self.busy[role] = True
            st.pending, st.needs_brief = [], False
            st.sent = sorted(sent)
        token = _ACTIVE.set(_ACTIVE.get() | {role})
        try:
            result = await self._attempt(task, model, box, cfg, emit)
        except runtime.StageError as exc:  # one bad turn must not stop the run: the task once more, in a fresh session
            emit("warning", message=f"{stage.name}: failed in the {role}'s session ({str(exc).splitlines()[0][:200]}); "
                                    f"trying it once in a fresh session")
            if main:
                self.busy[role] = False
            st.cost_usd += exc.cost_usd
            result = await runtime.run_stage(stage, cfg, emit)
            result.cost_usd += exc.cost_usd
            st.pending.append(f"{stage.name}: failed in your session and was done by a fresh session "
                              f"({' '.join(result.text.split())[:300] or 'done'}); its files are on disk")
            self.save()
            return result
        finally:
            _ACTIVE.reset(token)
            if main:
                self.busy[role] = False
        st.tasks += 1
        st.cost_usd += result.cost_usd
        if main:
            st.session = result.session_id or st.session
            st.idle_at = result.last_uuid or st.idle_at
            self._register_products(role, st, cfg)
            st.totals = runtime.session_totals(st.session) or st.totals
            if cfg.team.max_context and result.context_tokens > cfg.team.max_context:  # opted in: compact it now
                await self._compact(role, stage.cwd, result.context_tokens, cfg, emit)
        elif fork:
            st.forks += 1
            st.pending.append(f"{stage.name}: {' '.join(result.text.split())[:500] or 'done'}")
        self.save()
        return result

    async def _attempt(self, task: Stage, model, box: dict, cfg, emit) -> StageResult:
        """Run the task; its result is what it passed to `submit` (asked for again, up to twice, when it ended without)."""
        result = await runtime.run_stage(task, cfg, emit)
        if model is None or result.output is not None:
            return result
        cost, turns = result.cost_usd, result.num_turns
        for _ in range(2):
            if "output" in box or not result.session_id:
                break
            emit("warning", message=f"{task.name}: ended without `submit`; asking for the result")
            result = await runtime.run_stage(dataclasses.replace(
                task, prompt="Call `submit` with this task's result now (its schema is in the task above).",
                resume=result.session_id, fork=False), cfg, emit)
            cost, turns = cost + result.cost_usd, turns + result.num_turns
        if "output" not in box:
            raise runtime.StageError(f"{task.name}: no valid result was submitted ({box.get('problems', 'no submit call')})", cost)
        return dataclasses.replace(result, output=box["output"], cost_usd=cost, num_turns=turns)

    async def _compact(self, role: str, cwd: Path, size: int, cfg, emit) -> None:
        """The role's session grew past team.max_context in its task: Claude compacts it (the same session: its
        memory as a summary, old tool output gone); the next task gets the brief and the notes again. If compacting
        fails, the next task starts a fresh session (brief + notes)."""
        st = self.roles[role]
        emit("log", message=f"{role}: compacting its session (~{size // 1000}k tokens of context > team.max_context)")
        compact = Stage(name=f"team_{role}_compact", system_prompt=CHARTERS[role], cwd=cwd, role=role, resume=st.session,
                        prompt=f"/compact {COMPACT_KEEP}", builtin_tools=list(ROLE_BUILTINS[role]), task_tools=[],
                        on_compact=lambda: self._compacted(role))
        self.busy[role] = True
        try:
            done = await runtime.run_stage(compact, cfg, emit)
            st.cost_usd += done.cost_usd
            st.totals = runtime.session_totals(st.session) or st.totals
            st.needs_brief, st.sent = True, []
            st.idle_at = None  # (the next main task sets the new one; forks queue until then)
        except runtime.StageError as exc:
            emit("warning", message=f"{role}: compacting failed ({str(exc)[:160]}); the next task starts a fresh session")
            st.session, st.sent, st.totals, st.pending, st.idle_at = None, [], None, [], None
            st.rotations += 1
        finally:
            self.busy[role] = False
        self.save()

    def _compacted(self, role: str) -> None:
        st = self.roles[role]
        st.compactions += 1
        st.needs_brief, st.sent = True, []
        self.save()

    def _dedupe(self, text: str, sent: set[str], cfg, whole: bool = False, ref: str = "") -> str:
        """Blocks the session already has, unchanged, become a reference (they are in its context)."""
        limit = cfg.team.dedupe_chars
        if not limit:
            return text
        if whole:
            h = _hash(text)
            if h in sent:
                return ref
            sent.add(h)
            return text

        def paragraphs(section: str) -> str:  # inside a changed section: its unchanged long paragraphs still are
            parts = []
            for par in section.split("\n\n"):
                h = _hash(par)
                if len(par) >= limit and h in sent:
                    parts.append(f"[unchanged, as sent to you earlier in this session: «{par.strip().splitlines()[0][:100]}» …]")
                else:
                    if len(par) >= limit:
                        sent.add(h)
                    parts.append(par)
            return "\n\n".join(parts)

        out = []
        for block in _blocks(text):
            h = _hash(block)
            if len(block) >= limit and h in sent:
                first = block.strip().splitlines()[0][:100]
                out.append(f"[unchanged, as sent to you earlier in this session: «{first}» …]")
                continue
            if len(block) >= limit:
                sent.add(h)
            out.append(paragraphs(block))
        return "\n".join(out)

    def _register_products(self, role: str, st: "RoleState", cfg) -> None:
        """What the role's own task wrote is in its session: its sections are 'sent' (a later task that quotes them
        gets a reference). rv32im: the requirements task re-sent the whole spec the same session had just written."""
        limit = cfg.team.dedupe_chars
        if not limit:
            return
        for rel in ROLE_PRODUCTS.get(role, ()):
            path = self.project.root / rel
            if path.is_file():
                blocks = _blocks(path.read_text())
                pieces = [*blocks, *(par for b in blocks for par in b.split("\n\n"))]
                st.sent.extend(dict.fromkeys(h for h in (_hash(x) for x in pieces if len(x) >= limit) if h not in st.sent))

    def _union(self, role: str, stage: Stage | None, box: dict | None = None, model=None) -> list:
        """The role's tools so far: this task's own, the others answering that they are not available now (`notes` and
        `submit` first, in every task: the list only grows, in the same order — the prompt cache)."""
        known = self.tools[role]
        if not known:
            known["notes"] = _notes_tool(notes_path(self.project, role))
            known["submit"] = _submit_tool({}, None)
        mine = {"notes": known["notes"], "submit": _submit_tool(box if box is not None else {}, model),
                **{t.name: t for t in (stage.sdk_tools if stage else [])}}
        known.update(mine)
        return [mine.get(name) or dataclasses.replace(t, handler=_unavailable(name)) for name, t in known.items()]


def _blocks(text: str) -> list[str]:
    """A prompt's blocks for the dedupe: its markdown sections (a heading and what follows, up to the next heading) —
    spec paragraphs are mostly shorter than any useful threshold, sections are what a later task quotes again."""
    return re.split(r"\n(?=#{1,6} )", text)


# what a role's own tasks write (through its tools): in its session, so never re-sent in full
ROLE_PRODUCTS = {"architect": ("spec/spec.md",)}


def set_section(doc: str, section: str, text: str) -> str:
    """`## <section>` of a notes file set to `text` (added at the end when new); the rest unchanged. The notes tool
    only rewrote the whole file: the modeler appended a block's section, wiped the others, and re-sent them all —
    every block (rv32im: "I made the same mistake again — the notes tool overwrites the whole file")."""
    body = re.sub(r"^#+\s*" + re.escape(section) + r"\s*\n", "", text.strip(), flags=re.I)  # (a heading of its own)
    block = f"## {section}\n\n{body}\n"
    parts = re.split(r"(?m)^(?=## )", doc)
    for i, part in enumerate(parts):
        if part and part.splitlines()[0].strip().lower() == f"## {section}".lower():
            parts[i] = block + ("\n" if i < len(parts) - 1 else "")
            return "".join(parts)
    return (doc.rstrip() + "\n\n" if doc.strip() else "") + block


def _notes_tool(path: Path):
    from claude_agent_sdk import tool

    @tool("notes", "Your NOTES.md (decisions, conventions, open issues — short). It is read again after your session is "
                   "compacted. `section` (e.g. a block or topic name): sets just that section — added when new, replaced "
                   "when present, every other section kept (the usual way: one section per block / topic). No section: "
                   "`text` replaces the whole file (a clean-up).",
          {"type": "object", "properties": {"section": {"type": "string"}, "text": {"type": "string"}}, "required": ["text"]})
    async def notes(args: dict) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        text, section = (args.get("text") or "").strip(), (args.get("section") or "").strip()
        if not section:
            path.write_text(text + "\n")
        else:
            path.write_text(set_section(path.read_text() if path.is_file() else "", section, text))
        return {"content": [{"type": "text", "text": f"saved {path.name} ({len(path.read_text())} characters"
                                                     + (f"; section '{section}'" if section else "") + ")"}]}

    return notes


def _submit_tool(box: dict, model):
    from claude_agent_sdk import tool
    from pydantic import ValidationError

    @tool("submit", "Return this task's result: `result` = a JSON object of the schema given in the task. It is checked "
                    "at once: when the answer lists problems, fix them and call submit again.",
          {"type": "object", "properties": {"result": {"type": "object"}}, "required": ["result"]})
    async def submit(args: dict) -> dict:
        data = args.get("result")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass
        if model is None:
            return _text("this task answers in text: no submit needed")
        try:
            box["output"] = model.model_validate(data)
        except ValidationError as exc:
            box["problems"] = "; ".join(f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()[:20])
            return _text(f"not accepted — fix and call submit again: {box['problems']}")
        return _text("accepted: the task is done (end with a one-line summary)")

    return submit


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _unavailable(name: str):
    async def handler(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"`{name}` is not available in this task (it belongs to another of "
                                                     f"your tasks); use this task's tools"}]}

    return handler


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _kind(name: str) -> str:
    """A task's kind: its name without the unit (rtl_fifo_egress → rtl)."""
    return name.split("_")[0] if name.startswith(("rtl_", "model_")) else name


_TEAMS: dict[Path, Team] = {}
_ACTIVE: contextvars.ContextVar[frozenset] = contextvars.ContextVar("q3tui_active_roles", default=frozenset())


def team(project) -> Team:
    key = project.root.resolve()
    if key not in _TEAMS:
        _TEAMS[key] = Team(project)
    return _TEAMS[key]


def forget(project) -> None:
    """Drop the in-process state (tests; `q3tui team reset` from another process)."""
    _TEAMS.pop(project.root.resolve(), None)
