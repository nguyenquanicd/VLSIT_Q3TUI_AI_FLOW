"""The flow map: the whole VLSI AI agent flow (docs/spec/proposed-flow.yaml) as boxes. A box that belongs to a flow
that exists is solid — click it (or Tab to it and Enter) to work on that flow; the rest is faded: not developed yet.
Which group belongs to which flow is the `flow` of the Group below; a flow without a flow file counts as not developed."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

SPEC_SECTIONS = ["Requirements", "Architecture", "Interfaces", "Clock/reset", "Parameters", "Function", "CSR", "Errors", "PPA",
                 "RTL contract", "Verification", "Timing", "Traceability", "Decisions"]
LOOP = "[#e0795a]↩ {}[/]"


def stage(name: str, back: str = "") -> str:
    return f"{escape(name)}  {LOOP.format(back)}" if back else escape(name)


class Box(Static):
    """One stage of the map."""


class Group(Vertical):
    """A group of stages that belongs to a flow (None: no flow yet). Developed groups can be focused and picked."""

    def __init__(self, title: str, flow: str | None, developed: bool, current: bool, **kw):
        super().__init__(**kw)
        self.flow, self.developed = flow, developed
        self.border_title = title + (f"  ·  {flow}" if developed else "")
        self.can_focus = developed
        self.set_class(developed, "on")
        self.set_class(not developed, "off")
        self.set_class(current, "current")

    def on_click(self) -> None:
        self.screen.pick(self)  # type: ignore[attr-defined]


class FlowMapScreen(ModalScreen[str | None]):
    """Dismisses with the flow's name, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("enter", "select", "Select"), Binding("tab", "app.focus_next", show=False),
                Binding("shift+tab", "app.focus_previous", show=False)]
    DEFAULT_CSS = """
    FlowMapScreen { align: center middle; }
    FlowMapScreen #map-box { width: 104; height: 92%; border: round $primary; background: $surface; padding: 0 1; }
    FlowMapScreen #map-title { text-style: bold; padding: 0 1; }
    FlowMapScreen #map-hint { color: $text-muted; height: auto; padding: 0 1; }
    FlowMapScreen VerticalScroll { height: 1fr; }
    FlowMapScreen Group { height: auto; border: round $panel-lighten-2; border-title-color: $text; padding: 0 1; margin: 0; }
    FlowMapScreen Group.on { border: round $primary; }
    FlowMapScreen Group.on:hover { background: $boost; }
    FlowMapScreen Group.on:focus { border: heavy $accent; }
    FlowMapScreen Group.current { border: double $success; }
    FlowMapScreen Group.off { opacity: 45%; }
    FlowMapScreen Box { height: auto; background: $boost; padding: 0 1; margin: 0 0 0 0; }
    FlowMapScreen .row { height: auto; }
    FlowMapScreen .col { width: 1fr; height: auto; }
    FlowMapScreen .arrow { text-align: center; color: $text-muted; height: 1; }
    FlowMapScreen .chips { color: $text-muted; }
    FlowMapScreen .sub { border: round $panel-lighten-2; height: auto; width: 1fr; padding: 0 1; }
    """

    def __init__(self, available: set[str], current: str):
        super().__init__()
        self.available, self.current = available, current

    def _g(self, title: str, flow: str | None, **kw) -> Group:
        return Group(title, flow, flow in self.available, flow == self.current, **kw)

    def compose(self) -> ComposeResult:
        with Vertical(id="map-box"):
            yield Static("Flows", id="map-title")
            with VerticalScroll():
                with self._g("Spec", "vlsit"):
                    yield Static(" ".join(f"[{s}]" for s in SPEC_SECTIONS), classes="chips", markup=False)
                yield Static("▼", classes="arrow")
                with self._g("RTL gen", "vlsit"):
                    yield Box(stage("RTL", "Spec"))
                    yield Box(stage("Compile", "RTL"))
                    with Horizontal(classes="row"):
                        with Vertical(classes="col"):
                            yield Box(stage("SDC", "Spec, RTL"))
                            yield Box(stage("Basic lint", "RTL"))
                        with Vertical(classes="sub"):
                            yield Static(stage("Smoke test", "Spec, RTL"))
                            yield Box("VTB    SVA   ▸ Basic test")
                yield Static("▼", classes="arrow")
                with Horizontal(classes="row"):
                    with self._g("DV", None, classes="col"):
                        yield Box(stage("Testplan", "Spec"))
                        yield Box(stage("UVM env", "Spec, RTL"))
                        yield Box(stage("Formal", "Spec, RTL"))
                        yield Box(stage("Sim tests", "Spec, RTL"))
                        yield Box(stage("Coverage closure", "Spec, RTL"))
                    with Vertical(classes="col"):
                        with self._g("FPGA synthesis", "fpga"):
                            yield Box(stage("FPGA synthesis", "RTL"))
                            yield Box(stage("Timing + util report", "Spec, RTL"))
                            yield Box(stage("PPA optimize", "Spec, RTL"))
                        with self._g("ASIC synthesis", None):
                            yield Box(stage("ASIC port", "RTL"))
                            yield Box(stage("ASIC synthesis", "Spec, RTL"))
                            yield Box(stage("LEC", "RTL"))
                    with self._g("Sanity", None, classes="col"):
                        yield Box(stage("CDC", "Spec, RTL") + "  → waivers")
                        yield Box(stage("RDC", "Spec, RTL") + "  → waivers")
                        yield Box(stage("GCA", "Spec, RTL") + "  → waivers")
                yield Static("▼", classes="arrow")
                with self._g("Convergence gate", None):
                    yield Box("same RTL tag, all lanes pass")
            yield Static("Click a box (or Tab, Enter) to work on its flow · bright = developed, faded = not yet · "
                         "double border = current · [#e0795a]↩[/] loops back to that stage · Esc", id="map-hint")

    def on_mount(self) -> None:
        groups = [g for g in self.query(Group) if g.developed]
        focus = next((g for g in groups if g.flow == self.current), groups[0] if groups else None)
        if focus:
            focus.focus()

    def pick(self, group: Group) -> None:
        if not group.developed:
            self.notify(f"{group.border_title.split('  ·')[0]}: not developed yet", severity="warning")
            return
        self.dismiss(group.flow)

    def action_select(self) -> None:
        if isinstance(self.focused, Group):
            self.pick(self.focused)

    def action_cancel(self) -> None:
        self.dismiss(None)
