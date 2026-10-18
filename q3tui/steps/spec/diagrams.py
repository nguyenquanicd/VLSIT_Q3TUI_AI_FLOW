"""Spec figures: find, check and draw the diagrams of a spec (code, no LLM).

The figures stay in `spec.md` as fenced blocks, so the document renders anywhere (GitHub, VS Code, the TUI) and a hand edit
is the source of truth. Three kinds follow a convention that code can read and check:

- timing: a ```wavedrom block (WaveJSON; JSON5 as models usually write it is accepted) — drawn as terminal art for the
  TUI (`wave_text`) and as SVG for the document (`wave_svg`);
- block diagram: a Mermaid `flowchart` with one `subgraph` per clock domain; an edge between two domains carries a
  `CDC` / `sync` label or goes through a synchronizer node;
- state machine: a Mermaid `stateDiagram-v2` directly below the table of its states, with an initial `[*] --> STATE`.

`check_spec` returns the problems per section; the spec step hands them to its reviewer and reports what is left as
warnings (never a failed step). Mermaid is parsed with `termaid` (imported lazily).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

LANGS_MERMAID = ("mermaid", "mmd")
LANGS_WAVE = ("wavedrom", "wavejson")
LANGS = LANGS_MERMAID + LANGS_WAVE

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")
_CAPTION = re.compile(r"^\*?\s*(Table|Figure)\b", re.I)


@dataclass
class Figure:
    lang: str  # wavedrom | mermaid
    source: str
    caption: str = ""  # the caption line below it, without the emphasis markers
    table_above: list[str] = field(default_factory=list)  # first-column cells of the table directly above (FSM pairing)

    @property
    def kind(self) -> str:
        if self.lang in LANGS_WAVE:
            return "timing"
        head = next((ln.strip() for ln in self.source.splitlines() if ln.strip() and not ln.strip().startswith("%%")), "")
        if head.startswith(("flowchart", "graph")):
            return "block"
        if head.startswith("stateDiagram"):
            return "fsm"
        return "other"


def figures(markdown: str) -> list[Figure]:
    """The diagrams of a Markdown text (a section body or a whole spec), in order, with their captions."""
    lines = markdown.splitlines()
    out: list[Figure] = []
    last_table: list[str] = []  # the most recent table, until prose comes between it and a diagram
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2).lower()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(fence[:3]):
                j += 1
            if lang in LANGS:
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                cap = lines[k].strip() if k < len(lines) and _CAPTION.match(lines[k].strip()) else ""
                out.append(Figure(lang="wavedrom" if lang in LANGS_WAVE else "mermaid", source="\n".join(lines[i + 1:j]),
                                  caption=cap.strip("*_ ").strip(), table_above=last_table))
            last_table = []
            i = j + 1
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            last_table = _first_column(rows)
            continue
        if line.lstrip().startswith("#"):  # a new subsection: its diagram belongs to its own table (prose in between is fine)
            last_table = []
        i += 1
    return out


def _first_column(rows: list[str]) -> list[str]:
    """Body cells of a Markdown table's first column, only when its header names states or modes."""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if len(cells) < 3 or not re.search(r"state|mode", cells[0][0], re.I):
        return []
    return [_plain(r[0]) for r in cells[2:] if r and _plain(r[0])]


