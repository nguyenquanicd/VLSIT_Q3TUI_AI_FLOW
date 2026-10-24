"""TUI panels of the project settings flow's steps (the flow's `panels.py`, imported by the TUI when it opens the flow; docs/spec/project-flow.md):
`project` (settings you edit in place), `tools` (roles, modules, found or not), `libraries` (technologies and corners; add one by scanning a folder).
Every change is one of the flow's actions (`addon.py`) or `Ops.apply_settings`: the same ones the assistant and `q3tui act` use."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding

from q3tui.flows.project.lib import common
from q3tui.tui.panels import register_panel
from q3tui.tui.screens import EditScreen
from q3tui.tui.vlsit_panels import DataTab, TabSpec, VlsitPanel

if TYPE_CHECKING:
    from q3tui.pipeline.base import StepDef
    from q3tui.tui.app import Q3TUIApp

DASH = Text("–", style="grey50")


def _yes(v: bool | None) -> Text:
    if v is None:
        return Text("?", style="grey50")  # (not checked yet)
    return Text("✔", style="green") if v else Text("✘", style="red")


class _Tab(DataTab):
    """A table of one thing with keys that run the flow's actions."""

    def _act(self, name: str, **kw) -> bool:
        app = self.capp
        try:
            app.log_info(app.ops.run_action(name, **{k: v for k, v in kw.items() if v != ""}))
            ok = True
        except Exception as exc:  # noqa: BLE001 - friendly message, never a traceback
            app.notify(str(exc), severity="error")
            ok = False
        app.refresh_all()
        return ok

    def on_data_table_row_selected(self, event) -> None:
        """Enter edits the highlighted row, as in the other flows' views."""
        event.stop()
        edit = getattr(self, "action_edit", None)
        if edit is not None:
            edit()

    def _ask(self, title: str, fields: list[tuple], apply, hint: str = "", buttons=()) -> None:
        def done(values: dict | None) -> None:
            if values is not None:
                try:
                    apply(values)
                except (ValueError, TypeError) as exc:
                    self.app.notify(str(exc), severity="error")

        self.capp.push_screen(EditScreen(title, fields, hint or "ctrl+s to apply · Esc to cancel", buttons), done)


# -- project: the settings, editable in place -----------------------------------------------------------------------------------------------


def _show(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (list, tuple)):
        return ", ".join(map(str, v)) or "–"
    return str(v)


def _settings(app: "Q3TUIApp") -> list:
    """The settings of the Settings dialog's general and LLM tabs (this flow has no step options)."""
    return [(t, s) for t in app.ops.settings_tabs() if t.id in ("general", "llm") for s in t.settings if not s.hidden]


def _project_rows(app: "Q3TUIApp") -> list[tuple]:
    cfg = app.project.cfg
    rows = [("info.root", "Project", str(app.project.root), Text("folder", style="grey50")),
            ("info.config", "Config file", app.project.rel(Path(cfg.source)) if getattr(cfg, "source", "") else "(defaults)", Text("q3tui.yaml", style="grey50")),
            ("info.flow", "Flow of this project", cfg.pipeline.flow, Text("pipeline.flow: F switches the view, this is the start-up flow", style="grey50"))]
    vals = app.ops.settings()
    return rows + [(s.path, s.label, _show(vals.get(s.path)), Text(t.title, style="grey50")) for t, s in _settings(app)]


def _project_detail(app: "Q3TUIApp", key: str) -> str:
    if key.startswith("info."):
        return {"info.root": "The project folder: deliverables (`spec/`, `src/`, `docs/`, …), `q3tui.yaml`, `config/` (tools.json, pdks.json) and `.q3tui/` (state).",
                "info.config": "Every setting you change here is saved to this file (`q3tui.yaml`); a setting not in it has its default.",
                "info.flow": "`pipeline.flow` of `q3tui.yaml`: the flow the project opens in. **F** (or `/flow`) moves this session to another flow without changing it."}.get(key, "")
    found = next(((t, s) for t, s in _settings(app) if s.path == key), None)
    if found is None:
        return ""
    t, s = found
    return f"**{s.label}** — `{s.path}`\n\n{s.help or ''}\n\nEnter / `e` edit (saved to `q3tui.yaml`)."


