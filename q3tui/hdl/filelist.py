"""Simulator-style filelist (.f) parser.

Handles the common VCS/Xcelium dialect: `//` and `#` comments, `-f` (paths relative to the
invocation dir) and `-F` (paths relative to the filelist's own dir), `+incdir+`, `+define+`,
`-v` library files, `-y` library dirs, `+libext+`, `$VAR`/`${VAR}` expansion. Unknown tool
switches are kept in `other_args` rather than rejected.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path

HDL_EXTS = {".v", ".sv", ".vh", ".svh", ".vp", ".svp", ".vlib"}
_ENV_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


class FilelistError(ValueError):
    pass


@dataclass
class Filelist:
    files: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    lib_files: list[Path] = field(default_factory=list)
    lib_dirs: list[Path] = field(default_factory=list)
    lib_exts: list[str] = field(default_factory=list)
    other_args: list[str] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)  # every .f file read
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: ([str(x) for x in v] if isinstance(v, list) else v) for k, v in asdict(self).items()}

    def all_sources(self) -> list[Path]:
        """Design files plus -v library files (for parsers that need everything)."""
        seen: dict[Path, None] = dict.fromkeys(self.files)
        seen.update(dict.fromkeys(self.lib_files))
        return list(seen)


def _expand_env(token: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            raise FilelistError(f"undefined environment variable ${name}")
        return os.environ[name]

    return _ENV_RE.sub(repl, token)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"(^|\s)//.*$", "", line)
        line = re.sub(r"^\s*#.*$", "", line)
        lines.append(line)
    return "\n".join(lines)


def parse_filelist(path: str | Path, base_dir: Path | None = None) -> Filelist:
    path = Path(path).expanduser()
    base_dir = (base_dir or Path.cwd()).resolve()
    result = Filelist()
    _parse_into(result, path.resolve(), base_dir, stack=[])
    # de-duplicate while keeping order
    result.files = list(dict.fromkeys(result.files))
    result.incdirs = list(dict.fromkeys(result.incdirs))
    return result


def _resolve(token: str, rel_dir: Path, fallback_dir: Path) -> Path:
    p = Path(_expand_env(token)).expanduser()
    if p.is_absolute():
        return p
    first = (rel_dir / p).resolve()
    if first.exists():
        return first
    second = (fallback_dir / p).resolve()
    return second if second.exists() else first


def _parse_into(result: Filelist, fpath: Path, base_dir: Path, stack: list[Path]) -> None:
    if fpath in stack:
        raise FilelistError(f"recursive filelist include: {' -> '.join(map(str, stack + [fpath]))}")
    if not fpath.is_file():
        raise FilelistError(f"filelist not found: {fpath}")
    result.sources.append(fpath)
    fdir = fpath.parent
    try:
        tokens = shlex.split(_strip_comments(fpath.read_text()), posix=True)
    except ValueError as exc:
        raise FilelistError(f"{fpath}: {exc}") from exc

    it = iter(tokens)
    for tok in it:
        if tok in ("-f", "-F", "-file"):
            arg = next(it, None)
            if arg is None:
                raise FilelistError(f"{fpath}: {tok} needs an argument")
            # -F: relative to this filelist; -f: relative to invocation dir (fallback: this filelist)
            rel, fb = (fdir, base_dir) if tok == "-F" else (base_dir, fdir)
            _parse_into(result, _resolve(arg, rel, fb), base_dir, stack + [fpath])
        elif tok == "-v":
            result.lib_files.append(_resolve(next(it, ""), base_dir, fdir))
        elif tok == "-y":
            result.lib_dirs.append(_resolve(next(it, ""), base_dir, fdir))
        elif tok.startswith("+incdir+"):
            for d in filter(None, tok[len("+incdir+"):].split("+")):
                result.incdirs.append(_resolve(d, base_dir, fdir))
        elif tok.startswith("+define+"):
            for d in filter(None, tok[len("+define+"):].split("+")):
                name, _, value = d.partition("=")
                result.defines[name] = value if _ else None
        elif tok.startswith("+libext+"):
            result.lib_exts.extend(filter(None, tok[len("+libext+"):].split("+")))
        elif tok.startswith(("-", "+")):
            result.other_args.append(tok)
        else:
            p = _resolve(tok, base_dir, fdir)
            if not p.exists():
                result.missing.append(str(p))
            result.files.append(p)


def filelist_from_files(files: list[Path]) -> Filelist:
    fl = Filelist(files=[f.resolve() for f in files])
    fl.incdirs = list(dict.fromkeys(f.resolve().parent for f in files))
    return fl
