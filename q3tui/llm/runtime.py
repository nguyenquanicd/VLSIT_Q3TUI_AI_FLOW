"""LLM stage runner on the Claude Agent SDK.

One LLM stage = one `claude_agent_sdk.query()` with a stage-specific system prompt, a
restricted tool allow-list, optional in-process Q3TUI MCP tools, and optional JSON
schema output. Every SDK message is appended to the run's events.jsonl.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import BaseModel, ValidationError

from q3tui.core.config import Config
from q3tui.core.events import StepEmitter

if TYPE_CHECKING:  # the SDK (and its MCP stack) takes ~0.6 s to import: only load it to run a stage
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SdkMcpTool


def query(*, prompt: str, options: ClaudeAgentOptions):
    """claude_agent_sdk.query, imported on first use (tests replace this function)."""
    from claude_agent_sdk import query as sdk_query

    return sdk_query(prompt=prompt, options=options)

Q3TUI_MCP = "q3tui"


class StageError(RuntimeError):
    def __init__(self, message: str, cost_usd: float = 0.0):
        super().__init__(message)
        self.cost_usd = cost_usd


OUTPUT_RETRIES = 2  # re-ask (same session) when structured output fails schema validation


@dataclass
class Stage:
    name: str
    system_prompt: str
    prompt: str
    cwd: Path
    builtin_tools: list[str] = field(default_factory=lambda: ["Read", "Grep", "Glob"])
    sdk_tools: list[SdkMcpTool] = field(default_factory=list)
    output_model: type[BaseModel] | None = None
    add_dirs: list[Path] = field(default_factory=list)
    max_turns: int | None = None
    resume: str | None = None  # Agent-SDK session id to continue (assistant)
    deny_dirs: list[Path] = field(default_factory=list)  # never readable, even inside cwd
    write_dirs: list[Path] | None = None  # when set, Edit/Write only inside these
    write_files: list[Path] | None = None  # when set, Edit/Write only these exact files (stricter than write_dirs)
    effort: str | None = None  # overrides the configured effort (e.g. one this stage is known to need)
    timeout_s: int | None = None  # overrides llm.timeout_s (none by default: no time limit)
    # M10 role sessions (llm/roles.py): the role this task runs in, the built-in tools the task itself may use (the
    # session offers the role's union; the hook denies the rest), fork the resumed session, called on a compaction
    role: str | None = None
    task_tools: list[str] | None = None
    fork: bool = False
    fork_at: str | None = None  # with fork: branch the session at this message (its last idle point), not its live end
    on_compact: Any = None
    notes_file: Path | None = None  # the role's NOTES.md: always writable, whatever the task's own tools
    submit_tool: bool = False  # the result comes through the role's `submit` tool, not the SDK's structured output
    text_json: bool = False  # the result as a JSON block in the reply (after the SDK's structured output failed)

    @property
    def allowed_dirs(self) -> list[Path]:
        return [self.cwd, *self.add_dirs]


@dataclass
class StageResult:
    text: str
    output: BaseModel | None
    cost_usd: float
    num_turns: int
    session_id: str | None
    effort: str | None = None  # the effort the answer came from
    context_tokens: int = 0  # the session's size at the end of the query (input tokens per turn, cached or not)
    last_uuid: str | None = None  # the session's last message: a later fork branches here (its idle point)


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """pydantic JSON schema → self-contained structured-output schema.

    Inlines $defs, drops title/default, and closes every object. Only fields without a default
    are required: a model that leaves out an optional field (a summary, an empty list) must not
    fail the stage after five schema retries.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            out = {}
            for k, v in node.items():
                if k == "properties":  # keys here are field names, not schema keywords
                    out[k] = {name: fix(sub) for name, sub in v.items()}
                elif k not in ("title", "default"):
                    out[k] = fix(v)
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = [r for r in node.get("required", []) if r in out["properties"]]
            return out
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(raw)


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {"type": type(obj).__name__, **{f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def _extract_json(text: str) -> Any:
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else text[text.find("{") : text.rfind("}") + 1]
    return json.loads(candidate, strict=False)  # (raw newlines inside strings: the usual slip in long Markdown values)


def _short(value: Any, limit: int = 120) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= limit else s[: limit - 1] + "…"


FILE_TOOLS = "Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit"


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def check_tool_paths(stage: Stage, tool_input: dict[str, Any], tool_name: str | None = None) -> str | None:
    """Return a denial reason if a file tool reaches outside the stage's allowed dirs."""
    allowed = [d.resolve() for d in stage.allowed_dirs]
    denied = [d.resolve() for d in stage.deny_dirs]
    candidates = [tool_input[k] for k in ("file_path", "path", "notebook_path") if isinstance(tool_input.get(k), str)]
    if tool_name == "Grep":
        # a search over a folder that CONTAINS a denied one would read into it (the hook only sees the root):
        # search the allowed folders instead
        root = Path(tool_input.get("path") or stage.cwd).expanduser()
        root = (root if root.is_absolute() else stage.cwd / root).resolve()
        inside = [d for d in denied if d != root and d.is_relative_to(root)]
        if inside:
            ok = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                        and not _within(p.resolve(), denied)) if root.is_dir() else []
            return (f"searching {tool_input.get('path') or 'the project'} would include "
                    f"{', '.join(d.name for d in inside)}, which this task may not see; search one of these instead: "
                    f"{', '.join(ok) or '(none)'} (path=\"<folder>\")")
    pattern = tool_input.get("pattern")
    if isinstance(pattern, str) and pattern.startswith(("/", "~")):
        candidates.append(pattern.split("*", 1)[0] or "/")
    for raw in candidates:
        p = Path(raw).expanduser()
        p = (p if p.is_absolute() else stage.cwd / p).resolve()
        if _within(p, denied):
            return f"{raw} is not accessible to this stage"
        if not _within(p, allowed):
            roots = ", ".join(str(r) for r in allowed)
            return f"{raw} is outside the project; only these directories are accessible: {roots}"
        if tool_name in WRITE_TOOLS and stage.write_dirs is not None and not _within(p, [d.resolve() for d in stage.write_dirs]):
            return f"{raw} may not be modified here; writable: {', '.join(str(d) for d in stage.write_dirs)}"
        if tool_name in WRITE_TOOLS and stage.write_files is not None and p not in [f.resolve() for f in stage.write_files]:
            return f"{raw} may not be written; this task may only write: {', '.join(str(f) for f in stage.write_files)}"
    return None


def _path_guard(stage: Stage, emit: StepEmitter | None):
    async def hook(hook_input, tool_use_id, context):
        name = hook_input.get("tool_name")
        target = (hook_input.get("tool_input") or {}).get("file_path")
        if stage.notes_file is not None and name in WRITE_TOOLS | {"Read"} and isinstance(target, str) \
                and (stage.cwd / Path(target).expanduser()).resolve() == stage.notes_file.resolve():
            return {}
        if stage.task_tools is not None and name not in stage.task_tools and not str(name).startswith("mcp__"):
            reason = f"{name} is not available in this task (it has: {', '.join(stage.task_tools) or 'no file tools'})"
        else:
            reason = check_tool_paths(stage, hook_input.get("tool_input") or {}, name)
        if reason is None:
            return {}
        if emit is not None:
            emit("warning", message=f"{stage.name}: blocked {hook_input.get('tool_name')}: {reason}")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}

    return hook


