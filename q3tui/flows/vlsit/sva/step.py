"""VLSIT step `sva` (Phase 3b, docs/spec/vlsit-flow.md): SVA per module, bind, compile check, vacuity, the Gate 3b review.

Per module a stage `sva_<module>` returns the whole `<module>_sva.sv` as data; code checks it (structure, `// NL:` and
`// REQ:` above every property, labels, one assertion per requirement), writes it, generates the bind file and
filelist_sva.f, compiles it with the role `sim` (when that tool `supports: [sva]`; else the check is reported **N/A**, never
passed), runs a smoke pass for vacuity (covers that never hit), and writes GATE3_REVIEW.md + the first RTM rows. Only
assertions you confirm (`review.review_property` / `confirm_all`) count for the RTM. Approving the gate records it in
rtm.json (`gate_3_approved`) and confirms nothing by itself.

Update, not re-run: a module whose RTL, requirements, test cases and answers are unchanged keeps its file; `[sva:<module>]`
change requests (a rejected property) re-run only that module.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from q3tui.eda.base import ToolUnavailable
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.core.project import read_json, sha256_json, write_json
from q3tui.steps.common import Question, assign_question_ids, load_answers, settled_block, split_feedback
from q3tui.steps.vlsit import artifacts, rtm, simenv
from q3tui.flows.vlsit.sva import task as sva_prompts
from q3tui.steps.vlsit.base import VlsitStep

MAX_REPAIRS = 2
RTL_CHARS = 24_000


class SvaFile(BaseModel):
    content: str = Field(description="The complete `<module>_sva.sv`")
    summary: str = Field("", description="Which requirements have no assertion and why")
    questions: list[Question] = Field(default_factory=list)


def check_content(module: str, content: str, req_ids: list[str]) -> list[str]:
    """Structural problems of a generated SVA file (empty: fine)."""
    problems = []
    if not re.search(rf"\bmodule\s+{re.escape(module)}_sva\b", content):
        problems.append(f"the module must be named `{module}_sva`")
    if "`ifndef SYNTHESIS" not in content or "`endif" not in content:
        problems.append("assertions must be inside `ifndef SYNTHESIS … `endif")
    props = rtm.parse_sva_text(content, module)
    if not any(p["kind"] == "assert" for p in props):
        problems.append("no `label : assert property (…)` found")
    seen: set[str] = set()
    for p in props:
        if p["label"] in seen:
            problems.append(f"label {p['label']} is used twice")
        seen.add(p["label"])
        if not p["label"].startswith(("a_", "c_")):
            problems.append(f"{p['label']}: labels start with a_ (assert) or c_ (cover)")
        if not p["nl"]:
            problems.append(f"{p['label']}: the `// NL:` line above it is missing")
        if not p["req_ids"]:
            problems.append(f"{p['label']}: the `// REQ: REQ-xxx` line above it is missing")
    covered = {r for p in props if p["kind"] == "assert" for r in p["req_ids"]}
    missing = [r for r in req_ids if r not in covered]
    if missing:
        problems.append(f"no assertion for {', '.join(missing)} (write one, or say in `summary` why it cannot be checked by SVA)")
    return problems


def review_path(engine) -> Path:
    return simenv.layout(engine).sva / "GATE3_REVIEW.md"


def render_review(engine) -> Path:
    """GATE3_REVIEW.md: every property with its NL text, requirements, vacuity and your status (code only)."""
    lay = simenv.layout(engine)
    lay.sva.mkdir(parents=True, exist_ok=True)
    props = rtm.properties(engine)
    check = read_json(rtm.work_dir(engine, "sva") / "check.json", default={}) or {}
    vac = rtm.vacuity(engine)
    meta = engine.state.step_meta.get("sva", {}) if hasattr(engine.state, "step_meta") else {}
    n_conf = sum(p["status"] == "confirmed" for p in props)
    lines = ["# Gate 3b — property review", "",
             f"{len(props)} properties · {n_conf} confirmed · {sum(p['status'] == 'pending' for p in props)} pending · "
             f"{sum(p['status'] == 'rejected' for p in props)} rejected", "",
             f"- compile check: **{check.get('status', 'not run')}**" + (f" — {check['reason']}" if check.get("reason") else ""),
             f"- vacuity: " + (f"**{sum(1 for p in props if p['vacuous'])} vacuous** of {len(props)} (smoke run over {len(vac.get('tcs', []))} test case(s))"
                               if vac and vac.get("ran") else f"**N/A** — {(vac or {}).get('reason', 'not checked')}"), ""]
    warns = meta.get("warnings") or []
    if warns:
        lines += ["## Warnings", *[f"- {w}" for w in warns], ""]
    reqs = {r["req_id"]: r for r in rtm.requirements(engine)}
    asserted = {r for p in props if p["kind"] == "assert" for r in p["req_ids"]}
    nos = [r for r in reqs if r not in asserted]
    if nos:
        lines += ["## Requirements without an assertion", ", ".join(nos), ""]
    lines += ["Only **confirmed** assertions count for the RTM (condition 2). Confirm or reject each one (TUI: SVA tab).", ""]
    by_module: dict[str, list[dict]] = {}
    for p in props:
        by_module.setdefault(p["module"], []).append(p)
    for module, ps in by_module.items():
        lines += [f"## {module}", "", "| label | kind | REQ | status | vacuous | NL |", "|---|---|---|---|---|---|"]
        for p in ps:
            v = "?" if p["vacuous"] is None else ("yes" if p["vacuous"] else "no")
            lines.append(f"| {p['label']} | {p['kind']} | {', '.join(p['req_ids'])} | {p['status']} | {v} | {p['nl'].replace('|', '/')} |")
        lines.append("")
    path = review_path(engine)
    path.write_text("\n".join(lines))
    rtm.rehash(engine, [path])
    return path


class SvaStep(VlsitStep):
    name = "sva"
    title = "SVA"
    deps = ("rtl", "tb")
    artifact = "rtm.json"
    gate_fields = {"gate_3_approved": True, "gate_3_at": ""}
    tool_roles = ("sim", "run")

    # -- files ---------------------------------------------------------------------------------

    def inputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        rtl = sorted(lay.rtl.glob("*.sv")) + [lay.rtl / "filelist.f"] if lay.rtl.is_dir() else []
        return [lay.artifact("structured_spec.json"), lay.artifact("final_config.json"), lay.artifact("selected_testplan.json"),
                *rtl, self._q_paths(engine)[1]]

    def outputs(self, engine) -> list[Path]:
        lay = self.lay(engine)
        files = sorted(lay.sva.glob("*.sv")) if lay.sva.is_dir() else []
        extra = [lay.sva / "filelist_sva.f", review_path(engine), lay.artifact("rtm.json"), lay.schemas / "RTM.md"]
        return files + [p for p in extra if p.is_file()]

    def pending_reviews(self, engine) -> list[str]:
        from q3tui.flows.vlsit.sva import review

        return review.pending_ids(engine)

    def missing_input(self, engine) -> str | None:
        lay = self.lay(engine)
        if not lay.artifact("structured_spec.json").is_file():
            return "the parsed requirements are needed first (parse step: schemas/structured_spec.json)"
        if not lay.rtl.is_dir() or not any(lay.rtl.glob("*.sv")):
            return "the RTL is needed first (src/rtl)"
        return None

    def reset_files(self, engine) -> list[Path]:  # (your reviews in schemas/sva_reviews.json stay)
        return [*super().reset_files(engine), *self.outputs(engine)]

    # -- run -----------------------------------------------------------------------------------

    def _snapshot(self, engine, module: dict, rtl: str, reqs: list[dict], tcs: list[dict], answers: dict) -> str:
        return sha256_json({"rtl": rtl, "reqs": [(r["req_id"], r.get("text"), r.get("sva_hint")) for r in reqs],
                            "tcs": [(t["tc_id"], t.get("req_ids")) for t in tcs], "answers": answers, "v": 1})

    async def run(self, ctx: StepContext) -> None:
        import anyio

        engine, project = ctx.engine, ctx.project
        lay = self.lay(engine)
        spec = rtm.spec_data(engine)
        reqs = {r["req_id"]: r for r in rtm.requirements(engine)}
        if not reqs:
            raise StepFailed("schemas/structured_spec.json has no requirements")
        plan = rtm.testplan(engine)
        config = artifacts.read(project, "final_config.json", {}) or {}
        cfg_text = json.dumps({p.get("name"): p.get("value", p.get("default")) for p in config.get("parameters", []) if isinstance(p, dict)}) if config else ""
        lay.sva.mkdir(parents=True, exist_ok=True)
        meta = engine.state.step_meta.setdefault(self.name, {})
        mods_meta: dict = meta.setdefault("modules", {})
        answers = load_answers(self._q_paths(engine)[1])
        old_q = {q.id: q for q in self.questions_list(engine)}
        owner: dict[str, str] = meta.setdefault("q_owner", {})
        by_unit, general = split_feedback(ctx.feedback, self.name)
        settled = settled_block(engine.settled_questions(self.name))
        warnings: list[str] = []
        todo: list[tuple[dict, str, list[str]]] = []
        names = []
        for m in rtm.modules(engine):
            name = m["name"]
            rtl_file = lay.rtl / f"{name}.sv"
            if not rtl_file.is_file():
                warnings.append(f"{name}: no RTL file src/rtl/{name}.sv — no SVA written")
                continue
            names.append(name)
            mreqs = [reqs[r] for r in m["req_ids"] if r in reqs]
            mtcs = [t for t in plan if set(t.get("req_ids") or []) & set(m["req_ids"])]
            own = {k: v for k, v in answers.items() if owner.get(k) == name}
            snap = self._snapshot(engine, m, rtl_file.read_text(errors="replace"), mreqs, mtcs, own)
            path = lay.sva / f"{name}_sva.sv"
            fb = by_unit.get(name, []) + general
            if ctx.regenerate or fb or not path.is_file() or mods_meta.get(name, {}).get("hash") != snap:
                todo.append((m, snap, fb))
            else:
                mods_meta.setdefault(name, {})["hash"] = snap
        if not names:
            raise StepFailed("no module of the parsed requirements has an RTL file")

        new_questions: dict[str, list[Question]] = {}

        async def build(m: dict, snap: str, fb: list[str]) -> None:
            name = m["name"]
            path = lay.sva / f"{name}_sva.sv"
            previous = path.read_text() if path.is_file() else ""
            mreqs = [reqs[r] for r in m["req_ids"] if r in reqs]
            req_text = "\n".join(f"- {r['req_id']} [{r.get('category', '')}]: {r.get('text', '')}" for r in mreqs) or "(module không có REQ riêng: kiểm các cổng / reset / giao thức của nó)"
            hints = "\n".join(f"- {r['req_id']}: {r['sva_hint']}" for r in mreqs if r.get("sva_hint"))
            tcs = "\n".join(f"- {t['tc_id']} {t.get('title', '')}: {', '.join(t.get('req_ids') or [])}"
                            for t in plan if set(t.get("req_ids") or []) & set(m["req_ids"]))
            rtl = (lay.rtl / f"{name}.sv").read_text(errors="replace")
            rtl = rtl if len(rtl) <= RTL_CHARS else rtl[:RTL_CHARS] + "\n// … (cắt bớt)"
            problems: list[str] = []
            out: SvaFile | None = None
            for attempt in range(MAX_REPAIRS + 1):
                stage = Stage(name=f"sva_{name}" if attempt == 0 else f"sva_fix_{name}", system_prompt=sva_prompts.SYSTEM,
                              cwd=project.root, builtin_tools=["Read", "Grep", "Glob"], output_model=SvaFile, max_turns=12,
                              prompt=sva_prompts.module_prompt(module=name, rtl=rtl, reqs=req_text, hints=hints, config=cfg_text,
                                                               tcs=tcs, previous=(out.content if out else previous), feedback=fb,
                                                               settled=settled, problems=problems or None))
                ctx.emit("log", message=f"{name}: " + ("writing the assertions …" if attempt == 0 else f"fixing {len(problems)} problem(s) …"))
                out = (await ctx.llm(stage)).output
                problems = check_content(name, out.content, m["req_ids"])
                if not problems:
                    break
            if problems:
                warnings.extend(f"{name}: {p}" for p in problems)
            if path.is_file():
                project.backup([path])
            path.write_text(out.content.rstrip() + "\n")
            mods_meta[name] = {"hash": snap, "summary": out.summary}
            new_questions[name] = out.questions

        limiter = anyio.CapacityLimiter(max(1, ctx.cfg.rtl.parallel))

        async def guarded(m, snap, fb):
            async with limiter:
                await build(m, snap, fb)

        async with anyio.create_task_group() as tg:
            for m, snap, fb in todo:
                tg.start_soon(guarded, m, snap, fb)

        # questions: a regenerated module's unanswered ones are replaced by its new ones; answered ones stay
        if todo:
            regenerated = {m["name"] for m, _, _ in todo}
            kept = [q for q in old_q.values() if q.id in answers or owner.get(q.id) not in regenerated]
            pairs = [(mod, q) for mod, qs in new_questions.items() for q in qs]
            alloc = assign_question_ids([q for _, q in pairs], kept, "SV", taken=set(answers))
            for (mod, _), q in zip(pairs, alloc):
                owner[q.id] = mod
            self.save_questions(engine, [*kept, *alloc])
            meta["warnings"] = warnings
        else:
            old = [w for w in meta.get("warnings", []) if w not in warnings]
            meta["warnings"] = old + warnings

        await self._bind_and_check(ctx, names, spec, reqs)
        render_review(engine)
        rtm.write_rtm(engine)
        if self.options.get("auto_confirm", engine.auto_confirm_reviews()):  # auto approve (or the step option): no human review of the properties
            from q3tui.flows.vlsit.sva import review

            ctx.emit("warning", message="sva: auto approve — the pending assertions are confirmed automatically "
                                        "(vacuous ones stay pending); nobody reviewed their meaning")
            review.confirm_all(engine, note="auto-confirmed (auto approve), not reviewed")
        if not todo:
            ctx.unchanged = True
            ctx.emit("log", message="sva: nothing changed since the last run")
        props = rtm.properties(engine)
        ctx.emit("log", message=f"sva: {len(props)} properties in {len(names)} module(s)" + (f"; {len(warnings)} warning(s), see {self.lay(engine).sva.name}/GATE3_REVIEW.md" if warnings else ""))

    # -- bind, compile, vacuity ----------------------------------------------------------------------

    async def _bind_and_check(self, ctx: StepContext, names: list[str], spec: dict, reqs: dict) -> None:
        import anyio

        engine, project = ctx.engine, ctx.project
        lay = self.lay(engine)
        ip = (spec.get("metadata") or {}).get("ip_name") or "design"
        bind = lay.sva / f"{ip}_bind.sv"
        body = ["`default_nettype none", "`ifndef SYNTHESIS"]
        body += [f"bind {n} {n}_sva u_{n}_sva (.*);" for n in names if (lay.sva / f"{n}_sva.sv").is_file()]
        body += ["`endif", "`default_nettype wire", ""]
        bind.write_text("\n".join(body))
        files = [p for p in sorted(lay.sva.glob("*_sva.sv"))] + [bind]
        (lay.sva / "filelist_sva.f").write_text("".join(f"{project.rel(p)}\n" for p in files))
        check: dict = {"status": "N/A", "reason": ""}
        work = rtm.work_dir(engine, "sva")
        work.mkdir(parents=True, exist_ok=True)
        vac: dict = {"fingerprint": rtm.fingerprint(engine, tb=False), "ran": False, "reason": "", "hits": [], "tcs": []}
        try:
            from q3tui.eda import tools as T

            ts = T.load_tools(ctx.cfg, project.root)
            if "sim" not in ts.roles or not T.available(ts, "sim"):
                check["reason"] = "no simulator configured (tool role `sim`): the assertions were not compiled"
            elif not T.supports(ts, "sim", "sva"):
                check["reason"] = "the `sim` tool does not support SVA (supports: [sva] missing): compile check N/A"
            elif not any(lay.tb.glob("*.sv")):
                check["reason"] = "no testbench yet (src/tb): the assertions were not compiled"
            else:
                res = await anyio.to_thread.run_sync(lambda: simenv.compile_all(engine, ts, work, with_sva=True))
                errs = [d for d in res.diagnostics if d.severity == "error"]
                if res.ok:
                    check = {"status": "pass", "reason": ""}
                else:
                    fixed = await self._repair_compile(ctx, names, errs)
                    if fixed:
                        res = await anyio.to_thread.run_sync(lambda: simenv.compile_all(engine, ts, work, with_sva=True))
                        errs = [d for d in res.diagnostics if d.severity == "error"]
                    check = {"status": "pass" if res.ok else "fail", "reason": "" if res.ok else "; ".join(f"{d.file}:{d.line} {d.message}" for d in errs[:6]) or res.summary}
                if check["status"] == "pass":
                    tcs = [t["tc_id"] for t in rtm.testplan(engine)][: int(self.options.get("smoke_tcs", 10))]
                    if "run" in ts.roles and T.available(ts, "run") and tcs:
                        from q3tui.steps.vlsit import simlog

                        hits: set[str] = set()
                        for tc in tcs:
                            out = await anyio.to_thread.run_sync(lambda tc=tc: simenv.run_tc(engine, ts, work, tc))
                            hits |= simlog.cover_hits(simenv.output_text(out))
                        vac.update(ran=True, hits=sorted(hits), tcs=tcs)
                    else:
                        vac["reason"] = "no `run` tool or no selected test case: vacuity not checked"
                else:
                    vac["reason"] = "the assertions do not compile"
        except ToolUnavailable as exc:
            check["reason"] = str(exc)
        if not vac["ran"] and not vac["reason"]:
            vac["reason"] = check["reason"] or "not checked"
        write_json(work / "check.json", check)
        write_json(work / "vacuity.json", vac)
        if check["status"] == "fail":
            ctx.emit("warning", message=f"the assertions do not compile: {check['reason']}")
        elif check["status"] == "N/A":
            ctx.emit("warning", message=f"SVA compile check N/A: {check['reason']}")

    async def _repair_compile(self, ctx: StepContext, names: list[str], errs) -> bool:
        """One repair round: the modules whose SVA file the errors name get a fresh small session with them."""
        engine, project = ctx.engine, ctx.project
        lay = self.lay(engine)
        by_mod: dict[str, list[str]] = {}
        for d in errs:
            stem = Path(d.file or "").stem
            if stem.endswith("_sva") and stem[: -len("_sva")] in names:
                by_mod.setdefault(stem[: -len("_sva")], []).append(f"line {d.line}: {d.message}")
        reqs = {r["req_id"]: r for r in rtm.requirements(engine)}
        changed = False
        for name, msgs in by_mod.items():
            path = lay.sva / f"{name}_sva.sv"
            mod = next((m for m in rtm.modules(engine) if m["name"] == name), {"req_ids": []})
            rtl = (lay.rtl / f"{name}.sv").read_text(errors="replace")[:RTL_CHARS]
            stage = Stage(name=f"sva_fix_{name}", system_prompt=sva_prompts.SYSTEM, cwd=project.root, output_model=SvaFile,
                          builtin_tools=["Read", "Grep", "Glob"], max_turns=12,
                          prompt=sva_prompts.module_prompt(module=name, rtl=rtl, reqs="\n".join(f"- {r}: {reqs[r].get('text', '')}" for r in mod["req_ids"] if r in reqs),
                                                           hints="", config="", tcs="", previous=path.read_text(), feedback=[], settled="",
                                                           problems=[f"trình biên dịch báo lỗi — {m}" for m in msgs[:10]]))
            out = (await ctx.llm(stage)).output
            if not check_content(name, out.content, []):
                project.backup([path])
                path.write_text(out.content.rstrip() + "\n")
                changed = True
        return changed
