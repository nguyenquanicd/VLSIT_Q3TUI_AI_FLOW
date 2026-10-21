"""Step `rules` of the RTL flow: the rule stack → a consistency check → the Verible flags (`src/rtl/style_lint/`).

The LLM does the work with a skill (verible-flags, rule-consistency) and the Verible tools (`verible_help`, `verible_check_flags`): it reads
the rules, finds contradictions, writes the flags and proves them with the tool. Code does what is code: the rule stack and its diff by rule
id, the flags' shape check, writing the files, the snapshot. Update, not re-run: nothing in the rules changed → no LLM; a change → the task
names the rules that changed and the flags as they are."""

from __future__ import annotations

import re
from pathlib import Path
import anyio
from q3tui.core.project import sha256_json
from q3tui.flows import skill_sections
from q3tui.flows.rtl.lib import skills as skillmod
from q3tui.flows.rtl.lib import verible
from q3tui.flows.rtl.lib.base import RtlFlowStep
from q3tui.flows.rtl.lib.models import RuleFinding, RulesResult
from q3tui.flows.rtl.lib.tools import verible_tools
from q3tui.llm.runtime import Stage
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.flows.rtl.lib import rules_check

SYSTEM = skill_sections(Path(__file__).parent / "md" / "skill.md")["SYSTEM"]
SKILLS = ["verible-flags", "rule-consistency"]
_RULE_ID = re.compile(r"\*\*([A-Z][A-Z0-9]*-\d+[a-z]?)\*\*")


