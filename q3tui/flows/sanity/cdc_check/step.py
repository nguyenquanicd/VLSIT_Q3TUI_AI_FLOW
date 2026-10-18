"""Sanity step `cdc_check`: the CDC tool again with the review's waivers; what is left open must be what the review did not waive."""

from q3tui.flows.sanity.lib.check import CheckStep


class Step(CheckStep):
    name = "cdc_check"
    title = "CDC check after waivers"
    deps = ("cdc_waivers",)
    role = "cdc"
    tool_roles = ("cdc",)