class SettingsTab(_Tab):
    BINDINGS = [Binding("e", "edit", "Edit")]

    def action_edit(self) -> None:
        key = self.current_key()
        found = next(((t, s) for t, s in _settings(self.capp) if s.path == key), None)
        if found is None:
            self.app.notify("nothing to edit here (the first rows are information)", severity="information")
            return
        _t, s = found
        cur = self.capp.ops.settings().get(s.path)
        if s.kind == "bool":
            fields = [("v", s.label, "on" if cur else "off", "choice", ["on", "off"])]
        elif s.kind == "choice" and s.choices and str(cur) in [str(v) for v, _l in s.choices]:
            fields = [("v", s.label, str(cur), "choice", [str(v) for v, _l in s.choices])]
        else:
            fields = [("v", s.label + (" (empty: none)" if s.nullable else ""), "" if cur is None else (", ".join(map(str, cur)) if isinstance(cur, (list, tuple)) else str(cur)), "line")]
        self._ask(s.label, fields, lambda v: (self.capp.log_info(self.capp.ops.apply_settings({s.path: v["v"]}, save=True)), self.capp.refresh_all()),
                  hint=(s.help or "") + "\nctrl+s to save to q3tui.yaml · Esc to cancel")


# -- tools: roles, modules, found or not ----------------------------------------------------------------------------------------------------


def _tool_rows(app: "Q3TUIApp") -> list[tuple]:
    rows = common.load_tool_rows(app.project)
    return [(r["role"], r["role"], r["tool"] or DASH, _yes(r["found"]), ", ".join(r["modules"]) or DASH, r["from"]) for r in rows]


def _tool_detail(app: "Q3TUIApp", key: str) -> str:
    r = next((r for r in common.load_tool_rows(app.project) if r["role"] == key), None)
    if r is None:
        return ""
    return (f"**{r['role']}** — {'not checked yet' if r['found'] is None else 'found' if r['found'] else '**not found**'}\n\n{r['description'] or ''}\n\n- command: `{r['cmd'] or '(not defined)'}`\n- modules: {', '.join(r['modules']) or 'none'}\n"
            f"- from: {r['from']}\n- used by: {', '.join(r['used_by']) or '–'}\n- log reader: {r.get('parse', '–')}\n\n"
            "Enter / `e` edits the role (saved to the project's `config/tools.json`); `m` sets the modules loaded before every tool; `c` checks again.")


