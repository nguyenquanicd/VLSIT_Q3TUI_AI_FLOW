"""q3tui command line (docs/spec/pipeline.md "Pipeline controls")."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import anyio
from rich.markup import escape  # messages carry [scopes] / [block …] tags: never markup
import click
import yaml
from rich.console import Console
from rich.table import Table

from q3tui import __version__
from q3tui.core.events import Event, EventBus
from q3tui.hdl.filelist import FilelistError, filelist_from_files, parse_filelist
from q3tui.pipeline.engine import Engine, EngineError, StepView
from q3tui.core.project import Project, write_json

console = Console(soft_wrap=True, highlight=False)
err_console = Console(stderr=True)

ExistingFile = click.Path(exists=True, dir_okay=False, path_type=Path)
# the default flow's steps (shell completion; a project's flow may have others)
STEP_NAMES = ["spec", "parse", "config", "rtl", "tb", "sva", "verify", "doc"]


class StepParam(click.ParamType):
    """A step id of the project's flow. Any id is accepted here (flows bring their own); the engine rejects unknown
    ones with the list of the flow's steps."""

    name = "step"

    def __init__(self, extra: tuple[str, ...] = ()):
        self.extra = extra

    def convert(self, value, param, ctx):
        return str(value)

    def shell_complete(self, ctx, param, incomplete):
        from click.shell_completion import CompletionItem

        return [CompletionItem(n) for n in (*STEP_NAMES, *self.extra) if n.startswith(incomplete)]


STEP = StepParam()
GATES = StepParam(("all",))

STATUS_STYLE = {
    "done": ("✔", "green"), "user": ("✔", "green"), "stale": ("!", "yellow"), "pending": ("·", "dim"),
    "running": ("◐", "cyan"), "failed": ("✖", "red"), "missing_input": ("?", "yellow"), "unavailable": ("–", "dim"),
}


class Ctx:
    def __init__(self, config_path: Path | None, project_dir: Path | None, overrides: dict | None = None):
        self.config_path, self.project_dir, self.overrides = config_path, project_dir, overrides
        self._project: Project | None = None

    @property
    def project(self) -> Project:
        if self._project is None:
            self._project = Project.open(self.project_dir, self.config_path, self.overrides)
        return self._project

    def engine(self, bus: EventBus | None = None) -> Engine:
        return Engine(self.project, bus)


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="q3tui")
@click.option("--config", "config_path", type=ExistingFile, help="Path to q3tui.yaml.")
@click.option("-C", "--project", "project_dir", type=click.Path(file_okay=False, path_type=Path), help="Project directory (default: nearest with .q3tui/ or cwd).")
@click.option("-m", "--model", "llm_model", help="LLM model for this invocation (overrides llm.model), e.g. claude-sonnet-5-5.")
@click.option("--effort", type=click.Choice(["low", "medium", "high", "xhigh", "max", "none"]), help="Reasoning effort for this invocation.")
@click.option("--flow", "flow_ref", help="Flow for this invocation (overrides pipeline.flow): a name or a flow file; see `q3tui flow list`.")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, project_dir: Path | None, llm_model: str | None, effort: str | None,
         flow_ref: str | None = None) -> None:
    """Q3TUI — from intent to verified RTL. Without a command, opens the TUI."""
    llm: dict = {}
    if llm_model:
        llm["model"] = llm_model
        if llm_model.startswith("claude-haiku-4") and not effort:
            llm["effort"] = None  # Haiku 4.5 does not accept an effort setting
    if effort:
        llm["effort"] = None if effort == "none" else effort
    overrides: dict = {"llm": llm} if llm else {}
    if flow_ref:
        overrides["pipeline"] = {"flow": flow_ref}
    ctx.obj = Ctx(config_path, project_dir, overrides or None)
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui)


# -- event printing ------------------------------------------------------------------------


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


_last_progress = {"t": 0.0}


