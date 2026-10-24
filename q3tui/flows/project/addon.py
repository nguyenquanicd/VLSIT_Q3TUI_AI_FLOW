"""Add-on of the project settings flow (q3tui/addons.py): the actions behind its views — the assistant, `q3tui act NAME key=value` and the TUI's
`/act` use them too. They change the project's own files (`config/pdks.json`, `q3tui.yaml`)."""

from __future__ import annotations

from q3tui.flows.project.lib import common


def register(api) -> None:
    from q3tui.flows.fpga.lib import hints

    # the library in use is the ASIC flow's option (one choice for every flow): settable here too, with the assistant's set_settings
    api.setting("asic_synth", "library", "ASIC library (.db)", "text", "The standard-cell library the ASIC flow and the quick Fmax estimate use (a .db). The Libraries step picks it from "
                "the catalog (u).", default=hints.asic_default_library())

    @api.action("lib_scan", "Look for standard-cell libraries (.db files for Design Compiler) in a folder (searched below it) or a glob, and list "
                "the folders that hold them with their corners — to find what to add with lib_add.", {"folder": "a folder, or a glob like /pdk/*/lib/*.db"})
    def lib_scan(ops, folder: str) -> str:
        return common.describe_scan(common.scan(folder))

    @api.action("lib_identify", "Look at a standard-cell library folder and say what it is: an LLM stage (read only) reads the folder's names, README and "
                ".lib headers and proposes the catalog entry — id, name, node, which files are cell corners, the default corner, the estimate's probe — "
                "checked by code against the files. Writes nothing; lib_add does.", {"folder": "the folder of the library (searched below it), or a glob"})
    def lib_identify(ops, folder: str) -> str:
        g = common.identify(ops, folder)
        return (f"{g['id']} | {g['title']} | {g['node_nm'] or '?'} nm | {g['foundry'] or '–'}\nglob: {g['folder']}" + (f" (include {g['include']})" if g["include"] else "")
                + f"\ncorners: {', '.join(g['corners'][:8])}{' …' if len(g['corners']) > 8 else ''} | default {g['default'] or '(first)'} | probe {g['probe_ns'] or 'default'} ns\n{g['notes']}"
                + "".join(f"\n! {w}" for w in g["warnings"]))

    @api.action("lib_add", "Add (or replace) a standard-cell technology in this project's config/pdks.json. Give only the folder with the .db files of its corners "
                "and the rest is filled in by reading it (an LLM stage, checked by code); anything you give — the id, name, node, … — wins. The ASIC "
                "flow's dialogs then offer it, technology then corner.",
                {"folder": "the folder with its .db files (searched below it), or a glob", "id": "short id (letters, digits; default: from the folder)",
                 "title": "name shown", "node_nm": "technology node in nm (for the order and the label)", "default": "the corner (label) picked with the technology",
                 "probe_ns": "clock period the quick Fmax estimate starts from (a period the library cannot meet)", "include": "regex a file name must match"},
                optional=("id", "title", "node_nm", "default", "probe_ns", "include"))
    def lib_add(ops, folder: str, id: str = "", title: str = "", node_nm: str = "", default: str = "", probe_ns: str = "", include: str = "") -> str:
        note = ""
        if not id.strip():  # only the folder: read it and fill in what was not given
            g = common.identify(ops, folder)
            id, folder = g["id"], g["folder"]
            title, node_nm, default, probe_ns, include = (title or g["title"], node_nm or g["node_nm"], default or g["default"], probe_ns or g["probe_ns"],
                                                           include or g["include"])
            note = (" — " + g["notes"] if g["notes"] else "") + "".join(f" (! {w})" for w in g["warnings"])
        return ops._log("libraries", common.add_entry(ops.project, id, folder, title, node_nm, default, probe_ns, include) + note)

    @api.action("lib_remove", "Remove a technology from this project's config/pdks.json (the built-in open-source ones are not in it).", {"id": "the technology id"})
    def lib_remove(ops, id: str) -> str:
        return ops._log("libraries", common.remove_entry(ops.project, id))

    @api.action("lib_list", "List the technologies the ASIC flow can use here, their corners and which library is in use.")
    def lib_list(ops) -> str:
        from q3tui.flows.asic.lib import pdks
        from q3tui.flows.fpga.lib import hints

        cur = hints.asic_library(ops.project)
        lines = []
        for e in pdks.entries(ops.project):
            corners = [c for c, _ in pdks._corners(e["files"])]
            lines.append(f"{e['id']} | {e.get('title', e['id'])} | {e.get('node_nm', 0)} nm | {e['source']} | " + (f"{len(corners)} corners: {', '.join(corners[:4])}{' …' if len(corners) > 4 else ''}" if corners else "no files here"))
        return "\n".join(lines + [f"in use: {pdks.short_name(cur, ops.project) if cur else 'none'} ({cur})"])

    @api.action("lib_use", "Use a technology (its default corner, or the corner given) as the ASIC library, for both the ASIC and the FPGA flow; saved to q3tui.yaml.",
                {"id": "the technology id", "corner": "the corner label (default: the technology's default)"}, optional=("corner",))
    def lib_use(ops, id: str, corner: str = "") -> str:
        from q3tui.flows.asic.lib import pdks
        from q3tui.flows.fpga.lib import hints

        tech = next((p for p in pdks.catalog(ops.project) if p.id == id), None)
        if tech is None:
            raise ValueError(f"no technology '{id}' with libraries here (lib_list)")
        path = dict(tech.corners).get(corner) if corner else tech.default_path()
        if not path:
            raise ValueError(f"'{corner}' is not a corner of {id}: {', '.join(c for c, _ in tech.corners)}")
        return ops.apply_settings({hints.LIBRARY_OPTION: path}, save=True)

    @api.action("tool_set", "Change a tool role of this project: its command, the modules loaded for it (besides the global ones), its time limit, its log reader. "
                "Saved to the project's config/tools.json (created from the built-in preset when there is none, so the other roles stay) or tools.roles of q3tui.yaml.",
                {"role": "the role (lint, synth, fpga_synth, asic_synth, …)", "cmd": "the command line, with {placeholders} (empty: keep)",
                 "modules": "modules loaded for this role, separated by spaces (empty: keep)", "timeout_s": "time limit in seconds (empty: keep)",
                 "parse": "the log reader (vcs, dc, verilator, iverilog, yosys, vivado, generic; empty: keep)", "description": "what the role does (empty: keep)"},
                optional=("cmd", "modules", "timeout_s", "parse", "description"))
    def tool_set(ops, role: str, cmd: str = "", modules: str = "", timeout_s: str = "", parse: str = "", description: str = "") -> str:
        return ops._log("tools", common.set_role(ops.project, role, cmd, modules, timeout_s, parse, description))

    @api.action("tool_identify", "Look at a tool path (a binary, or an install folder with many tools) and say how Q3TUI would use each tool found: an LLM stage (read only) works out "
                "which role each serves, the command with that role's placeholders, the log reader and the modules, checked by code. Writes nothing; tool_add does.",
                {"path": "a tool's binary, or an install folder"})
    def tool_identify(ops, path: str) -> str:
        out = []
        for g in common.identify_tool(ops, path):
            out.append(f"role {g['role']} | {g['cmd']}\nlog reader {g['parse']} | modules {', '.join(g['modules']) or '–'}\n{g['description']}\n{g['notes']}" + "".join(f"\n! {w}" for w in g["warnings"]))
        return f"{len(out)} tool(s) found\n\n" + "\n\n".join(out)

    @api.action("tool_add", "Add tools to this project: give only a path (a binary, or an install folder with many tools) and every tool found gets its role, command, log reader and modules "
                "by reading it (an LLM stage, checked by code); a role / cmd / modules / parse you give wins (and adds just that role). Saved like tool_set.",
                {"path": "a tool's binary, or an install folder", "role": "the role it serves (default: worked out, one per tool found)", "cmd": "the command line (default: worked out)",
                 "modules": "modules, separated by spaces", "parse": "the log reader"}, optional=("role", "cmd", "modules", "parse"))
    def tool_add(ops, path: str, role: str = "", cmd: str = "", modules: str = "", parse: str = "") -> str:
        if role.strip() and cmd.strip():
            return ops._log("tools", common.set_role(ops.project, role, cmd, modules, "", parse, ""))
        found = common.identify_tool(ops, path)
        if role.strip():  # one role asked for: that tool only
            found = [g for g in found if g["role"] == common.slug(role)] or found[:1]
        msgs = []
        for g in found:
            r, c, m, pa = (role or g["role"]) if len(found) == 1 else g["role"], cmd or g["cmd"], modules or " ".join(g["modules"]), parse or g["parse"]
            note = (" — " + g["notes"] if g["notes"] else "") + "".join(f" (! {w})" for w in g["warnings"])
            msgs.append(common.set_role(ops.project, r, c, m, "", pa, g["description"]) + note)
        return ops._log("tools", "\n".join(msgs))

    @api.action("tools_check", "Check which tool roles the flows use and whether each tool is found (loads the modules: takes a few seconds).")
    def tools_check(ops) -> str:
        rows = common.tool_rows(ops.project)
        common.save_tool_rows(ops.project, rows)
        missing = [r["role"] for r in rows if not r["found"]]
        return ops._log("tools", f"{len(rows) - len(missing)} of {len(rows)} tool role(s) found" + (f"; missing: {', '.join(missing)}" if missing else ""))

    @api.action("tools_modules", "Set the modules loaded (`module load`) before every tool: names separated by spaces or commas; empty clears them. Saved to q3tui.yaml.",
                {"modules": "e.g. synopsys/syn/V-2023.12 oss-cad-suite"}, optional=("modules",))
    def tools_modules(ops, modules: str = "") -> str:
        names = [m for m in modules.replace(",", " ").split() if m]
        ops.project.cfg.tools.modules = names
        ops.project.save_config_values({"tools": {"modules": names}})
        return ops._log("tools", f"modules loaded before every tool: {', '.join(names) or 'none'} (saved to q3tui.yaml)")