def safe_glob(stage: Stage, pattern: str, path: str | None = None, limit: int = 300) -> str:
    """Glob that only lists what the stage may see. The built-in Glob ignores .ignore and a
    hook cannot filter results, so it would show .q3tui/ backups (and denied dirs) to the agent."""
    base = Path(path).expanduser() if path else stage.cwd
    base = (base if base.is_absolute() else stage.cwd / base).resolve()
    allowed = [d.resolve() for d in stage.allowed_dirs]
    denied = [d.resolve() for d in stage.deny_dirs]
    if not _within(base, allowed) or _within(base, denied):
        return f"{path} is not accessible to this stage"
    if pattern.startswith(("/", "~")) or ".." in Path(pattern).parts:
        return "use a pattern relative to the path, e.g. **/*.md"
    found = []
    for p in sorted(base.glob(pattern)):
        rp = p.resolve()
        if not p.is_file() or _within(rp, denied) or "__pycache__" in p.parts or any(part.startswith(".") for part in p.relative_to(base).parts):
            continue
        found.append(str(p.relative_to(stage.cwd)) if p.is_relative_to(stage.cwd) else str(p))
        if len(found) >= limit:
            found.append(f"… (first {limit} shown; narrow the pattern)")
            break
    return "\n".join(found) or "No files found"


def _glob_tool(stage: Stage):
    from claude_agent_sdk import tool

    @tool("glob", "Find files by glob pattern (e.g. \"**/*.md\", \"model/tests/test_*.py\"), optionally under a directory "
                  "(`path`). Lists only files this task may use.", {"pattern": str, "path": str})
    async def glob(args: dict) -> dict:
        text = safe_glob(stage, args.get("pattern") or "*", args.get("path") or None)
        return {"content": [{"type": "text", "text": text}]}

    return glob


