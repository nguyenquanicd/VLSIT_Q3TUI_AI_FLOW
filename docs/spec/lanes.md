# Lanes: RTL Tags, Fix Copies, Convergence

**Status: implemented** (`core/lanes.py`; tests: `tests/test_lanes.py`). The checking flows (DV, synthesis, sanity) run at the same time on one frozen
design, each failure is fixed in a folder of its own, the fixes are merged into the next design version and every flow checks again on it, until all
pass on the same version.

## Why

A project folder had one run at a time: one run lock (`.q3tui/run.lock`) and one state file (`.q3tui/pipeline.json`) for every flow, written whole by
whoever saved last. The checking flows are independent in their own steps, but a DV run waited for an FPGA run and the other way round (now: one
run per flow, pipeline.md "Runs of several flows"). A fix also lands in the one folder the others read: when the dv flow's triage has the rtl flow change `src/rtl` (pipeline.md "Fixes owned by another
flow"), a synthesis that is reading it sees half of a fix, and no result says which RTL it was for.

## The model

Every lane runs in a folder of its own, so nothing is shared while the lanes run. That folder **links** to the tag and **copies only what the lane
changes**: a lane that only checks copies nothing of the design. No git is needed.

```text
                         tag v1 (a frozen copy: spec + config + RTL)
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
          dv lane            fpga lane           sanity lane          each in its own folder over v1, at the same time
           FAIL                  FAIL                 PASS
              │                    │                    │
        fix in its folder    fix in its folder          │                the lane's own fix loop, in its folder only (triage →
   (spec / RTL → RTL gen  (spec / RTL → RTL gen         │                spec / RTL change → RTL gen → the lane again)
    → DV again: PASS)      → synthesis again: PASS)     │                until it passes
              └────────────────────┼────────────────────┘
                                   ▼
            merge: the lanes' change requests replayed in your project folder → tag v2
                                   ▼
            every lane again on v2 (a lane whose inputs v2 did not change keeps its result)
                                   ▼
            convergence gate: all lanes pass on the same tag → done; else the next round from v2
```

| Term | What it is |
|---|---|
| **tag** | A frozen design version: a read-only copy of the design files (`.q3tui/tags/<name>/files/`) with the engine state of the steps that made it. |
| **lane** | A checking flow run against a tag, in a project folder of its own that links to the tag (`lanes.flows` in `q3tui.yaml`, default `dv`, `fpga`, `sanity`). |
| **fix copy** | A failing lane's folder once its fix loop changes the spec or the RTL: those folders are copied from the tag first (the "fix branch" of the plan's diagram). |
| **merge** | The lanes' changes applied together in your project folder, then the next tag. |
| **round** | Lanes on a tag, merge. |
| **convergence gate** | All lanes pass on one tag. |

## Commands

| Command | What it does |
|---|---|
| `q3tui tag [NAME] [--force]` | Freeze the design: `.q3tui/tags/<name>/` (`v<N>` by default). |
| `q3tui lanes run [--tag T] [--lanes a,b]` | The lanes on a tag (default the latest) at the same time; each fixes in its own folder. |
| `q3tui lanes status` | The tags and each lane's result on them. |
| `q3tui lanes merge [--tag T] [--lanes a,b] [--yes]` | Take the lanes' fixes into the project folder, run the design flows, make the next tag (asks first unless `--yes`). |
| `q3tui lanes converge [--lanes a,b] [--max-rounds N] [--max-cost USD] [--yes]` | run → merge → run … until every lane passes on one tag. |
| `q3tui lanes clean [--tag T]` | Delete the lane folders; the tags, their results and what each lane changed stay. |

TUI: `L` opens the Lanes screen (tags × lanes: status, cost, why; refreshed from the files every 2 s): `t` tags the design, `r` runs the lanes, `m`
merges their fixes, `c` converges; run / merge / converge go to the background as `q3tui lanes …` (one background process at a time, as a run; `x`
stops it). The assistant has `tag_design`, `lanes_status` and `run_lanes` (`Ops.tag`, `Ops.lanes_status`, `Ops.lanes_merge_preview`).

Configuration (`lanes:` in `q3tui.yaml`, config.md): `flows` (the lanes), `design_flows` (whose steps make the design a tag freezes: default `rtl`,
which includes spec → parse → config), `dir` (where the lane folders go), `max_rounds`, `max_cost_usd`.

