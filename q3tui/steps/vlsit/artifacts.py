"""The VLSIT JSON artifacts: locations, reading / writing, validation against the original JSON Schemas.

| artifact | schema | written by |
|---|---|---|
| schemas/structured_spec.json | structured_spec_schema.json | parse (Gate 1) |
| schemas/final_config.json | final_config_schema.json | config (Gate 2) |
| schemas/synth_report.json | (none) | rtl |
| schemas/selected_testplan.json | (inline: TESTPLAN_SCHEMA) | tb (Gate 4) |
| schemas/rtm.json | rtm_schema.json | sva (Gate 3b), verify (Gate 5) |
| schemas/verification_report.json | verification_report_schema.json | verify |
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from q3tui.core.project import Project, write_json

SCHEMA_DIRS = sorted((Path(__file__).parents[2] / "flows" / "vlsit").glob("*/schemas"))  # each step's schemas/

NAMES = {  # artifact file -> schema file (None: not validated against a file)
    "structured_spec.json": "structured_spec_schema.json",
    "final_config.json": "final_config_schema.json",
    "rtm.json": "rtm_schema.json",
    "verification_report.json": "verification_report_schema.json",
    "selected_testplan.json": None,
    "synth_report.json": None,
}

# selected_testplan.json (tb_generator.md "Schema output"): written by tb, read by sva / verify
TESTPLAN_SCHEMA = {
    "type": "object",
    "required": ["metadata", "test_cases"],
    "properties": {
        "metadata": {"type": "object", "required": ["ip_name", "gate_4_approved"],
                     "properties": {"ip_name": {"type": "string"}, "gate_4_approved": {"type": "boolean"},
                                    "gate_4_at": {"type": ["string", "null"]}}},
        "test_cases": {"type": "array", "items": {
            "type": "object", "required": ["tc_id", "req_ids"],
            "properties": {"tc_id": {"type": "string"}, "title": {"type": "string"},
                           "req_ids": {"type": "array", "items": {"type": "string"}},
                           "selected": {"type": "boolean"}, "file": {"type": "string"}}}},
    },
}


def path(project: Project, name: str) -> Path:
    return project.schemas_dir / name


def schema_for(name: str) -> dict | None:
    if name == "selected_testplan.json":
        return TESTPLAN_SCHEMA
    fname = NAMES.get(name)
    schema = json.loads(next(d / fname for d in SCHEMA_DIRS if (d / fname).is_file()).read_text()) if fname else None
    if name == "final_config.json" and schema:  # the original pins each constraint's rule to one design's (rv32im) text
        for c in schema["properties"].get("constraints", {}).get("properties", {}).values():
            c.get("properties", {}).get("rule", {}).pop("const", None)
    return schema


def validate(name: str, data: dict) -> list[str]:
    """Problems of `data` against the artifact's JSON Schema (empty: valid)."""
    import jsonschema

    schema = schema_for(name)
    if schema is None:
        return []
    validator = jsonschema.Draft7Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))]


def read(project: Project, name: str, default=None):
    p = path(project, name)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return default


def write(project: Project, name: str, data: dict, check: bool = True) -> Path:
    """Write an artifact (validated unless `check=False`; raises ValueError listing the problems)."""
    problems = validate(name, data) if check else []
    if problems:
        raise ValueError(f"{name} does not match its schema: " + "; ".join(problems[:8]))
    return write_json(path(project, name), data)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_gate(project: Project, name: str, updates: dict[str, object], section: str = "metadata") -> bool:
    """Record a gate decision in an artifact's metadata (gate_status / gate_N_approved / gate_N_at…); True when written."""
    data = read(project, name)
    if not isinstance(data, dict):
        return False
    data.setdefault(section, {}).update(updates)
    write_json(path(project, name), data)
    return True
