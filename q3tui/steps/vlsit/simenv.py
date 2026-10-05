"""Simulating the VLSIT testbench through the tool roles `sim` (compile) and `run` (execute one test case)."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from q3tui.eda import tools as T
from q3tui.eda.base import ToolResult, ToolUnavailable
from q3tui.steps.vlsit import rtm, simlog
from q3tui.steps.vlsit.base import layout


def roles_problem(engine, names: tuple[str, ...]) -> str | None:
    """None when every role is configured and its binary can be found; else what is missing (names the role)."""
    try:
        ts = T.load_tools(engine.project.cfg, engine.project.root)
    except (ToolUnavailable, OSError, ValueError) as exc:
        return f"the tools file could not be read: {exc}"
    for n in names:
        if n not in ts.roles:
            return f"tool role '{n}' is not configured (tools.json: roles: {{{n}: {{cmd: …}}}}; docs/spec/flows.md)"
        if not T.available(ts, n):
            return f"tool role '{n}': '{T.binary_of(ts.roles[n])}' was not found (check tools.json, setup_script / modules)"
    return None


def read_filelist(path: Path, root: Path) -> tuple[list[Path], list[Path]]:
    """(files, include dirs) of a .f file; relative entries are tried against the project root, then the file's folder."""
    files, incs = [], []

    def resolve(tok: str) -> Path:
        for base in (root, path.parent):
            if (base / tok).exists():
                return (base / tok).resolve()
        return (root / tok).resolve()

    for raw in path.read_text(errors="replace").splitlines():
        line = re.sub(r"//.*|#.*", "", raw).strip()
        if not line:
            continue
        for tok in shlex.split(line):
            if tok.startswith("+incdir+"):
                incs += [resolve(t) for t in tok[len("+incdir+"):].split("+") if t]
            elif tok.startswith(("-", "+")):
                continue
            else:
                files.append(resolve(tok))
    return files, incs


def sources(engine, with_sva: bool, rtl_dir: Path | None = None) -> tuple[list[Path], list[Path]]:
    lay = layout(engine)
    root = engine.project.root
    files: list[Path] = []
    incs: list[Path] = []
    rtl = rtl_dir or lay.rtl
    f = rtl / "filelist.f"
    if f.is_file() and rtl_dir is None:
        a, b = read_filelist(f, root)
        files += a
        incs += b
    else:
        pkgs = sorted(rtl.glob("*_pkg.sv"))
        files += pkgs + [p for p in sorted(rtl.glob("*.sv")) if p not in pkgs]
    own_rtl = lay.rtl.resolve()

    def not_rtl(paths: list[Path]) -> list[Path]:
        # (the testbench / SVA filelists list the RTL too, for compiling them alone: here the RTL is `rtl` — or a mutant's copy
        # of it. Listing the original again made every mutant "survive": the simulator ran the unmutated module.)
        return [p for p in paths if own_rtl not in p.resolve().parents]

    if with_sva and lay.sva.is_dir():
        f = lay.sva / "filelist_sva.f"
        if f.is_file():
            a, b = read_filelist(f, root)
            files += not_rtl(a)
            incs += b
        else:
            files += sorted(lay.sva.glob("*.sv"))
    f = lay.tb / "filelist.f"
    if f.is_file():
        a, b = read_filelist(f, root)
        files += not_rtl(a)
        incs += b
    else:
        files += sorted(lay.tb.glob("*.sv"))
        incs.append(lay.tb)
    return list(dict.fromkeys(files)), list(dict.fromkeys(incs))


def tb_top(engine) -> str:
    plan = (rtm.artifacts.read(engine.project, "selected_testplan.json", {}) or {}).get("metadata", {})
    if plan.get("tb_top"):
        return plan["tb_top"]
    for f in sorted(layout(engine).tb.glob("*_tb.sv")) + sorted(layout(engine).tb.glob("tb_*.sv")):
        return f.stem
    return "tb"


def write_filelist(workdir: Path, files: list[Path], incs: list[Path]) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    f = workdir / "all.f"
    f.write_text("".join(f"+incdir+{d}\n" for d in incs) + "".join(f"{p}\n" for p in files))
    return f


def compile_all(engine, ts: T.ToolSet, workdir: Path, with_sva: bool, rtl_dir: Path | None = None) -> ToolResult:
    files, incs = sources(engine, with_sva, rtl_dir)
    fl = write_filelist(workdir, files, incs)
    res = T.run_role(ts, "sim", workdir, {"filelist": fl, "files": files, "incdirs": incs, "top": tb_top(engine),
                                          "workdir": workdir, "project": engine.project.root}, "compile.log")
    T.check_broken(res, "sim")  # (a compile that fails with no error line is the command's problem, not the code's)
    return res


def run_tc(engine, ts: T.ToolSet, workdir: Path, tc_id: str) -> simlog.TcOutcome:
    log_name = f"run_{re.sub(r'[^A-Za-z0-9_.-]+', '_', tc_id)}.log"
    res = T.run_role(ts, "run", workdir, {"tc": tc_id, "top": tb_top(engine), "workdir": workdir, "project": engine.project.root}, log_name)
    text = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
    out = simlog.parse_run(tc_id, text, res.returncode, res.timed_out, engine.project.rel(Path(res.log_path)) if res.log_path else "")
    out.__dict__["_text"] = text
    return out


def output_text(outcome: simlog.TcOutcome) -> str:
    return outcome.__dict__.get("_text", "")


_VERSIONS = [  # tool banner → version (read from the logs of the real runs: `--version` is not the same option everywhere)
    (re.compile(r"Chronologic VCS.*?\n\s*Version\s+(\S+)", re.S), "VCS {}"),
    (re.compile(r"Design Compiler[^\n]*\n.*?Version\s+([A-Z]-\d{4}\.\d+(?:-SP\d+)?)", re.S), "Design Compiler {}"),
    (re.compile(r"^\s*Version:\s+([A-Z]-\d{4}\.\d+(?:-SP\d+)?)", re.M), "Design Compiler {}"),
    (re.compile(r"Verilator (\d+\.\d+)"), "Verilator {}"), (re.compile(r"Yosys (\d+\.\d+(?:\+\d+)?)"), "Yosys {}"),
    (re.compile(r"Icarus Verilog version (\S+)"), "Icarus Verilog {}"),
]


def tool_versions(engine, ts: T.ToolSet, names: list[str]) -> dict[str, str]:
    """Tool versions as the tools printed them in the logs of this flow's runs (compile / lint / synthesis); a role whose
    logs show none is left out (never an error message as a "version")."""
    sim = engine.project.state_dir / "sim"
    logs = {"sim": [sim / "verify" / "compile.log", sim / "tb" / "compile.log"], "run": [sim / "verify" / "compile.log"],
            "lint": [sim / "rtl" / "lint_design.log"], "synth": sorted((sim / "rtl").glob("synth_*.log"))}
    out = {}
    for n in names:
        for log in logs.get(n, []):
            if not log.is_file():
                continue
            text = log.read_text(errors="replace")[:20000]
            hit = next((fmt.format(m.group(1)) for rx, fmt in _VERSIONS if (m := rx.search(text))), None)
            if hit:
                out[n] = hit
                break
    return out
