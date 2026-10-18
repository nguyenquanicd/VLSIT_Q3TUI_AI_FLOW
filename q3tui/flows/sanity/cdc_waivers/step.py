"""Sanity step `cdc_waivers`: one decision (waive / fix the RTL / ask the spec / change the SDC / ask you) for every CDC violation, reviewed at the gate."""

from pathlib import Path

from q3tui.flows import skill_sections
from q3tui.flows.sanity.lib.review import WaiverStep

HERE = Path(__file__).parent


class Step(WaiverStep):
    name = "cdc_waivers"
    title = "CDC waivers"
    deps = ("cdc",)
    role = "cdc"
    skill_path = HERE
    system = skill_sections(HERE / "md" / "skill.md")["SYSTEM"]