def print_event(e: Event) -> None:
    d, step = e.data, e.step or ""
    k = e.kind
    if k == "progress":  # heartbeat while the model thinks; at most every 30 s
        now = time.monotonic()
        if now - _last_progress["t"] >= 30 and d.get("thinking_tokens"):
            _last_progress["t"] = now
            console.print(f"  [dim]… {d.get('stage')} thinking (~{d['thinking_tokens'] / 1000:.1f}k tokens)[/dim]")
        return
    if k == "step_started":
        console.print(f"\n[bold]▶ {step}[/bold] — {d.get('title', '')}" + (f"  [dim](applying {len(d['feedback'])} change request(s))[/dim]" if d.get("feedback") else ""))
    elif k == "step_skipped":
        console.print(f"[dim]· {step}: {d.get('reason')}[/dim]")
    elif k == "stage" and d.get("status") == "llm_start":
        console.print(f"  [dim]{d.get('name')} ({d.get('model')})[/dim]")
    elif k == "llm_text":
        console.print(f"  [dim]{escape(_one_line(d.get('text', '')))}[/dim]")
    elif k == "tool_call":
        console.print(f"  [cyan]→ {d.get('tool')}[/cyan] [dim]{escape(_one_line(str(d.get('input', '')), 100))}[/dim]")
    elif k == "llm_done":
        console.print(f"  [dim]{d.get('stage')}: {d.get('turns')} turns, ${d.get('cost_usd', 0):.2f}[/dim]")
    elif k == "log":
        console.print(f"  • {escape(str(d.get('message')))}")
    elif k == "warning":
        console.print(f"  [yellow]! {escape(str(d.get('message')))}[/yellow]")
    elif k == "error":
        console.print(f"[red]✖ {step + ': ' if step else ''}{escape(str(d.get('message')))}[/red]")
    elif k == "step_finished":
        if d.get("status") == "done":
            console.print(f"[green]✔ {step}[/green] [dim]${d.get('cost_usd', 0):.2f}[/dim]")
        elif d.get("status") == "cancelled":
            console.print(f"[yellow]{step} cancelled[/yellow]")
        else:
            console.print(f"[red]✖ {step} failed: {d.get('error')}[/red]")
    elif k == "gate_opened":
        q = d.get("open_questions") or 0
        console.print(f"\n[yellow]⚑ review {step}[/yellow]: {', '.join(d.get('files', []))}" + (f" · {q} open question(s)" if q else ""))
    elif k == "run_finished" and d.get("total_cost_usd") is not None:
        console.print(f"[dim]total LLM cost so far: ${d['total_cost_usd']:.2f}[/dim]")


# -- pipeline commands -----------------------------------------------------------------------


def _gate_prompt(engine: Engine, step: str) -> str:
    """Interactive review at a gate. Returns 'continue' or 'stop'."""
    files = [f for f in engine.step(step).outputs(engine) if f.suffix == ".md" and f.is_file()]
    while True:
        choice = click.prompt(
            f"[{step}] (a)pprove, (e)dit, (q)uestions, (r)equest changes, (l)ater",
            type=click.Choice(["a", "e", "q", "r", "l"]), show_choices=False, default="l",
        )
        if choice == "a":
            try:
                console.print(engine.approve(step))
            except EngineError as exc:
                console.print(f"[red]{exc}[/red]")
                if click.confirm("Approve anyway (blocking questions stay unanswered)?", default=False):
                    console.print(engine.approve(step, force=True))
                else:
                    continue
            return "continue"
        if choice == "e":
            target = files[0] if files else None
            if target:
                click.edit(filename=str(target))
                console.print(f"[dim]edited {engine.project.rel(target)}[/dim]")
        elif choice == "q":
            _answer_interactive(engine, step)
        elif choice == "r":
            text = click.prompt("What should change?")
            console.print(engine.request_change(step, text))
            return "continue"
        else:
            console.print(f"[dim]later: `q3tui approve {step}` when ready[/dim]")
            return "stop"


