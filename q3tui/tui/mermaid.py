"""Mermaid diagrams as terminal art for the TUI's Markdown view (flowchart, sequence, state … via `termaid`).

Imported lazily and never fatal: with `termaid` missing, or a diagram it cannot parse, the caller shows the Mermaid
source as it is.
"""

from __future__ import annotations

from functools import lru_cache

LANGS = ("mermaid", "mmd")


@lru_cache(maxsize=64)
def render(source: str) -> str | None:
    """The diagram as Unicode box-drawing text, or None when it cannot be rendered."""
    try:
        from termaid import render as termaid_render

        art = termaid_render(source.strip("\n"))
    except Exception:  # noqa: BLE001 - unsupported diagram type / syntax / library missing: show the source instead
        return None
    return art.rstrip("\n") if art and art.strip() else None
