"""The dv flow's simulation triage: whose fault is a failing test, and where the fix goes (rule: a step never works around an earlier step's problem, it hands it back).

`simulate` calls `handle_failures` with the tests that did not pass. A second LLM stage (fresh session, read only; it may read the RTL, which the testbench stages may not) gives a
verdict per failure and code dispatches it, the way the rtl flow's smoke test does:

- `tb_bug`: a change request for the `tb` step, scoped to that test (`[tb:TC-019] …`; the UVM kind: the whole step). `simulate` goes on normally and marks its loop: the engine updates `tb`,
  then `simulate` runs again. Bounded by `options.max_rounds` or `pipeline.max_fix_iterations`.
- `rtl_bug`: a change request for the `rtl` step of the flow that owns the RTL (`handoff_flow` of the tb options, default `rtl`), scoped to the module (`[rtl:<module>] …`). That flow has to run
  to apply it; the RTL is an input of `tb`, so `tb` and `simulate` then run again on the new RTL. In an unattended run (`engine.unattended_run`) the fix loop runs that flow itself
  (`engine.hand_to_flow`), bounded like the tb fixes; a round after which the RTL did not change, or the failures did not go down, stops it. Otherwise `simulate` stays failed until you run it.
- `spec_issue` / `unsure`, or a failure that keeps coming back after its fix was handed over: a blocking question for you (spec_gap / design_choice); nothing is changed.
  The triage gives the ways to settle it (`options`, the recommended one first: the pick list, and the default), and the decision it waits for (`decision`):
  the failures that wait for the same decision are one question.

A module a generator made (the rtl flow's csr step: `src/rtl/csr/provided.json`) is named in the prompt: a bug in it is an `rtl_bug` (the rtl step changes it in
place and keeps the change as a patch that outlives the next generation), unless your spec forbids editing it or the rtl option `keep_generated` is on: then it
is a `spec_issue` with the choices.

Every verdict is in `schemas/dv_triage.json`. Models of the LLM's answer: `flows/rtl/lib/models.Verdict` (the same as the smoke triage's)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from q3tui.core.project import write_json
from q3tui.flows import skill_sections
from q3tui.flows.rtl.lib import skills as skillmod
from q3tui.flows.rtl.lib.models import Verdict
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import Question, assign_question_ids, load_answers, scoped, spec_block

TRIAGE = skill_sections(Path(__file__).parent / "md" / "triage.md")["TRIAGE"]
TRIAGE_JSON = "dv_triage.json"
DV_FLOW_FILE = Path(__file__).parent / "FLOW.md"  # (the skill is found next to the dv flow whichever flow file runs its steps: a project's own flow file has no skills/ folder)


class DvTriage(BaseModel):
    verdicts: list[Verdict]


def _log_tail(project, log: str, n: int = 40) -> str:
    p = project.root / log if log and not Path(log).is_absolute() else Path(log) if log else None
    if p is None or not p.is_file():
        return "(no log)"
    return "\n".join(p.read_text(errors="replace").splitlines()[-n:])


def _requirements(project) -> dict[str, str]:
    from q3tui.flows.spec.lib import artifacts

    spec = artifacts.read(project, "structured_spec.json") or {}
    return {r["req_id"]: r.get("text", "") for r in spec.get("requirements", []) if r.get("req_id")}


def _rtl_files(project, rtl_dir: Path) -> list[Path]:
    return sorted(p for p in rtl_dir.rglob("*") if p.is_file() and p.suffix in (".sv", ".v", ".svh", ".vh")) if rtl_dir.is_dir() else []


def reset(engine, step) -> None:
    """The tests pass: the fix rounds and what was handed over start afresh."""
    meta = engine.state.step_meta.setdefault(step.name, {})
    meta.update({"rounds": 0, "loop": False})
    meta.pop("history", None)
    meta.pop("flow_round", None)


def rtl_hash(files: list[Path]) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:16]


def failure_summary(failures: list[dict]) -> str:
    return "\n".join(f"- {f['id']}{' (' + f['name'] + ')' if f.get('name') else ''}: {f['note']}" for f in failures)


async def handle_failures(step, ctx: StepContext, failures: list[dict], rtl_dir: Path, kind: str, original: str) -> None:
    """Triage the failing tests and dispatch the verdicts. `failures`: {id, name, note, log, reqs}; `kind`: plain | uvm (a testbench change request is scoped to the test only for plain);
    `original`: the failure message the step gives when the triage cannot run. Returns when something was dispatched to this flow (the fix loop runs); raises StepFailed when it is waiting
    for you or for another flow."""
    engine, project, cfg = ctx.engine, ctx.project, ctx.cfg
    lay = step.lay(engine)
    summary = failure_summary(failures)
    meta = engine.state.step_meta.setdefault(step.name, {})
    answered = len(load_answers(step._q_paths(engine)[1]))
    if ctx.regenerate or "rounds" not in meta or meta.get("answered") != answered:
        meta["rounds"] = 0  # (counted until the tests pass, you regenerate, or you answer: not per run, or a limit every run starts over is never reached)
    meta["answered"] = answered
    meta["loop"] = False
    rounds = int(meta["rounds"])
    from q3tui.flows.dv import uvm

    tb_opts = uvm.tb_options(engine)
    limit = int(tb_opts.get("max_rounds") or cfg.pipeline.max_fix_iterations)
    if rounds >= limit:
        raise StepFailed(f"simulate: still failing after {rounds} fix round(s) (options.max_rounds / pipeline.max_fix_iterations):\n{summary}")
    history: list[dict] = meta.setdefault("history", [])
    reqs = _requirements(project)
    rtl = _rtl_files(project, rtl_dir)
    now_rtl = rtl_hash(rtl)
    last = meta.pop("flow_round", None)  # the RTL fixes the last round had the owner flow apply in this run
    stalled = ("the RTL did not change" if last and last.get("rtl") == now_rtl
               else f"the failures did not go down ({last.get('failed')} → {len(failures)})" if last and len(failures) >= int(last.get("failed") or 0) else "")
    tb_files = sorted(p for p in lay.tb.rglob("*") if p.is_file() and p.suffix in (".sv", ".svh", ".f")) if lay.tb.is_dir() else []
    skills = skillmod.discover(project.config_dir, DV_FLOW_FILE, ["sim-triage"])  # (the method is a playbook skill: yours in config/skills/ wins, as for the rtl flow's smoke-triage)
    block, _ = spec_block(project)
    parts = ["# Task: triage the failing simulation tests", "", "## Specification", block, "",
             "## Requirements", "\n".join(f"- {k}: {v}" for k, v in reqs.items()) or "(none)", "",
             f"## Files (read them): testbench ({kind}) {', '.join(project.rel(p) for p in tb_files[:40])}; RTL {', '.join(project.rel(p) for p in rtl[:40])}", "",
             *_generated_block(rtl_dir, cfg),
             "## Already handed over in earlier rounds (do not ask for the same again: say if it did not work, and why)",
             "\n".join(f"- {h['key']}" for h in history[-10:]) or "(nothing yet)", "", "## The failing tests (ground truth)"]
    for f in failures:
        parts += [f"### {f['id']} {f.get('name', '')}", f"status: {f['note']}", f"requirements: {', '.join(f.get('reqs') or []) or '–'}", f"log {f.get('log') or '(none)'} (end):",
                  "```", _log_tail(project, f.get("log") or ""), "```", ""]
    parts.append(skillmod.index(skills, project.root))
    stage = Stage(name="dv_triage", system_prompt=TRIAGE, prompt="\n".join(parts), cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], add_dirs=skillmod.add_dirs(skills), output_model=DvTriage,
                  deny_dirs=[project.model_dir, project.arch_dir, project.src_dir / "sva"], max_turns=ctx.cfg.tb.max_turns)
    try:
        verdicts: list[Verdict] = (await ctx.llm(stage)).output.verdicts
    except StepFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - no credentials, a stage that failed: the tests failed all the same
        raise StepFailed(f"{original}\n(the triage could not run: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__})") from exc

    modules = {p.stem for p in rtl}
    known = {f["id"] for f in failures}
    asks: list[Question] = []
    to_tb = 0
    by_module: dict[str, list[Verdict]] = {}
    by_decision: dict[str, list[Verdict]] = {}  # (failures that wait for the same decision: one question)
    seen: dict[str, int] = {}
    for h in history:  # (an RTL fix counts once the RTL changed after it: one handed over and never applied was not tried)
        if not h["key"].startswith("rtl_bug|") or h.get("rtl") not in (None, now_rtl):
            seen[h["key"]] = seen.get(h["key"], 0) + 1
    report = {"round": rounds + 1, "failures": failures, "verdicts": []}
    for v in verdicts:
        report["verdicts"].append(v.model_dump())
        unit = v.module if v.module in modules else next(iter(sorted(modules)), "")
        key = f"{v.verdict}|{unit if v.verdict == 'rtl_bug' else v.id}|{v.id}"
        if v.verdict in ("rtl_bug", "tb_bug") and v.request.strip() and seen.get(key, 0) >= cfg.pipeline.escalate_after_repeats:
            asks.append(Question(id="", blocking=True, kind="design_choice", question=(
                f"Test {v.id} keeps failing ({v.verdict.replace('_', ' ')}{' in ' + unit if v.verdict == 'rtl_bug' and unit else ''}, {seen[key] + 1} times): {v.summary} How should it be settled?"),
                default_assumption="Keep the spec as the reference and change the implementation once more, starting from the evidence."))
        elif v.verdict == "rtl_bug" and v.request.strip():
            by_module.setdefault(unit, []).append(v)
            history.append({"key": key, "round": rounds + 1, "rtl": now_rtl})
        elif v.verdict == "tb_bug" and v.request.strip():
            history.append({"key": key, "round": rounds + 1})
            text = f"From the simulation triage ({v.id}): {v.summary} {v.request}"
            engine.request_change("tb", scoped("tb", v.id, text) if kind == "plain" and v.id in known else text)
            ctx.emit("log", message=f"simulate {v.id}: testbench bug → tb: {v.summary[:100]}")
            to_tb += 1
        else:
            by_decision.setdefault(v.decision.strip().lower() or v.id, []).append(v)
    asks += [vs[0].question("Test", [v.id for v in vs]) for vs in by_decision.values()]
    answers = load_answers(step._q_paths(engine)[1])
    kept = [q for q in step.questions_list(engine) if q.id in answers]
    step.save_questions(engine, [*kept, *assign_question_ids(asks, kept, "SIM", taken=set(answers))])
    write_json(lay.schemas / TRIAGE_JSON, report)
    if to_tb:
        meta["loop"], meta["rounds"] = True, rounds + 1  # the engine updates tb on the change requests, then runs simulate again
    # the hand-over to the other flow last: it saves this engine's state, lets the other flow's engine write the change request into the same file, and reads it back
    handed: list[str] = []
    owner = str(tb_opts.get("handoff_flow") or "rtl")
    for unit, vs in by_module.items():
        text = scoped("rtl", unit, f"From the simulation of the testbench ({', '.join(v.id for v in vs)}): " + " | ".join(f"{v.summary} {v.request}" for v in vs))
        owner = _hand_to_rtl_flow(ctx, owner, text)
        ctx.emit("log", message=f"simulate {', '.join(v.id for v in vs)}: RTL bug → {owner}:rtl ({unit}): {vs[0].summary[:100]}")
        handed.append(f"{unit} → {owner}")
    meta = engine.state.step_meta.setdefault(step.name, {})  # (the hand-over reloaded the state: this step's meta is a new object)
    run_owner = bool(handed) and engine.unattended_run() and not stalled
    if run_owner:  # the fix loop runs the owner flow within this run, then tb and simulate on the new RTL
        engine.hand_to_flow(step.name, owner)
        meta["rounds"], meta["flow_round"] = rounds + 1, {"rtl": now_rtl, "failed": len(failures)}
        ctx.emit("log", message=f"simulate: the {owner} flow runs next for {', '.join(handed)}, then tb and simulate again")
    if to_tb or run_owner:
        if (handed and not run_owner) or asks:
            said = [("also handed to the RTL flow: " + ", ".join(handed) + (f" ({stalled} after the last {owner} flow run: run it yourself)" if stalled else ""))
                    if handed and not run_owner else "", f"{len(asks)} question(s) wait for you" if asks else ""]
            ctx.emit("warning", message="; ".join(x for x in said if x))
        return
    if handed:
        why = f"{stalled} after the {owner} flow ran for the last fixes; " if stalled else ""
        raise StepFailed(f"simulate: RTL bug(s) handed to the {owner} flow ({'; '.join(handed)}): {why}run it (q3tui --flow {owner} run), then this flow again: the RTL is an input of tb, "
                         f"so tb and simulate run on the new RTL.\n{summary}")
    if not asks:
        raise StepFailed(f"simulate: the triage gave no verdict to act on:\n{summary}")
    ids = ", ".join(q["id"] for q in engine.questions() if q["step"] == step.name and q["status"] == "open")
    raise StepFailed(f"simulate: the tests are failing and need your decision on {ids or 'the open questions'} (answer them, then run again):\n{summary}")


def _generated_block(rtl_dir: Path, cfg) -> list[str]:
    """The prompt's part on the RTL modules a generator made (the rtl flow's csr step): whether a bug in one can be an rtl_bug."""
    from q3tui.flows.rtl.lib import csr

    made = sorted(csr.read_provided(rtl_dir))
    if not made:
        return []
    if (cfg.pipeline.step_options.get("rtl") or {}).get("keep_generated"):
        how = ("Settings → rtl \"Never edit generated RTL\" is on: they are never changed, so a bug in one is a spec_issue whose proposal names the choices "
               "(fix the generator, revise the spec to the generated behaviour, or turn the setting off).")
    else:
        how = ("The rtl flow changes them in place on an RTL change request (name, ports and registers kept) and keeps the change as a patch "
               "(src/rtl/csr/patches/) applied again when the module is generated again: a bug in one is an rtl_bug for it, unless the specification "
               "forbids editing it; then it is a spec_issue whose proposal names the choices (fix the generator, allow the change in place, or revise the spec).")
    return ["## Generated RTL", f"Made by the APB-CSR-Generator (the rtl flow's csr step), not by hand: {', '.join(made)}. {how}", ""]


def _hand_to_rtl_flow(ctx: StepContext, flow_name: str, text: str) -> str:
    """A change request for the `rtl` step of the flow that owns the RTL (the way the FPGA flow hands over a PPA fix); returns the flow's name."""
    from q3tui import flows
    from q3tui.pipeline.engine import Engine

    owner = flows.load_flow(flow_name, ctx.project.root)
    if owner.name == ctx.engine.flow.name:
        raise StepFailed("handoff_flow is this flow itself: the RTL is the rtl flow's")
    ctx.engine.save()  # what this process holds is on disk; the other flow's engine reads and writes the same pipeline.json
    other = Engine(ctx.project, flow=owner)
    if "rtl" not in other.by_name:
        raise StepFailed(f"flow '{owner.name}' has no step 'rtl' to hand the RTL fix to")
    other.request_change("rtl", text)
    ctx.engine.reload()  # (the change request is in the state now)
    return owner.name
