"""Task input of the `uvm_vip` backend stages: system prompts from md/uvm_vip.md, per-task text built from data (no prompt text in code)."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections

TEXT = skill_sections(Path(__file__).parents[1] / "tb" / "md" / "uvm_vip.md")
INTENT_SYSTEM = TEXT["INTENT_SYSTEM"]
SEMANTIC_SYSTEM = TEXT["SEMANTIC_SYSTEM"]


def intent_prompt(*, contract: dict, spec_files: list[str], answers: dict[str, str], settled: str, feedback: list[str], problems: list[str],
                  previous: bool, vip: str | None = None) -> str:
    ic = contract["interface_contract"]
    parts = [f"# Task: verification contract of `{contract['metadata']['top_module']}`" + (" (update: keep every existing ID)" if previous else ""),
             f"\nSpecification documents (read them; they are the only behavioral source): {', '.join(spec_files)}",
             "\n## Ports (from the specification's interface tables)\n"
             + "\n".join(f"- {p['name']} {p['direction']} {p['width']}" + (f" (reset {p['reset_value']})" if p["reset_value"] else "")
                         + (" [row marked as an assumption]" if p["assumed"] else "") + f" — {p['section']}" for p in ic["ports"]),
             "\n## Parameters (approved configuration) and constraints\n"
             + "\n".join(f"- {x['name']} = {x['value']} (legal: {x['legal']})" for x in ic["parameters"]) + "\n"
             + "\n".join(f"- {c['id']}: {c['rule']}" + (" [assumption]" if c["assumed"] else "") for c in ic["constraints"]),
             "\n## Requirements (approved, with code-made hints)\n"
             + "\n".join(f"- {r['req_id']} [{r['category']}, {r['strength']}, hint {r['observability_hint']}, {r['origin_class']}"
                         + (f", assumption at {'; '.join(r['assumption_evidence'])}" if r["assumption_evidence"] else "") + f"]: {r['text']}"
                         for r in contract["requirements"])]
    if previous:
        keep = [f"- {k}: " + ", ".join(i.get("id") or i["tc_id"] for i in contract[k]) for k in ("objectives", "scoreboard_rules", "coverage", "tests", "open_issues") if contract[k]]
        parts.append("\n## Existing IDs (permanent)\n" + "\n".join(keep))
        if contract["open_issues"]:
            parts.append("\n## Existing open issues (the user reviewed exactly this text: do not reword, merge, split or renumber; you may only resolve them)\n"
                         + "\n".join(f"- {o['id']} [{o['kind']}] ({', '.join(o['requirement_ids'])}; {o['source']}): {o['question']} | default: {o['default_assumption']}"
                                      + (f" | RESOLVED: {o['resolution']}" if o.get("resolved") else "") for o in contract["open_issues"]))
    if answers:
        parts.append("\n## The user's answers\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()))
    if settled:
        parts.append(settled)
    if feedback:
        parts.append("\n## Change requests\n" + "\n".join(f"- {f}" for f in feedback))
    if problems:
        parts.append("\n## Problems found by Q3TUI's checks (fix every one)\n" + "\n".join(f"- {p}" for p in problems))
    parts.append("\n## The existing protocol VIP\n" + (f"`{vip}`: an interface it can drive as the master has the stimulus agent `vip_master`."
                 if vip else "None is configured: never use `vip_master`. Give a protocol interface the stimulus agent `none` (the testbench's own sequences drive its pins)."))
    parts.append("\nReturn the complete result: every requirement decision, interfaces, objectives, scoreboard rules, coverage, tests, open issues.")
    return "\n".join(parts)