def _answer_interactive(engine: Engine, step: str | None = None) -> None:
    open_q = [q for q in engine.questions() if q["status"] == "open" and (step is None or q["step"] == step)]
    if not open_q:
        console.print("[dim]no open questions[/dim]")
        return
    for q in open_q:
        console.print(f"\n[bold]{q['id']}[/bold]{' [red](blocking)[/red]' if q['blocking'] else ''} {q['question']}")
        text = click.prompt("  answer (- = skip)", default=q.get("default_assumption") or "-", show_default=bool(q.get("default_assumption")))
        if text.strip() and text.strip() != "-":
            console.print("  " + engine.answer(q["id"], text))


@main.command()
@click.option("--intent", help="Describe the block to build (starts at spec).")
@click.option("--intent-file", type=ExistingFile, help="File with the intent.")
@click.option("--spec", "specs", multiple=True, type=ExistingFile, help="Your spec document(s) (starts at parse).")
@click.option("--import", "imports", multiple=True, type=click.Path(exists=True, path_type=Path),
              help="Your own project folder (or file): spec documents, RTL, testbench / tests, SVA (see `q3tui import`).")
@click.option("--from", "start", type=STEP, help="First step to run.")
@click.option("--to", "stop", type=STEP, help="Last step to run.")
@click.option("--only", type=STEP, help="Run just this step.")
@click.option("--regenerate", multiple=True, type=STEP, help="Rebuild this step from scratch.")
@click.option("--yes", "-y", is_flag=True, help="Approve all review gates automatically.")
@click.option("--spec-review/--no-spec-review", default=None, help="Run the spec self-review pass (default from config: spec.self_review).")
@click.option("--auto-approve", is_flag=True, help="Approve review gates automatically, but stop for blocking questions (unlike --yes).")
@click.option("--auto-answer", is_flag=True, help="Unattended: approve every gate, answer every question with its default and "
                                                    "accept proposals (runs start to finish).")
@pass_ctx
def run(c: Ctx, intent, intent_file, specs, imports, start, stop, only, regenerate, yes, spec_review, auto_approve,
        auto_answer) -> None:
    """Run every pending or stale step, stopping at review gates."""
    if spec_review is not None:
        c.project.cfg.spec.self_review = spec_review
    if auto_approve:
        c.project.cfg.pipeline.auto_approve = True
    if auto_answer:
        c.project.cfg.pipeline.auto_answer = True
    bus = EventBus()
    bus.subscribe(print_event)
    engine = c.engine(bus)
    if intent or intent_file:
        engine.import_intent(intent or intent_file.read_text())
    if specs:
        engine.import_spec(list(specs))
    if imports:
        from q3tui.core.ops import import_summary

        for path in imports:
            click.echo(import_summary(engine.import_path(path)))

    interactive = sys.stdin.isatty() and not yes
    while True:
        outcome = anyio.run(lambda: engine.run(start=start, stop=stop, only=only, yes=yes, regenerate=set(regenerate)))
        if outcome != "gate" or not interactive:
            break
        gate = next(v.name for v in engine.status() if v.gate == "open" and v.status in ("done", "user"))
        if _gate_prompt(engine, gate) == "stop":
            break
        regenerate = ()
    sys.exit(0 if outcome in ("complete", "gate", "unavailable") else 1)


def _status_table(views: list[StepView]) -> Table:
    t = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    for col in ("", "step", "status", "review", "questions", "notes"):
        t.add_column(col)
    for v in views:
        icon, color = STATUS_STYLE[v.status]
        review = {"open": "[yellow]⚑ open[/yellow]", "approved": "[green]approved[/green]", "none": ""}[v.gate]
        notes = v.detail
        if v.edited:
            notes = (notes + " " if notes else "") + f"hand-edited: {', '.join(v.edited)}"
        label = "user-provided" if v.status == "user" else v.status.replace("_", " ")
        t.add_row(f"[{color}]{icon}[/{color}]", v.name, f"[{color}]{label}[/{color}]", review, str(v.open_questions or ""), notes)
    return t


