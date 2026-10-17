"""The standard-cell libraries the ASIC flow can use, by technology and corner (`pdks.json` of the flow folder, `<project>/pdks.json` of yours).

A technology (PDK) is a set of Design Compiler `.db` files, one per corner; the dialogs offer technology, then corner, and the choice is the
path of one `.db` (`asic_synth.library`). Only technologies whose files exist are offered. Nothing here runs a tool."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN = Path(__file__).resolve().parents[1] / "pdks.json"


@dataclass(frozen=True)
class Pdk:
    id: str
    title: str
    node_nm: int
    foundry: str = ""
    corners: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (label, path) in name order
    default: str = ""  # the corner label picked with the technology
    probe_ns: float = 0.0  # the period the quick Fmax estimate starts from (0: its default)

    @property
    def label(self) -> str:
        return f"{self.title} — {self.node_nm} nm"

    def default_path(self) -> str:
        by = dict(self.corners)
        return by.get(self.default) or (self.corners[0][1] if self.corners else "")


def _corners(files: list[str]) -> tuple[tuple[str, str], ...]:
    """(label, path): the label is the file name without what the files share in front (gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00 → tt_025C_5v00). The
    shared part is taken from the biggest family of names (those that start alike): a stray file in the folder (a tech.db) keeps its own name."""
    stems = [Path(f).stem for f in files]
    prefix = ""
    if len(stems) > 1:
        fams: dict[str, list[str]] = {}
        for st in stems:
            fams.setdefault(st.split("_")[0], []).append(st)
        fam = max(fams.values(), key=len)
        if len(fam) > 1:
            common = os.path.commonprefix(fam)
            prefix = common[: max(common.rfind("_") + 1, 0)]
    return tuple(sorted(((st[len(prefix):] if prefix and st.startswith(prefix) and st != prefix else st, f) for st, f in zip(stems, files)), key=lambda x: x[0]))


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def files_of(entry: dict) -> list[str]:
    """The `.db` files an entry's globs (and `include`) match on this machine."""
    files = sorted({f for g in entry.get("globs", []) for f in glob.glob(os.path.expandvars(os.path.expanduser(g)))})
    if entry.get("include"):
        rx = re.compile(entry["include"])
        files = [f for f in files if rx.search(Path(f).name)]
    return files


def entries(project=None) -> list[dict]:
    """Every entry of the catalog as written — the flow's `pdks.json`, then `<project>/pdks.json` (same id replaces; `"only": true` hides the
    built-in ones) — with `source` ("built-in" | "project") and the files it matches here, also when there are none (a view shows those)."""
    mine = _read(project.root / "pdks.json") if project is not None else {}
    merged: dict[str, dict] = {}
    if not mine.get("only"):
        merged.update({e["id"]: {**e, "source": "built-in"} for e in _read(BUILTIN).get("pdks", []) if e.get("id")})
    merged.update({e["id"]: {**e, "source": "project"} for e in mine.get("pdks", []) if e.get("id")})
    return [{**e, "files": files_of(e)} for e in merged.values()]


def catalog(project=None) -> list[Pdk]:
    """The technologies with files on this machine (`entries` that match something)."""
    out = []
    for e in entries(project):
        files = e["files"]
        if files:
            out.append(Pdk(e["id"], str(e.get("title") or e["id"]), int(e.get("node_nm") or 0), str(e.get("foundry") or ""), _corners(files), str(e.get("default") or ""), float(e.get("probe_ns") or 0)))
    return sorted(out, key=lambda p: (-p.node_nm, p.id))


def locate(path: str, project=None) -> tuple[Pdk, str] | None:
    """The technology and corner label a library path is in the catalog (None: not a catalogued library)."""
    for p in catalog(project):
        for label, f in p.corners:
            if f == path:
                return p, label
    return None


def short_name(path: str, project=None) -> str:
    """"GF180MCU 7-track 5 V · tt_025C_5v00" for a catalogued library, else the file name's last part."""
    found = locate(path, project)
    return f"{found[0].title} · {found[1]}" if found else Path(path).stem.split("__")[-1]