def _plain(cell: str) -> str:
    """`**IDLE**` / `` `S_IDLE` `` / `IDLE (reset)` → the first identifier."""
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]*", re.sub(r"[`*]", "", cell))
    return m.group(0) if m else ""


# -- WaveDrom ------------------------------------------------------------------------------------------------------------


def loads_json5(text: str):
    """WaveJSON as models and people write it (JSON5: unquoted keys, single quotes, comments, trailing commas) → data."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append('\\"' if text[j] == '"' else text[j])
                j += 1
            out.append('"' + "".join(buf).replace("\\'", "'") + '"')
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            out.append(word if word in ("true", "false", "null") else json.dumps(word))
            i = j
        elif c == ",":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j >= n or text[j] not in "]}":  # a trailing comma is dropped
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


_WAVE_CHARS = set("pPnNlLhH01=23456789xXzud.|")
_DATA = set("=23456789")


@dataclass
class Lane:
    name: str
    cells: list[tuple[str, str]]  # (state, label) per cycle; state: 0 1 x z p n data gap; label only on a data cell's first cycle


def wave_lanes(source: str) -> list[Lane]:
    """The lanes of a WaveJSON block (groups flattened, spacers dropped). Raises ValueError when it cannot be read."""
    try:
        data = loads_json5(source)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"not valid WaveJSON ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("signal"), list):
        raise ValueError("WaveJSON needs a top-level object with a `signal` list")
    lanes: list[Lane] = []

    def walk(items: list) -> None:
        for it in items:
            if isinstance(it, list):  # a group: ["name", lane, lane ...]
                walk([x for x in it[1:]])
            elif isinstance(it, dict) and "wave" in it:
                lanes.append(_lane(str(it.get("name", "")), str(it["wave"]), it.get("data")))

    walk(data["signal"])
    return lanes


def _lane(name: str, wave: str, data) -> Lane:
    labels = data.split() if isinstance(data, str) else [str(x) for x in data] if isinstance(data, list) else []
    cells: list[tuple[str, str]] = []
    prev = "x"
    k = 0
    for ch in wave:
        if ch in ".|":
            st = prev if prev not in ("data",) else "data+"
            cells.append(("gap" if ch == "|" else st, ""))
            continue
        if ch in _DATA:
            st, label = "data", (labels[k] if k < len(labels) else "")
            k += 1
        else:
            st, label = {"p": "p", "P": "p", "n": "n", "N": "n", "h": "1", "H": "1", "l": "0", "L": "0", "1": "1", "0": "0",
                         "x": "x", "X": "x", "z": "z", "u": "1", "d": "0"}.get(ch, "x"), ""
        cells.append((st, label))
        prev = st
    return Lane(name, cells)


def check_wave(source: str, known_names: set[str] | None = None) -> list[str]:
    """Problems of one WaveJSON block: unreadable, unknown wave characters, data labels that do not match the data
    cycles, and (with `known_names`) signals the rest of the spec never names."""
    try:
        data = loads_json5(source)
    except (ValueError, json.JSONDecodeError) as exc:
        return [f"timing diagram is not valid WaveJSON ({exc})"]
    if not isinstance(data, dict) or not isinstance(data.get("signal"), list):
        return ["timing diagram needs a top-level object with a `signal` list"]
    problems: list[str] = []
    raw: list[dict] = []

    def walk(items: list) -> None:
        for it in items:
            if isinstance(it, list):
                walk(it[1:])
            elif isinstance(it, dict) and "wave" in it:
                raw.append(it)

    walk(data["signal"])
    if not raw:
        problems.append("timing diagram has no signal lanes")
    for it in raw:
        name, wave = str(it.get("name", "")), str(it["wave"])
        bad = sorted({c for c in wave if c not in _WAVE_CHARS})
        if bad:
            problems.append(f"timing diagram: lane `{name}` has unknown wave characters {' '.join(bad)}")
        need = sum(c in _DATA for c in wave)
        d = it.get("data")
        have = len(d.split()) if isinstance(d, str) else len(d) if isinstance(d, list) else 0
        if need and have != need:
            problems.append(f"timing diagram: lane `{name}` has {need} data cycle(s) but {have} data label(s)")
        if known_names is not None and name and _ident(name) and _ident(name) not in known_names:
            problems.append(f"timing diagram: signal `{name}` is not named anywhere else in the spec (use the port / signal names)")
    return problems


def _ident(name: str) -> str:
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", name)
    return m.group(1) if m else ""


_GLYPH = {"1": "▔", "0": "▁", "z": "─", "x": "╳", "gap": "≈"}


@lru_cache(maxsize=64)
def wave_text(source: str) -> str | None:
    """A WaveJSON block as terminal art (one row per lane, a cycle ruler on top), or None when it cannot be read."""
    try:
        lanes = wave_lanes(source)
    except ValueError:
        return None
    if not lanes:
        return None
    labels = [lab for ln in lanes for _, lab in ln.cells if lab]
    w = max(4, *(len(lab) + 2 for lab in labels)) if labels else 4
    namew = max(len(ln.name) for ln in lanes)
    cycles = max(len(ln.cells) for ln in lanes)
    rows = [" " * (namew + 1) + "".join(str(c).ljust(w) for c in range(cycles)).rstrip()]
    for ln in lanes:
        out, level = [], None
        for st, lab in ln.cells:
            if st == "p":
                cell, level = "╱" + "▔" * (w // 2 - 1) + "╲" + "▁" * (w - w // 2 - 1), "0"
            elif st == "n":
                cell, level = "╲" + "▁" * (w // 2 - 1) + "╱" + "▔" * (w - w // 2 - 1), "1"
            elif st == "data":
                cell, level = "╳" + lab[: w - 1].center(w - 1, " "), "data"
            elif st == "data+":
                cell = " " * w
            elif st == "gap":
                cell = "≈" + (out[-1][-1] if out else " ") * (w - 1)
            else:
                glyph = _GLYPH[st]
                edge = {("0", "1"): "╱", ("1", "0"): "╲"}.get((level, st), glyph if st in ("0", "1") else "")
                if st in ("0", "1") and level not in ("0", "1", "z", None):
                    edge = "╳"
                cell, level = (edge + glyph * (w - len(edge))) if edge else glyph * w, st
            out.append(cell)
        rows.append(ln.name.rjust(namew) + " " + "".join(out))
    return "\n".join(r.rstrip() for r in rows)


_FILL = {"=": "#ffffff", "2": "#ffffb4", "3": "#ffe0b9", "4": "#b9e0ff", "5": "#ccfdfe", "6": "#cdfdc5", "7": "#f0c1fb",
         "8": "#f5c2c0", "9": "#e8e8e8"}


def wave_svg(source: str, title: str = "") -> str | None:
    """A WaveJSON block as a standalone SVG (for the document / PDF), or None when it cannot be read."""
    try:
        lanes = wave_lanes(source)
    except ValueError:
        return None
    if not lanes:
        return None
    try:
        raw_waves = [str(it.get("wave", "")) for it in _raw_lanes(loads_json5(source))]
    except Exception:  # noqa: BLE001 - colours are cosmetic
        raw_waves = []
    cw, lh, amp = 40, 32, 9  # cycle width, lane height, half the swing
    namew = 10 + 7 * max(len(ln.name) for ln in lanes)
    cycles = max(len(ln.cells) for ln in lanes)
    width, height = namew + cw * cycles + 10, 24 + lh * len(lanes) + 6
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'font-family="DejaVu Sans Mono, monospace" font-size="12">',
           f"<title>{esc(title)}</title>" if title else "", f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    for c in range(cycles + 1):
        x = namew + c * cw
        out.append(f'<line x1="{x}" y1="14" x2="{x}" y2="{height - 4}" stroke="#d0d0d0" stroke-dasharray="2,3"/>')
        if c < cycles:
            out.append(f'<text x="{x + cw / 2}" y="11" text-anchor="middle" fill="#888">{c}</text>')
    for li, ln in enumerate(lanes):
        mid = 24 + li * lh + lh / 2
        hi, lo = mid - amp, mid + amp
        out.append(f'<text x="{namew - 8}" y="{mid + 4}" text-anchor="end" fill="#111">{esc(ln.name)}</text>')
        chars = [ch for ch in (raw_waves[li] if li < len(raw_waves) else "")]
        pts: list[tuple[float, float]] = []
        c = 0
        while c < len(ln.cells):
            st, lab = ln.cells[c]
            x0 = namew + c * cw
            if st in ("data", "data+"):
                end = c + 1
                while end < len(ln.cells) and ln.cells[end][0] in ("data+", "gap"):
                    end += 1
                x1 = namew + end * cw
                fill = _FILL.get(chars[c] if c < len(chars) and chars[c] in _FILL else "=", "#ffffff")
                if pts:
                    out.append(_poly(pts))
                    pts = []
                out.append(f'<polygon points="{x0},{mid} {x0 + 4},{hi} {x1 - 4},{hi} {x1},{mid} {x1 - 4},{lo} {x0 + 4},{lo}" '
                           f'fill="{fill}" stroke="#000" stroke-width="1"/>')
                if lab:
                    out.append(f'<text x="{(x0 + x1) / 2}" y="{mid + 4}" text-anchor="middle">{esc(lab)}</text>')
                c = end
                continue
            if st in ("p", "n"):
                a, b = (lo, hi) if st == "p" else (hi, lo)
                if pts and pts[-1][1] != a:
                    pts.append((x0, a))
                pts += [(x0, a), (x0 + 2, b), (x0 + cw / 2, b), (x0 + cw / 2 + 2, a), (x0 + cw, a)]
            elif st == "x":
                if pts:
                    out.append(_poly(pts))
                    pts = []
                end = c + 1
                while end < len(ln.cells) and ln.cells[end][0] == "x":
                    end += 1
                x1 = namew + end * cw
                out.append(f'<rect x="{x0}" y="{hi}" width="{x1 - x0}" height="{2 * amp}" fill="#e0e0e0" stroke="#000" stroke-width="1"/>')
                out += [f'<path d="M{x},{lo} L{x + 2 * amp},{hi}" stroke="#999" stroke-width="0.6"/>' for x in range(x0, x1 - 2 * amp + 1, 8)]
                c = end
                continue
            elif st == "gap":
                out.append(f'<path d="M{x0 + 2},{lo + 4} l6,-{2 * amp + 8}" stroke="#000"/>')
                y = pts[-1][1] if pts else mid
                pts.append((x0 + cw, y))
            else:
                y = {"1": hi, "0": lo, "z": mid}.get(st, mid)
                if pts and pts[-1][1] != y:
                    pts.append((x0 + 2, y))
                elif not pts:
                    pts.append((x0, y))
                pts.append((x0 + cw, y))
            c += 1
        if pts:
            out.append(_poly(pts))
    out.append("</svg>")
    return "\n".join(s for s in out if s) + "\n"


def _poly(pts: list[tuple[float, float]]) -> str:
    return '<polyline points="' + " ".join(f"{x:g},{y:g}" for x, y in pts) + '" fill="none" stroke="#000" stroke-width="1.2"/>'


def _raw_lanes(data) -> list[dict]:
    out: list[dict] = []

    def walk(items: list) -> None:
        for it in items:
            if isinstance(it, list):
                walk(it[1:])
            elif isinstance(it, dict) and "wave" in it:
                out.append(it)

    walk(data.get("signal", []) if isinstance(data, dict) else [])
    return out


# -- Mermaid: block diagram and state machine ----------------------------------------------------------------------------


_SUBGRAPH = re.compile(r"^(\s*subgraph\s+[\w-]+)(?=\[)", re.M)


def normalize_mermaid(source: str) -> str:
    """Mermaid as models write it, in the form termaid reads: `subgraph id["label"]` → `subgraph id ["label"]` (else the
    id is read as `id["label` and the label drawn as `id["label"]`)."""
    return _SUBGRAPH.sub(r"\1 ", source)


_STADIUM = re.compile(r"(?<![\w-])([A-Za-z_]\w*)\(\[(.*?)\]\)")
_FLAG = re.compile(r"(?<![\w-])([A-Za-z_]\w*)>([^\]\n]*)\]")
_START = re.compile(r"^\s*\[\*\]\s*-->\s*([A-Za-z_]\w*)\s*(?::\s*(.*?))?\s*$")


def display_mermaid(source: str) -> tuple[str, list[str]]:
    """A diagram as it is drawn (the spec keeps the standard form) and the notes to print under it. A state machine's
    initial `[*] --> S` is not drawn as a start node with an arrow: S itself says it is where reset goes (`S (reset)`,
    or the transition's label). A transition three or more states take the same way (abort …) is not drawn from each:
    it becomes a note "from any state: abort → IDLE"."""
    source = normalize_mermaid(source)
    lines = source.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.strip() and not ln.strip().startswith("%%")), None)
    if head is not None and lines[head].strip().startswith(("flowchart", "graph")):
        # IO drawn as rounded boxes: termaid draws a stadium `([…])` with a column of brackets and a flag `>…]` lopsided
        source = _STADIUM.sub(r"\1(\2)", source)
        return _FLAG.sub(r"\1(\2)", source), []
    if head is None or not lines[head].strip().startswith("stateDiagram"):
        return source, []
    notes: list[str] = []
    g = _graph(source, "fsm")
    if g is not None:
        common: dict[tuple[str, str], set[str]] = {}
        for e in g.edges:
            if e.label.strip() and not e.source.startswith("[*]"):
                common.setdefault((e.target, " ".join(e.label.split())), set()).add(e.source)
        for (target, label), sources in common.items():
            if len(sources) < 3:
                continue
            alternatives = "|".join(map(re.escape, sources))
            label_re = r"\s+".join(map(re.escape, label.split()))
            edge = re.compile(rf"^\s*({alternatives})\s*-->\s*{re.escape(target)}\s*:\s*{label_re}\s*$")
            lines = [ln for ln in lines if not edge.match(ln)]
            notes.append(f"from any state: {label} → {target}")
    starts, depth = [], 0
    for i, ln in enumerate(lines):
        if depth == 0 and (m := _START.match(ln)):
            starts.append((i, m))
        depth += ln.count("{") - ln.count("}")
    if len(starts) != 1:
        return "\n".join(lines), notes
    at, m = starts[0]
    state, why = m.group(1), (m.group(2) or "reset").strip()
    if re.search(rf"^\s*state\s+{state}\s*\{{", source, re.M):  # a composite state keeps its start node
        return "\n".join(lines), notes
    rest = [ln for i, ln in enumerate(lines) if i != at]
    decl = re.compile(rf'^\s*state\s+"([^"]*)"\s+as\s+{state}\s*$')
    label = next((d.group(1) for ln in rest if (d := decl.match(ln))), state)
    rest = [ln for ln in rest if not decl.match(ln)]
    rest.insert(head + 1, f'  state "{label} ({why})" as {state}')  # declared first: drawn on top
    return "\n".join(rest), notes


