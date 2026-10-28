"""Changes made to generated RTL (the modules the csr step's APB-CSR-Generator makes), kept so they outlive the next generation.

`src/rtl/csr/patches/<module>.patch` is every change made to the module since the tool made it: a unified diff from the tool's output to the file in
src/rtl (the `rtl` step's lint fixes and change requests, edits by hand). `<module>.md` next to it says why, change by change. The tool's output itself
is kept in `.q3tui/csr/<module>.sv` (the base of the diff). When the tool makes the module again (the register map changed), the csr step applies the patch
to the new file by code, each hunk where its lines are; a hunk that fits nowhere goes to the `rtl` step as a change request, with the why. Delete the
patch to drop the changes (the next generation is then the tool's file as is)."""

from __future__ import annotations

import difflib
import re
from datetime import datetime
from pathlib import Path

from q3tui.flows.rtl.lib import csr

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def base_path(project, module: str) -> Path:
    return project.state_dir / "csr" / f"{module}.sv"


def patch_path(rtl: Path, module: str) -> Path:
    return rtl / "csr" / "patches" / f"{module}.patch"


def notes_path(rtl: Path, module: str) -> Path:
    return rtl / "csr" / "patches" / f"{module}.md"


def base(project, module: str) -> str | None:
    p = base_path(project, module)
    return p.read_text() if p.is_file() else None


def save_base(project, module: str, text: str) -> None:
    p = base_path(project, module)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def diff(old: str, new: str, module: str) -> str:
    """The unified diff old → new ('' when they are the same but for the tool's volatile header lines)."""
    a, b = old.splitlines(), new.splitlines()
    if len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b)):
        return ""
    lines = list(difflib.unified_diff(a, b, f"a/{module}.sv (generated)", f"b/{module}.sv", lineterm=""))
    return "\n".join(lines) + "\n" if lines else ""


def hunks(patch: str) -> list[tuple[int, list[str]]]:
    """(the line it starts at in the base, its lines with their ' ' / '-' / '+' mark) of each hunk."""
    out: list[tuple[int, list[str]]] = []
    for line in patch.splitlines():
        m = _HUNK.match(line)
        if m:
            out.append((int(m.group(1)), []))
        elif out and line[:1] in (" ", "-", "+"):
            out[-1][1].append(line)
        elif out and line == "":  # (an empty context line whose space was stripped)
            out[-1][1].append(" ")
    return out


def apply(text: str, patch: str) -> tuple[str, list[str]]:
    """`text` with the patch's hunks applied, each where its old lines are (the nearest place to where the patch had them: the new file may have moved
    them), with up to 2 context lines dropped at each end when the lines next to a change changed too (GNU patch's fuzz); the tool's volatile header
    lines match whatever date they carry. Returns (the text, the hunks that fit nowhere, as diff text)."""
    lines = text.splitlines()
    failed: list[str] = []
    shift = 0
    for start, whole in hunks(patch):
        for fuzz in range(3):
            lo, body = _trim(whole, fuzz)
            old = [ln[1:] for ln in body if ln[0] in " -"]
            at = _find(lines, old, start + lo - 1 + shift)
            if at is not None:
                break
        if at is None:
            failed.append(f"@@ -{start} @@\n" + "\n".join(whole))
            continue
        start += lo
        new, i = [], at
        for ln in body:
            if ln[0] == " ":
                new.append(lines[i])  # (the new file's own line: its date, not the patch's)
                i += 1
            elif ln[0] == "-":
                i += 1
            else:
                new.append(ln[1:])
        lines[at:at + len(old)] = new
        shift = at - (start - 1) + len(new) - len(old)
    return "\n".join(lines) + "\n", failed


def record(project, rtl: Path, module: str, why: list[str]) -> str:
    """After a change to the generated module: its patch (base → the file now; none when it is the tool's file again) and `why` in its notes. Returns
    the patch. Without a base (a module generated before patches were kept) the file as it is now becomes the base: changes from now on are recorded."""
    f = rtl / f"{module}.sv"
    if not f.is_file():
        return ""
    b = base(project, module)
    if b is None:
        save_base(project, module, f.read_text())
        _note(rtl, module, ["(the base of this patch is the file as it was here: the changes made before are part of it)"])
        b = f.read_text()
    p = diff(b, f.read_text(), module)
    pp = patch_path(rtl, module)
    if p:
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text(p)
    elif pp.is_file():
        pp.unlink()
    if why:
        _note(rtl, module, why)
    return p


def unrecorded(project, rtl: Path, module: str) -> bool:
    """The file has changes its patch does not have (made by hand, outside the steps)."""
    f, b = rtl / f"{module}.sv", base(project, module)
    if b is None or not f.is_file():
        return False
    pp = patch_path(rtl, module)
    return diff(b, f.read_text(), module) != (pp.read_text() if pp.is_file() else "")


def _note(rtl: Path, module: str, why: list[str]) -> None:
    p = notes_path(rtl, module)
    p.parent.mkdir(parents=True, exist_ok=True)
    head = "" if p.is_file() else (f"# Changes to the generated `{module}`\n\n`{module}.patch` is every change made to the module since the APB-CSR-Generator made it; "
                                   "the csr step applies it again whenever the tool makes the module again. Why, change by change:\n")
    with p.open("a") as out:
        out.write(head + f"\n## {datetime.now().isoformat(timespec='minutes')}\n" + "".join(f"- {w.strip()}\n" for w in why))


def _trim(body: list[str], fuzz: int) -> tuple[int, list[str]]:
    """(how many lines were dropped in front, the hunk with up to `fuzz` context lines dropped at each end)."""
    lo, hi = 0, len(body)
    while lo < min(fuzz, hi) and body[lo][0] == " ":
        lo += 1
    while hi > lo and len(body) - hi < fuzz and body[hi - 1][0] == " ":
        hi -= 1
    return lo, body[lo:hi]


def _same(a: str, b: str) -> bool:
    return a == b or (csr.volatile(a) and csr.volatile(b))


def _find(lines: list[str], old: list[str], near: int) -> int | None:
    n = len(old)
    if n == 0:
        return max(0, min(near, len(lines)))
    hits = [i for i in range(len(lines) - n + 1) if all(_same(lines[i + k], old[k]) for k in range(n))]
    return min(hits, key=lambda i: abs(i - near)) if hits else None
