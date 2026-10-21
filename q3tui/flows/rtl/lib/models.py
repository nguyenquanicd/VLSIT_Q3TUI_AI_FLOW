"""The structured answers of the RTL flow's LLM stages (pydantic): in a module of their own (importable by name, so the steps, which are loaded
from their files, and the tests share the same classes)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from q3tui.steps.common import Question


# -- rules -----------------------------------------------------------------------------------------------------------

class RuleFinding(BaseModel):
    severity: Literal["error", "warning"]
    ids: list[str] = Field(default_factory=list, description="the rule ids involved")
    problem: str
    suggestion: str = Field("", description="the exact change to make in config/rtl_rules.md")


class RulesResult(BaseModel):
    summary: str = ""
    findings: list[RuleFinding] = Field(default_factory=list)
    verible_flags: list[str] = Field(description="lint flags, one per line of the flags file (`--rules=…`)")
    format_flags: list[str] = Field(default_factory=list, description="formatter flags (`--indentation_spaces=2`), only what the rules state")
    mapping: list[str] = Field(default_factory=list, description="`RULE-ID: verible-rule` for every rule you enabled")
    not_checkable: list[str] = Field(default_factory=list, description="rule ids Verible cannot check")


# -- style_lint ------------------------------------------------------------------------------------------------------

class Fix(BaseModel):
    module: str = Field(description="the RTL module (file name without .sv)")
    request: str = Field(description="what to change, line by line, in the words of the rule; behaviour must stay the same")


class FlagProblem(BaseModel):
    rule: str = Field(description="the Verible rule (or flag) that is wrong")
    problem: str
    suggestion: str = Field("", description="what the flags should say instead")


class Waiver(BaseModel):
    module: str
    rule: str = Field(description="the Verible rule of the findings you accept")
    line_text: str = Field("", description="the source line (stripped); empty: every finding of this rule in the module")
    why: str = Field(description="the rule id and text that allow it")


class StyleVerdict(BaseModel):
    summary: str = ""
    fixes: list[Fix] = Field(default_factory=list)
    flag_problems: list[FlagProblem] = Field(default_factory=list)
    waived: list[Waiver] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list, description="only the user can decide (a rule that cannot be met without changing behaviour)")


# -- smoke -----------------------------------------------------------------------------------------------------------

class Behaviour(BaseModel):
    id: str = Field(description="short id used in the SMOKE_RESULT line (S1, S2, …)")
    description: str
    req_ids: list[str] = Field(default_factory=list)


class SmokePlan(BaseModel):
    behaviours: list[Behaviour]
    summary: str = ""
    questions: list[Question] = Field(default_factory=list, description="only for what the spec leaves open (kind spec_gap)")


class Verdict(BaseModel):
    id: str = Field(description="the failing behaviour (or 'compile')")
    verdict: Literal["rtl_bug", "tb_bug", "spec_issue", "unsure"]
    module: str = Field("", description="rtl_bug: the RTL module to change")
    summary: str
    request: str = Field("", description="rtl_bug / tb_bug: what to change, concretely")


class SmokeTriage(BaseModel):
    verdicts: list[Verdict]


# -- csr (the APB-CSR-Generator's register map) ----------------------------------------------------------------------

def _num(v):
    """An int, or a string like `0x10` / `16'h10` / `8` (what a spec or a model writes)."""
    if isinstance(v, str):
        t = v.strip().lower().replace("_", "")
        if "'h" in t:
            t = "0x" + t.split("'h", 1)[1]
        elif "'d" in t:
            t = t.split("'d", 1)[1]
        elif "'b" in t:
            return int(t.split("'b", 1)[1], 2)
        return int(t, 0)
    return v


class CsrField(BaseModel):
    name: str = Field(description="field name (identifier); `reserved` for bits that do nothing")
    msb: int
    lsb: int
    type: Literal["rw", "ro", "rwi", "w1c"] = Field(description=(
        "rw: software read/write. ro: read-only, driven by hardware through a port (`reserved` reads 0). rwi: software read/write, hardware may "
        "also write (software wins a same-cycle write). w1c: software writes 1 to clear, hardware may set"))
    reset: int = 0
    description: str = ""

    _n = field_validator("msb", "lsb", "reset", mode="before")(_num)


class CsrRegister(BaseModel):
    name: str = Field(description="register name (identifier, lowercase)")
    offset: int = Field(description="byte offset, a multiple of 4")
    description: str = ""
    fields: list[CsrField]

    _n = field_validator("offset", mode="before")(_num)


class CsrMap(BaseModel):
    has_registers: bool = Field(True, description="false when the spec has no software-visible register map (nothing to generate)")
    module_name: str = Field("", description="name of the generated CSR module: the one the module map gives the register block, else <top>_csr")
    parent: str = Field("", description="the module that instantiates it (empty: the top)")
    addr_width: int = 16
    data_width: int = 32
    asynchronous: bool = Field(False, description="the registers live on a clock of their own, asynchronous to the APB clock")
    registers: list[CsrRegister] = Field(default_factory=list)
    req_ids: list[str] = Field(default_factory=list, description="the requirement ids the register map implements")
    summary: str = ""
    questions: list[Question] = Field(default_factory=list, description="only for what the spec leaves open about the registers")