def _graph(source: str, kind: str):
    """termaid's graph of a Mermaid diagram, or None when termaid is missing / cannot read it."""
    source = normalize_mermaid(source)
    try:
        if kind == "fsm":
            from termaid.parser.statediagram import parse_state_diagram

            return parse_state_diagram(source)
        from termaid.parser.flowchart import parse_flowchart

        return parse_flowchart(source)
    except Exception:  # noqa: BLE001 - an unreadable diagram is reported by the caller
        return None


_DOMAIN = re.compile(r"clk|clock|domain", re.I)
_SYNC = re.compile(r"cdc|sync|async|gray|handshake", re.I)
_PORT = re.compile(r"\b(?:i|o|io)_[a-z0-9_]+\b")


def check_block(source: str, spec_text: str = "") -> list[str]:
    """Problems of a block diagram: unreadable / empty; a node or a connection that lists the signals of an interface
    instead of naming it (`APB interface`); a connection without a label; an edge between two clock-domain subgraphs
    with no synchronizer (a `CDC` / `sync` label, or a node named like one at either end); a port-like name (`i_…`,
    `o_…`) the rest of the spec never names."""
    g = _graph(source, "block")
    if g is None or not g.nodes:
        return ["block diagram could not be read as a Mermaid flowchart"]
    from termaid.graph.model import EdgeStyle

    problems: list[str] = []
    for nid in g.node_order:
        ports = list(dict.fromkeys(_PORT.findall(g.nodes[nid].label)))
        if len(ports) > 1:
            problems.append(f"block diagram: node {nid} lists the signals {', '.join(ports[:3])}{'…' if len(ports) > 3 else ''} — "
                            f"draw the interface as one node named for it (e.g. `APB interface`); its signals are in the port table")
    unlabelled, listing = [], []
    for e in g.edges:
        if e.style == EdgeStyle.INVISIBLE:
            continue
        if not e.label.strip():
            unlabelled.append(f"{e.source} → {e.target}")
        elif len(set(_PORT.findall(e.label))) > 1:
            listing.append(f"{e.source} → {e.target}")
    problems += _boundary_problems(g, spec_text)
    if unlabelled:
        more = f" (+{len(unlabelled) - 6} more)" if len(unlabelled) > 6 else ""
        problems.append(f"block diagram: connection(s) without a label: {', '.join(unlabelled[:6])}{more} — label every "
                        f"connection with what it carries (`-->|TX data| `, `-->|APB interface| `, `-->|irq| `)")
    if listing:
        problems.append(f"block diagram: connection label(s) list signals instead of naming the interface: {', '.join(listing[:6])}")
    domains = {}

    def walk(subs, dom):
        for sg in subs:
            d = sg.id if _DOMAIN.search(f"{sg.id} {sg.label}") else dom
            for nid in sg.node_ids:
                domains[nid] = d
            walk(sg.children, d)

    walk(g.subgraphs, None)
    crossings: dict[tuple[str, str], bool] = {}  # (source, target) → synchronized (any edge between them says how)
    for e in g.edges:
        a, b = domains.get(e.source), domains.get(e.target)
        if a and b and a != b:
            names = f"{e.label} {e.source} {e.target} {g.nodes[e.source].label if e.source in g.nodes else ''} " \
                    f"{g.nodes[e.target].label if e.target in g.nodes else ''}"
            key = (e.source, e.target)
            crossings[key] = crossings.get(key, False) or bool(_SYNC.search(names))
    for (src, dst), synced in crossings.items():
        if not synced:
            a, b = _domain_name(domains[src]), _domain_name(domains[dst])
            problems.append(f"block diagram: edge {src} → {dst} crosses clock domains ({a} → {b}) with no "
                            f"synchronizer — label it `CDC: <how>` or route it through the synchronizer block")
    if spec_text:
        known = set(_PORT.findall(_without_figures(spec_text)))
        names = set(_PORT.findall(" ".join([*(n.label for n in g.nodes.values()), *g.nodes, *(e.label for e in g.edges)])))
        for p in sorted(names - known):
            problems.append(f"block diagram: `{p}` is not a port or signal named anywhere else in the spec")
    return problems


