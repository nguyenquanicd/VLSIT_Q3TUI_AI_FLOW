"""Draggable splitter between two panes: drag to resize the pane before it (left or above);
double-click resets it. Sizes are remembered per project in .q3tui/tui/layout.json."""

from __future__ import annotations

from textual import events
from textual.widget import Widget


class Splitter(Widget):
    DEFAULT_CSS = """
    Splitter { background: $panel; }
    Splitter.-vertical { width: 1; height: 1fr; }
    Splitter.-horizontal { height: 1; width: 1fr; }
    Splitter:hover, Splitter.-dragging { background: $accent; }
    """

    def __init__(self, *, vertical: bool, min_size: int = 8, id: str):
        """vertical=True: a | bar between left and right panes (changes the left one's width);
        vertical=False: a ─ bar between upper and lower panes (changes the upper one's height)."""
        super().__init__(id=id, classes="-vertical" if vertical else "-horizontal")
        self.vertical, self.min_size = vertical, min_size
        self._drag: tuple[int, int] | None = None  # (mouse position, pane size) at the start

    def render(self) -> str:
        return ("┃\n" * max(1, self.size.height)).rstrip() if self.vertical else "━" * max(1, self.size.width)

    @property
    def pane(self) -> Widget | None:
        siblings = list(self.parent.children) if self.parent else []
        i = siblings.index(self)
        return next((w for w in reversed(siblings[:i]) if w.display), None)

    def _limit(self, size: int) -> int:
        total = self.parent.size.width if self.vertical else self.parent.size.height
        if total <= 0:  # not laid out yet (a hidden step view): keep the remembered size
            return max(self.min_size, size)
        return max(self.min_size, min(size, max(self.min_size, total - self.min_size - 1)))

    def set_size(self, size: int | None) -> None:
        pane = self.pane
        if pane is None:
            return
        value = None if size is None else self._limit(size)
        if self.vertical:
            pane.styles.width = value
        else:
            pane.styles.height = value

    def on_mount(self) -> None:
        saved = getattr(self.app, "layout_sizes", {}).get(self.id)
        if saved:
            self.call_after_refresh(self.set_size, saved)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        pane = self.pane
        if pane is None:
            return
        self.capture_mouse()
        self.add_class("-dragging")
        size = pane.outer_size.width if self.vertical else pane.outer_size.height
        self._drag = (event.screen_x if self.vertical else event.screen_y, size)
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag is None:
            return
        start, size = self._drag
        pos = event.screen_x if self.vertical else event.screen_y
        self.set_size(size + pos - start)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag is None:
            return
        self._drag = None
        self.release_mouse()
        self.remove_class("-dragging")
        pane = self.pane
        if pane is not None and hasattr(self.app, "remember_layout"):
            self.app.remember_layout(self.id, pane.outer_size.width if self.vertical else pane.outer_size.height)
        event.stop()

    def on_click(self, event: events.Click) -> None:
        if event.chain >= 2:  # double-click: back to the default size
            self.set_size(None)
            if hasattr(self.app, "remember_layout"):
                self.app.remember_layout(self.id, None)
            event.stop()