@main.command()
@click.option("--template", "with_template", is_flag=True, help="Also copy the spec template into spec/template.yaml to customise sections.")
@click.option("--force", is_flag=True, help="Overwrite an existing spec/intent.md.")
@pass_ctx
def init(c: Ctx, with_template: bool, force: bool) -> None:
    """Start a project here: spec/intent.md skeleton with the template's sections."""
    from q3tui.steps.spec.template import ensure_project_template, intent_skeleton, load_template

    project = c.project
    flow_template = c.engine().spec_template()  # (the flow's: vlsit → the VLSIT layout)
    template, source = load_template(project.spec_dir, flow_template)
    intent = project.spec_dir / "intent.md"
    if intent.exists() and not force:
        console.print(f"[yellow]{project.rel(intent)} exists[/yellow] (use --force to overwrite)")
    else:
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(intent_skeleton(template))
        console.print(f"[green]✔[/green] wrote {project.rel(intent)} — fill in what you know, delete the rest")
    if with_template:
        console.print(f"[green]✔[/green] {project.rel(ensure_project_template(project.spec_dir, flow_template))} — add/remove/reorder sections here")
    else:
        console.print(f"[dim]spec sections come from {source} (`q3tui init --template` to customise per project)[/dim]")
    console.print("next: edit the intent, then `q3tui run` (or `q3tui` for the TUI)")


@main.command()
@pass_ctx
def status(c: Ctx) -> None:
    """Show step states, review gates, open questions."""
    engine = c.engine()
    console.print(f"[bold]{engine.state.top or '(unnamed design)'}[/bold]  [dim]{engine.project.root} · ${engine.state.total_cost_usd:.2f} spent[/dim]\n")
    console.print(_status_table(engine.status()))


@main.command()
@click.argument("step", type=GATES)
@click.option("--only", is_flag=True, help="Reset just this step (downstream steps become stale instead).")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@pass_ctx
def reset(c: Ctx, step: str, only: bool, yes: bool) -> None:
    """Clear a step's generated outputs (and downstream steps) to start it from scratch; `all`: every step of the flow."""
    engine = c.engine()
    if step == "all":
        step, only = engine.pipeline_steps[0].name, False  # (the first step and everything after it)
    plan = engine.reset_plan(step, only)
    files = [f for fs in plan.values() for f in fs]
    console.print(f"reset: {', '.join(plan)}")
    for f in files:
        console.print(f"  [red]-[/red] {engine.project.rel(f)}")
    if not files:
        console.print("  [dim](no generated files; review/answer state is cleared)[/dim]")
    console.print("[dim]inputs you wrote (intent.md, template.yaml, your own spec/model files) are kept; deleted files are backed up to .q3tui/bkp/[/dim]")
    if not yes and not click.confirm("Proceed?", default=False):
        return
    engine.bus.subscribe(print_event)
    engine.reset(step, only)


@main.command()
@click.option("--since", help="Only usage after this ISO date/time, e.g. 2026-09-26 or 2026-09-26T20:00.")
@click.option("--reset", "do_reset", is_flag=True, help="Start the statistics over (the run logs are kept).")
@pass_ctx
def stats(c: Ctx, since: str | None, do_reset: bool) -> None:
    """Usage statistics: tokens and cost per model and per step, LLM time, step wall time."""
    from q3tui.core import stats as st

    if do_reset:
        console.print(st.reset(c.project.state_dir))
        return
    console.print(st.text_report(st.report(c.project.state_dir, since), c.project.cfg.llm.stage_token_budget), markup=False, highlight=False)