_IO_WORDS = re.compile(r"\b(interface|ports?|pins?|interrupts?)\b", re.I)


def _is_io(node) -> bool:
    """An IO of the design on a block diagram: a port name or an interface / pins / interrupts node."""
    return bool(_PORT.search(node.label) or _PORT.fullmatch(node.id) or _IO_WORDS.search(node.label))


def _members(sg) -> set[str]:
    return set(sg.node_ids) | {n for c in sg.children for n in _members(c)}


def _boundary_problems(g, spec_text: str) -> list[str]:
    """A block diagram that draws the design's IO shows the module boundary: a subgraph for the top module holding the
    blocks (and the clock domains), with every IO outside it, connected across the boundary."""
    io = [nid for nid in g.node_order if _is_io(g.nodes[nid])]
    if not io:
        return []  # a dataflow sketch without IO: no boundary needed
    blocks = {nid for nid in g.node_order if nid not in io}
    boundary = next((sg for sg in _all_subgraphs(g.subgraphs)
                     if not _DOMAIN.search(f"{sg.id} {sg.label}") and blocks & _members(sg)), None)
    if boundary is None:
        from q3tui.steps.spec.document import spec_top_module

        top = spec_top_module(spec_text) if spec_text else ""
        return [f"block diagram: no module boundary — put the blocks (and their clock-domain subgraphs) in "
                f"`subgraph top[\"{top or '<top module>'}\"]` and draw each IO outside it as a rounded node "
                f"(`apb_if(APB interface)`): inputs on the left, outputs on the right"]
    inside = sorted(set(io) & _members(boundary))
    if inside:
        return [f"block diagram: IO drawn inside the module boundary: {', '.join(inside)} — draw IO outside "
                f"`{_domain_name(boundary.id)}`, connected across it"]
    return []


