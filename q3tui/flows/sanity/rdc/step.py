"""Sanity step `rdc`: static RDC analysis of the RTL with the shared SDC (tool role `rdc`); the violations are kept as data and as a table."""

from q3tui.flows.sanity.lib.analysis import AnalysisStep


class Step(AnalysisStep):
    name = "rdc"
    title = "RDC analysis"
    deps = ("sdc",)
    role = "rdc"
    tool_roles = ("rdc",)