## RTL tags

- `q3tui tag` copies the design files once the design flows' steps are done and approved (a step this project cannot run, `unavailable`, does not
  count). It refuses a tag on a stale or failed design step (`--force` tags anyway and records why), while a run is going on in the project folder,
  and when `config/tools.json` or `q3tui.yaml` names the project folder by its absolute path (a lane would read the wrong files: use `{project}`).
- Copied: `spec/`, `config/`, `constraints/`, `schemas/`, `src/`, `reviews/`, `flows/`, `prompts/`, `q3tui.yaml` (the `project.*` folder names). Not
  copied: `build/`, `reports/`, `.q3tui/` (3.4 MB for rv32iv). The tag's files are made read-only; `pipeline.json` (the whole engine state) is copied
  beside them, so a lane starts with the design steps done and never runs them again unless it changes them.
- A tag is never changed. `tag.json` says when, from which tag, how big, and what a `--force` overrode.

## Lanes

- A lane folder is `<project>.lanes/<tag>/<lane>/`, **beside** the project folder (`lanes.dir` moves it): inside `.q3tui/`, whose `.ignore` (`*`)
  ripgrep honours from parent folders, the agents' searches in a lane would find nothing. It is a project folder of its own: its own
  `.q3tui/pipeline.json` (from the tag), lock, run logs and build output, and `.q3tui/lane.json` (which tag, which lane).
- **Copy on write, by hard links.** The bulk of the design (`spec/`, `src/`) is hard-linked to the tag's read-only files; the small folders
  (`config/`, `constraints/`, `schemas/`, `reviews/`, `flows/`, `q3tui.yaml`) are copied. Just before a step runs, the engine makes the folders of its
  outputs real copies (`lanes.before_step` → `materialize`: each file still linked gets its own copy). A write that was not declared hits a read-only
  file and fails loudly; it never changes the tag. Hard links, not symlinks: paths stay inside the lane folder (`Project.rel`, the agents' path guard
  resolve paths). Another file system: plain copies.
- `run_lanes` runs every lane at the same time in one process (anyio tasks, each lane its own engine, state and lock); a lane's flow runs
  unattended (its gates approved), with its own fix loop: the dv triage and the FPGA PPA hand-off run the owner flow inside the run
  (`Engine.hand_to_flow`), the Sanity flow (a step with `pending_handoff`) goes through `q3tui loop`. One lane's crash is that lane's result.
- A lane's result (`.q3tui/tags/<tag>/results/<lane>.json`): `passed` (the flow completed and the lane changed nothing: it passes on the tag),
  `fixed` (completed after changes in its folder), `failed`, `needs_you` (a blocking question is open: answer it in the lane folder,
  `q3tui -C <folder> answer …` or a TUI there, then `q3tui lanes run --tag <tag>`: an existing lane folder goes on), with the cost, the failing step
  and the counts of what it changed. The licenses are the limit of what runs at once (`tb.sim_parallel`, the synthesis jobs: per lane).

## A run that forks

You do not have to tag and start lanes to keep the checking flows apart: `r` in dv, fpga and sanity runs them in the project folder at the
same time, and the one whose run is about to change the design forks into a lane by itself (`lanes.fork`, default on; `Engine.run_step` →
`ForkToLane` → `fork_run`):

- What forks: a run of a checking flow (`lanes.flows`) that reaches a design step (the steps of `lanes.design_flows`) with change requests
  or decisions queued — its own fix loop's (the dv triage's RTL bug, the FPGA PPA hand-off and `fpga_port`'s RTL orders run the owner flow
  inside the run) or a decision for the spec. A design step that is out of date for another reason (you edited the spec) runs in place,
  as before. The flow runs inside the checking run (`RUN_FLOW`, the flow of the outermost run) raise the fork; the checking run catches it.
