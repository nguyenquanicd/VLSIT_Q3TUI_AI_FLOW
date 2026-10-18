"""Sanity step `rdc_check`: the RDC tool again with the review's waivers; what is left open must be what the review did not waive."""

from q3tui.flows.sanity.lib.check import CheckStep


class Step(CheckStep):
    name = "rdc_check"
    title = "RDC check after waivers"
    deps = ("rdc_waivers",)
    role = "rdc"
    tool_roles = ("rdc",)
