"""dv step `testplan`: spec + structured spec + final config → schemas/test_plan.json (the test cases, each with the REQs it checks).

The plan of the plain SystemVerilog testbench, as a step of its own so it can be reviewed (gate: human) before the testbench is written and so a
changed plan reaches `tb` as a changed input (only the test cases that changed are rewritten). The UVM testbench (`testbench: uvm`) plans its tests in the verification contract instead.

Independence as for `tb`: the stage sees the spec, the requirements and the configuration only (deny_dirs on the RTL and the SVA).
Code: numbering and names of the test cases, unknown REQ ids dropped, conditional test cases by parameter, REQ coverage (an uncovered REQ is asked
for again, then becomes a blocking question). The LLM only plans. Changes propagate as updates: the plan is made again when a requirement, a
parameter, an answer or a change request changed, and the previous plan goes in with the list of what changed.
"""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import read_json, sha256_json, write_json
from q3tui.flows.dv.plain import plan as tb_plan
from q3tui.flows.dv.plain import task as tb_prompts
from q3tui.flows.dv.plain.plan import TbPlan
from q3tui.flows.spec.lib import artifacts
from q3tui.flows.spec.lib.base import VlsitStep
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import Question, assign_question_ids, drop_settled, load_answers, render_questions, settled_block, split_feedback

PLAN = "test_plan.json"