- The fork: the design as it is becomes a tag (made while the other runs go on: they never change the design; `tag.json: fork`), the flow
  gets its lane folder over it, the queued requests to the design steps move there (recorded in its `lane_changes.jsonl`, removed from the
  project folder), the design flows apply them in the copy, and the flow runs again there (`run_lane`: unattended, its fix loop goes on in
  the copy; its result is the lane's).
- Back: a fork that fixed something merges by itself when the run is unattended (auto-answer / auto-approve / `--yes`) and
  `lanes.auto_merge` is on, once no other run is going on in the project folder (its lock says `merge_wait` meanwhile; forks waiting to merge
  go one at a time, `merge.lock`); else it says so and its fixes wait for `q3tui lanes merge` (the Lanes screen's `m`). After a merge the
  other flows' results are out of date for the new design: run them again (`r`), or `converge`.

## What a lane changes

- A change request to a design step (`lanes.design_steps`: the steps of the design flows) from the lane's own work or from you is recorded in the
  lane's `.q3tui/lane_changes.jsonl` (`request`: target step, the exact change request, origin step) — the decisions given to `spec`, the scoped
  requests to `rtl` (`[rtl:m_soc_csr] …`). The engine knows who made it: the running step (`lanes.CURRENT_STEP`, set by `run_step`) or the step a
  question belongs to (`push_decision`). The design steps' requests to each other are not recorded: the merge's own run makes them again.
- An own step (not a design step) that ran with change requests is recorded too (`own`): a fix of the lane's own files (the dv lane's `src/tb/`).

## Merge

- `merge` replays the recorded change requests of the lanes that fixed something (or of `--lanes`) **in your project folder**: they are queued on
  their owning steps, then the design flows run (`yes=True`) and apply them as updates (pipeline.md "Changes propagate as updates") to what is there:
  the tag, plus anything you changed since. Not by copying files back: two fixes to one module copied over each other keep only the last, and
  AI-written RTL can combine cleanly as text and still be wrong; a replay makes the owning step apply both fixes knowing both.
- The lanes' own steps are taken as the lane left them (their output files, their questions files and their engine records: status, hashes,
  `step_meta`, gates), from the lanes that fixed something **and the lanes that passed**: a result the next tag does not make stale is kept, the lane
  does not run it again. A step two lanes left different (e.g. one SDC step in two flows) is a conflict: the merge refuses it until you name the lanes
  to take (`--lanes`; then only those lanes' own steps are taken).
- A unit both lanes changed (`rtl:m_x`, or the step) is reported as an overlap: both requests go to the owning step, which sees both.
- The merge asks first (`q3tui lanes merge`, the TUI's `m`), unless `--yes`. A design flow that does not complete stops the merge before the next
  tag. Then the next tag is made from the project folder.

## Re-validation and the convergence gate

- `converge` tags the project folder when it has no tag or changed since the latest one, then: lanes on the tag; every lane `passed` → the tag is
  converged (`.q3tui/tags/<tag>/converged`); else the fixes are merged into the next tag and the lanes run on it, the ones that passed included (a fix
  for one lane can break another; a lane whose inputs the new tag did not change keeps its result by content hash).
- It stops at the gate, when a round has no fewer lanes failing than the best one (`no improvement`: the best tag is said), at `--max-rounds`
  (default `lanes.max_rounds`, else `pipeline.max_fix_iterations`) or `--max-cost`, when no lane fixed anything (one waits for you, or nothing could
  be fixed), or before a merge when you did not let it merge (`--yes`, or the question at the start).
- Disk: a lane folder holds its own build output (the dv lane's simulator build: ~240 MB on rv32iv) and the design folders it changed; the rest are
  hard links. `q3tui lanes clean` deletes the folders and keeps each lane's `lane_changes.jsonl` with the tag (`.q3tui/tags/<tag>/changes/`).

## Relation to what exists

| Before | With lanes |
|---|---|
| one run lock and one `pipeline.json` per project | one per lane folder; your project folder keeps its own |
| `Engine.hand_to_flow`: the owner flow runs inside the run, in the one folder | the same, inside a lane folder |
| `q3tui loop` (sanity: analysis → hand-off → owner flow, bounded, best RTL kept in `.q3tui/loop/best_rtl/`) | the Sanity lane's fix loop |
| results tied to the RTL by content hash only | results recorded per tag |
| the TUI follows one run | the Lanes screen (`L`): tag × lane |

## Open points

- What a lane is when a project has both `fpga` and `asic`: today, two lanes if `lanes.flows` lists both.
- Whether a lane may answer a spec question on its own in an unattended run: today the project's `pipeline.auto_answer` applies in the lane too; a
  decision made in one lane binds the others at the merge.
- Licenses: a cap per tool across lanes, beyond each lane's own setting; lanes started in separate processes (today: one process, anyio tasks).
- A lane's per-check counts (tests failed, slack, violations open) in its result, and a finer "better" for `converge` than the number of lanes
  failing.