@main.command()
@click.option("--reset", "do_reset", is_flag=True, help="Set the pipeline's LLM cost counter back to $0.")
@pass_ctx
def cost(c: Ctx, do_reset: bool) -> None:
    """Show (or reset) the LLM cost recorded for this project."""
    engine = c.engine()
    if do_reset:
        console.print(engine.reset_cost())
        return
    summary = engine.cost_summary()
    console.print(f"[bold]${summary['total_usd']:.2f}[/bold] total LLM cost for this project")
    for name, usd in summary["steps"].items():
        console.print(f"  {name:8} ${usd:.2f}  [dim](last run)[/dim]")
    for r in summary["resets"]:
        console.print(f"  [dim]reset {r['at']}: ${r['amount_usd']:.2f} cleared[/dim]")


@main.command()
@click.argument("step", type=GATES)
@click.option("--force", is_flag=True, help="Approve even with unanswered blocking questions.")
@pass_ctx
def approve(c: Ctx, step: str, force: bool) -> None:
    """Approve a review gate (or `all` open gates); open questions are closed with their default assumption."""
    if step == "all":
        for line in c.engine().approve_all(force=force):
            console.print(line)
        return
    console.print(c.engine().approve(step, force=force))


@main.command("import")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--kind", type=click.Choice(["all", "spec", "rtl", "tb", "sva"]), default="all", show_default=True,
              help="Import only this kind.")
@pass_ctx
def import_cmd(c: Ctx, path: Path, kind: str) -> None:
    """Import your own files: a folder (a whole reference project) or a file. Code sorts them into spec documents,
    RTL (checked against the flow's RTL rules and updated where it does not follow them), testbench / tests and SVA
    (ported by the tb / sva steps into the flow's format). Nothing runs: `q3tui run` next."""
    from q3tui.core.ops import Ops

    console.print(escape(Ops(c.engine()).import_files(kind, str(path.resolve()))))


@main.command()
@click.argument("step", type=STEP)
@click.option("-m", "--message", required=True, help="What should change.")
@pass_ctx
def change(c: Ctx, step: str, message: str) -> None:
    """Request a change to a step's output (re-runs it on the next `run`)."""
    console.print(c.engine().request_change(step, message))


@main.command()
@click.argument("question_id", required=False)
@click.argument("text", required=False)
@pass_ctx
def answer(c: Ctx, question_id: str | None, text: str | None) -> None:
    """Answer an open question (interactive when no arguments); `answer all` takes every open question's default."""
    engine = c.engine()
    if question_id == "all" and not text:
        console.print(engine.answer_all())
    elif question_id and text:
        console.print(engine.answer(question_id, text))
    elif question_id:
        raise click.UsageError("give both QUESTION_ID and TEXT, or neither for interactive mode")
    else:
        _answer_interactive(engine)


@main.command()
@click.option("--all", "show_all", is_flag=True, help="Include answered questions.")
@pass_ctx
def questions(c: Ctx, show_all: bool) -> None:
    """List open questions from all steps."""
    qs = [q for q in c.engine().questions() if show_all or q["status"] == "open"]
    if not qs:
        console.print("[dim]no open questions[/dim]")
    for q in qs:
        flag = " [red](blocking)[/red]" if q["blocking"] else ""
        console.print(f"[bold]{q['id']}[/bold] [dim]{q['step']}[/dim]{flag} {q['question']}")
        if q["status"] == "default":
            console.print(f"    [cyan]→ default accepted: {q['answer']}[/cyan]")
        elif q.get("answer"):
            console.print(f"    [green]→ {q['answer']}[/green]")
        elif q.get("default_assumption"):
            console.print(f"    [dim]assumed: {q['default_assumption']}[/dim]")


