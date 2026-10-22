"""What the Sanity flow already tried: every RTL / spec / SDC request handed off for a violation, kept in `.q3tui/sanity/handoffs.json`
(code-owned memory, not a chat session). The waiver review is told what was tried for each violation that is still open, and a request
that was already sent is not sent again: the violation becomes a question for you, with what was tried."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from q3tui.core.project import Project, read_json, write_json

MAX_TRIES = 2  # RTL / spec requests per violation before the review must ask you instead of proposing another
_REQ = re.compile(r"\bREQ-[A-Z0-9][A-Z0-9-]*")
_CONFLICT = re.compile(r"conflict|contradict|violat|against|forbid|disagree", re.I)


def path(project: Project) -> Path:
    return project.state_dir / "sanity" / "handoffs.json"


def text_key(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:12]


def attempts(project: Project, sig: str) -> list[dict]:
    return (read_json(path(project), {}) or {}).get(sig, [])


def record(project: Project, sig: str, kind: str, text: str, module: str = "") -> None:
    data = read_json(path(project), {}) or {}
    data.setdefault(sig, []).append({"kind": kind, "module": module, "text": text, "key": text_key(text),
                                     "at": datetime.now().isoformat(timespec="seconds")})
    write_json(path(project), data)


def tried_lines(project: Project, sig: str) -> list[str]:
    """The prompt lines for a violation that was handed off before and is still open."""
    tries = attempts(project, sig)
    if not tries:
        return []
    out = [f"  Already tried for this violation ({len(tries)} time(s)) and it is still open — do not propose the same again; "
           "choose a different fix, a spec decision, or ask:"]
    out += [f"  - {t['kind']}{' in ' + t['module'] if t.get('module') else ''}: {t['text'][:220]}" for t in tries[-3:]]
    return out


def repeated(project: Project, sig: str, kind: str, text: str) -> list[dict]:
    """The earlier attempts when this request must not go out again: the same text was sent, or MAX_TRIES requests of RTL / spec were."""
    tries = [t for t in attempts(project, sig) if t["kind"] in ("fix_rtl", "spec")]
    if kind not in ("fix_rtl", "spec"):
        return []
    if any(t["key"] == text_key(text) for t in tries) or len(tries) >= MAX_TRIES:
        return tries
    return []


def conflicts_with_requirement(reason: str, change: str) -> bool:
    """A fix that its own text says goes against a requirement is a decision about the spec, not an RTL change."""
    text = f"{reason} {change}"
    return bool(_REQ.search(text)) and bool(_CONFLICT.search(text))
