"""The VLSIT RTL module plan: which modules, what each implements, who instantiates whom.

It comes from `schemas/structured_spec.json` (track parse: `modules: [{name, description, req_ids, instances}]`, or the
requirements' `rtl_modules`); when the spec gives none, the LLM stage `rtl_plan` proposes one and it is kept in
`schemas/rtl_plan.json`. Order is code: packages first, then leaves, the top last.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlanModule(BaseModel):
    name: str
    description: str = ""
    req_ids: list[str] = Field(default_factory=list, description="REQ ids this module's logic implements (each must be tagged in its RTL)")
    instances: list[str] = Field(default_factory=list, description="Names of the modules this one instantiates")

    @property
    def is_package(self) -> bool:
        return self.name.endswith("_pkg")


class RtlPlan(BaseModel):
    top: str = Field(description="The top module")
    modules: list[PlanModule]


def _names(v) -> list[str]:
    out = []
    for x in v or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            n = x.get("module") or x.get("name")
            if n:
                out.append(str(n))
    return out


def plan_from_spec(spec: dict) -> RtlPlan | None:
    """The plan the structured spec implies (None when it names no module)."""
    reqs = spec.get("requirements") or []
    meta = spec.get("metadata") or {}
    by_name: dict[str, PlanModule] = {}
    for m in spec.get("modules") or []:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or m.get("module")
        if not name:
            continue
        by_name[name] = PlanModule(name=name, description=str(m.get("description") or m.get("purpose") or ""),
                                   req_ids=[str(r) for r in (m.get("req_ids") or m.get("requirements") or [])],
                                   instances=_names(m.get("instances") or m.get("children")))
    for r in reqs:  # requirement → module mapping (spec_parser step 6.2)
        for mod in r.get("rtl_modules") or []:
            name = mod if isinstance(mod, str) else (mod.get("name") or mod.get("module"))
            if not name:
                continue
            pm = by_name.setdefault(name, PlanModule(name=name))
            if r.get("req_id") and r["req_id"] not in pm.req_ids:
                pm.req_ids.append(r["req_id"])
    if not by_name:
        return None
    top = meta.get("top_module") or spec.get("top_module")
    if not top or top not in by_name:
        # the module nobody instantiates; else the one named like the IP
        inst = {i for m in by_name.values() for i in m.instances}
        roots = [n for n in by_name if n not in inst and not by_name[n].is_package]
        ip = str(meta.get("ip_name") or "")
        top = next((n for n in roots if n == ip), roots[-1] if roots else next(iter(by_name)))
    if not any(m.instances for m in by_name.values()):  # no hierarchy given: the top instantiates the others
        by_name[top].instances = [n for n, m in by_name.items() if n != top and not m.is_package]
    known = set(by_name)
    for m in by_name.values():
        m.instances = [i for i in m.instances if i in known and i != m.name]
    return RtlPlan(top=top, modules=list(by_name.values()))


def dependencies(plan: RtlPlan, module: str) -> list[str]:
    """What must exist before `module` is written: the packages, then what it instantiates (transitively), leaves first."""
    by_name = {m.name: m for m in plan.modules}
    out: list[str] = [m.name for m in plan.modules if m.is_package and m.name != module]
    seen = set(out)

    def visit(n: str):
        for c in by_name[n].instances:
            if c in by_name and c not in seen and c != module:
                seen.add(c)
                visit(c)
                out.append(c)

    visit(module)
    return out


def levels(plan: RtlPlan) -> list[list[PlanModule]]:
    """Generation levels: packages, then modules whose instances are all written, the top last. A cycle raises."""
    by_name = {m.name: m for m in plan.modules}
    done: set[str] = set()
    out: list[list[PlanModule]] = []
    pkgs = [m for m in plan.modules if m.is_package]
    if pkgs:
        out.append(pkgs)
        done |= {m.name for m in pkgs}
    left = [m for m in plan.modules if not m.is_package]
    while left:
        ready = [m for m in left if all(i in done or i not in by_name for i in m.instances)]
        if not ready:
            raise ValueError("the module hierarchy has a cycle: " + ", ".join(m.name for m in left))
        out.append(ready)
        done |= {m.name for m in ready}
        left = [m for m in left if m.name not in done]
    return out


def compile_order(plan: RtlPlan) -> list[str]:
    return [m.name for lvl in levels(plan) for m in lvl]
