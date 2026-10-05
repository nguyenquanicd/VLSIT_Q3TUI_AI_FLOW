"""Deterministic RTL rule checks for the VLSIT flow (`options.rules` files are the source of the rules; the LLM gets
them as text, this code checks what can be checked without one).

Two profiles follow the two rule files shipped in `rtl/rules/`: `project` (rtl_rule.md: i_ / o_ / reg_ /
w_ / PR_ / LP_, `i_resetn_<d>`, names up to 40 chars — an example of a project-local supplement, not loaded by
default) and `default` (vlsit_rtl_rule_default.md: m_<function>, PARA_ / LPARA_, `i_rst_n_<d>`, p_com_ / p_ff_,
names up to 30 chars — the VLSIT baseline, `rtl/md/skill.md`'s default `options.rules`). The profile comes from the
first rule file named like one of them (conflicts: the first file wins); with no known file only the universal
checks (P-list constructs, named connections, labels) run.

pyslang's syntax tree gives names, ports, parameters, instances and blocks; constructs that are plain text (delays,
`define`, system tasks) are matched in comment-free text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Violation:
    rule: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        return f"{f'line {self.line}: ' if self.line else ''}{self.message} [{self.rule}]"


@dataclass
class Profile:
    name: str
    module_re: str | None = None
    in_re: str = r"i_\w+"
    out_re: str = r"o_\w+"
    inout_re: str = r"io_\w+"
    clk_re: str = r"i_clk_\w+"
    rst_re: str = r"i_resetn_\w+"
    param_re: str | None = None
    lparam_re: str | None = None
    type_re: str | None = None
    enum_re: str | None = None
    inst_re: str = r"u_\w+"
    gen_re: str = r"g_\w+"
    proc_ff_re: str = r"p_\w+"
    proc_comb_re: str = r"p_\w+"
    state_prefix: bool = True  # reg_ for flops, w_ for the rest
    allow_package: bool = True
    forbid_wire_reg: bool = False  # reg / wire / integer declarations (R7, P20)
    forbid_signed: bool = False  # P22
    forbid_two_state: bool = False  # P21
    forbid_define: bool = False  # P12
    forbid_casez: bool = False
    max_len: int | None = None


PROFILES = {
    "project": Profile("project", None, param_re=r"PR_[A-Z0-9_]+", lparam_re=r"LP_[A-Z0-9_]+", type_re=r".*_t", enum_re=r"[A-Z][A-Z0-9_]*",
                       forbid_wire_reg=True, forbid_casez=True, max_len=40),
    "default": Profile("default", r"m_[a-z0-9_]+", in_re=r"i_\w+", out_re=r"o_\w+", clk_re=r"i_clk_\w+", rst_re=r"i_rst_n_\w+",
                       param_re=r"PARA_[A-Z0-9_]+", lparam_re=r"LPARA_[A-Z0-9_]+", type_re=r"[a-z0-9_]+_type_def",
                       enum_re=r"ENUM_[A-Z0-9_]+", inst_re=r"u_\w+", gen_re=r"gen_\w+", proc_ff_re=r"p_ff_\w+", proc_comb_re=r"p_com_\w+",
                       allow_package=False, forbid_wire_reg=True, forbid_signed=True, forbid_two_state=True, forbid_define=True,
                       max_len=30),  # NAM-07 (v2): every RTL name, prefix/suffix included, at most 30 characters
    "universal": Profile("universal", state_prefix=False, in_re=r"\w+", out_re=r"\w+", inout_re=r"\w+", clk_re=r"\w+", rst_re=r"\w+",
                         inst_re=r"\w+", gen_re=r"\w+", proc_ff_re=r"\w+", proc_comb_re=r"\w+"),
}
KNOWN_FILES = {"rtl_rule.md": "project", "vlsit_rtl_rule_default.md": "default"}
OVERRIDES = {"two_state": "forbid_two_state", "signed": "forbid_signed", "max_len": "max_len"}


def profile_for(rule_files: list[str], overrides: dict | None = None) -> Profile:
    name = next((KNOWN_FILES[Path(f).name] for f in rule_files if Path(f).name in KNOWN_FILES), "universal")
    p = Profile(**PROFILES[name].__dict__)
    for k, v in (overrides or {}).items():  # options.naming: {param_re: ..., max_len: 32, ...}
        if hasattr(p, k):
            setattr(p, k, v)
    return p


_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def strip_comments(text: str) -> str:
    """Comments removed, line structure kept (strings blanked so `//` in them is not a comment)."""
    text = _STRING.sub(lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', text)
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


@dataclass
class Facts:
    kind: str = ""  # module | package | ""
    name: str = ""
    ports: list[tuple[str, str, int | None]] = field(default_factory=list)  # (direction, name, line)
    params: list[tuple[str, str, int | None]] = field(default_factory=list)  # (parameter|localparam, name, line)
    signals: list[tuple[str, str, int | None]] = field(default_factory=list)  # (type word, name, line)
    typedefs: list[tuple[str, int | None]] = field(default_factory=list)
    enum_members: list[tuple[str, int | None]] = field(default_factory=list)
    instances: list[tuple[str, str, bool, bool, int | None]] = field(default_factory=list)  # module, inst, positional, wildcard, line
    blocks: list[tuple[str, str | None, int | None]] = field(default_factory=list)  # (always_ff|always_comb|always|..., label, line)
    gens: list[tuple[str, int | None]] = field(default_factory=list)
    nba_in_comb: list[int | None] = field(default_factory=list)
    ba_in_ff: list[int | None] = field(default_factory=list)
    flop_targets: set[str] = field(default_factory=set)
    cases_without_default: list[int | None] = field(default_factory=list)
    ff_headers: list[tuple[str, int | None]] = field(default_factory=list)  # event control text
    parsed: bool = False


def extract(text: str) -> Facts:
    """Names and structure of one source text (empty Facts with parsed=False when pyslang cannot be used)."""
    facts = Facts()
    try:
        from pyslang import syntax
    except Exception:  # noqa: BLE001
        return facts
    tree = syntax.SyntaxTree.fromText(text)

    def line(n) -> int | None:
        try:
            return text.count("\n", 0, n.sourceRange.start.offset) + 1
        except Exception:  # noqa: BLE001
            return None

    def base_name(expr: str) -> str:
        m = re.match(r"\s*([A-Za-z_]\w*)", expr)
        return m.group(1) if m else ""

    def visit(n):
        k = str(n.kind).split(".")[-1]
        if k in ("ModuleDeclaration", "PackageDeclaration") and not facts.kind:
            facts.kind = "module" if k == "ModuleDeclaration" else "package"
            facts.name = (n.header.name if k == "ModuleDeclaration" else n.header.name).valueText
        elif k == "ImplicitAnsiPort":
            d = n.header.direction.valueText if getattr(n.header, "direction", None) else ""
            facts.ports.append((d, n.declarator.name.valueText, line(n)))
        elif k == "ParameterDeclaration":
            kw = n.keyword.valueText
            for d in n.declarators:
                if hasattr(d, "name"):
                    facts.params.append((kw, d.name.valueText, line(n)))
        elif k == "DataDeclaration":
            tw = str(n.type).strip().split()[0] if str(n.type).strip() else ""
            for d in n.declarators:
                if hasattr(d, "name"):
                    facts.signals.append((tw, d.name.valueText, line(n)))
        elif k == "TypedefDeclaration":
            facts.typedefs.append((n.name.valueText, line(n)))
        elif k == "EnumType":
            for d in n.members:
                if hasattr(d, "name"):
                    facts.enum_members.append((d.name.valueText, line(d)))
        elif k == "HierarchyInstantiation":
            for i in n.instances:
                if not hasattr(i, "decl"):
                    continue
                conns = [str(c.kind).split(".")[-1] for c in i.connections]
                facts.instances.append((n.type.valueText, str(i.decl.name).strip(), "OrderedPortConnection" in conns,
                                        "WildcardPortConnection" in conns, line(n)))
        elif k in ("AlwaysFFBlock", "AlwaysCombBlock", "AlwaysBlock", "AlwaysLatchBlock", "InitialBlock", "FinalBlock"):
            st, label, header = n.statement, None, ""
            if str(st.kind).endswith("TimingControlStatement"):
                header = str(st.timingControl).strip()
                st = st.statement
            bn = getattr(st, "blockName", None)
            if bn is not None:
                label = bn.name.valueText
            kind = {"AlwaysFFBlock": "always_ff", "AlwaysCombBlock": "always_comb", "AlwaysBlock": "always",
                    "AlwaysLatchBlock": "always_latch", "InitialBlock": "initial", "FinalBlock": "final"}[k]
            facts.blocks.append((kind, label, line(n)))
            if kind == "always_ff":
                facts.ff_headers.append((header, line(n)))

            def inner(x):
                kk = str(x.kind).split(".")[-1]
                if kind == "always_ff" and kk == "NonblockingAssignmentExpression":
                    facts.flop_targets.add(base_name(str(x.left)))
                elif kind == "always_ff" and kk == "AssignmentExpression":
                    facts.ba_in_ff.append(line(x))
                elif kind == "always_comb" and kk == "NonblockingAssignmentExpression":
                    facts.nba_in_comb.append(line(x))

            n.visit(inner)
        elif k == "GenerateBlock" and getattr(n, "beginName", None) is not None:
            facts.gens.append((n.beginName.name.valueText, line(n)))
        elif k == "CaseStatement":
            if not any(str(i.kind).endswith("DefaultCaseItem") for i in n.items):
                facts.cases_without_default.append(line(n))

    tree.root.visit(visit)
    facts.parsed = True
    return facts


def _line_of(text: str, m: re.Match) -> int:
    return text.count("\n", 0, m.start()) + 1


def check_source(text: str, module: str, prof: Profile, is_package: bool = False) -> list[Violation]:
    """Rule violations of one RTL source (empty: none found by code)."""
    out: list[Violation] = []
    code = strip_comments(text)
    facts = extract(text)

    def bad(rule, msg, ln=None):
        out.append(Violation(rule, msg, ln))

    # -- constructs (text): universal ---------------------------------------------------------
    for m in re.finditer(r"(?<![\w)])#\s*\d", code):
        bad("P1", "delay control `#<n>` in RTL", _line_of(code, m))
    for m in re.finditer(r"\bdefparam\b", code):
        bad("P4", "defparam: use named parameter overrides at the instance", _line_of(code, m))
    for m in re.finditer(r"\bcasex\b", code):
        bad("P3", "casex is not allowed", _line_of(code, m))
    if prof.forbid_casez:
        for m in re.finditer(r"\bcasez\b", code):
            bad("P3", "casez is not allowed", _line_of(code, m))
    for m in re.finditer(r"\$(display|write|random|urandom|finish|fatal|stop|monitor|strobe|dumpfile|dumpvars)\b", code):
        bad("P14", f"testbench-only system task ${m.group(1)} in RTL", _line_of(code, m))
    for m in re.finditer(r"\binout\b", code):
        bad("P17", "inout port inside functional design logic", _line_of(code, m))
    if prof.forbid_define:
        for m in re.finditer(r"`(define|undef)\b", code):
            bad("P12", f"`{m.group(1)} macro directive in RTL", _line_of(code, m))
    if prof.forbid_signed:
        for m in re.finditer(r"\bsigned\b|\$signed\b|\d'[sS][bdhoBDHO]", code):
            bad("P22", "signed data / casts / literals are not allowed (use unsigned vectors)", _line_of(code, m))
    if prof.forbid_wire_reg:
        for m in re.finditer(r"^\s*(reg|wire|integer)\b", code, re.M):
            bad("R7/P20", f"`{m.group(1)}` declaration: declare signals as logic", _line_of(code, m))
    for m in re.finditer(r"\bassert\s+property\b|\bcover\s+property\b|\bassume\s+property\b", code):
        bad("P19", "SVA in the RTL source set (assertions belong to src/sva)", _line_of(code, m))

    if not facts.parsed:
        return out
    # -- structure (syntax tree) -------------------------------------------------------------
    if not facts.kind:
        bad("R9", f"no module or package declaration found (expected `{'package' if is_package else 'module'} {module}`)")
    elif facts.name != module:
        bad("R9", f"file declares `{facts.name}` but the module is `{module}` (one module per file, named like the file)")
    if facts.kind == "package" and not prof.allow_package:
        bad("P11", "package declarations are not allowed in design RTL")
    for kind, label, ln in facts.blocks:
        if kind == "initial":
            bad("P2", "initial block in RTL", ln)
        elif kind == "always":
            bad("P6", "plain `always`: use always_ff / always_comb", ln)
        elif kind == "always_latch":
            bad("P7", "always_latch / latches are not allowed", ln)
        elif kind in ("always_ff", "always_comb"):
            if not label:
                bad("R8", f"{kind} block has no label", ln)
            else:
                pat = prof.proc_ff_re if kind == "always_ff" else prof.proc_comb_re
                if not re.fullmatch(pat, label):
                    bad("R8", f"block label `{label}` does not match `{pat}`", ln)
    for header, ln in facts.ff_headers:
        edges = re.findall(r"\b(posedge|negedge)\s+([A-Za-z_]\w*)", header)
        for edge, sig in edges:
            is_clk = "clk" in sig.lower()
            if is_clk and edge == "negedge":
                bad("R1", f"negedge on clock `{sig}` is not allowed", ln)
            if is_clk and not re.fullmatch(prof.clk_re, sig):
                bad("N-CLK", f"clock `{sig}` does not match `{prof.clk_re}`", ln)
            if not is_clk and re.search(r"rst|reset", sig, re.I) and not re.fullmatch(prof.rst_re, sig):
                bad("N-RST", f"reset `{sig}` does not match `{prof.rst_re}`", ln)
            if not is_clk and re.search(r"rst|reset", sig, re.I) and edge == "posedge":
                bad("R1", f"reset `{sig}` must be active-low (negedge)", ln)
    for ln in facts.nba_in_comb:
        bad("R3", "non-blocking `<=` inside always_comb", ln)
    for ln in facts.ba_in_ff:
        bad("R3", "blocking `=` inside always_ff", ln)
    for ln in facts.cases_without_default:
        bad("R6", "case without `default`", ln)
    for mod, inst, positional, wildcard, ln in facts.instances:
        if positional:
            bad("P13", f"instance `{inst}` of `{mod}` uses positional connections", ln)
        if wildcard:
            bad("P13", f"instance `{inst}` of `{mod}` uses `.*`", ln)
        if not re.fullmatch(prof.inst_re, inst):
            bad("N-INST", f"instance name `{inst}` does not match `{prof.inst_re}`", ln)
        if prof.name == "default":
            func = re.sub(r"^m_", "", mod)
            if not (inst == f"u_{func}" or re.fullmatch(rf"u_{re.escape(func)}_\d+", inst)):
                bad("N-INST", f"instance `{inst}` of `{mod}`: expected `u_{func}` (or `u_{func}_<index>`)", ln)
    for name, ln in facts.gens:
        if not re.fullmatch(prof.gen_re, name):
            bad("N-GEN", f"generate label `{name}` does not match `{prof.gen_re}`", ln)
    if prof.module_re and facts.kind == "module" and not re.fullmatch(prof.module_re, facts.name):
        bad("N-MOD", f"module name `{facts.name}` does not match `{prof.module_re}`")
    if prof.name == "project" and facts.kind == "package" and not facts.name.endswith("_pkg"):
        bad("N-PKG", f"package `{facts.name}` must end with `_pkg`")
    # ports
    for direction, name, ln in facts.ports:
        pat = {"input": prof.in_re, "output": prof.out_re, "inout": prof.inout_re}.get(direction)
        if pat and not re.fullmatch(pat, name):
            bad("N-PORT", f"{direction} port `{name}` does not match `{pat}`", ln)
        if re.search(r"(^|_)clk(_|$)", name) and direction == "input" and not re.fullmatch(prof.clk_re, name):
            bad("N-CLK", f"clock port `{name}` does not match `{prof.clk_re}`", ln)
        if re.search(r"(^|_)(rst|resetn|reset)(_|$|n)", name) and direction == "input" and not re.fullmatch(prof.rst_re, name):
            bad("N-RST", f"reset port `{name}` does not match `{prof.rst_re}`", ln)
    # parameters
    for kw, name, ln in facts.params:
        pat = prof.param_re if kw == "parameter" else prof.lparam_re
        if pat and not re.fullmatch(pat, name):
            bad("N-PARAM", f"{kw} `{name}` does not match `{pat}`", ln)
    for name, ln in facts.typedefs:
        if prof.type_re and not re.fullmatch(prof.type_re, name):
            bad("N-TYPE", f"type `{name}` does not match `{prof.type_re}`", ln)
    for name, ln in facts.enum_members:
        if prof.enum_re and not re.fullmatch(prof.enum_re, name):
            bad("N-ENUM", f"enum member `{name}` does not match `{prof.enum_re}`", ln)
    # signals
    for tw, name, ln in facts.signals:
        if prof.forbid_two_state and tw in ("bit", "byte", "shortint", "int", "longint"):
            bad("P21", f"two-state type `{tw}` for `{name}`: use logic", ln)
        if prof.state_prefix and tw not in ("typedef",):
            want = "reg_" if name in facts.flop_targets else "w_"
            if not name.startswith(want):
                # an enum / struct typed state signal also follows the prefix: only genvar-like helpers are exempt
                bad("N-SIG", f"signal `{name}` must start with `{want}` ({'flop' if want == 'reg_' else 'combinational'})", ln)
    if prof.max_len:
        seen = {n for _, n, _ in [*facts.ports, *facts.params, *facts.signals]} | {n for n, _ in facts.typedefs}
        for n in sorted(seen):
            if len(n) > prof.max_len:
                bad("N-LEN", f"name `{n}` is {len(n)} characters (max {prof.max_len})")
    return out


_REQ = re.compile(r"\bREQ-\d{3,}\b")


def req_tags(text: str) -> set[str]:
    """REQ ids mentioned in comments of a source (the trace tags `// REQ-xxx`)."""
    return set(_REQ.findall(text))


def count_tags(text: str) -> int:
    return len(_REQ.findall(text))
