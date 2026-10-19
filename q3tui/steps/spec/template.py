"""Spec template: the sections (and key fields) every generated spec must contain."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from q3tui.core.config import q3tui_home

DEFAULT_TEMPLATE = Path(__file__).with_name("default_template.yaml")
# built-in templates a flow can pick with the spec step's `options: {template: <name>}` (flows/*.yaml)
BUILTIN_TEMPLATES = {"default": DEFAULT_TEMPLATE, "vlsit": Path(__file__).parents[2] / "flows" / "vlsit" / "spec" / "vlsit_template.yaml"}
USER_TEMPLATE_NAME = "spec_template.yaml"
PROJECT_TEMPLATE_NAME = "template.yaml"  # inside spec/


class TemplateField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    guidance: str = ""
    required: bool = True


class TemplateSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    required: bool = True
    guidance: str = ""
    fields: list[TemplateField] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"section id '{v}' must be snake_case")
        return v


class SpecTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_extra_sections: bool = True
    sections: list[TemplateSection]

    @field_validator("sections")
    @classmethod
    def _unique(cls, v: list[TemplateSection]) -> list[TemplateSection]:
        ids = [s.id for s in v]
        dups = {i for i in ids if ids.count(i) > 1}
        if dups:
            raise ValueError(f"duplicate section ids: {', '.join(sorted(dups))}")
        if not v:
            raise ValueError("template has no sections")
        return v

    def by_id(self) -> dict[str, TemplateSection]:
        return {s.id: s for s in self.sections}


def template_candidates(spec_dir: Path, builtin: str | None = None) -> list[Path]:
    """The project's own, then the user's, then the built-in one (`builtin`: the flow's choice, default: Q3TUI's)."""
    if builtin and builtin not in BUILTIN_TEMPLATES:
        raise ValueError(f"unknown spec template '{builtin}' (built-in: {', '.join(BUILTIN_TEMPLATES)})")
    user = [q3tui_home() / USER_TEMPLATE_NAME] if not builtin or builtin == "default" else []
    return [spec_dir / PROJECT_TEMPLATE_NAME, *user, BUILTIN_TEMPLATES.get(builtin or "default", DEFAULT_TEMPLATE)]


def find_template(spec_dir: Path, builtin: str | None = None) -> Path:
    return next(p for p in template_candidates(spec_dir, builtin) if p.is_file())


_TEMPLATE_CACHE: dict[tuple[str, int], SpecTemplate] = {}


def load_template(spec_dir: Path, builtin: str | None = None) -> tuple[SpecTemplate, Path]:
    """Parsed template (cached by path + mtime). Returns a copy: callers may edit it."""
    path = find_template(spec_dir, builtin)
    key = (str(path), path.stat().st_mtime_ns)
    cached = _TEMPLATE_CACHE.get(key)
    if cached is None:
        try:
            cached = SpecTemplate.model_validate(yaml.safe_load(path.read_text()) or {})
        except Exception as exc:  # noqa: BLE001 - surfaced with the file name
            raise ValueError(f"invalid spec template {path}: {exc}") from exc
        if len(_TEMPLATE_CACHE) > 32:
            _TEMPLATE_CACHE.clear()
        _TEMPLATE_CACHE[key] = cached
    return cached.model_copy(deep=True), path


def describe(template: SpecTemplate) -> str:
    """Template as prompt text."""
    lines = []
    for i, s in enumerate(template.sections, 1):
        req = "required" if s.required else "optional — include only if it applies"
        lines.append(f"{i}. id={s.id} · \"{s.title}\" ({req})")
        if s.guidance:
            lines.append(f"   {' '.join(s.guidance.split())}")
        for f in s.fields:
            lines.append(f"   field \"{f.name}\"{'' if f.required else ' (optional)'}: {f.guidance}")
    extra = ("You may append further sections (with new snake_case ids) after these if the design needs them."
             if template.allow_extra_sections else "Do not add sections that are not listed.")
    return "\n".join(lines) + "\n" + extra


def intent_skeleton(template: SpecTemplate) -> str:
    """spec/intent.md starter: the template's sections as prompts for the user."""
    out = [
        "# Intent",
        "",
        "<!-- Describe the block in your own words. Fill in what you know and delete the rest;",
        "     anything left out is decided by the spec writer and listed as an open question. -->",
        "",
    ]
    for s in template.sections:
        out += [f"## {s.title}", "", f"<!-- {' '.join(s.guidance.split())} -->"]
        out += [f"- {f.name}: " for f in s.fields]
        out.append("")
    return "\n".join(out)


def save_project_template(spec_dir: Path, template: SpecTemplate) -> Path:
    """Write the template to <project>/spec/template.yaml (the project's own copy)."""
    path = spec_dir / PROJECT_TEMPLATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    data = template.model_dump(exclude_defaults=False)
    for sec in data["sections"]:
        if not sec["fields"]:
            del sec["fields"]
        for f in sec.get("fields", []):
            if f.get("required", True):
                f.pop("required", None)
    header = ("# Q3TUI spec template for this project (edit freely; see the built-in default for comments).\n"
              "# Sections are written in this order; required: false = only when it applies.\n")
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, width=100))
    return path


def ensure_project_template(spec_dir: Path, builtin: str | None = None) -> Path:
    """Copy the effective template (the flow's built-in one, or the default) into the project if it has none yet."""
    path = spec_dir / PROJECT_TEMPLATE_NAME
    if not path.is_file():
        src = find_template(spec_dir, builtin)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src.read_text())
    return path
