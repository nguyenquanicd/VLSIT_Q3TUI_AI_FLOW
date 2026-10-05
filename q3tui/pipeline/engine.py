"""Pipeline engine: step graph, staleness, gates, entry points (docs/spec/pipeline.md)."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from q3tui.core.events import EventBus
from q3tui.pipeline.base import StepContext, StepDef, StepFailed, checkpoint_dir
from q3tui.pipeline.state import GateRecord, PipelineState
from q3tui.core.project import Project, sha256_file, sha256_json

DECISION_PREFIX = "[decision]"  # change requests that come from the user's own answers to a later step's question

Status = Literal["pending", "stale", "done", "user", "failed", "running", "missing_input", "unavailable"]
Outcome = Literal["complete", "gate", "failed", "unavailable", "blocked", "cancelled", "restart"]


@dataclass
class StepView:
    name: str
    title: str
    status: Status
    gate: Literal["none", "open", "approved"]
    edited: list[str]
    open_questions: int
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.status in ("done", "user") and self.gate != "open"



def describe_error(exc: BaseException) -> str:
    """The real error(s) inside task-group wrappers, e.g. "StageError: model_x: …" instead of
    "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)"."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe_error(e) for e in exc.exceptions)
    if isinstance(exc, StepFailed):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"

class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(self, project: Project, bus: EventBus | None = None, steps: list[StepDef] | None = None,
                 flow: "flows.Flow | None" = None):
        from q3tui import flows

        self.project = project
        self.bus = bus or EventBus()
        if steps is not None:  # built by the caller (tests, extensions): a flow of exactly these steps
            self.flow = flow or flows.Flow("custom", [flows.StepSpec(s.name, s.kind or s.name) for s in steps])
            self.steps: list[StepDef] = steps
        else:
            self.flow = flow or flows.load_flow(project.flow_ref(), project.root)
            self.steps = flows.build_steps(self.flow)
            flows.apply_overrides(self.flow, project.cfg.pipeline.step_flow)
            for step in self.steps:  # q3tui.yaml `pipeline.step_options` on top of the flow file's options
                step.options = {**step.options, **(project.cfg.pipeline.step_options.get(step.name) or {})}
        self.by_name = {s.name: s for s in self.steps}
        self.state_path = project.state_dir / "pipeline.json"
        self.state = PipelineState.load(self.state_path)
        self._running: set[str] = set()  # steps executing right now (usually 0 or 1; 2 with parallel_rtl_tb)
        # parallel_rtl_tb + parallel_multi_agent: step name -> LLM role to use instead of the flow's shared
        # "flow" session for the duration of the parallel run (StepContext.llm, pipeline/base.py)
        self._step_role_override: dict[str, str] = {}
        from q3tui.core.stats import StatsRecorder

        if not getattr(self.bus, "stats_recorder", None):  # once per bus, however many engines share it
            self.bus.stats_recorder = StatsRecorder(project.state_dir / "stats.jsonl")
            self.bus.subscribe(self.bus.stats_recorder)
        self._drop_decisions_for_user_steps()
        self._refresh_reports()

    @property
    def running(self) -> str | None:
        """One running step's name (None: nothing is running). With parallel_rtl_tb both run_step() calls are
        in `_running` at once; this picks one for the single-name messages (quit confirm, reset's error) —
        `running_steps` lists all of them."""
        return next(iter(self._running), None)

    @running.setter
    def running(self, value: str | None) -> None:
        self._running = {value} if value else set()

    @property
    def running_steps(self) -> list[str]:
        return sorted(self._running)

    def is_running(self, name: str) -> bool:
        return name in self._running

    def user_owned(self, name: str) -> bool:
        """The step's output is yours (a hand-written spec, imported RTL …): no step rewrites it for a decision."""
        step = self.by_name.get(name)
        rec = self.state.steps.get(name)
        return step is not None and ((rec is not None and rec.origin == "user") or (rec is None and step.existing_user_outputs(self)))

    def _drop_decisions_for_user_steps(self) -> None:
        """Decisions queued for a user-provided step (before they were refused: a later step's gap on your own spec) would
        make it "stale" and run it with nothing to regenerate from: dropped."""
        dropped = []
        for name, fb in list(self.state.feedback.items()):
            if fb and self.user_owned(name):
                keep = [f for f in fb if not f.startswith(DECISION_PREFIX)]
                if len(keep) != len(fb):
                    dropped.append(name)
                    self.state.feedback[name] = keep
        if dropped:
            self.save()
            self.bus.emit("log", None, message=f"dropped queued decisions for {', '.join(dropped)}: its output is yours, nothing rewrites it")

    def _refresh_reports(self) -> None:
        changed = False
        for step in self.steps:
            try:
                written = step.refresh_reports(self)
            except Exception as exc:  # noqa: BLE001 - a report is never worth failing the startup
                self.bus.emit("warning", step.name, message=f"could not re-render reports: {exc}")
                continue
            rec = self.state.steps.get(step.name)
            for path in written:
                rel = self.project.rel(path)
                if rec is not None and rel in rec.outputs:
                    rec.outputs[rel] = sha256_file(path)
                    changed = True
                self.bus.emit("log", step.name, message=f"re-rendered {rel} (newer report layout)")
        if changed:
            self.save()

    # -- state helpers ----------------------------------------------------------------------

    read_only = False  # `q3tui tui --read-only`: look, never change anything

    def save(self) -> None:
        if self.read_only:
            return
        other = self.other_run()
        if other:  # another process runs the pipeline and owns the state: this one only watches
            if not getattr(self, "_warned_watch", False):
                self._warned_watch = True
                self.bus.emit("warning", None, message=f"a run is going on in another process (pid {other['pid']}): "
                                                       "watching only — nothing is saved here until it ends")
            return
        self.project.ensure_state_dir()
        self.state.save(self.state_path)

    # -- one run at a time (a TUI may watch a run started elsewhere) ----------------------------

    @property
    def lock_path(self) -> Path:
        return self.project.state_dir / "run.lock"

    def other_run(self) -> dict | None:
        """The run of another, live process ({pid, started}), or None."""
        import json
        import os

        try:
            info = json.loads(self.lock_path.read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if pid == os.getpid():
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None  # a run that was killed left its lock behind
        except PermissionError:
            pass
        return info

    def _take_lock(self) -> None:
        import json
        import os

        self.project.ensure_state_dir()
        self.lock_path.write_text(json.dumps({"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds")}))

    def _drop_lock(self) -> None:
        import json
        import os

        try:
            if int(json.loads(self.lock_path.read_text())["pid"]) == os.getpid():
                self.lock_path.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def reload(self) -> None:
        self.state = PipelineState.load(self.state_path)

    def step(self, name: str) -> StepDef:
        if name not in self.by_name:
            raise EngineError(f"unknown step '{name}' (steps: {', '.join(self.by_name)})")
        return self.by_name[name]

    def _hash_files(self, files: list[Path]) -> dict[str, str]:
        return {self.project.rel(f): sha256_file(f) for f in files if f.is_file()}

    def _inputs_hash_without_feedback(self, step: StepDef, feedback: list[str]) -> str:
        """The inputs hash as it was before `feedback` was queued (to tell if anything else changed)."""
        saved = self.state.feedback.get(step.name, [])
        self.state.feedback[step.name] = [f for f in saved if f not in feedback]
        try:
            return self.inputs_hash(step)
        finally:
            self.state.feedback[step.name] = saved

    def inputs_hash(self, step: StepDef) -> str:
        data = {
            "files": self._hash_files(step.inputs(self)),
            "missing": sorted(self.project.rel(f) for f in step.inputs(self) if not f.is_file()),
            "feedback": self.state.feedback.get(step.name, []),
        }
        if step.checks_version:  # Q3TUI's checks on this step's result changed: re-check it (no LLM when it passes)
            data["checks"] = step.checks_version
        spec = self.flow.spec(step.name)
        if spec is not None and (spec.options or spec.deps is not None or spec.passes or spec.notes):  # the flow file's settings for it are inputs too
            data["flow"] = spec.fingerprint()
        return sha256_json(data)

    # -- flow: gates, labels ------------------------------------------------------------------------

    def gate_modes(self) -> dict[str, str]:
        """step -> gate mode (human | auto | auto_answer) for every step with a review gate. The flow file sets them;
        `pipeline.gates` (when set) replaces which steps have one; `pipeline.gate_modes` overrides the mode."""
        pc = self.project.cfg.pipeline
        modes = dict(self.flow.gates)
        if "gates" in pc.model_fields_set:
            modes = {g: modes.get(g, "human") for g in pc.gates}
        modes.update(pc.gate_modes)
        return {k: v for k, v in modes.items() if v != "none" and k in self.by_name}

    def spec_template(self) -> str | None:
        """The spec template the flow asked the spec step for (`options: {template: vlsit}`), None: the default one. Everything
        that shows or edits the spec's sections must load the same template the spec step wrote it with."""
        step = next((s for s in self.steps if s.kind == "spec"), None)
        return (step.options.get("template") if step else None) or None

    def session_mode(self) -> str:
        """"flow": every LLM task of the flow goes to one persistent session (VLSIT, like the original single Claude Code
        conversation); "fresh": per-stage sessions (Q3TUI flows; team mode may group them by role)."""
        return self.project.cfg.pipeline.session or self.flow.session

    def gate_mode(self, name: str) -> str | None:
        return self.gate_modes().get(name)

    def label(self, name: str) -> str:
        """The short tag shown before a step's name: the flow's, else its position."""
        spec = self.flow.spec(name)
        if spec is not None and spec.label:
            return spec.label
        names = [s.name for s in self.pipeline_steps]
        return str(names.index(name) + 1) if name in names else ""

    def gate_required(self, step: StepDef) -> bool:
        """A configured review gate — or a one-off one: a gateless step asked blocking questions."""
        if step.name in self.gate_modes():
            return True
        rec = self.state.gates.get(step.name)
        return rec is not None and rec.for_questions and rec.status == "open"

    # -- evaluation ---------------------------------------------------------------------------

    def evaluate(self, step: StepDef) -> StepView:
        rec = self.state.steps.get(step.name)
        gate_rec = self.state.gates.get(step.name)
        gate = "none" if not self.gate_required(step) else ("approved" if gate_rec and gate_rec.status == "approved" else "open")
        edited: list[str] = []
        detail = ""

        if self.is_running(step.name):
            status: Status = "running"
        elif rec and rec.status == "running" and self.other_run():
            status, detail = "running", "running in another process"
        elif rec and rec.status == "running":
            # left "running" by a process that was killed mid-step: interrupted — it runs again (never "yours")
            status, detail = ("stale" if rec.outputs else "pending"), "interrupted (the run was stopped during this step)"
        elif rec and rec.status == "done":
            status = "user" if rec.origin == "user" else "done"
            if rec.inputs_hash != self.inputs_hash(step):
                status = "stale"
            current = self._hash_files(step.outputs(self))
            edited = sorted(p for p, h in rec.outputs.items() if current.get(p) not in (None, h))
        elif rec and rec.status == "failed":
            status, detail = "failed", rec.error or ""
        elif step.existing_user_outputs(self):
            status = "user"
        elif not step.implemented:
            status, detail = "unavailable", f"planned for {step.milestone}"
        elif (msg := step.missing_input(self)) is not None:
            status, detail = "missing_input", msg
        else:
            status = "pending"

        if status in ("stale", "failed") and not self.is_running(step.name):
            # a step that would run again waits too when what it needs is not there (e.g. the top simulation while
            # blocks are still being verified)
            msg = step.missing_input(self)
            if msg is not None:
                status, detail = "missing_input", msg
        if status == "user":
            gate = "none" if gate == "none" else "approved"  # your own artifacts need no review
        if status in ("pending", "missing_input", "unavailable", "failed") and gate == "open":
            gate = "none" if not rec else gate
        return StepView(step.name, step.title, status, gate, edited, self.open_question_count(step.name) if status in ("done", "user", "stale") else 0, detail)

    def view_files(self, name: str) -> list[Path]:
        """Extra files the flow file wants shown for a step (its `view` globs)."""
        spec = self.flow.spec(name)
        return sorted({p for g in (spec.view if spec else []) for p in self.project.root.glob(g) if p.is_file()})

    @property
    def pipeline_steps(self) -> list[StepDef]:
        """The steps of the pipeline, in order."""
        return list(self.steps)

    def status(self) -> list[StepView]:
        self._questions_memo = self.questions()
        try:
            return [self.evaluate(s) for s in self.pipeline_steps]
        finally:
            self._questions_memo = None

    # -- user actions ------------------------------------------------------------------------

    def approve(self, name: str, force: bool = False, auto: bool = False) -> str:
        """Approve a gate. Open non-blocking questions are closed with their default
        assumption (already written into the output); open blocking questions refuse
        the approval unless `force`."""
        step = self.step(name)
        view = self.evaluate(step)
        if not self.gate_required(step):
            return f"'{name}' has no review gate"
        if view.status not in ("done", "user"):
            raise EngineError(f"cannot approve '{name}': step is {view.status}")
        open_q = [q for q in self.questions() if q["step"] == name and q.get("status") == "open"]
        blocking = [q for q in open_q if q.get("blocking")]
        if blocking and not force:
            ids = ", ".join(q["id"] for q in blocking)
            raise EngineError(f"cannot approve '{name}': blocking question(s) {ids} need an answer (or approve with force)")
        if not force and not auto and not self.auto_confirm_reviews():
            # `auto` (gate mode "auto", or --yes/auto-answer's own force=True call): the gate still signs itself —
            # unconfirmed items simply do not count (RTM condition 2, "SVA traced"), same as a human who signs with none reviewed
            pending = step.pending_reviews(self)
            if pending:
                shown = ", ".join(pending[:5]) + (f", … ({len(pending)} total)" if len(pending) > 5 else "")
                raise EngineError(f"cannot approve '{name}': {len(pending)} item(s) not reviewed yet ({shown}) "
                                  "— review them, turn on auto-confirm (Settings → General), or approve with force")
        self._accept_defaults(step, open_q)
        self.state.gates[name] = GateRecord(status="approved", approved_at=datetime.now().isoformat(timespec="seconds"))
        step.on_approve(self)
        rec = self.state.steps.get(name)
        if rec is not None and rec.status == "done":  # (the step's own writes at approval are not user edits)
            rec.outputs = self._hash_files(step.outputs(self))
        if name == "spec":
            from q3tui.steps.spec.sections import mark_reviewed

            mark_reviewed(self)
        self.save()
        self.bus.emit("gate_resolved", name, gate=name, decision="approved", accepted_defaults=[q["id"] for q in open_q])
        note = f"; accepted the default for {', '.join(q['id'] for q in open_q)}" if open_q else ""
        return f"approved {name}{note}"

    def _accept_defaults(self, step: StepDef, open_q: list[dict], defer: bool = False) -> None:
        """Close open questions with their default assumption (already in the step's output)."""
        name = step.name
        accepted = self.state.accepted_defaults.setdefault(name, {})
        for q in open_q:
            accepted[q["id"]] = q.get("default_assumption") or "(no default — approved without an answer)"
            self._close_question(name, q, accepted[q["id"]], "default")
            todo = getattr(step, "default_to_apply", lambda q: None)(q)
            if todo:  # a default that is not in the step's output yet: a change to make
                self.push_decision(name, name, f"From {name} question {q['id']} ({q['question']}): {todo}", rebase=False)
                continue
            target = self.gap_target(name, q)
            if target and q.get("default_assumption"):
                # the default is already in this step's output; hand it to the step it belongs to,
                # without making this step redo its work because of that edit
                # (a step that only proposes has nothing to rebase)
                # accepted automatically (run --yes, auto-approve, no gate): recorded on the owning step and folded in
                # the next time it runs anyway — writing it down alone must not re-run the pipeline behind it
                self._push_decision(name, target, q, q["default_assumption"],
                                    rebase=not getattr(step, "proposes_only", False), defer=defer)
        self._refresh_questions_file(step)

    def _auto_answer_proposals(self, step: StepDef) -> bool:
        """pipeline.auto_answer: a step that only proposes gets its proposals accepted as suggested — you
        chose to run unattended — and the fix loop goes on (bounded by pipeline.max_fix_iterations)."""
        open_q = [q for q in self.questions() if q["step"] == step.name and q["status"] == "open"]
        if not open_q:
            return True
        meta = self.state.step_meta.setdefault(step.name, {})
        run = getattr(self, "_run_id", None)
        if meta.get("auto_answered_run") != run:  # the bound is per run: an earlier run's rounds do not use this one's up
            meta["auto_answered"], meta["auto_answered_run"] = 0, run
        rounds = int(meta.get("auto_answered", 0))
        if rounds >= self.project.cfg.pipeline.max_fix_iterations:
            self.bus.emit("warning", step.name, message=f"auto-answer: {rounds} round(s) of accepted proposals already "
                                                        f"(pipeline.max_fix_iterations); waiting for you on "
                                                        f"{', '.join(q['id'] for q in open_q)}")
            self.bus.emit("gate_opened", step.name, gate=step.name, open_questions=len(open_q),
                          files=[self.project.rel(p) for p in step.outputs(self)])
            return False
        for q in open_q:
            self.answer(q["id"], q.get("default_assumption") or "yes")
        meta["auto_answered"] = rounds + 1
        meta["loop"] = True  # the accepted changes update their steps, then this step runs again
        self.save()
        self.bus.emit("warning", step.name, message=f"auto-answer: accepted {', '.join(q['id'] for q in open_q)} as proposed "
                                                    f"(a changed document is re-reviewed by nobody — you are running unattended)")
        return True

    def pending_approvals(self) -> list[str]:
        """Gates that are open on a finished, up-to-date step, in pipeline order."""
        return [v.name for v in self.status() if v.gate == "open" and v.status in ("done", "user")]

    def approve_all(self, force: bool = False) -> list[str]:
        """Approve every open gate that can be approved. Gates with blocking questions are
        skipped (unless `force`); returns one line per gate."""
        lines = []
        for name in self.pending_approvals():
            try:
                lines.append(self.approve(name, force=force))
            except EngineError as exc:
                lines.append(f"skipped {name}: {exc}")
        stale = [v.name for v in self.status() if v.gate == "open" and v.status == "stale"]
        if stale:
            lines.append(f"out of date, run first: {', '.join(stale)}")
        return lines or ["nothing waiting for review"]

    def request_change(self, name: str, text: str) -> str:
        """Queue feedback for a step (also used for 'reject with feedback'); makes it stale."""
        step = self.step(name)
        if not step.implemented:
            raise EngineError(f"'{name}' is not implemented yet")
        self.state.feedback.setdefault(name, []).append(text.strip())
        if self.gate_required(step):
            self.state.gates[name] = GateRecord(status="open")
        self.save()
        self.bus.emit("gate_resolved", name, gate=name, decision="changes_requested", feedback=text)
        return f"queued change for {name}; it will re-run on the next `run`"

    def _close_question(self, step: str, q: dict, answer: str, how: str) -> None:
        self.state.closed_questions.setdefault(step, {})[q["id"]] = {
            "question": q["question"], "answer": answer, "how": how, "blocking": q.get("blocking", False),
            "at": datetime.now().isoformat(timespec="seconds"),
        }

    def _refresh_questions_file(self, step: StepDef) -> None:
        """Rewrite the step's questions Markdown and keep its recorded hash in sync (not a user edit)."""
        writer = getattr(step, "write_questions_file", None)
        path = writer(self) if writer else None
        rec = self.state.steps.get(step.name)
        if path and rec and self.project.rel(path) in rec.outputs:
            rec.outputs[self.project.rel(path)] = sha256_file(path)

    def answer(self, question_id: str, text: str) -> str:
        current = {q["id"]: q for q in self.questions()}
        for step in self.steps:
            answer_fn = getattr(step, "answer", None)
            if answer_fn and answer_fn(self, question_id, text):
                self.state.accepted_defaults.get(step.name, {}).pop(question_id, None)
                q = current.get(question_id)
                if q is not None:
                    self._close_question(step.name, q, text.strip(), "answered")
                target = self.gap_target(step.name, q) if q is not None else None
                routes = getattr(step, "routes_answer", None)
                if target and routes is not None and not routes(q, text):
                    target = None  # e.g. a rejected proposal: nothing is sent anywhere
                if target:
                    self._push_decision(step.name, target, q, text.strip(), rebase=False)
                gate = self.state.gates.get(step.name)
                if gate and gate.for_questions and not any(
                        x["step"] == step.name and x["status"] == "open" and x.get("blocking") for x in self.questions()):
                    self.state.gates.pop(step.name)  # the one-off review is over: the run can go on
                self._refresh_questions_file(step)
                self.save()
                self.bus.emit("log", step.name, message=f"answered {question_id}" + (f" (handed to {target})" if target else ""))
                where = f"{target}, then {step.name}," if target else step.name
                return f"answered {question_id}; {where} will update on the next `run`"
        raise EngineError(f"unknown question '{question_id}'")

    def answer_all(self) -> str:
        """Answer every open question with its default assumption as a real answer (blocking ones too): the step updates its
        document with it (an assumption written in the spec becomes a statement), as if you had typed it. A step that only proposes changes to
        your documents is skipped: its proposals are never accepted on your behalf."""
        done: list[str] = []
        skipped: list[str] = []
        for step in self.steps:
            open_q = [q for q in self.questions() if q["step"] == step.name and q["status"] == "open"]
            if not open_q:
                continue
            if getattr(step, "proposes_only", False):
                skipped += [q["id"] for q in open_q]
                continue
            plain = []
            for q in open_q:
                if q.get("default_assumption"):  # a real answer: the step (and the one that owns it) update the document with it
                    try:
                        self.answer(q["id"], q["default_assumption"])
                        continue
                    except EngineError:
                        pass
                plain.append(q)
            if plain:  # no default text to apply
                self._accept_defaults(step, plain)
            gate = self.state.gates.get(step.name)
            if gate and gate.for_questions:
                self.state.gates.pop(step.name)  # the one-off stop for blocking questions is over
            done += [q["id"] for q in open_q]
            self.bus.emit("log", step.name, message=f"answered with the defaults: {', '.join(q['id'] for q in open_q)}")
        self.save()
        if not done and not skipped:
            return "no open questions"
        return (f"answered {len(done)} question(s) with their defaults ({', '.join(done)}); the steps update on the next `run`" if done else "") \
            + (f"{'; ' if done else ''}left for you (proposals change your documents): {', '.join(skipped)}" if skipped else "")

    def gap_target(self, origin: str, q: dict) -> str | None:
        """The earlier step a question belongs to ("→ spec", or the flow's `gap_targets`), or None when
        the answer stays in `origin` (design choice, or a gap toward itself / a later step)."""
        from q3tui.steps.common import GAP_TARGETS

        # no kind given: the testbench's question is about its own work (a design choice), anyone else's a gap
        kind = q.get("kind") or ("design_choice" if origin.split(":")[-1] == "tb" else "spec_gap")
        target = self.flow.gap_targets.get(kind, GAP_TARGETS.get(kind))
        order = [s.name for s in self.steps]
        if target not in order or origin not in order or order.index(target) >= order.index(origin):
            return None
        rec = self.state.steps.get(target)
        if (rec is not None and rec.origin == "user") or (rec is None and self.by_name[target].existing_user_outputs(self)):
            return None  # your own artifact: no step rewrites it for a decision — the answer stays with the step that asked
        return target

    _GAP_HINT = {"spec": "The spec did not settle this; state it in the appropriate section."}

    def _push_decision(self, origin: str, target: str, q: dict, answer: str, rebase: bool, defer: bool = False) -> None:
        """A gap found by a later step was decided (answered, or its default accepted): a change request on the step
        it belongs to, applied at once — the run goes back and updates that step (the documents always say what was
        decided). `defer` is ignored (decisions used to wait for the step's next run)."""
        text = f"From {origin} question {q['id']} ({q['question']}): {answer}. " + self._GAP_HINT.get(target, "")
        text = text.rstrip()
        self.push_decision(target, origin, text, rebase)

    def _apply_old_deferred(self) -> None:
        """Projects from when accepted defaults waited: their queued decisions become change requests now."""
        for name, meta in self.state.step_meta.items():
            for d in meta.pop("deferred_decisions", []):
                self.state.feedback.setdefault(name, []).append(f"{DECISION_PREFIX} {d['text']}")
                if d.get("rebase"):
                    meta["rebase"] = sorted(set(meta.get("rebase", [])) | {d["origin"]})
        self.save()

    def deferred_decisions(self, name: str) -> list[dict]:
        """Accepted defaults recorded for `name` but not written into it yet (applied when it next runs)."""
        return list(self.state.step_meta.get(name, {}).get("deferred_decisions", []))

    def push_decision(self, target: str, origin: str, text: str, rebase: bool) -> None:
        """Queue a user decision made in a later step as a change request on `target`.

        rebase=True: the origin step's output already reflects the decision, so once the target
        absorbs it the origin step is marked up to date instead of re-running (keeps user edits)."""
        if self.user_owned(target):  # (see gap_target: the answer stays with the step that asked)
            self.bus.emit("log", origin, message=f"decision not sent to {target}: its output is yours")
            return
        self.state.feedback.setdefault(target, []).append(f"{DECISION_PREFIX} {text}")
        if rebase:
            meta = self.state.step_meta.setdefault(target, {})
            meta["rebase"] = sorted(set(meta.get("rebase", [])) | {origin})
        self.save()

    def push_spec_decision(self, origin: str, text: str, rebase: bool) -> None:
        self.push_decision("spec", origin, text, rebase)

    def questions(self) -> list[dict]:
        """Current questions of every step (status open / answered / default) plus closed ones
        that later runs dropped (history=True)."""
        out = []
        for step in self.steps:
            fn = getattr(step, "question_list", None)
            seen: set[str] = set()
            accepted = self.state.accepted_defaults.get(step.name, {})
            for q in (fn(self) if fn else []):
                q = dict(q, step=step.name)
                if not q.get("options"):  # every question has a pick list
                    from q3tui.steps.common import derive_options

                    q["options"] = derive_options(q.get("question", ""), q.get("default_assumption") or "")
                seen.add(q["id"])
                if q.get("answer"):
                    q["status"] = "answered"
                elif q["id"] in accepted:
                    q["status"], q["answer"] = "default", accepted[q["id"]]
                else:
                    q["status"] = "open"
                out.append(q)
            for qid, h in self.state.closed_questions.get(step.name, {}).items():
                if qid not in seen:
                    out.append({"id": qid, "step": step.name, "question": h["question"], "answer": h["answer"],
                                "blocking": h.get("blocking", False), "default_assumption": "",
                                "status": "default" if h["how"] == "default" else "answered", "history": True, "closed_at": h["at"]})
        return out

    def settled_questions(self, name: str) -> list[dict]:
        """Questions of the steps before `name` that the user answered or accepted a default
        for: decisions later steps must apply and never ask again."""
        before = {s.name for s in self.steps[: [s.name for s in self.steps].index(name)]}
        if name in self.flow.sees:  # independence: decisions only reach steps allowed to see the step that made them
            before &= set(self.flow.sees[name])
        return [q for q in self.questions() if q["step"] in before and q["status"] in ("answered", "default") and q.get("answer")]

    def open_question_count(self, name: str) -> int:
        qs = getattr(self, "_questions_memo", None) or self.questions()
        return sum(q["step"] == name and q["status"] == "open" for q in qs)

    # -- cost ------------------------------------------------------------------------------------

    def cost_summary(self) -> dict:
        return {
            "total_usd": self.state.total_cost_usd,
            "steps": {n: r.cost_usd for n, r in self.state.steps.items() if r.cost_usd},
            "resets": self.state.cost_resets,
        }

    def reset_cost(self) -> str:
        amount = self.state.total_cost_usd
        self.state.cost_resets.append({"at": datetime.now().isoformat(timespec="seconds"), "amount_usd": round(amount, 4)})
        self.state.total_cost_usd = 0.0
        for rec in self.state.steps.values():
            rec.cost_usd = 0.0
        self.save()
        return f"pipeline cost reset to $0.00 (was ${amount:.2f}; recorded in pipeline.json cost_resets)"

    # -- reset ---------------------------------------------------------------------------------

    def downstream(self, name: str) -> list[str]:
        """`name` and every step that depends on it (transitively), in pipeline order."""
        affected = {name}
        for step in self.steps:
            if any(d in affected for d in step.deps):
                affected.add(step.name)
        return [s.name for s in self.steps if s.name in affected]

    def reset_plan(self, name: str, only: bool = False) -> dict[str, list[Path]]:
        """Files a reset would delete, per step. User-provided artifacts are never deleted."""
        self.step(name)
        plan: dict[str, list[Path]] = {}
        for n in [name] if only else self.downstream(name):
            step = self.by_name[n]
            rec = self.state.steps.get(n)
            files: list[Path] = []
            user_provided = (rec is not None and rec.origin == "user") or (rec is None and step.existing_user_outputs(self))
            if user_provided:
                plan[n] = []  # your own artifacts: forget nothing on disk
                continue
            if rec and rec.origin == "generated":
                files += [self.project.root / rel for rel in rec.outputs]
            files += step.reset_files(self)
            seen, keep = set(), []
            for f in files:
                r = f.resolve()
                if f.is_file() and r not in seen:
                    seen.add(r)
                    keep.append(f)
            plan[n] = keep
        return plan

    def reset(self, name: str, only: bool = False) -> list[Path]:
        """Delete generated outputs + review/answer state of `name` (and downstream); back up first."""
        if self.running:
            raise EngineError(f"'{', '.join(self.running_steps)}' is running; stop it before resetting")
        if self.read_only:
            raise EngineError("read-only: this TUI only looks (start it without --read-only to reset)")
        if self.other_run():
            raise EngineError(f"a run is going on in another process (pid {self.other_run()['pid']}): stop it before resetting")
        plan = self.reset_plan(name, only)
        files = [f for fs in plan.values() for f in fs]
        full = set(plan) >= {s.name for s in self.pipeline_steps}
        if full and self.session_mode() == "flow":  # starting over: the flow's one session must not remember the old design
            from q3tui.llm import roles

            notes = roles.notes_path(self.project, "flow")
            files += [notes] if notes.is_file() else []
            roles.team(self.project).reset("flow")
            self.bus.emit("log", name, message="reset the flow's LLM session too (it would remember the old design)")
        backup = self.project.backup(files)
        for f in files:
            f.unlink(missing_ok=True)
        for n in plan:
            self.by_name[n].on_reset(self)
            self.state.sections.pop(n, None)
            self.state.accepted_defaults.pop(n, None)
            self.state.closed_questions.pop(n, None)
            self.state.step_meta.pop(n, None)
            self.state.steps.pop(n, None)
            self.state.gates.pop(n, None)
            self.state.feedback.pop(n, None)
        self.save()
        where = f" (backup: {self.project.rel(backup)})" if backup else ""
        self.bus.emit("log", name, message=f"reset {', '.join(plan)}: removed {len(files)} file(s){where}")
        return files

    # -- imports (bring your own artifacts) ---------------------------------------------------

    def import_intent(self, text: str) -> Path:
        path = self.project.spec_dir / "intent.md"
        if path.is_file() and path.read_text().strip() == text.strip():
            return path
        self.project.backup([path])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n")
        return path

    def import_spec(self, files: list[Path]) -> list[Path]:
        dest = []
        for f in files:
            target = self.project.spec_dir / f.name
            if f.resolve() != target.resolve():
                self.project.backup([target])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
            dest.append(target)
        return dest

    # -- execution -----------------------------------------------------------------------------

    async def run_step(self, step: StepDef, regenerate: bool = False) -> None:
        emit = self.bus.scoped(step.name)
        rec = self.state.step(step.name)
        previous = rec.model_copy()  # restored if the run is cancelled
        meta = self.state.step_meta.setdefault(step.name, {})
        for d in meta.pop("deferred_decisions", []):  # the step runs anyway: fold in the defaults accepted meanwhile
            self.state.feedback.setdefault(step.name, []).append(f"{DECISION_PREFIX} {d['text']}")
            if d.get("rebase"):
                meta["rebase"] = sorted(set(meta.get("rebase", [])) | {d["origin"]})
        feedback = list(self.state.feedback.get(step.name, []))
        prev_gate = self.state.gates.get(step.name)
        decisions_only = bool(feedback) and all(f.startswith(DECISION_PREFIX) for f in feedback)
        other_inputs_same = rec.status == "done" and rec.inputs_hash == self._inputs_hash_without_feedback(step, feedback)
        self._running.add(step.name)
        rec.status, rec.error = "running", None
        self.save()
        emit("step_started", title=step.title, feedback=feedback, regenerate=regenerate)
        fspec = self.flow.spec(step.name)
        ctx = StepContext(self, emit, feedback=feedback, regenerate=regenerate, notes=fspec.notes if fspec else "")
        checkpoints = checkpoint_dir(self.project, step.name)
        if regenerate:
            shutil.rmtree(checkpoints, ignore_errors=True)
        try:
            await step.run(ctx)
            if fspec and fspec.passes:  # the flow file's pass conditions: a step that does not meet them has failed
                from q3tui.flows import check_pass

                unmet = check_pass(fspec.passes, self.project.root)
                if unmet:
                    raise StepFailed("pass condition not met: " + "; ".join(unmet))
        except BaseException as exc:
            while isinstance(exc, BaseExceptionGroup) and all(isinstance(e, (StepFailed, BaseExceptionGroup)) for e in exc.exceptions):
                exc = exc.exceptions[0]  # (a step's own failures raised inside a task group: the failure, not the wrapper)
            self._running.discard(step.name)
            self.state.total_cost_usd += ctx.cost_usd
            if isinstance(exc, StepFailed):  # its own checks failed: replaying the same answers would fail the same way
                shutil.rmtree(checkpoints, ignore_errors=True)
            if isinstance(exc, Exception):
                rec.status, rec.error, rec.cost_usd = "failed", describe_error(exc), ctx.cost_usd
                self.save()
                emit("step_finished", status="failed", error=rec.error)
            else:  # cancelled (stop / quit): back to where it was
                self.state.steps[step.name] = previous
                self.save()
                emit("step_finished", status="cancelled")
            raise exc
        self._running.discard(step.name)
        shutil.rmtree(checkpoints, ignore_errors=True)  # finished: nothing to resume
        if feedback:
            self.state.feedback_history.setdefault(step.name, []).extend(feedback)
            self.state.feedback[step.name] = self.state.feedback.get(step.name, [])[len(feedback):]
        rec.status, rec.origin = "done", "generated"
        rec.outputs = self._hash_files(step.outputs(self))
        # change requests that arrived DURING the run are not in its result yet: it stays stale for them
        arrived = list(self.state.feedback.get(step.name, []))
        rec.inputs_hash = self._inputs_hash_without_feedback(step, arrived) if arrived else self.inputs_hash(step)
        rec.finished = datetime.now().isoformat(timespec="seconds")
        rec.cost_usd = ctx.cost_usd
        self.state.total_cost_usd += ctx.cost_usd
        blocking = [q for q in self.questions() if q["step"] == step.name and q["status"] == "open" and q.get("blocking")]
        if step.name not in self.gate_modes():
            # no review gate: blocking questions still stop the pipeline until you answer them
            if blocking:
                self.state.gates[step.name] = GateRecord(status="open", for_questions=True)
            else:
                self.state.gates.pop(step.name, None)
        elif self.gate_required(step):
            if decisions_only and other_inputs_same and prev_gate and prev_gate.status == "approved":
                # only your own decisions changed it: the review stays approved
                self.state.gates[step.name] = prev_gate
                emit("log", message="updated with your decisions; review kept (changed sections show as not reviewed)")
            elif ctx.unchanged and prev_gate and prev_gate.status == "approved":
                self.state.gates[step.name] = prev_gate
                emit("log", message="result unchanged; your approval is kept")
            else:
                self.state.gates[step.name] = GateRecord(status="open")
        names = [s.name for s in self.steps]
        for origin in self.state.step_meta.get(step.name, {}).pop("rebase", []):
            o_rec, o_step = self.state.steps.get(origin), self.by_name.get(origin)
            if o_rec and o_step and o_rec.status == "done" and self._rebase_ok(o_step, emit):
                o_step.on_rebase(self)
                o_rec.inputs_hash = self.inputs_hash(o_step)  # it already contains the decision
                # the steps between (e.g. req, between spec and arch) update for the same decision next: the origin
                # already contains it, so their update must not send it round again
                between = names[names.index(step.name) + 1: names.index(origin)] if origin in names else []
                if between:
                    self.state.step_meta.setdefault(origin, {})["rebase_after"] = between
        own = [f for f in feedback if not f.startswith(DECISION_PREFIX)]
        for origin, m in list(self.state.step_meta.items()):
            chain = m.get("rebase_after") or []
            if step.name not in chain or not isinstance(m, dict):
                continue
            chain.remove(step.name)
            o_rec, o_step = self.state.steps.get(origin), self.by_name.get(origin)
            if not own and o_rec and o_step and o_rec.status == "done" and self._rebase_ok(o_step, emit):
                # it ran for the origin's decisions only
                o_step.on_rebase(self)
                o_rec.inputs_hash = self.inputs_hash(o_step)
                emit("log", message=f"updated for {origin}'s own decisions: {origin} already contains them (kept, not re-run)")
            if not chain:
                m.pop("rebase_after", None)
        self.save()
        emit("step_finished", status="done", cost_usd=ctx.cost_usd, outputs=list(rec.outputs))

    def _rebase_ok(self, step: StepDef, emit) -> bool:
        try:
            problems = step.rebase_problems(self)
        except Exception as exc:  # noqa: BLE001 — a check that cannot run: re-run the step rather than assume
            problems = [f"its checks could not run ({exc})"]
        if problems:
            emit("warning", message=f"{step.name} is not kept as it is — against its new inputs: "
                                    + "; ".join(problems[:5]) + f". {step.name} runs again")
        return not problems

    def _record_user(self, step: StepDef) -> None:
        rec = self.state.step(step.name)
        rec.status, rec.origin = "done", "user"
        rec.outputs = self._hash_files(step.outputs(self))
        rec.inputs_hash = self.inputs_hash(step)
        rec.finished = datetime.now().isoformat(timespec="seconds")
        self.save()

    def _range(self, start: str | None, stop: str | None, only: str | None) -> list[str]:
        names = [s.name for s in self.steps]
        if only:
            self.step(only)
            return [only]
        lo = names.index(self.step(start).name) if start else 0
        hi = names.index(self.step(stop).name) if stop else len(names) - 1
        if lo > hi:
            raise EngineError(f"--from {start} comes after --to {stop}")
        return names[lo : hi + 1]

    async def run(
        self,
        start: str | None = None,
        stop: str | None = None,
        only: str | None = None,
        yes: bool = False,
        regenerate: set[str] | None = None,
    ) -> Outcome:
        """Run pending/stale steps in order until done, a gate, or an error."""
        if self.read_only:
            self.bus.emit("error", message="read-only: this TUI only looks (start it without --read-only to run)")
            return "blocked"
        other = self.other_run()
        if other:
            self.bus.emit("error", message=f"a run is already going on in another process (pid {other['pid']}, started "
                                           f"{other.get('started', '?').replace('T', ' ')}): watch it, or stop it first")
            return "blocked"
        self._take_lock()
        try:
            return await self._run_locked(start, stop, only, yes, regenerate)
        finally:
            self._drop_lock()

    def auto_reviews(self) -> bool:
        """Auto approve in the wide sense (project switches `pipeline.auto_approve` / `auto_answer`, or this run's --yes):
        besides the gates, the VLSIT flow confirms its assertions and signs its ready requirements itself
        (step options `sva.auto_confirm`, `verify.auto_sign` override per step)."""
        pc = self.project.cfg.pipeline
        return bool(pc.auto_approve or pc.auto_answer or getattr(self, "_yes", False))

    def auto_confirm_reviews(self) -> bool:
        """Confirm a step's own reviewable items (SVA assertions, flagged requirements, …) without asking: the
        dedicated `pipeline.auto_confirm_reviews` switch (Settings → General), or any wider unattended mode
        (`auto_reviews()`). Narrower than `auto_reviews()`: a project may skip per-item review while still
        stopping at every gate for a human to press Approve."""
        return bool(self.project.cfg.pipeline.auto_confirm_reviews or self.auto_reviews())

    async def _run_locked(self, start, stop, only, yes, regenerate) -> Outcome:
        self._yes = bool(yes)
        regenerate = regenerate or set()
        selected = self._range(start, stop, only)
        run_dir = self.project.new_run_dir()
        self._run_id = run_dir.name
        self.bus.log_to(run_dir / "events.jsonl")
        self.bus.emit("run_started", steps=selected, run_dir=str(run_dir))
        outcome: Outcome = "complete"
        self._apply_old_deferred()
        try:
            outcome = await self._run(selected, yes, regenerate)
            for _ in range(len(self.steps)):  # a gate sent decisions to an earlier step: update it, then go on
                if outcome != "restart":
                    break
                outcome = await self._run(selected, yes, set())
            if outcome == "restart":
                outcome = "complete"
            # the fix loop: a step dispatched fixes (verify) -> the owning steps update, then it looks again
            # (bounded by pipeline.max_fix_iterations; a review gate or a failure stops it)
            while outcome in ("complete", "unavailable", "blocked") and (
                    looping := [n for n, m in self.state.step_meta.items() if isinstance(m, dict) and m.get("loop")]):
                for name in looping:
                    self.state.step_meta[name]["loop"] = False
                self.save()
                for name in looping:
                    self.bus.emit("log", name.split(":")[0], message="fix loop: updating the steps that got fixes, then running again")
                outcome = await self._run(selected, yes, set())
                while outcome == "restart":
                    outcome = await self._run(selected, yes, set())
        except StepFailed as exc:
            self.bus.emit("error", message=str(exc))
            outcome = "failed"
        except Exception as exc:  # noqa: BLE001 - reported to the user, state already saved
            self.bus.emit("error", message=describe_error(exc))
            outcome = "failed"
        except BaseException:
            self.bus.emit("run_finished", outcome="cancelled")
            raise
        self.bus.emit("run_finished", outcome=outcome, total_cost_usd=self.state.total_cost_usd)
        return outcome

    def _parallel_tb_ready(self, selected: list[str], regenerate: set[str]) -> StepDef | None:
        """vlsit: rtl (lint + synthesis) and tb (testbench generator) both read only spec/parse/config
        (FLOW.md `sees`), neither needs the other's output. `pipeline.parallel_rtl_tb` on, 'tb' in the
        requested range, and it actually needs a real run: returns the tb StepDef to run alongside rtl."""
        if not self.project.cfg.pipeline.parallel_rtl_tb:
            return None
        tb_step = self.by_name.get("tb")
        if tb_step is None or tb_step.name not in selected:
            return None
        view = self.evaluate(tb_step)
        if view.status == "unavailable" or view.status == "missing_input":
            return None
        if view.status in ("user", "done") and tb_step.name not in regenerate:
            return None  # skipped same as the sequential path would skip it — nothing to parallelise
        return tb_step

    async def _run_parallel(self, rtl_step: StepDef, rtl_regen: bool, tb_step: StepDef, tb_regen: bool) -> None:
        """Run rtl and tb together. The flow's one shared session (role "flow") runs one task at a time —
        without parallel_multi_agent, tb's LLM calls queue behind rtl's and only the non-LLM parts of each
        step (tool runs: lint, synth, sim) truly overlap. parallel_multi_agent gives tb its own persistent
        session (role "verifier", the same charter tb's stages would get outside flow sessions) so both
        sets of LLM calls run at the same time too."""
        multi_agent = self.project.cfg.pipeline.parallel_multi_agent
        if multi_agent:
            self._step_role_override[tb_step.name] = "verifier"
        self.bus.emit("log", None, message=f"running {rtl_step.name} and {tb_step.name} in parallel"
                      + (" (tb: its own agent session)" if multi_agent
                         else " (shared session: tb's LLM calls queue behind rtl's; their tool runs still overlap)"))
        try:
            results = await asyncio.gather(self.run_step(rtl_step, regenerate=rtl_regen),
                                           self.run_step(tb_step, regenerate=tb_regen), return_exceptions=True)
        finally:
            self._step_role_override.pop(tb_step.name, None)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]

    async def _run(self, selected: list[str], yes: bool, regenerate: set[str]) -> Outcome:
        first = selected[0]
        started = False
        handled: set[str] = set()  # tb already run alongside rtl: its own loop turn is a no-op
        for step in self.steps:
            if step.name in handled:
                continue
            view = self.evaluate(step)
            in_range = step.name in selected
            if step.name == first:
                started = True
            if not started:
                # Upstream of the requested range: must already be usable.
                if view.status == "user":
                    self._record_user(step)
                if view.status not in ("done", "user", "stale"):
                    self.bus.emit("error", step.name, message=f"'{step.name}' is {view.status}; run it first or drop --from")
                    return "blocked"
                if view.status == "stale":
                    self.bus.emit("warning", step.name, message=f"'{step.name}' is stale but outside the requested range")
                if not self._gate_ok(step, yes):
                    return "gate"
                continue
            if not in_range:
                return "complete"

            regen = step.name in regenerate
            if view.status == "user" and not regen:
                if self.state.steps.get(step.name) is None or self.state.steps[step.name].origin != "user":
                    self._record_user(step)
                self.bus.emit("step_skipped", step.name, reason="user-provided")
            elif view.status == "done" and not regen:
                self.bus.emit("step_skipped", step.name, reason="up to date")
            elif view.status == "unavailable":
                done = [s.name for s in self.steps[: self.steps.index(step)]]
                self.bus.emit("log", step.name, message=f"everything up to {done[-1] if done else 'here'} is done; "
                                                        f"step '{step.name}' is not built yet ({view.detail})")
                return "unavailable"
            elif view.status == "missing_input":
                self.bus.emit("error", step.name, message=view.detail)
                return "blocked"
            elif step.name == "rtl" and (tb_step := self._parallel_tb_ready(selected, regenerate)) is not None:
                await self._run_parallel(step, regen, tb_step, tb_step.name in regenerate)
                handled.add(tb_step.name)
                if not self._gate_ok(tb_step, yes):
                    return "gate"
                tb_earlier = [s.name for s in self.steps[: self.steps.index(tb_step)] if s.name in selected
                             and any(f.startswith(DECISION_PREFIX) for f in self.state.feedback.get(s.name, []))]
                if tb_earlier:
                    self.bus.emit("log", tb_step.name, message=f"decisions for {', '.join(tb_earlier)}: updating "
                                                                f"{'it' if len(tb_earlier) == 1 else 'them'} first, then going on")
                    return "restart"
            else:
                await self.run_step(step, regenerate=regen)
            if not self._gate_ok(step, yes):
                return "gate"
            earlier = [s.name for s in self.steps[: self.steps.index(step)] if s.name in selected
                       and any(f.startswith(DECISION_PREFIX) for f in self.state.feedback.get(s.name, []))]
            if earlier:
                self.bus.emit("log", step.name, message=f"decisions for {', '.join(earlier)}: updating "
                                                        f"{'it' if len(earlier) == 1 else 'them'} first, then going on")
                return "restart"
        return "complete"

    def _gate_ok(self, step: StepDef, yes: bool) -> bool:
        view = self.evaluate(step)
        if view.gate == "none" and view.status in ("done", "user") and not getattr(step, "proposes_only", False):
            # no review: its non-blocking questions take their defaults (already in the output)
            open_q = [q for q in self.questions() if q["step"] == step.name and q["status"] == "open" and not q.get("blocking")]
            if open_q:
                self._accept_defaults(step, open_q)
                self.save()
                self.bus.emit("log", step.name, message=f"no review gate: accepted the default for {', '.join(q['id'] for q in open_q)}")
        if view.gate != "open":
            return True
        mode = self.gate_mode(step.name)  # this gate's own policy, on top of the project-wide switches
        auto_answer = self.project.cfg.pipeline.auto_answer or mode == "auto_answer"
        if auto_answer and getattr(step, "proposes_only", False):
            return self._auto_answer_proposals(step)
        if yes or auto_answer:
            blocking = [q["id"] for q in self.questions() if q["step"] == step.name and q["status"] == "open" and q.get("blocking")]
            if blocking and getattr(step, "proposes_only", False):
                # proposals change your documents: never accepted on your behalf
                self.bus.emit("warning", step.name, message=f"waiting for your decision on {', '.join(blocking)}")
                self.bus.emit("gate_opened", step.name, gate=step.name, open_questions=view.open_questions,
                              files=[self.project.rel(p) for p in step.outputs(self)])
                return False
            if blocking:
                self.bus.emit("warning", step.name, message=f"{'auto-answer' if auto_answer and not yes else '--yes'}: approving with "
                                                            f"unanswered blocking question(s) {', '.join(blocking)} (their default assumptions are used)")
            self.approve(step.name, force=True, auto=True)
            return True
        if self.project.cfg.pipeline.auto_approve or mode == "auto":
            blocking = [q["id"] for q in self.questions() if q["step"] == step.name and q["status"] == "open" and q.get("blocking")]
            if not blocking:
                msg = self.approve(step.name, auto=True)
                self.bus.emit("log", step.name, message=f"auto-approve: {msg}")
                return True
            self.bus.emit("warning", step.name, message=f"auto-approve paused: blocking question(s) {', '.join(blocking)} need your answer")
        self.bus.emit("gate_opened", step.name, gate=step.name, open_questions=view.open_questions, files=[self.project.rel(p) for p in step.outputs(self)])
        return False
