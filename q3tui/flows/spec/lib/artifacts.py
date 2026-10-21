"""The spec flow's JSON artifacts: locations, reading / writing, validation against the original JSON Schemas.

| artifact | schema | written by |
|---|---|---|
| schemas/structured_spec.json | structured_spec_schema.json | parse (Gate 1) |
| schemas/final_config.json | final_config_schema.json | config (Gate 2) |
| schemas/synth_report.json | (none) | rtl (rtl flow) |
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from q3tui.core.project import Project, write_json

SCHEMA_DIRS = sorted((Path(__file__).parents[1]).glob("*/schemas"))  # each step's schemas/

NAMES = {  # artifact file -> schema file (None: not validated against a file)
    "structured_spec.json": "structured_spec_schema.json",
    "final_config.json": "final_config_schema.json",
    "synth_report.json": None,
}

def path(project: Project, name: str) -> Path:
    return project.schemas_dir / name


def schema_for(name: str) -> dict | None:
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
