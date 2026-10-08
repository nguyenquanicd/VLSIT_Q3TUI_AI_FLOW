"""Mutation testing of the RTL (RTM condition 5).

A mutant is the RTL with one small change (an operator, a constant, a condition); the testbench must notice it (a failing
test case or an SVA violation = *killed*); one that passes everything *survived* — a check that would not see that bug.
Mutants that do not compile are skipped. The score of a requirement = killed / (total − compile errors) over the mutants on
RTL tagged `// REQ-xxx` for it; a requirement with no mutant is `N/A` (never a pass).

Two sources: the role `mutate` when tools.json has one (it writes `{workdir}/mutation.json`: `{"mutants": [{"id", "module",
"file", "line", "req_ids"?, "status": killed|survived|compile_error}]}`), else the built-in mutator below + the roles
`sim` and `run`.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from q3tui.eda import tools as T
from q3tui.core.project import read_json, write_json
from q3tui.steps.vlsit import rtm, simenv
from q3tui.steps.vlsit.base import layout

# (regex on the code part of a line, replacement) — each match is one candidate
OPERATORS = [
    (r"==", "!="), (r"!=", "=="), (r"&&", "||"), (r"\|\|", "&&"),
    (r"(?<=\s)\+(?=\s)", "-"), (r"(?<=\s)-(?=\s)", "+"),
    (r"(?<=\s)>(?=\s)", "<="), (r"(?<=\s)<(?=\s)", ">="),
    (r"(?<=\s)&(?=\s)", "|"), (r"(?<=\s)\|(?=\s)", "&"),
    (r"1'b0", "1'b1"), (r"1'b1", "1'b0"),
    (r"\bif\s*\((?!\s*!)", "if (!"),
]
_TAG_NEAR = 40


@dataclass
class Mutant:
    id: str
    module: str
    file: str
    line: int
    col: int
    old: str
    new: str
    req_ids: list[str] = field(default_factory=list)
    status: str = "pending"  # killed | survived | compile_error
    killed_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _code_part(line: str) -> str:
    return line.split("//", 1)[0]


def _tags_by_line(lines: list[str]) -> dict[int, list[str]]:
    out = {}
    for i, l in enumerate(lines, 1):
        if "//" in l:
            ids = rtm.REQ_RE.findall(l.split("//", 1)[1])
            if ids:
                out[i] = ids
    return out


def _reqs_at(tags: dict[int, list[str]], line: int) -> list[str]:
    if line in tags:
        return tags[line]
    for l in range(line - 1, max(0, line - _TAG_NEAR), -1):
        if l in tags:
            ids = list(tags[l])
            k = l - 1
            while k in tags:  # consecutive tag lines belong together
                ids += tags[k]
                k -= 1
            return sorted(set(ids))
    return []


_ELAB_TASK = re.compile(r"\$(?:error|fatal|warning|info)\b")


def _elab_checks(lines: list[str]) -> set[int]:
    """Lines of the condition of an elaboration check (`if (PARA_… < 1) begin : gen_chk … $error(…)`): mutating it
    only makes elaboration fail (a compile error, no test run), so it is never a candidate."""
    out: set[int] = set()
    for n, raw in enumerate(lines, 1):
        if not _ELAB_TASK.search(_code_part(raw)):
            continue
        for k in range(n - 1, max(0, n - 7), -1):  # back to the `if` that opens its block (a statement in between: none)
            code = _code_part(lines[k - 1]).strip()
            if re.match(r"(?:end\s+)?(?:else\s+)?if\b", code):
                out.update(range(k, n))
                break
            if code.endswith(";"):
                break
    return out


def candidates(text: str) -> list[tuple[int, int, str, str]]:
    """(line, column, old, new) of every possible mutation, outside comments, string literals, elaboration checks and
    `ifndef SYNTHESIS` regions."""
    out = []
    skip = 0
    lines = text.splitlines()
    checks = _elab_checks(lines)
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if s.startswith("`ifndef SYNTHESIS"):
            skip += 1
        elif skip and s.startswith("`endif"):
            skip -= 1
        if skip or n in checks or s.startswith(("//", "`")) or re.match(r"(?:input|output|inout|parameter|localparam|import|module)\b", s):
            continue
        code = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: " " * len(m.group(0)), _code_part(raw))  # (columns kept)
        for pat, new in OPERATORS:
            for m in re.finditer(pat, code):
                out.append((n, m.start(), m.group(0), new))
    return out


def generate(engine, per_module: int, rtl_dir: Path | None = None) -> list[Mutant]:
    """A bounded, deterministic sample of mutants per RTL file (evenly spaced over its candidates)."""
    project = engine.project
    mutants: list[Mutant] = []
    for f in sorted((rtl_dir or layout(engine).rtl).glob("*.sv")):
        if f.stem.endswith("_pkg"):
            continue
        text = f.read_text(errors="replace")
        cands = candidates(text)
        if not cands:
            continue
        if len(cands) > per_module:
            step = len(cands) / per_module
            cands = [cands[int(i * step)] for i in range(per_module)]
        tags = _tags_by_line(text.splitlines())
        for k, (line, col, old, new) in enumerate(cands):
            mutants.append(Mutant(f"{f.stem}#{k + 1}", f.stem, project.rel(f), line, col, old, new, _reqs_at(tags, line)))
    return mutants


def apply(text: str, m: Mutant) -> str:
    lines = text.splitlines(keepends=True)
    l = lines[m.line - 1]
    lines[m.line - 1] = l[: m.col] + m.new + l[m.col + len(m.old):]
    return "".join(lines)


def _summarise(mutants: list[Mutant]) -> dict:
    per: dict[str, dict] = {}
    for m in mutants:
        for rid in m.req_ids:
            d = per.setdefault(rid, {"total": 0, "killed": 0, "survived": 0, "compile_error": 0})
            d["total"] += 1
            if m.status in d:
                d[m.status] += 1
    return per


def totals(mutants: list[Mutant]) -> dict:
    killed = sum(m.status == "killed" for m in mutants)
    ce = sum(m.status == "compile_error" for m in mutants)
    ran = len(mutants) - ce
    return {"total_mutations": len(mutants), "total_killed": killed, "compile_error": ce,
            "global_score": round(killed / ran, 4) if ran else 0.0}


def save(engine, mutants: list[Mutant], ran: bool, reason: str = "") -> dict:
    data = {"fingerprint": rtm.fingerprint(engine), "ran": ran, "reason": reason,
            "mutants": [m.to_dict() for m in mutants], "per_req": _summarise(mutants), **totals(mutants)}
    write_json(rtm.work_dir(engine, "verify") / "mutation.json", data)
    return data


def not_run(engine, reason: str) -> dict:
    """Mutation could not run (no tool, failing baseline): every requirement gets N/A (tool missing) — never a pass."""
    return save(engine, [], False, reason)


async def run_builtin(engine, ts: T.ToolSet, tcs: list[str], per_module: int, with_sva: bool, parallel: int, emit=None) -> dict:
    import anyio

    mutants = generate(engine, per_module)
    base = rtm.work_dir(engine, "mutation")
    shutil.rmtree(base, ignore_errors=True)
    limiter = anyio.CapacityLimiter(max(1, parallel))
    lay = layout(engine)

    def one(m: Mutant) -> None:
        mdir = base / re.sub(r"[^\w]+", "_", m.id)
        rdir = mdir / "rtl"
        rdir.mkdir(parents=True, exist_ok=True)
        for f in lay.rtl.glob("*.sv"):
            shutil.copy(f, rdir / f.name)
        target = rdir / Path(m.file).name
        target.write_text(apply(target.read_text(errors="replace"), m))
        res = simenv.compile_all(engine, ts, mdir, with_sva, rtl_dir=rdir)
        if not res.ok:
            m.status = "compile_error"
            return
        for tc in tcs:
            out = simenv.run_tc(engine, ts, mdir, tc)
            if out.status != "pass":
                m.status, m.killed_by = "killed", tc
                return
        m.status = "survived"

    async def guarded(m: Mutant) -> None:
        async with limiter:
            await anyio.to_thread.run_sync(one, m)
            if emit:
                emit("log", message=f"mutant {m.id} line {m.line} ({m.old} → {m.new}): {m.status}")

    async with anyio.create_task_group() as tg:
        for m in mutants:
            tg.start_soon(guarded, m)
    shutil.rmtree(base, ignore_errors=True)
    return save(engine, mutants, True)


def run_tool(engine, ts: T.ToolSet, tcs: list[str], per_module: int) -> dict:
    """The role `mutate`: it writes mutation.json (see the module doc); its mutants are attributed to REQs by the RTL tags."""
    workdir = rtm.work_dir(engine, "mutation")
    shutil.rmtree(workdir, ignore_errors=True)
    files, incs = simenv.sources(engine, False)
    fl = simenv.write_filelist(workdir, files, incs)
    res = T.run_role(ts, "mutate", workdir, {"filelist": fl, "files": files, "incdirs": incs, "top": simenv.tb_top(engine),
                                             "workdir": workdir, "project": engine.project.root, "tcs": tcs,
                                             "mutants": per_module, "rtl_dir": layout(engine).rtl}, "mutate.log")
    out = read_json(workdir / "mutation.json", default=None)
    if not isinstance(out, dict) or not res.returncode == 0 and not out:
        return not_run(engine, f"the mutate tool produced no mutation.json ({res.summary})")
    mutants = []
    tags_cache: dict[str, dict] = {}
    for i, raw in enumerate(out.get("mutants") or []):
        file = raw.get("file", "")
        p = engine.project.root / file
        if file and file not in tags_cache and p.is_file():
            tags_cache[file] = _tags_by_line(p.read_text(errors="replace").splitlines())
        reqs = raw.get("req_ids") or (_reqs_at(tags_cache.get(file, {}), int(raw.get("line", 0))) if raw.get("line") else [])
        mutants.append(Mutant(str(raw.get("id", i)), raw.get("module", Path(file).stem), file, int(raw.get("line", 0) or 0), 0,
                              "", "", list(reqs), raw.get("status", "survived")))
    return save(engine, mutants, True)
