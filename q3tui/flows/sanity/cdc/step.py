"""Sanity step `cdc`: static CDC analysis of the RTL with the shared SDC (tool role `cdc`); the violations are kept as data and as a table."""

from q3tui.flows.sanity.lib.analysis import AnalysisStep


class Step(AnalysisStep):
    name = "cdc"
    title = "CDC analysis"
    deps = ("sdc",)
    role = "cdc"
    tool_roles = ("cdc",)