def capped(tool: SdkMcpTool, limit: int) -> SdkMcpTool:
    """The same tool with its text output cut to `limit` characters (head and tail kept): a tool result is
    re-sent with every later turn of the session."""
    handler = tool.handler

    async def wrapped(args: dict) -> dict:
        out = await handler(args)
        items = []
        for c in out.get("content", []):
            text = c.get("text") if isinstance(c, dict) else None
            if text is not None and len(text) > limit:
                head, tail = text[: limit * 2 // 3], text[-limit // 3:]
                c = {**c, "text": f"{head}\n… [{len(text) - len(head) - len(tail)} characters cut by Q3TUI] …\n{tail}"}
            items.append(c)
        return {**out, "content": items}

    return dataclasses.replace(tool, handler=wrapped)


def access_note(stage: Stage) -> str:
    """What this task may not read, and where to search."""
    if not stage.deny_dirs:
        return ""
    dirs = ", ".join(str(d) for d in stage.deny_dirs)
    note = f"\n\nDo not read or search these directories; they are not part of this task: {dirs}"
    denied = {Path(d).resolve() for d in stage.deny_dirs}
    try:
        searchable = sorted(p.name for p in Path(stage.cwd).iterdir()
                            if p.is_dir() and not p.name.startswith(".") and p.resolve() not in denied)
    except OSError:
        searchable = []
    if searchable:  # the project root also holds folders this task may not see: say where searching is allowed
        note += (f"\nSearch (Grep / Glob) inside these folders only, never the project root: "
                 f"{', '.join(f'{a}/' for a in searchable)} (pass path=<folder>).")
    return note


def build_options(stage: Stage, cfg: Config, emit: StepEmitter | None = None) -> ClaudeAgentOptions:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, create_sdk_mcp_server

    mcp_servers: dict[str, Any] = {}
    builtin = [t for t in stage.builtin_tools if t != "Glob"]  # replaced by the filtered glob below
    sdk_tools = list(stage.sdk_tools) + ([_glob_tool(stage)] if "Glob" in stage.builtin_tools else [])
    sdk_tools = [capped(t, cfg.llm.tool_output_limit) for t in sdk_tools]
    allowed = list(builtin)
    if sdk_tools:
        mcp_servers[Q3TUI_MCP] = create_sdk_mcp_server(Q3TUI_MCP, tools=sdk_tools)
        allowed += [f"mcp__{Q3TUI_MCP}__{t.name}" for t in sdk_tools]
    for server in cfg.mcp_servers:
        if server.type == "stdio":
            mcp_servers[server.name] = {"type": "stdio", "command": server.command, "args": server.args, "env": server.env}
        else:
            mcp_servers[server.name] = {"type": server.type, "url": server.url, "headers": server.headers}
        allowed.append(f"mcp__{server.name}")

    # a role session's system prompt is its charter, the same for every task (the prompt cache): the access note
    # goes into the task's message instead (llm/roles.py)
    system_prompt = stage.system_prompt if stage.role else stage.system_prompt + access_note(stage)

    # the step's own model / effort when one is set (llm.steps.<step>), else the global ones
    llm = cfg.llm.for_stage(emit.step if emit is not None else None, stage.name)
    if stage.effort and llm.effort:  # (only for models that take an effort)
        llm = llm.model_copy(update={"effort": stage.effort})
    thinking = None
    if llm.effort is None and llm.thinking_budget is not None:  # effort steers thinking when it is set
        budget = llm.thinking_budget
        thinking = {"type": "disabled"} if budget == 0 else {"type": "enabled", "budget_tokens": max(1024, budget)}

    return ClaudeAgentOptions(
        model=llm.model,
        fallback_model=cfg.llm.fallback_model,
        effort=llm.effort,
        thinking=thinking,
        system_prompt=system_prompt,
        tools=builtin,
        allowed_tools=allowed,
        permission_mode="dontAsk",
        mcp_servers=mcp_servers,
        cwd=str(stage.cwd),
        add_dirs=[str(d) for d in stage.add_dirs],
        max_turns=None,  # like Claude Code: Claude decides when the task is done (stage / config turn limits ignored)
        max_budget_usd=cfg.llm.max_budget_usd,
        output_format=({"type": "json_schema", "schema": output_schema(stage.output_model)}
                       if stage.output_model and not stage.submit_tool and not stage.text_json else None),
        # a tool may run for minutes (the architect's `delegate` runs sub-architects): wait as long as the stage may
        env={"MCP_TOOL_TIMEOUT": str((cfg.llm.timeout_s or 7 * 24 * 3600) * 1000), **cfg.llm.env},
        setting_sources=[],  # isolate from the user's own Claude Code settings/CLAUDE.md
        resume=stage.resume,
        fork_session=bool(stage.fork and stage.resume),
        resume_session_at=stage.fork_at if (stage.fork and stage.resume) else None,
        hooks={"PreToolUse": [HookMatcher(matcher=FILE_TOOLS, hooks=[_path_guard(stage, emit)])]},
    )


async def _query_once(prompt: str, options: ClaudeAgentOptions, stage: Stage, emit: StepEmitter
                      ) -> tuple[ResultMessage | None, list[str], str | None]:
    """One query, streamed as events. Like Claude Code: the model decides how long it thinks and the effort stays what
    was configured — no thinking limit, no watchdog, no lowered effort (only llm.timeout_s bounds a stage)."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock, UserMessage

    result: ResultMessage | None = None
    texts: list[str] = []
    last_progress = 0.0
    last_uuid: str | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
            emit("log", message=f"{stage.name}: the session's context was compacted")
            if stage.on_compact:
                stage.on_compact()
        if isinstance(message, SystemMessage) and message.subtype == "thinking_tokens":
            now = time.monotonic()
            if now - last_progress >= 1.0:  # front ends show it; not written to the log
                last_progress = now
                emit("progress", stage=stage.name, thinking_tokens=(message.data or {}).get("estimated_tokens"))
            continue
        emit("sdk", stage=stage.name, message=_jsonable(message))
        if isinstance(message, UserMessage) and "Output token limit hit" in str(message.content)[:200]:
            emit("log", message=f"{stage.name}: the answer hit the output token limit; the model continues in a new response")
        if isinstance(message, AssistantMessage):
            last_uuid = getattr(message, "uuid", None) or last_uuid
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    texts.append(block.text)
                    emit("llm_text", stage=stage.name, text=block.text.strip())
                elif isinstance(block, ToolUseBlock) and block.name != "StructuredOutput":
                    emit("tool_call", stage=stage.name, tool=block.name, input=_short(block.input))
        elif isinstance(message, ResultMessage):
            result = message
    return result, texts, last_uuid


# A resumed (or forked) session reports the session's running totals, not this query's: every result is recorded
# as the difference to what the session had reported before (in-process; role sessions persist theirs).
_TOTALS: dict[str, dict] = {}


def session_totals(session_id: str | None) -> dict | None:
    return _TOTALS.get(session_id) if session_id else None


def seed_totals(session_id: str, totals: dict | None) -> None:
    if session_id and totals and session_id not in _TOTALS:
        _TOTALS[session_id] = totals


def _delta(result, base: dict | None) -> tuple[float, dict]:
    """(cost, model_usage) of this query alone."""
    cost = result.total_cost_usd or 0.0
    usage = {k: dict(v) for k, v in (getattr(result, "model_usage", None) or {}).items()}
    if base:
        cost = max(0.0, cost - base.get("cost", 0.0))
        for name, prev in (base.get("model_usage") or {}).items():
            row = usage.get(name)
            if row is None:
                continue
            for key, value in prev.items():
                if isinstance(value, (int, float)) and isinstance(row.get(key), (int, float)):
                    row[key] = max(0, row[key] - value)
    return cost, usage


def _summary_only(model) -> bool:
    """A model that can be built from a summary alone (every other field has a default)."""
    fields = getattr(model, "model_fields", {})
    return "summary" in fields and all(not f.is_required() for n, f in fields.items() if n != "summary")


def _validate(stage: Stage, result: ResultMessage, texts: list[str]) -> tuple[BaseModel | None, str | None]:
    """Return (output, error). error is a message suitable to send back to the model."""
    data = result.structured_output
    if data is None:
        try:
            data = _extract_json(result.result or "\n".join(texts))
        except (ValueError, json.JSONDecodeError):
            text = (result.result or "\n".join(texts)).strip()
            if text and _summary_only(stage.output_model):
                # an end-of-session answer whose fields all have defaults (the work is on disk, checked by code):
                # the text is the summary — re-asking would re-send the whole session for nothing
                return stage.output_model(summary=text[-2000:]), None
            return None, ("No JSON block was found. Reply with the complete result as one JSON object in a ```json block."
                          if stage.text_json else
                          "No structured output was returned. Call the StructuredOutput tool with the complete result.")
    try:
        return stage.output_model.model_validate(data), None
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()[:20])
        how = "Reply again with the complete, corrected object in a ```json block" if stage.text_json else \
            "Call StructuredOutput again with the complete, corrected object"
        return None, f"The structured output failed schema validation ({problems}). {how} — every required field present."


async def run_stage(stage: Stage, cfg: Config, emit: StepEmitter) -> StageResult:
    """Run one LLM stage; stream progress as events; return validated output.

    If the structured output does not validate, the same session is resumed with the
    validation errors (up to OUTPUT_RETRIES times) before giving up.
    """
    options = build_options(stage, cfg, emit)
    emit("stage", name=stage.name, status="llm_start", model=cfg.llm.model)
    try:
        return await _run_stage(stage, cfg, emit, options)
    except StageError as exc:
        if stage.text_json or not stage.output_model or "valid structured output" not in str(exc):
            raise
        # the SDK's structured output gave up (long Markdown values break its JSON): once more, the result as a JSON
        # block in the reply — parsed leniently, validated here, asked again on problems
        emit("warning", message=f"{stage.name}: the structured output failed ({str(exc).splitlines()[0][:160]}); "
                                f"asking for the result as a JSON block instead")
        retry = dataclasses.replace(stage, text_json=True, prompt=stage.prompt + TEXT_JSON.format(
            schema=json.dumps(output_schema(stage.output_model), separators=(",", ":"))))
        result = await run_stage(retry, cfg, emit)
        result.cost_usd += exc.cost_usd
        return result
    except TimeoutError as exc:
        raise StageError(f"{stage.name}: did not finish within {stage.timeout_s or cfg.llm.timeout_s} s (llm.timeout_s)", 0.0) from exc


TEXT_JSON = """

## Your result
Reply with the result as ONE JSON object in a ```json fenced block — nothing else after it. Inside JSON strings write a
newline as \\n and a double quote as \\". Schema:
```json
{schema}
```"""




async def _run_stage(stage: Stage, cfg: Config, emit: StepEmitter, options) -> StageResult:
    from claude_agent_sdk import ClaudeSDKError

    total_cost, turns, prompt = 0.0, 0, stage.prompt
    base = session_totals(options.resume)  # what the resumed session had already reported
    with anyio.fail_after(stage.timeout_s or cfg.llm.timeout_s):
        for attempt in range(OUTPUT_RETRIES + 1):
            try:
                result, texts, last_uuid = await _query_once(prompt, options, stage, emit)
            except ClaudeSDKError as exc:  # e.g. max turns reached, CLI process failure
                raise StageError(f"{stage.name}: {str(exc).splitlines()[0]}", total_cost) from exc
            if result is None:
                raise StageError(f"{stage.name}: agent ended without a result message", total_cost)
            cost, model_usage = _delta(result, base)
            if result.session_id:
                base = _TOTALS[result.session_id] = {"cost": result.total_cost_usd or 0.0,
                                                     "model_usage": getattr(result, "model_usage", None) or {}}
            total_cost += cost
            turns += result.num_turns
            emit.add_cost(cost)
            from q3tui.core.stats import usage_from_result

            usage = usage_from_result({"model_usage": model_usage, "usage": result.usage, "total_cost_usd": cost})
            emit("llm_done", stage=stage.name, is_error=result.is_error, subtype=result.subtype, cost_usd=cost, turns=result.num_turns,
                 model=options.model, duration_ms=result.duration_ms, usage=usage)
            read = sum(u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write", 0) for u in (usage or {}).values())
            context = int(read / max(1, result.num_turns))
            if cfg.llm.stage_token_budget and read > cfg.llm.stage_token_budget:
                emit("warning", message=f"{stage.name}: read {read / 1e6:.1f}M input tokens in {result.num_turns} turns "
                                        f"(over llm.stage_token_budget {cfg.llm.stage_token_budget / 1e6:.1f}M) — see `q3tui stats`")
            if result.is_error:
                raise StageError(f"{stage.name}: {result.subtype} — {result.result or result.errors}", total_cost)
            if stage.output_model is None or stage.submit_tool:  # (text_json: validated below from the text)
                return StageResult(result.result or "\n".join(texts), None, total_cost, turns, result.session_id, options.effort,
                                   context, last_uuid)
            output, error = _validate(stage, result, texts)
            if output is not None:
                return StageResult(result.result or "", output, total_cost, turns, result.session_id, options.effort, context,
                                   last_uuid)
            if attempt == OUTPUT_RETRIES or not result.session_id:
                raise StageError(f"{stage.name}: {error}", total_cost)
            emit("warning", message=f"{stage.name}: output did not match the schema, asking the model to fix it ({attempt + 1}/{OUTPUT_RETRIES})")
            options = dataclasses.replace(options, resume=result.session_id, fork_session=False)  # a fork goes on as itself
            prompt = error
    raise AssertionError("unreachable")