@main.command()
@click.option("--read-only", is_flag=True, help="Only look: follow the project live; never run, reset, approve, answer or save.")
@pass_ctx
def tui(c: Ctx, read_only: bool) -> None:
    """Open the terminal UI (it watches, read-only, while another process runs the pipeline)."""
    from q3tui.tui.app import Q3TUIApp

    Q3TUIApp(c.project, read_only=read_only).run()


# -- flows -------------------------------------------------------------------------------------


@main.group("flow")
def flow_group() -> None:
    """Flows: which steps run, in which order, with which gates (docs/spec/flows.md)."""


@flow_group.command("list")
@pass_ctx
def flow_list(c: Ctx) -> None:
    """The flows you can select (`pipeline.flow` / `--flow`): the project's, yours, the built-in ones."""
    from q3tui import flows

    root = c.project_dir.resolve() if c.project_dir else Path.cwd()
    current = None
    try:
        current = c.project.flow_ref()
    except Exception:  # noqa: BLE001 - listing works outside a project
        pass
    for name, path in flows.list_flows(root).items():
        try:
            title = flows.parse_flow(flows._read(path), path).title
        except flows.FlowError as exc:
            title = f"[red]{exc}[/red]"
        console.print(f"{'*' if name == current else ' '} {name:<14} {title}  [dim]{path}[/dim]")


@flow_group.command("check")
@click.argument("flow", required=False)
@pass_ctx
def flow_check(c: Ctx, flow: str | None) -> None:
    """Validate a flow (default: the project's) and print its dependency graph and the tools it needs."""
    from q3tui import flows

    root = c.project_dir.resolve() if c.project_dir else Path.cwd()
    try:
        fl = flows.load_flow(flow or c.project.flow_ref(), root)
    except flows.FlowError as exc:
        raise click.ClickException(str(exc))
    console.print(flows.describe(fl), highlight=False, markup=False)
    problems = flows.validate(fl)
    if problems:
        for p in problems:
            err_console.print(f"[red]✖[/red] {p}")
        raise SystemExit(1)
    from q3tui.eda.tools import roles_needed

    needed = roles_needed(fl)
    if needed:
        console.print(f"tools needed: {', '.join(needed)}  (tools.json / tools.roles in q3tui.yaml)", markup=False)
    console.print("[green]✔[/green] flow is valid")


@main.group("tools")
def tools_group() -> None:
    """Tool roles (tools.json): what the flows run for lint, synthesis, simulation …"""


@tools_group.command("init")
@click.option("--preset", default="synopsys", show_default=True, help="Tool set to start from.")
@click.option("--force", is_flag=True, help="Overwrite an existing tools.json.")
@pass_ctx
def tools_init(c: Ctx, preset: str, force: bool) -> None:
    """Write tools.json into the project (VCS lint / compile / run, Design Compiler): edit modules and library paths in it."""
    from q3tui.eda import tools as T

    try:
        dest = T.write_preset(c.project.root, preset, force)
    except T.ToolUnavailable as exc:
        raise click.ClickException(str(exc))
    console.print(f"[green]✔[/green] wrote {c.project.rel(dest)} — set `modules` / the Design Compiler library for your site")


@tools_group.command("check")
@pass_ctx
def tools_check(c: Ctx) -> None:
    """Which tool roles the flow needs, whether each is configured and its binary can be found."""
    from q3tui import flows
    from q3tui.eda import tools as T

    root = c.project.root
    flow = flows.load_flow(c.project.flow_ref(), root)
    try:
        ts = T.load_tools(c.project.cfg, root)
    except T.ToolUnavailable as exc:
        raise click.ClickException(str(exc))
    bad = 0
    for role in T.roles_needed(flow):
        if role not in ts.roles:
            console.print(f"[red]✖[/red] {role}: not configured")
            bad += 1
        elif not T.available(ts, role):
            console.print(f"[red]✖[/red] {role}: `{T.binary_of(ts.roles[role])}` not found (modules / setup_script?)")
            bad += 1
        else:
            console.print(f"[green]✔[/green] {role}: {T.binary_of(ts.roles[role])}")
    if bad:
        console.print("[dim]`q3tui tools init` writes a tools.json to start from[/dim]")
        raise SystemExit(1)