class ToolsTab(_Tab):
    BINDINGS = [Binding("a", "add", "Add"), Binding("e", "edit", "Edit"), Binding("c", "check", "Check"), Binding("m", "modules", "Modules")]

    KEEP = "(keep)"

    def action_add(self) -> None:
        """a: give the path of a tool; an LLM stage reads it and works out the role, command, log reader and modules, then the form opens filled in."""
        self._ask("Add a tool", [("path", "Path of the tool: its binary, or its install folder (the rest is filled in by reading it)", "", "line")],
                  lambda v: self._identify(v["path"].strip()),
                  hint="ctrl+s: a read-only LLM stage looks at what is there and which role it serves, then you review · Esc cancels")

    def _identify(self, where: str) -> None:
        app = self.capp
        app.notify("Reading the tool to identify it (an LLM stage, read only)…")

        async def go() -> None:  # (on the app's loop: the stage's events reach the log)
            try:
                g = await common.identify_tool_async(app.ops, where)
            except Exception as exc:  # noqa: BLE001 - no LLM, no executable, …: the form opens to fill in by hand
                app.notify(f"Could not identify it: {exc}", severity="error")
                self._tool_form({"role": "", "cmd": where, "modules": [], "parse": "generic", "description": ""}, "Not identified — fill in by hand.")
                return
            note = (g["notes"] + "\n" if g["notes"] else "") + "".join(f"! {w}\n" for w in g["warnings"]) + "Review, change what is wrong, ctrl+s saves."
            self._tool_form(g, note)

        self.run_worker(go(), exclusive=True, group="tool-identify")

    def _tool_form(self, g: dict, note: str) -> None:
        from q3tui.eda import tools as toolmod

        readers = sorted({*toolmod.PARSERS, "generic"})
        known = ", ".join(r["role"] for r in common.role_contracts(self.capp.project))
        parse = g.get("parse") if g.get("parse") in readers else (self.KEEP if g.get("parse") == "" else "generic")
        fields = [("role", f"Role it serves (existing: {known}; a new name makes a new role)", g.get("role", ""), "line"), ("cmd", "Command (with the role's {placeholders})", g.get("cmd", ""), "line"),
                  ("modules", "Modules loaded for this role, separated by spaces (empty: none)", " ".join(g.get("modules") or []), "line"),
                  ("parse", "Log reader ((keep): the role's own patterns stay)", parse, "choice", [self.KEEP, *readers])]
        self._ask("Add a tool" if g.get("role") else "Add a tool (by hand)", fields,
                  lambda v: self._act("tool_set", role=common.slug(v["role"]), cmd=v["cmd"].strip(), modules=v["modules"].strip(), parse="" if v["parse"] == self.KEEP else v["parse"], description=g.get("description", "")),
                  hint=(note + "\n" if note else "") + "saved to the project's config/tools.json · ctrl+s saves · Esc cancels")

    def action_edit(self) -> None:
        """Enter / e: the command, the modules and the time limit of the highlighted role (saved to the project's config/tools.json)."""
        key = self.current_key()
        r = next((r for r in common.load_tool_rows(self.capp.project) if r["role"] == key), None)
        if r is None:
            self.app.notify("nothing to edit here — add a tool (a) or run `q3tui tools init`", severity="information")
            return
        tpl = None if r["from"] != "not configured" else common.template_role(key)  # (a role the file lacks: start from its template)
        cur = r if tpl is None else {"cmd": tpl.cmd, "own_modules": tpl.modules, "timeout_s": tpl.timeout_s}
        fields = [("cmd", "Command (with {placeholders})" + (" — not configured: the template is filled in" if tpl else ""), cur["cmd"], "line"),
                  ("modules", "Modules loaded for this role, separated by spaces (empty: keep)", " ".join(cur.get("own_modules") or []), "line"),
                  ("timeout_s", "Time limit (s; empty: keep)", str(cur.get("timeout_s") or ""), "line")]
        self._ask(f"Tool role {r['role']}", fields,
                  lambda v: self._act("tool_set", role=r["role"], cmd=v["cmd"].strip(), modules=v["modules"].strip(), timeout_s=v["timeout_s"].strip()),
                  hint=f"saved to the project's config/tools.json (the role's log reader stays: {r.get('parse', '–')}) · ctrl+s saves · Esc cancels")

    def action_check(self) -> None:
        """c: look for every tool (a shell per role loads the modules: a few seconds, in a thread)."""
        import anyio

        app = self.capp
        app.notify("Checking the tools (loading their modules)…")

        async def go() -> None:
            try:
                msg = await anyio.to_thread.run_sync(lambda: app.ops.run_action("tools_check"))
            except Exception as exc:  # noqa: BLE001
                app.notify(str(exc), severity="error")
                return
            app.log_info(msg)
            app.refresh_all()

        self.run_worker(go(), exclusive=True, group="tools-check")

    def action_modules(self) -> None:
        cur = " ".join(self.capp.project.cfg.tools.modules)
        self._ask("Modules loaded before every tool", [("modules", "Modules (`module load`), separated by spaces; empty: none", cur, "line")],
                  lambda v: self._act("tools_modules", modules=v["modules"].strip()) if v["modules"].strip() else self._act_clear_modules())

    def _act_clear_modules(self) -> None:
        self.capp.log_info(self.capp.ops.run_action("tools_modules"))
        self.capp.refresh_all()


# -- libraries: technologies and corners ----------------------------------------------------------------------------------------------------


def _entries(app: "Q3TUIApp") -> list[dict]:
    from q3tui.flows.asic.lib import pdks

    return pdks.entries(app.project)


def _lib_rows(app: "Q3TUIApp") -> list[tuple]:
    from q3tui.flows.asic.lib import pdks
    from q3tui.flows.fpga.lib import hints

    cur = pdks.locate(hints.asic_library(app.project), app.project)
    rows = []
    for e in sorted(_entries(app), key=lambda e: (-int(e.get("node_nm") or 0), e["id"])):
        n = len(e["files"])
        rows.append((e["id"], e.get("title", e["id"]), f"{e.get('node_nm') or '–'} nm", (str(n) if n else Text("none here", style="red")), e["source"],
                     Text(f"● {cur[1]}", style="green") if cur and cur[0].id == e["id"] else ""))
    return rows