def _domain_name(sg_id: str) -> str:
    return re.split(r"[\[(\"]", sg_id, maxsplit=1)[0].strip() or sg_id  # termaid keeps `core["…` of `subgraph core["…"]`


def check_fsm(source: str, states_in_table: list[str] | None = None) -> list[str]:
    """Problems of a state machine: unreadable / empty; no initial `[*] --> S`; states not reachable from it; states
    with no way out (other than a final `--> [*]`); state names that differ from the state table directly above."""
    g = _graph(source, "fsm")
    if g is None or not g.nodes:
        return ["state diagram could not be read as a Mermaid stateDiagram"]
    # only the top level is checked: a composite state (`state BUSY { … }`, a subgraph) counts as one state, its inner
    # states are its own business; pseudo-states (`<<choice>>`, `<<fork>>`, `<<join>>`) route, they are not states
    inner = {n for sg in _all_subgraphs(g.subgraphs) for n in sg.node_ids}
    pseudo = set(re.findall(r"^\s*state\s+([A-Za-z_]\w*)\s*<<\w+>>", source, re.M))
    states = {n for n in g.nodes if not n.startswith("[*]") and n not in inner} | {sg.id for sg in g.subgraphs}
    for ln in source.splitlines():  # `state X` alone declares a state termaid leaves out
        m = re.match(r"\s*state\s+(?:\"[^\"]*\"\s+as\s+)?([A-Za-z_]\w*)\s*$", ln)
        if m and m.group(1) not in inner:
            states.add(m.group(1))
    starts = [n for n in g.nodes if n.startswith("[*]_start") and n not in inner]
    problems: list[str] = []
    if not starts:
        problems.append("state diagram has no initial state (`[*] --> <reset state>`)")
    succ: dict[str, set[str]] = {}
    for e in g.edges:
        succ.setdefault(e.source, set()).add(e.target)
    seen, todo = set(starts), list(starts)
    while todo:
        for t in succ.get(todo.pop(), ()):
            if t not in seen:
                seen.add(t)
                todo.append(t)
    states -= pseudo
    if starts:
        lost = sorted(states - seen)
        if lost:
            problems.append(f"state diagram: state(s) not reachable from the initial state: {', '.join(lost)}")
    dead = sorted(s for s in states if not (succ.get(s, set()) - {s}))
    if dead:
        problems.append(f"state diagram: state(s) with no exit (add the transition out, or `--> [*]` if final): {', '.join(dead)}")
    common: dict[tuple[str, str], set[str]] = {}
    for e in g.edges:
        if e.label.strip() and e.source in states:
            common.setdefault((e.target, " ".join(e.label.split()).lower()), set()).add(e.source)
    for (target, label), sources in sorted(common.items()):
        if len(sources) >= 3:
            problems.append(f"state diagram: `{label}` → {target} is drawn from {len(sources)} states ({', '.join(sorted(sources))}) — "
                            f"draw a transition every state takes once: say it in the caption / table (\"from any state: {label} → {target}\")")
    if states_in_table:
        norm = lambda s: s.lower().removeprefix("s_").removeprefix("st_")  # noqa: E731
        tab, dia = {norm(s): s for s in states_in_table}, {norm(s): s for s in states}
        only_t, only_d = sorted(tab[k] for k in tab.keys() - dia.keys()), sorted(dia[k] for k in dia.keys() - tab.keys())
        if only_t:
            problems.append(f"state diagram: state(s) in the table above but not in the diagram: {', '.join(only_t)}")
        if only_d:
            problems.append(f"state diagram: state(s) in the diagram but not in the table above: {', '.join(only_d)}")
    return problems


