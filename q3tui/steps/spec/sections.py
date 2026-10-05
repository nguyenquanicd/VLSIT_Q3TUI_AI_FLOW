"""Per-section status of spec.md: reviewed / not reviewed / edited / TBD / change requested / missing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from q3tui.core.icons import icon as I
from q3tui.steps.spec.document import TBD, parse_sections
from q3tui.steps.spec.template import load_template


def section_hash(body: str) -> str:
    return hashlib.sha256(" ".join(body.split()).encode()).hexdigest()


_SECTION_HASHES: dict[str, str] = {}


def _hash_cached(body: str) -> str:
    h = _SECTION_HASHES.get(body)
    if h is None:
        if len(_SECTION_HASHES) > 512:
            _SECTION_HASHES.clear()
        h = _SECTION_HASHES[body] = section_hash(body)
    return h


_PARSE_CACHE: dict[tuple[str, int, int], dict[str, tuple[str, str]]] = {}


def current_sections(engine) -> dict[str, tuple[str, str]]:
    """Sections of spec/spec.md (cached by mtime/size)."""
    path = engine.project.spec_dir / "spec.md"
    if not path.is_file():
        return {}
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _PARSE_CACHE:
        if len(_PARSE_CACHE) > 16:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[key] = {sid: (title, body) for sid, title, body in parse_sections(path.read_text())}
    return dict(_PARSE_CACHE[key])


def record_generated(engine) -> None:
    """After the spec step writes spec.md: remember each section's generated text.
    A section keeps its review if its text is unchanged since it was reviewed."""
    old = engine.state.sections.get("spec", {})
    new = {}
    for sid, (_, body) in current_sections(engine).items():
        h = section_hash(body)
        reviewed = old.get(sid, {}).get("reviewed")
        new[sid] = {"generated": h, "reviewed": reviewed if reviewed == h else None}
    engine.state.sections["spec"] = new


def mark_reviewed(engine, sid: str | None = None, reviewed: bool = True) -> None:
    """Mark one section (or all when sid is None) as reviewed at its current text."""
    track = engine.state.sections.setdefault("spec", {})
    for s_id, (_, body) in current_sections(engine).items():
        if sid is not None and s_id != sid:
            continue
        entry = track.setdefault(s_id, {"generated": None, "reviewed": None})
        entry["reviewed"] = section_hash(body) if reviewed else None
    engine.save()


@dataclass
class SectionState:
    id: str
    title: str
    required: bool | None  # None = not in template
    in_spec: bool
    reviewed: bool = False
    edited: bool = False
    tbd: bool = False
    change_requested: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def label(self) -> tuple[str, str]:
        """(text, style) for the status column."""
        if not self.in_spec:
            return f"{I('pending')} missing", "grey50"
        parts, style = [], "yellow"
        if self.required is None:
            parts.append("+ extra")
            style = "cyan"
        if self.edited:
            parts.append(f"{I('edited')} edited")
            style = "cyan"
        if self.reviewed:
            parts.insert(0, f"{I('approved')} reviewed")
            style = "green"
        elif not self.edited:
            parts.insert(0, f"{I('review')} not reviewed")
        if self.tbd:
            parts.append("? TBD")
            style = "yellow" if style != "green" else "yellow"
        if self.change_requested:
            parts.append(f"{I('change')} change pending")
            style = "magenta"
        return " ".join(parts), style


def section_states(engine) -> list[SectionState]:
    template, _ = load_template(engine.project.spec_dir, engine.spec_template())
    written = current_sections(engine)
    track = engine.state.sections.get("spec", {})
    user_spec = (engine.state.steps.get("spec") and engine.state.steps["spec"].origin == "user")
    pending = " ".join(engine.state.feedback.get("spec", [])).lower()

    def build(sid: str, title: str, required: bool | None) -> SectionState:
        st = SectionState(sid, title, required, sid in written)
        if st.in_spec:
            body = written[sid][1]
            h = _hash_cached(body)
            entry = track.get(sid, {})
            st.reviewed = entry.get("reviewed") == h or bool(user_spec)
            generated = entry.get("generated")
            # changed since the spec step wrote it, or added afterwards (by hand / the assistant)
            st.edited = generated not in (None, h) or (generated is None and bool(track))
            st.tbd = TBD in body
        st.change_requested = f"in section '{title.lower()}'" in pending
        return st

    out = [build(s.id, s.title, s.required) for s in template.sections]
    known = template.by_id()
    out += [build(sid, title, None) for sid, (title, _) in written.items() if sid not in known]
    return out