def _lib_detail(app: "Q3TUIApp", key: str) -> str:
    from q3tui.flows.asic.lib import pdks

    e = next((e for e in _entries(app) if e["id"] == key), None)
    if e is None:
        return ""
    corners = pdks._corners(e["files"]) if e["files"] else ()
    body = "\n".join(f"- `{c}` — `{p}`" for c, p in corners[:40]) + (f"\n- … {len(corners) - 40} more" if len(corners) > 40 else "")
    where = "this project's `config/pdks.json`" if e["source"] == "project" else "the built-in catalog (open-source PDKs)"
    return (f"**{e.get('title', e['id'])}** — {e.get('node_nm') or '?'} nm · from {where}\n\n- globs: {', '.join(f'`{g}`' for g in e.get('globs', []))}\n"
            + (f"- include: `{e['include']}`\n" if e.get("include") else "") + f"- default corner: `{e.get('default') or '(first)'}` · estimate probe: {e.get('probe_ns') or 'default'} ns\n\n"
            + (f"**Corners ({len(corners)})**\n\n{body}\n\n" if corners else "_No `.db` file matches here: the technology is not offered until it does._\n\n")
            + "`u` pick the corner used as the ASIC library · Enter / `e` edit · `d` remove (project entries) · `a` add a technology: give the folder, the rest is filled in.")


class LibTab(_Tab):
    BINDINGS = [Binding("a", "add", "Add"), Binding("e", "edit", "Edit"), Binding("d", "remove", "Remove"), Binding("u", "use", "Use corner")]

    FIRST = "(first)"

    def action_add(self) -> None:
        """a: give the folder; an LLM stage reads it and fills in the rest, then the form opens with what it found for you to review."""
        self._ask("Add a technology", [("folder", "Folder with the library files (the rest is filled in by reading it)", "", "line")],
                  lambda v: self._identify(v["folder"].strip()),
                  hint="ctrl+s: a read-only LLM stage reads the folder (names, README, .lib headers) and fills in the id, name, node, corners, probe · then you review · Esc cancels")

    def _identify(self, where: str) -> None:
        app = self.capp
        app.notify("Reading the folder to identify the library (an LLM stage, read only)…")

        async def go() -> None:  # (on the app's loop: the stage's events reach the log like any other stage's)
            try:
                g = await common.identify_async(app.ops, where)
            except Exception as exc:  # noqa: BLE001 - no LLM, no files, …: the form still opens, to fill in by hand
                app.notify(f"Could not identify it: {exc}", severity="error")
                self._form({"globs": [where]}, existing=False, note="Not identified — fill in by hand (Scan lists what is there).")
                return
            entry = {"id": g["id"], "title": g["title"], "node_nm": g["node_nm"], "globs": [g["folder"]], "include": g["include"], "default": g["default"], "probe_ns": g["probe_ns"]}
            note = (g["notes"] + "\n" if g["notes"] else "") + "".join(f"! {w}\n" for w in g["warnings"]) + "Review, change what is wrong, ctrl+s saves."
            self._form(entry, existing=False, note=note)

        self.run_worker(go(), exclusive=True, group="lib-identify")

    def action_edit(self) -> None:
        key = self.current_key()
        e = next((e for e in _entries(self.capp) if e["id"] == key), None)
        if e is not None:
            self._form(e, existing=True)

    def _form(self, e: dict, existing: bool, note: str = "") -> None:
        """The entry as a form: where its files are, its name, node, the default corner (picked from the corners the files have), the estimate's
        probe, and whether to use it as the ASIC library. Saving an existing id replaces that entry (of a built-in one: overrides it here)."""
        from q3tui.flows.asic.lib import pdks
        from q3tui.flows.fpga.lib import hints

        glob = (e.get("globs") or [""])[0]
        files = pdks.files_of({"globs": e.get("globs") or [], "include": e.get("include", "")}) if glob else []
        labels = [c for c, _p in pdks._corners(files)]
        cur = pdks.locate(hints.asic_library(self.capp.project), self.capp.project)
        in_use = bool(existing and cur and cur[0].id == e.get("id"))
        default = str(e.get("default") or "")
        fields = [("folder", "Folder with the .db files of its corners (searched below it), or a glob", glob, "line")]
        if not existing:
            fields.append(("id", "Technology id (letters, digits)", str(e.get("id") or ""), "line"))
        fields += [("title", "Name shown", str(e.get("title") or ""), "line"), ("node_nm", "Node (nm)", str(e.get("node_nm") or ""), "line")]
        if labels:
            fields.append(("default", "Default corner (the typical one)", default if default in labels else self.FIRST, "choice", [self.FIRST, *labels]))
        else:
            fields.append(("default", "Default corner (label; empty: the first)", default, "line"))
        fields += [("probe_ns", "Estimate probe period (ns; empty: 1 ns — one the library cannot meet; less for a fast node)", str(e.get("probe_ns") or ""), "line"),
                   ("include", "Only files matching this regex (optional)", str(e.get("include") or ""), "line"),
                   ("use", "Use this technology's default corner as the ASIC library", "yes" if in_use else "no", "choice", ["yes", "no"])]

        def scan(screen: EditScreen) -> None:
            v = screen._values()
            try:
                found = common.scan(str(v.get("folder") or ""))
            except ValueError as exc:
                screen.set_status(f"Not scanned: {exc}")
                return
            screen.set_status(common.describe_scan(found))

        def apply(v: dict) -> None:
            ident = e["id"] if existing else v["id"].strip()
            dflt = "" if v["default"] == self.FIRST else v["default"].strip()
            ok = self._act("lib_add", id=ident, folder=v["folder"].strip(), title=v["title"].strip(), node_nm=v["node_nm"].strip(), default=dflt,
                           probe_ns=v["probe_ns"].strip(), include=v["include"].strip())
            if ok and v["use"] == "yes":
                self._act("lib_use", id=common.slug(ident))

        self._ask(f"Technology {e['id']}" if existing else "Add a technology", fields, apply,
                  hint=(note + "\n" if note else "") + "ctrl+s saves to this project's config/pdks.json · Esc cancels", buttons=[("scan", "Scan the folder", scan)])

    def action_remove(self) -> None:
        key = self.current_key()
        if key:
            self._act("lib_remove", id=key)

    def action_use(self) -> None:
        """u: pick the corner of the highlighted technology that is the ASIC library."""
        from q3tui.flows.asic.lib import pdks
        from q3tui.flows.fpga.lib import hints

        key = self.current_key()
        tech = next((p for p in pdks.catalog(self.capp.project) if p.id == key), None)
        if tech is None:
            self.app.notify("this technology has no library files here", severity="information")
            return
        cur = pdks.locate(hints.asic_library(self.capp.project), self.capp.project)
        labels = [c for c, _p in tech.corners]
        start = cur[1] if cur and cur[0].id == tech.id else next((c for c, p in tech.corners if p == tech.default_path()), labels[0])
        self._ask(f"ASIC library corner of {tech.title}", [("corner", "Corner used as the ASIC library (the ASIC and FPGA flows)", start, "choice", labels)],
                  lambda v: self._act("lib_use", id=tech.id, corner=v["corner"]), hint="ctrl+s uses it (saved to q3tui.yaml) · Esc cancels")