def _all_subgraphs(subs) -> list:
    out = []
    for sg in subs:
        out += [sg, *_all_subgraphs(sg.children)]
    return out


# -- the whole spec -------------------------------------------------------------------------------------------------------


def _without_figures(markdown: str) -> str:
    """The spec's text outside its diagrams (what "named elsewhere in the spec" means)."""
    out, skip, fence = [], False, ""
    for ln in markdown.splitlines():
        m = _FENCE.match(ln)
        if not skip and m and m.group(2).lower() in LANGS:
            skip, fence = True, m.group(1)[:3]
            continue
        if skip:
            if ln.strip().startswith(fence):
                skip = False
            continue
        out.append(ln)
    return "\n".join(out)


def check_figure(fig: Figure, spec_text: str = "") -> list[str]:
    if fig.kind == "timing":
        known = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _without_figures(spec_text))) if spec_text else None
        return check_wave(fig.source, known)
    if fig.kind == "block":
        return check_block(fig.source, spec_text)
    if fig.kind == "fsm":
        return check_fsm(fig.source, fig.table_above)
    return []


def check_sections(sections: list[tuple[str, str]], spec_text: str = "") -> list[tuple[str, str]]:
    """(section title, problem) for every diagram problem in these (title, body) sections; `spec_text` (the whole spec)
    is what names are looked up in."""
    out: list[tuple[str, str]] = []
    for title, body in sections:
        for fig in figures(body):
            out += [(title, p) for p in check_figure(fig, spec_text or body)]
    return out
