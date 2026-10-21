# The project settings flow (`project`) — the project, its tools, its libraries

A built-in folder flow (`q3tui/flows/project/`, `q3tui --flow project`, **F** → "Project settings" at the bottom of the flow map, `/flow project`)
for the settings of the project itself, as views you work in instead of files you edit. No LLM, no gates: the steps' runs only check and report
(`.q3tui/project/<step>.json`); every change is made in a view (or by an action: the assistant and `q3tui act` call the same) and saved to the
project's own files — `q3tui.yaml`, `config/pdks.json`. The design flows (spec, rtl, fpga, asic) are untouched.

| Step | View | Does |
|---|---|---|
| `project` | **Settings**: the project root, the config file, the start-up flow, then the settings of the Settings dialog's General and LLM tabs (auto-approve, auto-answer, model, effort, budgets, icons, language, …) | Enter / `e` edits the highlighted setting in place and saves it to `q3tui.yaml` (`Ops.apply_settings`) |
| `tools` | **Tools**: every tool role the flows know (Vivado, Design Compiler, Formality, Yosys, nextpnr, the lint / sim roles of the rtl flow) — tool, found ✔ / ✘, modules, where the role comes from (the project's tools.json, `tools.roles`, a flow's default, the preset) | `a` add a tool (give its path; the rest is filled in), Enter / `e` edits the role (command, modules, time limit), `c` checks which tools are found (loads the modules: a few seconds, in a thread), `m` sets the modules loaded before every tool (`tools.modules`) |
| `libraries` | **Libraries**: the technologies of the ASIC flow's catalog (built-in open-source + the project's `config/pdks.json`), their corners, which corner is in use | `a` add (give the folder; the rest is filled in), Enter / `e` edit, `d` remove, `u` pick the corner used as the ASIC library |

Enter edits the highlighted row in all three views, as in the other flows' views.

## Adding a library (`libraries`, `a`)

A technology is a folder of Design Compiler `.db` files, one per corner. **You give only the folder.** A read-only LLM stage (`lib_identify`; the
prompt is the `<!-- SYSTEM -->` section of `libraries/md/skill.md`; tools `Read`, `Grep`, `Glob`, confined to that folder) is handed what code found
(the folders with `.db` files, their file names and corner labels — ground truth) and reads what the folder says about itself (a README, the file names, the
`.lib` text next to a `.db`: `library (…)`, `nom_voltage`, `nom_temperature`). It proposes the id, the name, the node, the foundry, which of the files are
the cell corners (`include`: it leaves out `tech.db`, I/O, PLL, SRAM, level shifters, power-gating variants), the typical default corner, the estimate's
probe period and a note. **Code checks every claim**: the glob must be one the scan found, the `include` must compile and match files, the default corner
must be one of the corners; what fails is dropped and shown as a warning. The form then opens **filled in** for you to review (id, name, node, default corner
picked from the corners the files have, probe, include, *use as the ASIC library*); Scan lists what the folder holds. If the LLM cannot run (no credentials,
no `.db`), the form opens empty to fill in by hand. Saving writes `<project>/config/pdks.json` (a backup of the old file goes to `.q3tui/bkp/`); the same id replaces
the entry, and for a built-in technology it overrides it in this project. When the files are in several folders and the one given holds none directly,
nothing is written and the folders are listed (name one). The built-in catalog (`q3tui/flows/asic/pdks.json`) lists open-source PDKs only; licensed ones are
the project's.

**Corners.** Enter edits the technology: its *default corner* is a list of the corners its files have (the label is the file name without what the files share:
`tt_025C_5v00`), and *use as the ASIC library* sets that corner as `asic_synth.library` (saved to `q3tui.yaml`: one choice for the ASIC and FPGA flows). `u`
picks any other corner of the highlighted technology as the library without changing the technology's default; the corner in use is preselected and shown in the
*In use* column. The clock edit dialog of the SDC tab (either flow) selects technology and corner too.

## Adding a tool (`tools`, `a`)

**You give only the path** — a binary or its install folder. Code finds the executables (the `bin` folders, four levels down), the roles that exist (each with the
command a flow's step runs and the placeholders it passes), the log readers and the environment modules (`module avail`). A read-only LLM stage (`tool_identify`;
the prompt is the `<!-- SYSTEM -->` section of `tools/md/skill.md`; `Read`, `Grep`, `Glob`, confined to the tool's folder; it runs nothing) works out which **role**
the tool serves — an existing one (another simulator → `sim`) or a new name — the command with that role's placeholders, the log reader, the modules and a note.
**Code checks it**: the command's executable must be one that was found, a placeholder the role's steps do not pass is flagged, a log reader or a module that does not
exist is dropped, and a role that reads its log with its own patterns (`asic_lec`) keeps them when the stage says `generic`. The form opens filled in
(role, command, modules, log reader); saving writes the role like Enter does. Without an LLM the form opens empty to fill in by hand. On the real Formality install
folder it answered `asic_lec`, `fm_shell -f {script}` and the module `synopsys/formality/V-2023.12`.

## Editing a tool role (`tools`, Enter)

The form changes the role's command, the modules loaded for it (besides the global ones) and its time limit; the log reader and the description stay. It is saved where
the project keeps its roles: `config/tools.json` (created, from the roles the built-in preset gives, when the project has none — adding one role must not lose the others; a
flow's default role becomes the project's own once edited), else `tools.roles` of `q3tui.yaml`; a project that uses `tools.yaml` or a `tools.file` that is not JSON is
told to edit it by hand.

## Actions

`lib_scan folder=…`, `lib_identify folder=…` (what the LLM makes of it, nothing written), `lib_add folder=… [id title node_nm default probe_ns include]` (without an id the folder
is identified; what you give wins), `lib_remove id=…`, `lib_list`, `lib_use id=… [corner=…]`, `tool_identify path=…` (nothing written), `tool_add path=… [role cmd modules parse]` (without a role and a command the tool is
identified; what you give wins), `tool_set role=… [cmd modules timeout_s parse description]`, `tools_check`, `tools_modules modules="a b"`. Settings of the first view are also `set_settings` of the assistant (`pipeline.*`, `llm.*`, `tui.*`).

## What the runs report

`project`: the flow exists, the folders, which config files exist. `tools`: how many roles are found, which are missing (warnings, not a failed step).
`libraries`: technologies that match no file here, and a library in use that does not exist. A change of `q3tui.yaml`, `config/tools.json` or `config/pdks.json` makes
the step stale.

## Not built yet

Editing a tool role's log reader; the flow map's box is hard-coded like the others; per-user catalogs (`~/.q3tui/pdks.json`).