def parse_rules(text: str) -> dict[str, str]:
    """Rule id → the line that states it (rules are bullets `- **ID** …`)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _RULE_ID.search(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = line.strip()
    return out


def diff_rules(old: dict[str, str], new: dict[str, str]) -> list[str]:
    out = [f"changed {i}: {new[i]}" for i in new if i in old and old[i] != new[i]]
    return out + [f"added {i}: {new[i]}" for i in new if i not in old] + [f"removed {i}" for i in old if i not in new]


class Step(RtlFlowStep):
    name = "rules"
    title = "Style rules → Verible flags"
    deps = ()
    tool_roles = ("verible_lint",)

    def _files(self, engine) -> dict[str, Path]:
        d = self.style_dir(engine)
        return {"lint": d / verible.LINT_FLAGS, "format": d / verible.FORMAT_FLAGS, "check": d / "rules_check.json", "resolved": d / "rules_resolved.md"}

    def inputs(self, engine) -> list[Path]:
        return self.rule_paths(engine)

    def outputs(self, engine) -> list[Path]:
        return [p for p in self._files(engine).values() if p.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        return self.tool_problem(engine, "verible_lint")

    def _naming(self, engine) -> dict:
        prof = rules_check.profile_for(self.rule_files(engine), engine.by_name["rtl"].options.get("naming"))
        return {k: v for k, v in prof.__dict__.items() if v not in (None, "", False)}

    def _catalog(self, engine, ts, work: Path) -> str:
        """The installed Verible's rules in a compact catalog (code: `--help_rules=all`), so the LLM does not ask for them one by one."""
        res = verible.run_lint(ts, work, [], ["--help_rules=all"])
        return verible.rule_catalog(verible.log_text(res, 200_000))

    def _prompt(self, ctx: StepContext, rules: str, naming: dict, flags: dict[str, list[str]], changes: list[str], index: str, catalog: str = "") -> str:
        parts = ["# Task: write the Verible flags for these rules and check the rules for contradictions", "",
                 "## The rules (the first file wins where two conflict)", rules, "",
                 "## What Q3TUI itself enforces by code (naming profile, regexes)", "\n".join(f"- {k}: {v}" for k, v in naming.items()), ""]
        if catalog:
            parts += ["## Verible's lint rules (installed version; `*` = on by default; `[parameter=default]`). Ask `verible_help` only for a rule's exact meaning", catalog, ""]
        if flags["lint"] or flags["format"]:
            parts += ["## The flags as they are now", "lint: " + (" ".join(flags["lint"]) or "(none)"), "format: " + (" ".join(flags["format"]) or "(none)"), ""]
        if changes:
            parts += ["## What changed in the rules since these flags were written (update the flags for exactly this, keep the rest)", *map("- {}".format, changes), ""]
        return "\n".join([*parts, index])

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        files = self._files(engine)
        rules = self.rule_text(engine)
        blocks = parse_rules(rules)
        meta = engine.state.step_meta.setdefault(self.name, {})
        flags = {"lint": verible.read_flags(files["lint"]), "format": verible.read_flags(files["format"])}
        have = files["lint"].is_file() and not ctx.regenerate
        same = have and meta.get("rules") == sha256_json(rules) and not ctx.feedback
        if same:
            ctx.emit("log", message="rules: nothing changed since the flags were written (no LLM)")
            ctx.unchanged = True
            return
        changes = diff_rules(meta.get("blocks") or {}, blocks) if have and meta.get("blocks") else []
        ts = self.tools(engine)
        work = self.workdir(engine)
        skills = skillmod.discover(project.config_dir, engine.flow.path, SKILLS)
        catalog = await anyio.to_thread.run_sync(lambda: self._catalog(engine, ts, work))
        prompt = self._prompt(ctx, rules, self._naming(engine), flags, changes, skillmod.index(skills, project.root), catalog)
        if ctx.feedback:
            prompt += "\n\n## Change requests\n" + "\n".join(f"- {f}" for f in ctx.feedback)
        problems: list[str] = []
        for attempt in range(3):
            stage = Stage(name="rules_style" if not attempt else "rules_style_fix", system_prompt=SYSTEM,
                          prompt=prompt + (("\n\n## Problems with your previous answer (fix them)\n" + "\n".join(f"- {p}" for p in problems)) if problems else ""),
                          cwd=project.root, builtin_tools=["Read", "Grep", "Glob"],
                          sdk_tools=verible_tools(ts, work, project.root, lambda: self.rtl_files(engine), files["lint"]),
                          add_dirs=skillmod.add_dirs(skills), output_model=RulesResult,
                          deny_dirs=[project.model_dir, project.build_dir, self.rtl_dir(engine)])  # (the rules are read, not the RTL)
            out: RulesResult = (await ctx.llm(stage)).output
            problems = verible.validate_flags([*out.verible_flags]) + [f"format flag `{f}`: not a flag" for f in out.format_flags if not re.match(r"^--[a-z_]+(=.*)?$", f)]
            if not problems:
                break
        else:
            raise StepFailed("the Verible flags did not pass the format check after 3 tries: " + "; ".join(problems))
        files["lint"].parent.mkdir(parents=True, exist_ok=True)
        files["resolved"].write_text(rules)
        files["check"].write_text(self._report(out))
        errors = [f for f in out.findings if f.severity == "error"]
        if errors:
            raise StepFailed("the rules contradict each other — edit config/rtl_rules.md (the first file wins; a rule id in both is an override):\n"
                             + "\n".join(f"- {', '.join(f.ids)}: {f.problem}" + (f" → {f.suggestion}" if f.suggestion else "") for f in errors))
        header = f"# Verible flags written by Q3TUI from the rules ({', '.join(self.rule_files(engine))}); edit the rules, not this file\n"
        files["lint"].write_text(header + "\n".join(out.verible_flags) + "\n")
        files["format"].write_text(header + "\n".join(out.format_flags) + "\n")
        meta["rules"], meta["blocks"] = sha256_json(rules), blocks
        warns = [f for f in out.findings if f.severity == "warning"]
        ctx.emit("log", message=f"rules: {len(out.mapping)} rule(s) mapped to Verible, {len(out.not_checkable)} not checkable, {len(warns)} warning(s)"
                                + (f" — {out.summary}" if out.summary else ""))
        for f in warns:
            ctx.emit("warning", message=f"rules {', '.join(f.ids)}: {f.problem}")

    def _report(self, out: RulesResult) -> str:
        import json

        return json.dumps({"summary": out.summary, "findings": [f.model_dump() for f in out.findings], "mapping": out.mapping,
                           "not_checkable": out.not_checkable}, indent=2) + "\n"
