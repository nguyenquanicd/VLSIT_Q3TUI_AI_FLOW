"""Status icons, in three sets (setting `tui.icons`), so every terminal font can show them.

- unicode: dingbats/geometric symbols (need a font with ✔ ⚑ ◐ …, e.g. DejaVu Sans Mono)
- nerd:    Nerd Font icons (private-use code points; use a "… Nerd Font Mono" font)
- ascii:   plain characters, work everywhere
"""

from __future__ import annotations

SETS: dict[str, dict[str, str]] = {
    "unicode": {
        "done": "✔", "approved": "✔", "review": "⚑", "stale": "!", "user": "★", "pending": "○", "running": "◐",
        "failed": "✖", "missing": "?", "planned": "–", "edited": "✎", "change": "↻", "blocking": "⚑", "open": "○",
        "answered": "✔", "pass": "✔", "fail": "✖", "arrow": "▸", "unverified": "○", "na": "–",
    },
    "nerd": {  # Font Awesome glyphs of Nerd Fonts v3
        "done": "", "approved": "", "review": "", "stale": "", "user": "", "pending": "",
        "running": "", "failed": "", "missing": "", "planned": "", "edited": "", "change": "",
        "blocking": "", "open": "", "answered": "", "pass": "", "fail": "", "arrow": "",
        "unverified": "", "na": "\uf068",
    },
    "ascii": {
        "done": "+", "approved": "+", "review": "#", "stale": "!", "user": "*", "pending": ".", "running": "~",
        "failed": "x", "missing": "?", "planned": "-", "edited": "e", "change": "@", "blocking": "#", "open": "o",
        "answered": "+", "pass": "+", "fail": "x", "arrow": ">", "unverified": "o", "na": "-",
    },
}
STYLES = tuple(SETS)
_current = dict(SETS["unicode"])


def set_style(name: str) -> None:
    _current.clear()
    _current.update(SETS.get(name, SETS["unicode"]))


def icon(name: str) -> str:
    return _current.get(name, "?")