@main.command("confirm-properties")
@click.option("--include-vacuous", is_flag=True, help="Also confirm assertions that never fired in the smoke run.")
@pass_ctx
def confirm_properties_cmd(c: Ctx, include_vacuous: bool) -> None:
    """VLSIT flow: confirm every pending SVA assertion at once (the review of Gate 3b, in bulk)."""
    from q3tui.core.ops import Ops

    console.print(Ops(c.engine()).confirm_properties(include_vacuous))


@main.command("sign")
@click.argument("reqs", nargs=-1)
@click.option("--all-ready", is_flag=True, help="Sign every requirement whose conditions 1–5 hold (what the RTM calls `pending`).")
@click.option("--withdraw", is_flag=True, help="Withdraw the sign-off instead.")
@click.option("-m", "--note", default="", help="A note recorded with each sign-off.")
@pass_ctx
def sign_cmd(c: Ctx, reqs: tuple[str, ...], all_ready: bool, withdraw: bool, note: str) -> None:
    """VLSIT flow: sign requirements off in the RTM (a human action — typing this command is the signature).

    `q3tui sign REQ-001 REQ-002`, or `q3tui sign --all-ready`. A requirement that is not ready is refused with its reasons."""
    from q3tui.core.ops import Ops

    ops = Ops(c.engine())
    names = list(reqs)
    if all_ready:
        names += [r["req_id"] for r in ops.rtm() if r["status"] == "pending"]
    if not names:
        raise click.UsageError("name requirements (REQ-001 …) or use --all-ready (none is ready right now)" if all_ready else "name requirements (REQ-001 …) or use --all-ready")
    failed = 0
    for rid in names:
        try:
            console.print(ops.sign_req(rid, not withdraw, note))
        except Exception as exc:  # noqa: BLE001 - one refused requirement must not hide the others
            failed += 1
            err_console.print(f"[red]✖[/red] {rid}: {exc}")
    if failed:
        raise SystemExit(1)


@main.command("gate")
@click.argument("step", type=STEP)
@click.argument("mode", type=click.Choice(["human", "auto", "auto_answer", "none"]))
@pass_ctx
def gate_cmd(c: Ctx, step: str, mode: str) -> None:
    """Set a step's review gate: human (stops for you), auto (approves itself), auto_answer (unattended), none."""
    from q3tui.core.ops import Ops

    console.print(Ops(c.engine()).set_gate_mode(step, mode, save=True))


# -- config ------------------------------------------------------------------------------------


@main.group()
def config() -> None:
    """Inspect configuration."""


@config.command("show")
@pass_ctx
def config_show(c: Ctx) -> None:
    """Print the effective configuration and its source."""
    console.print(f"[dim]# source: {c.project.cfg.source}[/dim]")
    click.echo(yaml.safe_dump(c.project.cfg.model_dump(mode="json"), sort_keys=False))


# -- design utilities (deterministic) ------------------------------------------------------------


def _filelist(filelist: Path | None, top_file: Path | None):
    if filelist:
        fl = parse_filelist(filelist, base_dir=Path.cwd())
        if fl.missing:
            raise click.ClickException("filelist references missing files:\n  " + "\n  ".join(fl.missing))
        return fl
    if top_file:
        return filelist_from_files([top_file])
    raise click.UsageError("pass --filelist or --top-module-file")


def design_inputs(fn):
    fn = click.option("--filelist", "-f", type=ExistingFile, help="Design filelist (.f).")(fn)
    fn = click.option("--top-module-file", type=ExistingFile, help="Single source file.")(fn)
    fn = click.option("--top-module-name", "--top", "top", required=True, help="Top module name.")(fn)
    return fn


