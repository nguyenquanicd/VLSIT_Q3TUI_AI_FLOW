"""Combinational loops, found statically on the elaborated design (pyslang).

A zero-delay loop (a → b → … → a through continuous assigns, always_comb and port connections)
makes a simulation spin forever at one time step, so the RTL step must never hand one downstream.
The graph is per signal (not per bit), over the flattened hierarchy: an edge `x → y` means y is
computed combinationally from x (a read in the right-hand side, or in an enclosing if / case
condition). Registers (always_ff, edge-triggered always) break the loop. A signal feeding itself
(`v[1] = v[0]`) is ignored: that is almost always different bits of one vector.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyslang import ast


@dataclass
class Loop:
    signals: list[str]  # hierarchical paths, in loop order
    where: list[tuple[str, int]]  # (file, line) of the logic closing each edge

    def describe(self, top: str) -> str:
        names = [s.removeprefix(top + ".") for s in self.signals]
        return " → ".join([*names, names[0]])


def _reads(expr) -> set:
    out: set = set()
    if expr is None:
        return out

    def visit(node):
        if getattr(node, "kind", None) in (ast.ExpressionKind.NamedValue, ast.ExpressionKind.HierarchicalValue):
            sym = node.symbol
            if sym is not None and sym.kind in (ast.SymbolKind.Variable, ast.SymbolKind.Net):
                out.add(sym)
        return True

    expr.visit(visit)
    return out


def _lhs(expr) -> set:
    """Signals written by an assignment's left side (selects and concatenations included)."""
    return _reads(expr)


def _lhs_index_reads(expr) -> set:
    """Signals read to compute where the left side writes (v[idx] = …: idx)."""
    out: set = set()

    def walk(e):
        k = e.kind
        if k in (ast.ExpressionKind.ElementSelect, ast.ExpressionKind.RangeSelect):
            walk(e.value)
            for sel in ((e.selector,) if k == ast.ExpressionKind.ElementSelect else (e.left, e.right)):
                out.update(_reads(sel))
        elif k == ast.ExpressionKind.Concatenation:
            for op in e.operands:
                walk(op)
        elif k == ast.ExpressionKind.MemberAccess:
            walk(e.value)

    walk(expr)
    return out


class _Graph:
    def __init__(self, sm):
        self.sm = sm
        self.edges: dict = {}  # symbol -> {symbol: location}
        self.loop_vars: set = set()  # for-loop iteration variables: unrolled, not signals

    def add(self, sources, targets, location) -> None:
        for t in targets:
            for s in sources:
                if s is not t:
                    self.edges.setdefault(s, {}).setdefault(t, location)

    def assignments(self, stmt, conds: frozenset) -> None:
        """Every assignment in a combinational statement: target ← right side + enclosing conditions."""
        if stmt is None:
            return
        k = stmt.kind
        S = ast.StatementKind
        if k == S.List:
            for s in stmt.list:
                self.assignments(s, conds)
        elif k == S.Block:
            self.assignments(stmt.body, conds)
        elif k == S.Conditional:
            c = set(conds)
            for cond in stmt.conditions:
                c |= _reads(cond.expr)
            c = frozenset(c)
            self.assignments(stmt.ifTrue, c)
            self.assignments(stmt.ifFalse, c)
        elif k == S.Case:
            c = set(conds) | _reads(stmt.expr)
            for item in stmt.items:
                for e in item.expressions:
                    c |= _reads(e)
            c = frozenset(c)
            for item in stmt.items:
                self.assignments(item.stmt, c)
            self.assignments(stmt.defaultCase, c)
        elif k == S.ExpressionStatement:
            self.expression(stmt.expr, conds, stmt.sourceRange.start)
        elif k in (S.ForLoop, S.ForeachLoop, S.WhileLoop, S.RepeatLoop, S.DoWhileLoop, S.ForeverLoop):
            if k == S.ForLoop:
                self.loop_vars.update(getattr(stmt, "loopVars", None) or ())
                for init in getattr(stmt, "initializers", None) or ():
                    if init.kind == ast.ExpressionKind.Assignment:
                        self.loop_vars |= _lhs(init.left)
            c = set(conds)
            for attr in ("stopExpr", "cond", "count"):
                c |= _reads(getattr(stmt, attr, None))
            self.assignments(stmt.body, frozenset(c))
        elif k == S.Timed:  # @(...) / #n inside: not combinational
            return
        else:  # anything else: every assignment inside, conservatively
            stmt.visit(lambda n: self._fallback(n, conds))

    def _fallback(self, node, conds) -> bool:
        if getattr(node, "kind", None) == ast.ExpressionKind.Assignment:
            self.expression(node, conds, node.sourceRange.start)
            return False
        return True

    def expression(self, expr, conds, location) -> None:
        if expr.kind == ast.ExpressionKind.Assignment:
            self.add(_reads(expr.right) | _lhs_index_reads(expr.left) | conds, _lhs(expr.left), location)
        else:  # a call with output arguments, etc.: nested assignments
            expr.visit(lambda n: self._fallback(n, conds))