# -- registry ---------------------------------------------------------------------------------------------------------------------------------


def _tabs_for(kind: str) -> tuple[TabSpec, ...]:
    if kind == "project.project":
        return (TabSpec("settings", "Settings", ("Setting", "Value", "Where"), _project_rows, _project_detail,
                        "Enter / e edit a setting (saved to q3tui.yaml) · the same settings as the Settings dialog (,)", widget=SettingsTab),)
    if kind == "project.tools":
        return (TabSpec("tools", "Tools", ("Role", "Tool", "Found", "Modules", "From"), _tool_rows, _tool_detail,
                        "a add a tool (give its path: the rest is filled in) · Enter / e edit a role · c check which tools are found · m modules loaded before every tool", widget=ToolsTab,
                        empty="no tools file yet — `q3tui tools init` writes config/tools.json, or press a to add a tool"),)
    if kind == "project.libraries":
        return (TabSpec("libraries", "Libraries", ("Technology", "Node", "Corners", "From", "In use"), _lib_rows, _lib_detail,
                        "a add (give the folder: the rest is filled in) · Enter / e edit · d remove · u pick the corner used as the ASIC library", widget=LibTab, empty="no technology"),)
    return ()


def _builder(kind: str):
    def build(step: "StepDef"):
        return VlsitPanel(step, _tabs_for(kind))

    return build


for _kind in ("project.project", "project.tools", "project.libraries"):
    register_panel(_kind, _builder(_kind))