@main.group(invoke_without_command=True)
@click.pass_context
def team(click_ctx: click.Context) -> None:
    """The role sessions (team mode, `team: {enabled: true}`): one persistent Claude session per role."""
    if click_ctx.invoked_subcommand is None:
        click_ctx.invoke(team_status)


@team.command("status")
@pass_ctx
def team_status(c: Ctx) -> None:
    """Each role's session: tasks, forks, compactions, cost."""
    from q3tui.llm import roles

    t = roles.Team(c.project)
    table = Table("role", "session", "tasks", "forks", "compactions", "cost")
    for name, st in t.roles.items():
        table.add_row(name, st.session or "[dim]not started[/dim]", str(st.tasks), str(st.forks), str(st.compactions),
                      f"${st.cost_usd:.2f}")
    console.print(table)
    if not c.project.cfg.team.enabled:
        console.print("[dim]team mode is off: set `team: {enabled: true}` in q3tui.yaml[/dim]")


@team.command("reset")
@click.argument("role", required=False, type=click.Choice(["architect", "modeler", "designer", "verifier", "reviewer"]))
@pass_ctx
def team_reset(c: Ctx, role: str | None) -> None:
    """Start a role's session (or every role's) fresh; the next task begins with its init turn."""
    from q3tui.llm import roles

    names = roles.Team(c.project).reset(role)
    console.print(f"restarted: {', '.join(names)}")


@main.group()
def design() -> None:
    """RTL utilities: parse and compile-check a design (no LLM)."""


@design.command("parse")
@design_inputs
@click.option("--json", "as_json", is_flag=True, help="Print the parsed design as JSON.")
@pass_ctx
def design_parse(c: Ctx, top: str, top_module_file: Path | None, filelist: Path | None, as_json: bool) -> None:
    """Elaborate with pyslang: hierarchy, ports, parameters, clocks/resets, diagnostics."""
    from q3tui.hdl.parse import parse_design, summarize

    parsed = parse_design(_filelist(filelist, top_module_file), top)
    out = write_json(c.project.state_dir / "cache" / "parsed_design.json", parsed.to_dict())
    if as_json:
        click.echo(out.read_text())
    else:
        console.print(summarize(parsed))
        console.print(f"\n[dim]wrote {c.project.rel(out)}[/dim]")
    sys.exit(0 if parsed.ok else 1)


@design.command("check")
@design_inputs
@click.option("--tool", type=click.Choice(["syntax", "simulator"]), default="syntax", show_default=True)
@pass_ctx
def design_check(c: Ctx, top: str, top_module_file: Path | None, filelist: Path | None, tool: str) -> None:
    """Compile the design with a tool role and report diagnostics."""
    from q3tui.eda import get_adapter
    from q3tui.eda.base import ToolRequest

    adapter = get_adapter(tool, c.project.cfg)
    if not adapter.available():
        raise click.ClickException(
            f"{tool} tool '{getattr(adapter, 'bin', adapter.vendor)}' not found; add its module to tools.modules (or tools.setup_script) in q3tui.yaml"
        )
    request = ToolRequest(filelist=_filelist(filelist, top_module_file), top=top)
    workdir = c.project.state_dir / "cache" / f"check_{tool}"
    result = adapter.compile(request, workdir) if hasattr(adapter, "compile") else adapter.run(request, workdir)
    for d in result.diagnostics:
        color = {"error": "red", "warning": "yellow"}.get(d.severity, "dim")
        console.print(f"[{color}]{d.severity:7}[/{color}] {d.code:22} {d.file}:{d.line}: {d.message}")
    console.print(("[green]PASS[/green] " if result.ok else "[red]FAIL[/red] ") + result.summary)
    sys.exit(0 if result.ok else 1)


def run_cli() -> None:
    try:
        main(standalone_mode=True)
    except (EngineError, FilelistError, FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        sys.exit(2)


if __name__ == "__main__":
    run_cli()
