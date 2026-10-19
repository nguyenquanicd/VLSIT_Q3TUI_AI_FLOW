"""Block diagrams drawn by Q3TUI itself (termaid's layout routes wires into shared lanes, where two wires look like one).

A Mermaid `flowchart` that draws the design's IO (or a module boundary) is laid out like a schematic:

- columns: input IO (left, rounded boxes) · the blocks in layers, signal flow left to right · output IO (right);
- the module boundary (a non-clock subgraph) is a double line `═ ║` around the blocks, the clock domains are dashed
  horizontal bands inside it, each labelled in its top border;
- every connection has its own port on each box and its own vertical track in the gap between two columns; a
  connection that cannot go straight to the next column (backwards, or across columns) runs in a channel above or
  below the bands; its label is written on the segment that enters the target, the arrow at the target;
- a wire crossing another is drawn with a line jump (`─╴│╶─`); a wire through the boundary is a pin (`╫` / `╪`).

`render(source)` returns the text, or None when the diagram is not a block diagram (the caller draws it with termaid).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from q3tui.steps.spec import diagrams

UP, DOWN, LEFT, RIGHT = 1, 2, 4, 8
H, V = LEFT | RIGHT, UP | DOWN
_BOX = {H: "─", V: "│", RIGHT | DOWN: "┌", LEFT | DOWN: "┐", RIGHT | UP: "└", LEFT | UP: "┘", H | DOWN: "┬", H | UP: "┴",
        V | RIGHT: "├", V | LEFT: "┤", H | V: "┼", LEFT: "─", RIGHT: "─", UP: "│", DOWN: "│"}
_ROUND = {RIGHT | DOWN: "╭", LEFT | DOWN: "╮", RIGHT | UP: "╰", LEFT | UP: "╯"}
LAST: dict = {}  # diagnostics of the last drawing (tests): "overlaps" = cells two wires share without crossing
TRACK = 3  # columns per vertical track (room for a line jump on both sides)


@dataclass
class Node:
    id: str
    lines: list[str]
    kind: str  # in | out | block
    band: str = ""
    col: int = 0
    order: float = 0.0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 3
    ports: dict = field(default_factory=lambda: {"L": [], "R": []})  # side -> [edge index]
    port_y: dict = field(default_factory=dict)  # (edge index, side) -> row


@dataclass
class Wire:
    i: int
    src: str
    dst: str
    label: str
    arrow_start: bool
    arrow_end: bool
    exit_gap: int = 0
    entry_gap: int = 0
    exit_side: str = "R"
    entry_side: str = "L"
    channel: str = ""  # "" (direct) | top | bottom
    lane: int = 0
    tracks: dict = field(default_factory=dict)  # gap -> x


def _graph(source: str):
    return diagrams._graph(source, "block")


def render(source: str) -> str | None:
    """The block diagram as text, or None when the flowchart is not a block diagram (no IO, no module boundary)."""
    try:
        return _render(source)
    except Exception:  # noqa: BLE001 - never fatal: the caller falls back to termaid
        return None


def _render(source: str) -> str | None:
    g = _graph(source)
    if g is None or not g.nodes or not str(source).lstrip().startswith(("flowchart", "graph")):
        return None
    subs = diagrams._all_subgraphs(g.subgraphs)
    domains = [sg for sg in subs if diagrams._DOMAIN.search(f"{sg.id} {sg.label}")]
    io_ids = {nid for nid in g.node_order if diagrams._is_io(g.nodes[nid])}
    boundary = next((sg for sg in subs if sg not in domains and (set(g.node_order) - io_ids) & diagrams._members(sg)), None)
    if boundary is not None:  # a node outside the boundary (and in no domain) is IO too
        inside = diagrams._members(boundary)
        io_ids |= {nid for nid in g.node_order if nid not in inside and not any(nid in diagrams._members(d) for d in domains)}
    if not io_ids and boundary is None:
        return None
    if not set(g.node_order) - io_ids:
        return None

    edges = [e for e in g.edges if e.source != e.target and e.style.name != "INVISIBLE"
             and not (e.source in io_ids and e.target in io_ids)]
    band_of: dict[str, str] = {}
    for d in domains:  # the outermost domain a node is in
        for nid in diagrams._members(d):
            band_of.setdefault(nid, d.id)
    band_label = {d.id: d.label or d.id for d in domains}

    nodes: dict[str, Node] = {}
    for nid in g.node_order:
        label = g.nodes[nid].label.replace("<br/>", "\n").replace("<br>", "\n")
        lines = [ln.strip() for ln in label.split("\n") if ln.strip()] or [nid]
        if nid in io_ids:
            outgoing = any(e.source == nid for e in edges)
            kind = "in" if outgoing or not any(e.target == nid for e in edges) else "out"
        else:
            kind = "block"
        nodes[nid] = Node(nid, lines, kind, band_of.get(nid, "") if kind == "block" else "")

    blocks = [n for n in nodes.values() if n.kind == "block"]
    _layers(blocks, edges, nodes)
    last = max(n.col for n in blocks)
    for n in nodes.values():
        if n.kind == "in":
            n.col = 0
        elif n.kind == "out":
            n.col = last + 1

    wires: list[Wire] = []
    for i, e in enumerate(edges):
        a, b = nodes[e.source], nodes[e.target]
        w = Wire(i, a.id, b.id, " ".join(e.label.split()), e.has_arrow_start, e.has_arrow_end)
        # forward: out of the right side into the left one; backward (feedback, a response to an input): out of the left
        # side into the right one, so it stays in the gap between the two columns
        # side into the right one, so it stays in the gap between the two columns; same column: a loop in the gap on
        # the right
        backward, same = b.col < a.col, b.col == a.col
        w.exit_side = "L" if backward else "R"
        w.entry_side = "R" if backward or same or b.kind == "in" else "L"
        w.exit_gap = a.col if w.exit_side == "R" else a.col - 1
        w.entry_gap = b.col if w.entry_side == "R" else b.col - 1
        a.ports[w.exit_side].append(i)
        b.ports[w.entry_side].append(i)
        wires.append(w)

    _order(nodes, wires, last)
    for n in nodes.values():
        n.w = max(len(ln) for ln in n.lines) + 4
        n.h = max(3, len(n.lines) + 2, len(n.ports["L"]) + 2, len(n.ports["R"]) + 2)

    # -- columns and gaps (x) ---------------------------------------------------------------------------------------
    ncol = last + 2
    colw = [max([n.w for n in nodes.values() if n.col == c], default=0) for c in range(ncol)]
    verticals: dict[int, list[Wire]] = {gp: [] for gp in range(ncol - 1)}
    for w in wires:
        verticals[w.exit_gap].append(w)
        if w.entry_gap != w.exit_gap:
            verticals[w.entry_gap].append(w)
    left_area = {gp: 3 for gp in range(ncol - 1)}
    right_area = {gp: 3 for gp in range(ncol - 1)}
    for w in wires:
        need = len(w.label) + 6 if w.label else 3
        if w.entry_side == "L":
            right_area[w.entry_gap] = max(right_area[w.entry_gap], need)
        else:
            left_area[w.entry_gap] = max(left_area[w.entry_gap], need)
    has_bound, has_bands = boundary is not None, bool(domains)
    xs, gap_x, x = [], {}, 0
    border_x: dict[str, int] = {}
    for c in range(ncol):
        xs.append(x)
        x += colw[c]
        if c == ncol - 1:
            break
        gp = c
        start = x
        x += left_area[gp]
        if gp == last and last >= 1:  # the right edge of the bands / the boundary, then the tracks outside it
            if has_bands:
                border_x["band_r"] = x
                x += 2
            if has_bound:
                border_x["bound_r"] = x
                x += 2
        tracks_at = x + 1
        x += TRACK * max(1, len(verticals[gp])) + 1
        if gp == 0:
            if has_bound:
                border_x["bound_l"] = x
                x += 2
            if has_bands:
                border_x["band_l"] = x
                x += 2
        gap_x[gp] = (start, tracks_at, x, start + left_area[gp])  # (gap start, first track, labels right, labels left end)
        x += right_area[gp]
    width = x + 1
    for n in nodes.values():
        n.x = xs[n.col] + (colw[n.col] - n.w) // 2 if n.kind == "block" else xs[n.col] + (colw[n.col] - n.w if n.kind == "in" else 0)
    for w in wires:
        w.channel = "" if w.exit_gap == w.entry_gap else "?"

    # -- rows (y) -----------------------------------------------------------------------------------------------------
    band_ids = [d.id for d in domains if any(n.band == d.id for n in blocks)]
    if any(n.band == "" for n in blocks) or not band_ids:
        band_ids.append("")
    top_lanes, bottom_lanes = 0, 0
    for _ in range(2):  # once to choose the channels, once with their height
        y = 1 if has_bound else 0
        y += top_lanes + (1 if top_lanes else 0)
        band_box: dict[str, tuple[int, int]] = {}
        for b in band_ids:
            framed = has_bands and b != ""
            y0 = y
            y += 2 if framed else 0
            members = [n for n in blocks if n.band == b]
            stacks = {}
            for c in range(1, last + 1):
                col_nodes = sorted((n for n in members if n.col == c), key=lambda n: n.order)
                stacks[c] = col_nodes
            height = max((sum(n.h for n in s) + max(0, len(s) - 1) for s in stacks.values()), default=0)
            for s in stacks.values():
                yy = y + (height - (sum(n.h for n in s) + max(0, len(s) - 1))) // 2
                for n in s:
                    n.y = yy
                    yy += n.h + 1
            y += height + (2 if framed else 0)
            band_box[b] = (y0, y - 1)
            y += 1
        bottom_start = y
        y += bottom_lanes + (1 if bottom_lanes else 0)
        inner_bottom = y
        mid = (band_box[band_ids[0]][0] + band_box[band_ids[-1]][1]) / 2
        _place_io(nodes, wires, top=0)
        _assign_ports(nodes, wires)
        if _ == 0:
            top, bottom = [], []
            for w in wires:
                if w.channel:
                    ya = nodes[w.src].port_y[(w.i, w.exit_side)]
                    yb = nodes[w.dst].port_y[(w.i, w.entry_side)]
                    w.channel = "top" if (ya + yb) / 2 < mid else "bottom"
                    (top if w.channel == "top" else bottom).append(w)
            top_lanes = _pack_lanes(top, gap_x)
            bottom_lanes = _pack_lanes(bottom, gap_x)
    bound_top = 0
    bound_bottom = inner_bottom
    top_channel = [bound_top + 1 + k for k in range(top_lanes)]
    bottom_channel = [bottom_start + k for k in range(bottom_lanes)]
    height = max([bound_bottom + 1, *(n.y + n.h for n in nodes.values())]) + 1

    # -- tracks --------------------------------------------------------------------------------------------------------
    def channel_y(w: Wire) -> int:
        return top_channel[w.lane] if w.channel == "top" else bottom_channel[w.lane]

    for gp, ws in verticals.items():
        _order_tracks(gp, ws, nodes, channel_y)
        for k, w in enumerate(ws):
            w.tracks[gp] = gap_x[gp][1] + TRACK * k

    # -- draw ------------------------------------------------------------------------------------------------------------
    grid = [[" "] * width for _ in range(height)]
    kind = [[""] * width for _ in range(height)]

    def put(xx, yy, ch, k):
        if 0 <= yy < height and 0 <= xx < width:
            grid[yy][xx], kind[yy][xx] = ch, k

    if has_bound:
        x0, x1 = border_x.get("bound_l", 0), border_x.get("bound_r", width - 1)
        _rect(put, x0, bound_top, x1, bound_bottom, "╔", "╗", "╚", "╝", "═", "║", "bound")
        _title(put, x0, bound_top, x1, boundary.label or boundary.id)
    if has_bands:
        bx0, bx1 = border_x.get("band_l", 0), border_x.get("band_r", width - 1)
        for b in band_ids:
            if b:
                y0, y1 = band_box[b]
                _rect(put, bx0, y0, bx1, y1, "┌", "┐", "└", "┘", "┄", "┆", "band")
                _title(put, bx0, y0, bx1, band_label[b])
    for n in nodes.values():
        corners = ("╭", "╮", "╰", "╯") if n.kind != "block" else ("┌", "┐", "└", "┘")
        _rect(put, n.x, n.y, n.x + n.w - 1, n.y + n.h - 1, *corners, "─", "│", "node")
        top_line = n.y + (n.h - len(n.lines)) // 2
        for k, ln in enumerate(n.lines):
            for j, ch in enumerate(ln.center(n.w - 2)):
                put(n.x + 1 + j, top_line + k, ch, "node")

    cells: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def mark(xx, yy, wid, bits):
        cells.setdefault((xx, yy), []).append((wid, bits))

    arrows: list[tuple[int, int, str]] = []
    labels: list[tuple[int, int, int, str]] = []
    for w in wires:
        a, b = nodes[w.src], nodes[w.dst]
        ya, yb = a.port_y[(w.i, w.exit_side)], b.port_y[(w.i, w.entry_side)]
        xa = a.x + a.w if w.exit_side == "R" else a.x - 1
        xb = b.x - 1 if w.entry_side == "L" else b.x + b.w
        pts = [(xa, ya), (w.tracks[w.exit_gap], ya)]
        if w.channel:
            cy = channel_y(w)
            pts += [(w.tracks[w.exit_gap], cy), (w.tracks[w.entry_gap], cy), (w.tracks[w.entry_gap], yb)]
        else:
            pts += [(w.tracks[w.exit_gap], yb)]
        pts.append((xb, yb))
        _trace(pts, lambda xx, yy, bits, wid=w.i: mark(xx, yy, wid, bits),
               LEFT if w.exit_side == "R" else RIGHT, RIGHT if w.entry_side == "L" else LEFT)
        put(a.x + a.w - 1 if w.exit_side == "R" else a.x, ya, "├" if w.exit_side == "R" else "┤", "node")
        if w.arrow_end:
            arrows.append((xb, yb, "►" if w.entry_side == "L" else "◄"))
        if w.arrow_start:
            arrows.append((xa, ya, "◄" if w.exit_side == "R" else "►"))
        if w.label:
            _, tx0, right_from, left_to = gap_x[w.entry_gap]
            lo, hi = (right_from, xb - 2) if w.entry_side == "L" else (xb + 2, left_to - 1)
            labels.append((lo, hi, yb, w.label))

    overlaps: list = []
    LAST["overlaps"] = overlaps
    for (xx, yy), owners in cells.items():
        bits = 0
        for _, b in owners:
            bits |= b
        wires_here = {wid for wid, _ in owners}
        base = kind[yy][xx] if 0 <= yy < height and 0 <= xx < width else ""
        if base == "node":
            continue
        per = {}
        for wid, b in owners:
            per[wid] = per.get(wid, 0) | b
        if len(wires_here) == 2 and sorted(per.values()) == sorted([H, V]):
            put(xx, yy, "", "jump")  # a crossing: resolved below
            continue
        if len(wires_here) > 1:
            overlaps.append((xx, yy, sorted(wires_here)))  # two wires on one cell, not a crossing (kept for tests)
        ch = _BOX.get(bits, "┼")
        if len(wires_here) == 1 and bits in _ROUND:
            ch = _ROUND[bits]
        if base == "bound":
            ch = "╫" if bits == H else "╪" if bits == V else ch
        put(xx, yy, ch, "wire")
    for yy in range(height):
        for xx in range(width):
            if grid[yy][xx] == "":
                grid[yy][xx] = "│"
                if xx and grid[yy][xx - 1] == "─":
                    grid[yy][xx - 1] = "╴"
                if xx + 1 < width and grid[yy][xx + 1] == "─":
                    grid[yy][xx + 1] = "╶"
    for xx, yy, ch in arrows:
        put(xx, yy, ch, "wire")
    for lo, hi, yy, text in labels:
        room = hi - lo + 1
        if room >= len(text) + 2:
            start = lo + (room - len(text) - 2) // 2
            for j, ch in enumerate(f" {text} "):
                put(start + j, yy, ch, "label")
    return "\n".join("".join(row).rstrip() for row in grid).rstrip("\n").strip("\n")


def _rect(put, x0, y0, x1, y1, tl, tr, bl, br, hz, vt, kind):
    put(x0, y0, tl, kind)
    put(x1, y0, tr, kind)
    put(x0, y1, bl, kind)
    put(x1, y1, br, kind)
    for x in range(x0 + 1, x1):
        put(x, y0, hz, kind)
        put(x, y1, hz, kind)
    for y in range(y0 + 1, y1):
        put(x0, y, vt, kind)
        put(x1, y, vt, kind)


def _title(put, x0, y, x1, text):
    text = f" {text} "[: max(0, x1 - x0 - 3)]
    for j, ch in enumerate(text):
        put(x0 + 2 + j, y, ch, "label")


def _trace(pts, mark, start_dir: int, end_dir: int) -> None:
    """Orthogonal polyline → (x, y, direction bits) per cell; the two ends also point into their boxes."""
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if (x0, y0) == (x1, y1):
            continue
        if y0 == y1:
            lo, hi = sorted((x0, x1))
            for x in range(lo, hi + 1):
                mark(x, y0, (LEFT if x > lo else 0) | (RIGHT if x < hi else 0))
        else:
            lo, hi = sorted((y0, y1))
            for y in range(lo, hi + 1):
                mark(x0, y, (UP if y > lo else 0) | (DOWN if y < hi else 0))
    mark(*pts[0], start_dir)
    mark(*pts[-1], end_dir)


def _layers(blocks: list[Node], edges, nodes: dict[str, Node]) -> None:
    """Column = 1 + the fewest block-to-block hops from an input (signal flow left to right, compact even when the
    blocks form control loops); blocks no input reaches follow the block that feeds them, else column 1."""
    ids = {n.id for n in blocks}
    succ: dict[str, list[str]] = {n.id: [] for n in blocks}
    for e in edges:
        if e.source in ids and e.target in ids:
            succ[e.source].append(e.target)
    layer: dict[str, int] = {}
    frontier = [e.target for e in edges if nodes[e.source].kind == "in" and e.target in ids]
    for nid in frontier:
        layer[nid] = 1
    while frontier:
        nxt = []
        for u in frontier:
            for v in succ[u]:
                if v not in layer:
                    layer[v] = layer[u] + 1
                    nxt.append(v)
        frontier = nxt
    for n in blocks:  # not reachable from an input
        if n.id not in layer:
            feeders = [layer[e.source] for e in edges if e.target == n.id and e.source in layer and e.source in ids]
            layer[n.id] = (max(feeders) + 1) if feeders else 1
    for n in blocks:
        n.col = layer[n.id]


def _order(nodes: dict[str, Node], wires: list[Wire], last: int) -> None:
    """Order inside each column (barycenter of the neighbours, a few sweeps)."""
    for k, n in enumerate(nodes.values()):
        n.order = k
    nbrs: dict[str, list[str]] = {n: [] for n in nodes}
    for w in wires:
        nbrs[w.src].append(w.dst)
        nbrs[w.dst].append(w.src)
    for _ in range(4):
        for c in [*range(1, last + 1), 0, last + 1]:
            col = [n for n in nodes.values() if n.col == c]
            for n in col:
                around = [nodes[m].order for m in nbrs[n.id] if nodes[m].col != c]
                n.order = sum(around) / len(around) if around else n.order
            for k, n in enumerate(sorted(col, key=lambda n: (n.band, n.order))):
                n.order = k


def _place_io(nodes: dict[str, Node], wires: list[Wire], top: int) -> None:
    """IO next to the blocks they connect to, without overlapping."""
    for side in ("in", "out"):
        io = [n for n in nodes.values() if n.kind == side]
        want = {}
        for n in io:
            mates = [nodes[w.dst if w.src == n.id else w.src] for w in wires if n.id in (w.src, w.dst)]
            want[n.id] = (sum(m.y + m.h / 2 for m in mates) / len(mates) - n.h / 2) if mates else 0
        y = top
        for n in sorted(io, key=lambda n: want[n.id]):
            n.y = max(y, round(want[n.id]))
            y = n.y + n.h + 1


def _assign_ports(nodes: dict[str, Node], wires: list[Wire]) -> None:
    """Ports along each side, ordered by where the other end is (fewer crossings), spread over the inner rows."""
    by_i = {w.i: w for w in wires}
    for n in nodes.values():
        for side, ids in n.ports.items():
            if not ids:
                continue

            def other_y(i, me=n.id):
                w = by_i[i]
                m = nodes[w.dst if w.src == me else w.src]
                return m.y + m.h / 2

            ids.sort(key=other_y)
            inner = n.h - 2
            k = len(ids)
            rows = [n.y + 1 + (inner // 2 if k == 1 else round(j * (inner - 1) / (k - 1))) for j in range(k)]
            for i, r in zip(ids, rows):
                n.port_y[(i, side)] = r


def _pack_lanes(ws: list[Wire], gap_x: dict) -> int:
    """Channel rows for the wires that go around: a wire spans the gaps from its exit gap to its entry gap; wires whose
    spans do not overlap share a row."""
    lanes: list[list[tuple[int, int]]] = []
    for w in sorted(ws, key=lambda w: (min(w.exit_gap, w.entry_gap), w.i)):
        lo, hi = min(w.exit_gap, w.entry_gap), max(w.exit_gap, w.entry_gap)
        for k, lane in enumerate(lanes):
            if all(hi < a or lo > b for a, b in lane):
                lane.append((lo, hi))
                w.lane = k
                break
        else:
            lanes.append([(lo, hi)])
            w.lane = len(lanes) - 1
    return len(lanes)


def _order_tracks(gp: int, ws: list[Wire], nodes: dict[str, Node], channel_y) -> None:
    """Tracks of a gap, left to right. On one row a wire leaving the left column and one entering the right column must
    not overlap: the leaving one's track comes first. Otherwise by where the vertical runs (less crossing)."""
    def span(w):
        a, b = nodes[w.src], nodes[w.dst]
        ys = []
        if w.exit_gap == gp:
            ys.append(a.port_y[(w.i, w.exit_side)])
        if w.entry_gap == gp:
            ys.append(b.port_y[(w.i, w.entry_side)])
        if w.channel:
            ys.append(channel_y(w))
        return ys

    def rows_on(w, column_side: str) -> set[int]:
        """Rows where this wire meets the gap's left column (its boxes' right sides) or right column (left sides)."""
        side = "R" if column_side == "left" else "L"
        out = set()
        if w.exit_gap == gp and w.exit_side == side:
            out.add(nodes[w.src].port_y[(w.i, side)])
        if w.entry_gap == gp and w.entry_side == side:
            out.add(nodes[w.dst].port_y[(w.i, side)])
        return out

    base = sorted(ws, key=lambda w: (sum(span(w)) / len(span(w)), w.i))
    before: dict[int, set[int]] = {w.i: set() for w in ws}  # i must come before these
    left = {w.i: rows_on(w, "left") for w in ws}
    right = {w.i: rows_on(w, "right") for w in ws}
    for u in ws:
        for v in ws:
            if u is not v and left[u.i] & right[v.i]:
                before[u.i].add(v.i)
    placed, out = set(), []
    pending = list(base)
    while pending:
        for w in pending:
            if not any(w.i in before[o.i] for o in pending if o is not w):
                break
        else:
            w = pending[0]  # a cycle: keep the order (a rare overlap)
        out.append(w)
        placed.add(w.i)
        pending.remove(w)
    ws[:] = out