def _combinational(block) -> bool:
    PK = ast.ProceduralBlockKind
    kind = block.procedureKind
    if kind == PK.AlwaysComb:
        return True
    if kind != PK.Always:
        return False
    body = block.body
    if body.kind != ast.StatementKind.Timed:
        return False
    timing = body.timing
    if timing.kind == ast.TimingControlKind.ImplicitEvent:  # @*
        return True
    text = str(timing.syntax) if timing.syntax is not None else ""
    return "posedge" not in text and "negedge" not in text and timing.kind in (
        ast.TimingControlKind.SignalEvent, ast.TimingControlKind.EventList)


def _scope(g: _Graph, scope, instances: list) -> None:
    for m in scope:
        k = m.kind
        if k == ast.SymbolKind.ContinuousAssign:
            g.expression(m.assignment, frozenset(), m.location)
        elif k == ast.SymbolKind.ProceduralBlock and _combinational(m):
            body = m.body.stmt if m.body.kind == ast.StatementKind.Timed else m.body
            g.assignments(body, frozenset())
        elif k == ast.SymbolKind.Net and getattr(m, "initializer", None) is not None:  # wire x = …;
            g.add(_reads(m.initializer), {m}, m.location)
        elif k == ast.SymbolKind.Instance:
            instances.append(m)
        elif k in (ast.SymbolKind.GenerateBlock, ast.SymbolKind.GenerateBlockArray):
            if k == ast.SymbolKind.GenerateBlock and getattr(m, "isUninstantiated", False):
                continue
            _scope(g, m, instances)


def _instance(g: _Graph, inst, depth: int = 0) -> None:
    children: list = []
    _scope(g, inst.body, children)
    for child in children:
        for pc in child.portConnections:
            port, expr = pc.port, pc.expression
            inner = getattr(port, "internalSymbol", None)
            if expr is None or inner is None:
                continue
            d = str(port.direction).split(".")[-1]
            if d == "In":
                g.add(_reads(expr), {inner}, child.location)
            elif d == "Out":
                outer = _lhs(expr.left) if expr.kind == ast.ExpressionKind.Assignment else _reads(expr)
                g.add({inner}, outer, child.location)
        if depth < 64:
            _instance(g, child, depth + 1)


def _cycles(edges: dict) -> list[list]:
    """Strongly connected components with more than one signal (Tarjan, iterative)."""
    index: dict = {}
    low: dict = {}
    on: set = set()
    stack: list = []
    out: list[list] = []
    counter = 0
    for root in list(edges):
        if root in index:
            continue
        work = [(root, iter(edges.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(edges.get(w, ()))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w is v:
                        break
                if len(comp) > 1:
                    out.append(comp)
    return out


def _one_cycle(edges: dict, comp: list) -> list:
    """A shortest simple cycle through the component (for a readable report)."""
    members = set(comp)
    start = comp[0]
    prev = {start: None}
    queue = [start]
    for v in queue:
        for w in edges.get(v, ()):
            if w is start:
                path = [v]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return path[::-1]
            if w in members and w not in prev:
                prev[w] = v
                queue.append(w)
    return comp


def _graph(comp, sm) -> "_Graph":
    g = _Graph(sm)
    for top in comp.getRoot().topInstances:
        _instance(g, top)
    for v in g.loop_vars:
        g.edges.pop(v, None)
    for targets in g.edges.values():
        for v in g.loop_vars:
            targets.pop(v, None)
    return g


def comb_edges(comp, sm) -> dict[str, list[str]]:
    """The combinational graph by hierarchical path: signal → the signals computed from it in the same cycle."""
    g = _graph(comp, sm)
    return {a.hierarchicalPath: [b.hierarchicalPath for b in bs] for a, bs in g.edges.items()}


def comb_path(edges: dict[str, list[str]], src: str, dst: str) -> list[str] | None:
    """A combinational path src → … → dst (by hierarchical path), or None."""
    prev: dict[str, str | None] = {src: None}
    queue = [src]
    for v in queue:
        for w in edges.get(v, ()):
            if w in prev:
                continue
            prev[w] = v
            if w == dst:
                path = [w]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return path[::-1]
            queue.append(w)
    return None


def find_loops(comp, sm) -> list[Loop]:
    """Combinational loops in the elaborated compilation `comp`."""
    g = _graph(comp, sm)
    loops = []
    for c in _cycles(g.edges):
        cyc = _one_cycle(g.edges, c)
        where = []
        for a, b in zip(cyc, [*cyc[1:], cyc[0]]):
            loc = g.edges[a][b]
            try:
                where.append((sm.getFileName(loc), sm.getLineNumber(loc)))
            except Exception:  # noqa: BLE001
                where.append(("?", 0))
        loops.append(Loop([s.hierarchicalPath for s in cyc], where))
    return loops
