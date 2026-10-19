"""Elaboration constraints as checkable expressions (C1…Cn of the spec), evaluated by code — never by the LLM.

`PR_BOOT_ADDR[1:0] == 2'b00` · `PR_BOOT_ADDR % 4 == 0` · `PR_IRQ_EN == 1 -> PR_CSR_EN == 1` · `PR_W in (8, 16, 32)` ·
`PR_DEPTH >= 2 && PR_DEPTH <= 1024`. Verilog literals (`32'h8000_0000`, `'d12`, `4'b01x0` → not evaluable), `&&` `||` `!`,
implication (`->`, `→`, `=>`, `implies`), bit slices and `$clog2` are understood. `evaluate` returns (True | False | None, detail):
None = the expression cannot be evaluated (it is then a question for the user, never silently passed).
"""

from __future__ import annotations

import ast
import math
import re

_LIT = re.compile(r"(\d*)\s*'([sS]?)([bBoOdDhH])\s*([0-9a-fA-F_xXzZ?]+)")
_SLICE = re.compile(r"\b([A-Za-z_]\w*)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]")
_BIT = re.compile(r"\b([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]")
_IMPLIES = re.compile(r"\s*(?:->|→|=>|\bimplies\b)\s*")


class NotEvaluable(ValueError):
    pass


def to_int(text: str) -> int:
    """A parameter value as an integer: decimal, 0x…, Verilog literal, true/false."""
    t = str(text).strip().replace("_", "")
    low = t.lower()
    if low in ("true", "yes"):
        return 1
    if low in ("false", "no"):
        return 0
    m = _LIT.fullmatch(t)
    if m:
        return _literal(m)
    try:
        return int(t, 0)
    except ValueError as exc:
        raise NotEvaluable(f"'{text}' is not a number") from exc


def _literal(m: re.Match) -> int:
    base = {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(3).lower()]
    digits = m.group(4).replace("_", "")
    if re.search(r"[xXzZ?]", digits):
        raise NotEvaluable(f"literal {m.group(0)} has x/z bits")
    return int(digits, base)


def _translate(expr: str) -> str:
    e = _LIT.sub(lambda m: str(_literal(m)), expr.replace("_", "_"))
    e = _SLICE.sub(lambda m: f"(({m.group(1)} >> {m.group(3)}) & {(1 << (int(m.group(2)) - int(m.group(3)) + 1)) - 1})", e)
    e = _BIT.sub(lambda m: f"(({m.group(1)} >> {m.group(2)}) & 1)", e)
    e = e.replace("&&", " and ").replace("||", " or ").replace("$clog2", "clog2")
    e = re.sub(r"!(?!=)", " not ", e)
    e = re.sub(r"(?<![<>=!])===?(?!=)", "==", e)
    return e


class _Eval(ast.NodeVisitor):
    def __init__(self, env: dict[str, int]):
        self.env = env

    def visit_Expression(self, n):
        return self.visit(n.body)

    def visit_Constant(self, n):
        if isinstance(n.value, (int, bool)):
            return int(n.value)
        raise NotEvaluable(f"unsupported constant {n.value!r}")

    def visit_Name(self, n):
        if n.id not in self.env:
            raise NotEvaluable(f"unknown parameter {n.id}")
        return self.env[n.id]

    def visit_BoolOp(self, n):
        vals = [self.visit(v) for v in n.values]
        return int(all(vals) if isinstance(n.op, ast.And) else any(vals))

    def visit_UnaryOp(self, n):
        v = self.visit(n.operand)
        return {ast.Not: lambda x: int(not x), ast.USub: lambda x: -x, ast.UAdd: lambda x: x, ast.Invert: lambda x: ~x}[type(n.op)](v)

    def visit_BinOp(self, n):
        a, b = self.visit(n.left), self.visit(n.right)
        ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.Mod: lambda: a % b,
               ast.FloorDiv: lambda: a // b, ast.Div: lambda: a // b, ast.LShift: lambda: a << b, ast.RShift: lambda: a >> b,
               ast.BitAnd: lambda: a & b, ast.BitOr: lambda: a | b, ast.BitXor: lambda: a ^ b, ast.Pow: lambda: a ** b}
        if type(n.op) not in ops:
            raise NotEvaluable("unsupported operator")
        try:
            return ops[type(n.op)]()
        except ZeroDivisionError as exc:
            raise NotEvaluable("division by zero") from exc

    def visit_Compare(self, n):
        left = self.visit(n.left)
        for op, right_n in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                if not isinstance(right_n, (ast.Tuple, ast.List, ast.Set)):
                    raise NotEvaluable("`in` needs a list")
                ok = left in [self.visit(e) for e in right_n.elts]
                ok = ok if isinstance(op, ast.In) else not ok
            else:
                right = self.visit(right_n)
                ok = {ast.Eq: left == right, ast.NotEq: left != right, ast.Lt: left < right, ast.LtE: left <= right,
                      ast.Gt: left > right, ast.GtE: left >= right}[type(op)]
                left = right
            if not ok:
                return 0
        return 1

    def visit_Call(self, n):
        if isinstance(n.func, ast.Name) and n.func.id == "clog2" and len(n.args) == 1:
            v = self.visit(n.args[0])
            return max(0, math.ceil(math.log2(v))) if v > 0 else 0
        raise NotEvaluable("unsupported function")

    def generic_visit(self, n):
        raise NotEvaluable(f"unsupported syntax ({type(n).__name__})")


def _eval(expr: str, env: dict[str, int]) -> int:
    try:
        tree = ast.parse(_translate(expr).strip(), mode="eval")
    except SyntaxError as exc:
        raise NotEvaluable(f"cannot parse '{expr}'") from exc
    return _Eval(env).visit(tree)


def evaluate(rule: str, values: dict[str, str]) -> tuple[bool | None, str]:
    """(verdict, detail) of one rule against the parameter values (strings as in final_config.json)."""
    env: dict[str, int] = {}
    for k, v in values.items():
        try:
            env[k] = to_int(v)
        except NotEvaluable:
            pass
    parts = _IMPLIES.split(rule.strip(), maxsplit=1)
    try:
        if len(parts) == 2:
            ante, cons = (_eval(p, env) for p in parts)
            if not ante:
                return True, "condition not active"
            return bool(cons), "holds" if cons else f"{parts[0].strip()} but not {parts[1].strip()}"
        ok = bool(_eval(rule, env))
        return ok, "holds" if ok else "violated"
    except NotEvaluable as exc:
        return None, f"cannot be evaluated by code: {exc}"


def in_range(value: str, valid_range: str) -> bool:
    """Whether `value` fits a parameter's range text when the text is checkable ("0/1", "1..64", "1-64", "0 or 1",
    "align4"); unknown wording never rejects."""
    try:
        v = to_int(value)
    except NotEvaluable:
        return True
    r = (valid_range or "").strip().lower()
    if m := re.fullmatch(r"align\s*(\d+)", r):
        return v % int(m.group(1)) == 0
    if m := re.fullmatch(r"\s*(-?\w+)\s*(?:\.\.|…|to)\s*(-?\w+)\s*", r):
        try:
            return to_int(m.group(1)) <= v <= to_int(m.group(2))
        except NotEvaluable:
            return True
    if m := re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", r):
        return int(m.group(1)) <= v <= int(m.group(2))
    if re.fullmatch(r"[\w']+(\s*(/|,|\bor\b)\s*[\w']+)+", r):
        try:
            return v in {to_int(x) for x in re.split(r"\s*(?:/|,|\bor\b)\s*", r)}
        except NotEvaluable:
            return True
    return True
