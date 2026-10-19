"""Mermaid diagrams as terminal art for the TUI's Markdown view (flowchart, sequence, state … via `termaid`).

Imported lazily and never fatal: with `termaid` missing, or a diagram it cannot parse, the caller shows the Mermaid
source as it is.

A block diagram (a flowchart that draws IO or a module boundary) is laid out by `blockdiagram.py` instead.

Wires that cross without connecting are drawn with a line jump: the horizontal wire is broken around the vertical
one (`─╴│╶─`) instead of termaid's `┼`, which then only means a real junction.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

from q3tui.steps.spec.diagrams import display_mermaid

LANGS = ("mermaid", "mmd")
_JUMP = ""  # a crossing on termaid's canvas, until the text is out


@contextmanager
def _line_jumps():
    """While termaid draws: a straight wire laid over a perpendicular straight wire (not a node border) is a crossing,
    marked `_JUMP`; a corner or a T merged into a wire stays a junction."""
    from termaid.renderer import canvas as cv

    horizontal, vertical = cv.LEFT | cv.RIGHT, cv.UP | cv.DOWN
    original = cv.Canvas.put

    def put(self, row, col, ch, merge=True, style=""):
        crossings = self.__dict__.setdefault("_q3_crossings", set())
        if (merge and 0 <= row < self.height and 0 <= col < self.width and not self._protected[row][col]
                and self._style_grid[row][col] != "subgraph"):  # a wire through a module / domain boundary: a pin, no jump
            if {self._directions[row][col], cv._CHAR_TO_DIRECTIONS.get(ch, 0)} == {horizontal, vertical}:
                crossings.add((row, col))
        original(self, row, col, ch, merge, style)
        if (row, col) in crossings and self._grid[row][col] == "┼":
            self._grid[row][col] = _JUMP

    cv.Canvas.put = put
    try:
        yield
    finally:
        cv.Canvas.put = original


def _draw_jumps(art: str) -> str:
    """`_JUMP` cells → the vertical wire passes, the horizontal one is broken around it."""
    out = []
    for line in art.split("\n"):
        cells = list(line)
        for i, c in enumerate(cells):
            if c != _JUMP:
                continue
            cells[i] = "│"
            if i and cells[i - 1] == "─":
                cells[i - 1] = "╴"
            if i + 1 < len(cells) and cells[i + 1] == "─":
                cells[i + 1] = "╶"
        out.append("".join(cells))
    return "\n".join(out)


@lru_cache(maxsize=64)
def render(source: str) -> str | None:
    """The diagram as Unicode box-drawing text, or None when it cannot be rendered."""
    try:
        from termaid import render as termaid_render

        shown, notes = display_mermaid(source)
        from q3tui.tui import blockdiagram

        block = blockdiagram.render(shown)  # a block diagram (IO / module boundary): Q3TUI's own schematic layout
        if block is not None:
            return block
        with _line_jumps():
            art = termaid_render(shown.strip("\n"))
    except Exception:  # noqa: BLE001 - unsupported diagram type / syntax / library missing: show the source instead
        return None
    if not (art and art.strip()):
        return None
    art = _draw_jumps(art)
    return art.rstrip("\n") + "".join(f"\n\n{note}" if i == 0 else f"\n{note}" for i, note in enumerate(notes))