class PlainPlan(VlsitStep):
    name = "testplan"
    title = "Test plan"
    deps = ("config",)
    artifact = PLAN
    gate_fields = {"gate_testplan_approved": True, "gate_testplan_at": ""}

    # -- files -------------------------------------------------------------------------------------

    def inputs(self, engine) -> list[Path]:
        p = engine.project
        return [*p.spec_documents(), artifacts.path(p, "structured_spec.json"), artifacts.path(p, "final_config.json"), self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        return [f for f in (artifacts.path(engine.project, PLAN), self._q_paths(engine)[2]) if f.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return [*self.outputs(engine), *super().reset_files(engine)]

    def missing_input(self, engine) -> str | None:
        p = engine.project
        if not artifacts.path(p, "structured_spec.json").is_file():
            return "the parsed spec is needed first (step parse: schemas/structured_spec.json)"
        if not artifacts.path(p, "final_config.json").is_file():
            return "the configuration is needed first (step config: schemas/final_config.json)"
        return None

    # -- the stage --------------------------------------------------------------------------------------

    def _deny(self, ctx: StepContext, lay) -> list[Path]:
        p = ctx.project
        return [lay.rtl, lay.sva, p.model_dir, p.arch_dir, p.rtl_dir, p.tb_dir]  # independence: spec + requirements only

    async def _plan(self, ctx: StepContext, lay, spec: dict, params: dict, top: str | None, spec_files: list[str], answers: dict, settled: str,
                    feedback: list[str], previous: list[dict] | None, changes: list[str], uncovered: list[str]) -> TbPlan:
        reqs = [(r["req_id"], r.get("category", ""), r.get("text", "")) for r in spec.get("requirements", []) if r.get("req_id")]
        res = await ctx.llm(Stage(
            name="tb_plan", system_prompt=tb_prompts.PLAN_SYSTEM, cwd=ctx.project.root, builtin_tools=["Read", "Grep", "Glob"],
            output_model=TbPlan, deny_dirs=self._deny(ctx, lay), max_turns=ctx.cfg.tb.max_turns,  # (stage name `tb_plan`: the verifier role's stage family)
            prompt=tb_prompts.plan_prompt(reqs=reqs, params=params, spec_files=spec_files, top=top, previous=previous, changes=changes,
                                          answers=answers, settled=settled, feedback=feedback, uncovered=uncovered)))
        return res.output

    def _changes(self, old_snap: dict | None, new: dict, old_answers: dict | None, answers: dict) -> list[str]:
        if not old_snap:
            return []
        out = []
        a, b = old_snap.get("reqs", {}), new["reqs"]
        for r in sorted(set(b) - set(a)):
            out.append(f"new requirement {r}: {b[r][2]}")
        for r in sorted(set(a) - set(b)):
            out.append(f"{r} was removed: drop it from the test cases")
        for r in sorted(set(a) & set(b)):
            if a[r] != b[r]:
                out.append(f"{r} changed: {b[r][2]} (was: {a[r][2]})")
        for k in sorted(set(old_snap.get("params", {})) | set(new["params"])):
            if old_snap.get("params", {}).get(k) != new["params"].get(k):
                out.append(f"parameter {k} = {new['params'].get(k)} (was {old_snap.get('params', {}).get(k)})")
        if (old_answers or {}) != answers:
            out.append("the user's answers changed (see below)")
        return out

    # -- run -------------------------------------------------------------------------------------------

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        lay = self.lay(engine)
        spec = artifacts.read(project, "structured_spec.json")
        config = artifacts.read(project, "final_config.json")
        if not spec or not config:
            raise StepFailed("schemas/structured_spec.json and schemas/final_config.json are needed (steps parse and config)")
        params = {k: str((v or {}).get("value")) for k, v in (config.get("parameters") or {}).items()}
        reqs = {r["req_id"]: r.get("text", "") for r in spec.get("requirements", []) if r.get("req_id")}
        meta = engine.state.step_meta.setdefault(self.name, {})
        q_json, answers_path, q_md = self._q_paths(engine)
        answers = load_answers(answers_path)
        settled = settled_block(engine.settled_questions(self.name))
        _by_unit, general = split_feedback(ctx.feedback, self.name)  # (a change request for this step is about the plan as a whole)
        top = (spec.get("metadata") or {}).get("top_module")
        if not top:
            from q3tui.flows.rtl.rtl.plan import plan_from_spec

            pl = plan_from_spec(spec)
            top = pl.top if pl else None
        spec_files = [project.rel(p) for p in project.spec_documents()]

        snapshot = {"reqs": {r["req_id"]: [r.get("category"), r.get("sva_hint"), r.get("text")] for r in spec.get("requirements", []) if r.get("req_id")},
                    "params": params, "top": top}
        plan_hash = sha256_json({**snapshot, "answers": answers})
        old = meta.get("plan") or {}
        need = bool(ctx.regenerate or general or not old.get("cases") or old.get("hash") != plan_hash or not artifacts.path(project, PLAN).is_file())
        cases: list[dict] = list(old.get("cases") or [])
        excluded: list[dict] = list(old.get("excluded") or [])  # (requirements no simulation can check, each with its reason)
        asked: list[Question] = []
        warnings: list[str] = []
        wrote = False
        if need:
            changes = self._changes(old.get("snapshot"), snapshot, old.get("answers"), answers)
            ctx.emit("log", message="planning the test cases …" if not cases else "updating the test plan …")
            plan = await self._plan(ctx, lay, spec, params, top, spec_files, answers, settled, general, cases or None, changes, [])
            norm, warnings = tb_plan.normalize(plan.test_cases, set(reqs), cases)
            cases = [c.model_dump() for c in norm]
            asked = list(plan.questions)
            covered_now = {r for c in cases if tb_plan.applies(c, params) for r in c["req_ids"]}
            excluded, w_ex = tb_plan.normalize_excluded(plan.excluded, set(reqs), covered_now)
            warnings += w_ex
            cov = tb_plan.coverage([c for c in cases if tb_plan.applies(c, params)], spec, {e["req_id"] for e in excluded})
            if cov["uncovered_req_ids"] and ctx.cfg.tb.max_fix_attempts != 0:
                ctx.emit("warning", message=f"no test case covers {', '.join(cov['uncovered_req_ids'])}: asking for more")
                plan = await self._plan(ctx, lay, spec, params, top, spec_files, answers, settled, general, cases, [], cov["uncovered_req_ids"])
                norm, w2 = tb_plan.normalize(plan.test_cases, set(reqs), cases)
                warnings += w2
                cases = [c.model_dump() for c in norm]
                asked += plan.questions
                more, w_ex = tb_plan.normalize_excluded(plan.excluded, set(reqs), {r for c in cases if tb_plan.applies(c, params) for r in c["req_ids"]})
                warnings += w_ex
                excluded = list({e["req_id"]: e for e in [*excluded, *more]}.values())
            meta["plan"] = {"hash": plan_hash, "snapshot": snapshot, "answers": dict(answers), "cases": cases, "excluded": excluded}
            wrote = True
        for w in warnings:
            ctx.emit("warning", message=w)
        selected = [c for c in cases if tb_plan.applies(c, params)]
        for c in cases:
            if c not in selected:
                ctx.emit("log", message=f"{c['tc_id']} {c['name']}: left out — {c['conditional_param']} is {params.get(c['conditional_param'])}")
        cov = tb_plan.coverage(selected, spec, {e["req_id"] for e in excluded})
        if cov["uncovered_req_ids"]:
            ctx.emit("warning", message=f"requirements with no test case: {', '.join(cov['uncovered_req_ids'])}")
        for e in excluded:
            ctx.emit("log", message=f"{e['req_id']}: left out of simulation — {e['reason'] or 'no reason given'}")

        self._write_plan_file(ctx, spec, top, cases, excluded, params, cov, wrote)
        self._write_questions(ctx, q_json, answers_path, q_md, answers, asked, cov, wrote)
        ctx.unchanged = not wrote
        ctx.emit("log", message=f"test plan: {len(selected)}/{len(cases)} test case(s), REQ coverage {cov['coverage_pct']}%"
                                f" ({len(cov['covered_req_ids'])}/{cov['total_req_ids']}), {len(excluded)} requirement(s) left out of simulation")

    # -- outputs -----------------------------------------------------------------------------------------------------

    def _write_plan_file(self, ctx: StepContext, spec: dict, top: str | None, cases: list[dict], excluded: list[dict], params: dict, cov: dict, wrote: bool) -> None:
        engine, project = ctx.engine, ctx.project
        old = artifacts.read(project, PLAN) or {}
        md = {"ip_name": (spec.get("metadata") or {}).get("ip_name") or top or "", "gate_testplan_approved": False, "gate_testplan_at": None}
        gate = engine.state.gates.get(self.name)
        if gate and gate.status == "approved" and not wrote:
            md.update({k: v for k, v in (old.get("metadata") or {}).items() if k.startswith("gate_testplan")})  # unchanged: the approval stays
        artifacts.write(project, PLAN, {"metadata": md, "test_cases": [{**c, "selected": tb_plan.applies(c, params)} for c in cases], "excluded": excluded, "coverage": cov,
                                            "params": params},
                        check=False)

    def _uncovered_question(self, cov: dict, answers: dict, q_raw: list[dict]) -> list[Question]:
        if not cov["uncovered_req_ids"]:
            return []
        prior = [q for q in q_raw if q.get("code") == "uncovered" and q["id"] in answers]
        if any(set(cov["uncovered_req_ids"]) <= set(q.get("uncovered") or []) for q in prior):
            return []  # answered before for these requirements (accepted as not simulated)
        ids = ", ".join(cov["uncovered_req_ids"])
        return [Question(id="", question=f"No test case covers {ids}. Add test cases for them, or accept that they are not checked by simulation (SVA / review only)?",
                         blocking=True, default_assumption="Accept: not checked by simulation (recorded as uncovered in test_plan.json)", kind="design_choice")]

    def _write_questions(self, ctx: StepContext, q_json: Path, answers_path: Path, q_md: Path, answers: dict, asked: list[Question], cov: dict, wrote: bool) -> None:
        engine = ctx.engine
        prev_raw = read_json(q_json, default=[]) or []
        # the LLM's questions are replaced when it planned again, kept otherwise; code questions follow the coverage now
        kept = [q for q in prev_raw if q.get("code") is None and not wrote]
        new: list[tuple[Question, dict]] = []
        if wrote:
            fresh, dropped = drop_settled(asked, engine.settled_questions(self.name))
            new += [(q, {}) for q in fresh]
            if dropped:
                ctx.emit("log", message="dropped question(s) already settled: " + ", ".join(sid for _, sid in dropped))
        new += [(q, {"code": "uncovered", "uncovered": list(cov["uncovered_req_ids"])}) for q in self._uncovered_question(cov, answers, prev_raw)]
        old_code = [q for q in prev_raw if q.get("code") and q["id"] in answers]
        ided = assign_question_ids([q for q, _ in new], [Question.model_validate(q) for q in prev_raw], "Q-TP")
        kept_ids = {q["id"] for q in kept} | {q["id"] for q in old_code}
        entries = kept + [q for q in old_code if q["id"] not in {n.id for n in ided}] + [
            {**q.model_dump(), **extra} for (_, extra), q in zip(new, ided) if q.id not in kept_ids]
        q_json.parent.mkdir(parents=True, exist_ok=True)
        write_json(q_json, entries)
        qs = [Question.model_validate(q) for q in entries]
        if qs:
            q_md.write_text(render_questions(qs, load_answers(answers_path), f"Open questions — {self.title}", engine.state.accepted_defaults.get(self.name, {})))
        open_q = [q for q in qs if q.id not in load_answers(answers_path)]
        ctx.emit("questions", count=len(open_q), blocking=sum(q.blocking for q in open_q))
