"""Project directory: deliverable folders + .q3tui/ state (docs/spec/project-layout.md)."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from q3tui.core.config import CONFIG_FILENAME, Config, q3tui_home, load_config

SPEC_DOC_EXTS = {".md", ".pdf", ".txt", ".html", ".htm", ".rst", ".adoc", ".docx", ".odt", ".rtf"}
# Files in spec/ that Q3TUI manages and that are not spec documents.
SPEC_CONTROL_FILES = {"intent.md", "questions.md", "questions.json", "answers.json"}


_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def sha256_file(path: Path) -> str:
    """Content hash, cached by (path, mtime, size) — status checks run on every UI refresh."""
    st = path.stat()
    key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    if len(_HASH_CACHE) > 4096:
        _HASH_CACHE.clear()
    _HASH_CACHE[key] = h.hexdigest()
    return _HASH_CACHE[key]


def sha256_json(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def find_project_root(start: Path) -> Path:
    """Nearest ancestor that is a Q3TUI project; else `start` itself.

    A project has `.q3tui/pipeline.json` (it has been run) or a `q3tui.yaml`.
    `$Q3TUI_HOME` (default ~/.q3tui, the user config dir) is not a project marker.
    """
    start = start.resolve()
    home_cfg_dir = q3tui_home().resolve()
    for d in (start, *start.parents):
        state = d / ".q3tui"
        if (d / CONFIG_FILENAME).is_file() or (state.resolve() != home_cfg_dir and (state / "pipeline.json").is_file()):
            return d
    return start


@dataclass
class Project:
    root: Path
    cfg: Config

    @classmethod
    def open(cls, path: Path | None = None, config: Path | None = None, overrides: dict | None = None) -> "Project":
        root = find_project_root(path or Path.cwd())
        return cls(root, load_config(config, cwd=root, overrides=overrides))

    def _dir(self, name: str) -> Path:
        return self.root / getattr(self.cfg.project, name)

    @property
    def spec_dir(self) -> Path:
        return self._dir("spec_dir")

    @property
    def req_dir(self) -> Path:
        return self._dir("req_dir")

    @property
    def schemas_dir(self) -> Path:
        return self._dir("schemas_dir")

    @property
    def src_dir(self) -> Path:
        return self._dir("src_dir")

    @property
    def docs_dir(self) -> Path:
        return self._dir("docs_dir")

    @property
    def arch_dir(self) -> Path:
        return self._dir("arch_dir")

    @property
    def model_dir(self) -> Path:
        return self._dir("model_dir")

    @property
    def rtl_dir(self) -> Path:
        return self._dir("rtl_dir")

    @property
    def tb_dir(self) -> Path:
        return self._dir("tb_dir")

    @property
    def state_dir(self) -> Path:
        return self._dir("state_dir")

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    def flow_ref(self) -> str:
        """The flow this project uses (`pipeline.flow`)."""
        return self.cfg.pipeline.flow

    def spec_documents(self) -> list[Path]:
        """User- or agent-written specification documents in spec/ (not control files)."""
        if not self.spec_dir.is_dir():
            return []
        return sorted(
            p for p in self.spec_dir.iterdir() if p.is_file() and p.suffix.lower() in SPEC_DOC_EXTS and p.name not in SPEC_CONTROL_FILES
        )

    def save_config_values(self, values: dict) -> Path:
        """Deep-merge `values` into <project>/q3tui.yaml (created if missing)."""
        import yaml

        from q3tui.core.config import _merge

        path = self.root / CONFIG_FILENAME
        current = (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}
        path.write_text(yaml.safe_dump(_merge(current, values), sort_keys=False))
        return path

    def ensure_state_dir(self) -> Path:
        """Create .q3tui/ with an .ignore file so agent file searches (ripgrep) skip it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        ignore = self.state_dir / ".ignore"
        if not ignore.is_file():
            ignore.write_text("# Q3TUI internal state (backups, logs): hidden from LLM file searches\n*\n")
        return self.state_dir

    def backup(self, files: list[Path]) -> Path | None:
        """Copy existing files to .q3tui/bkp/<timestamp>/ (relative paths kept)."""
        existing = [f for f in files if f.is_file()]
        if not existing:
            return None
        dest_root = self.ensure_state_dir() / "bkp" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        for f in existing:
            dest = dest_root / self.rel(f)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        return dest_root

    def new_run_dir(self) -> Path:
        base = self.ensure_state_dir() / "runs" / datetime.now().strftime("run_%Y%m%d_%H%M%S")
        path, n = base, 1
        while path.exists():
            n += 1
            path = base.with_name(f"{base.name}_{n}")
        path.mkdir(parents=True)
        return path
