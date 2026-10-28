"""The software tools of the dv flow's `prepare` step: a RISC-V GCC, the Spike ISA simulator (the reference model) and `make` (tool role `sw_build`), found by
code and never guessed. A configured role wins (`riscv_gcc`, `iss`, `sw_build` in config/tools.json); without one, the tools are looked for in `$RISCV/bin`, on
`PATH` and in `~/.q3tui/tools` (`$Q3TUI_HOME/tools`). `setup` writes what it finds into config/tools.json; `install` puts what is missing into
`~/.q3tui/tools`: the xPack GCC (a download), Spike and dtc (built from source: git, make and a C++ compiler are needed) and riscv-tests (a clone the LLM may
read and reuse). Nothing is ever downloaded during a run: the step only reports what is missing (`q3tui act riscv_tools_setup`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GCC_NAMES = ("riscv64-unknown-elf-gcc", "riscv32-unknown-elf-gcc", "riscv-none-elf-gcc")
GCC_URL = ("https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/"
           "xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz")
SPIKE_REPO = "https://github.com/riscv-software-src/riscv-isa-sim"
DTC_REPO = "https://git.kernel.org/pub/scm/utils/dtc/dtc.git"
TESTS_REPO = "https://github.com/riscv-software-src/riscv-tests"
SW_BUILD = {"cmd": "make -C {src} -j8 PROG_DIR={out} RISCV_PREFIX={riscv_prefix} SPIKE={spike}", "parse": "generic", "timeout_s": 3600,
            "description": "build the test programs of src/sw (the Makefile the prepare step writes): GCC images and Spike reference results into PROG_DIR"}


def home_tools() -> Path:
    return Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser() / "tools"


@dataclass
class Toolchain:
    gcc: str | None = None  # the gcc driver; the prefix of the other binutils is its name without "gcc"
    spike: str | None = None
    dtc: str | None = None  # Spike runs dtc to make the device tree of the simulated machine
    make: str | None = None
    tests: Path | None = None  # a riscv-tests checkout (a reference the LLM may read)
    problems: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return self.gcc[: -len("gcc")] if self.gcc and self.gcc.endswith("gcc") else ""

    @property
    def path_dirs(self) -> list[str]:
        """The folders to put in front of PATH for a build (Spike finds dtc on PATH)."""
        return list(dict.fromkeys(str(Path(b).parent) for b in (self.gcc, self.spike, self.dtc) if b and "/" in b))

    @property
    def ready(self) -> bool:
        return bool(self.gcc and self.spike and self.dtc and self.make)


def _search_dirs() -> list[Path]:
    home = home_tools()
    dirs = [Path(os.environ["RISCV"]) / "bin"] if os.environ.get("RISCV") else []
    dirs += [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    dirs += [home / "riscv-gcc" / "bin", home / "spike" / "bin", home / "dtc" / "bin"]
    return list(dict.fromkeys(dirs))


def _which(names, dirs) -> str | None:
    for d in dirs:
        for n in names:
            p = d / n
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


def _role_binary(tools, name: str) -> tuple[str | None, str | None]:
    """(binary, problem) of a configured role; (None, None) when it is not configured."""
    from q3tui.eda.tools import available, binary_of

    role = tools.roles.get(name) if tools is not None else None
    if role is None:
        return None, None
    b = binary_of(role)
    if not available(tools, name):
        return None, f"the tool '{b}' (role '{name}') was not found (check config/tools.json)"
    return shutil.which(b) or b, None


def find(tools=None) -> Toolchain:
    """The toolchain: configured roles first, then a search (see the module doc)."""
    tc = Toolchain()
    dirs = _search_dirs()
    tc.gcc, p = _role_binary(tools, "riscv_gcc")
    tc.problems += [p] if p else []
    tc.gcc = tc.gcc or (None if p else _which(GCC_NAMES, dirs))
    tc.spike, p = _role_binary(tools, "iss")
    tc.problems += [p] if p else []
    tc.spike = tc.spike or (None if p else _which(["spike"], dirs))
    near = [Path(tc.spike).parent, Path(tc.spike).parent.parent.parent / "bin"] if tc.spike else []  # (a site build: <tools>/spike/bin/spike next to <tools>/bin/dtc)
    tc.dtc = _which(["dtc"], [*near, *dirs])
    tc.make = shutil.which("make")
    t = home_tools() / "riscv-tests"
    tc.tests = t if (t / "isa").is_dir() else None
    for what, have in (("a RISC-V GCC (riscv64-unknown-elf-gcc, riscv32-unknown-elf-gcc or riscv-none-elf-gcc)", tc.gcc), ("Spike (the reference ISA simulator)", tc.spike),
                       ("dtc (Spike needs it)", tc.dtc), ("make", tc.make)):
        if not have:
            tc.problems.append(f"{what} was not found")
    return tc


def roles_for(tc: Toolchain) -> dict:
    """The roles `setup` writes for a toolchain it found: the binaries by full path, PATH for Spike's dtc."""
    path = os.pathsep.join([*tc.path_dirs, "/usr/bin", "/bin"])
    return {"riscv_gcc": {"cmd": f"{tc.gcc} {{args}}", "parse": "generic", "description": "the RISC-V GCC the prepare step builds the test programs with"},
            "iss": {"cmd": f"{tc.spike} {{args}}", "parse": "generic", "env": {"PATH": path}, "description": "Spike, the reference ISA simulator (expected results)"},
            "sw_build": {**SW_BUILD, "env": {"PATH": path}}}


def write_roles(tools_json: Path, roles: dict) -> list[str]:
    """Add the roles to config/tools.json (one already there is kept: it is yours). Returns the names added."""
    data = json.loads(tools_json.read_text()) if tools_json.is_file() else {"roles": {}}
    have = data.setdefault("roles", {})
    added = [n for n in roles if n not in have]
    for n in added:
        have[n] = roles[n]
    if added:
        tools_json.parent.mkdir(parents=True, exist_ok=True)
        tools_json.write_text(json.dumps(data, indent=2) + "\n")
    return added


def install_steps(tc: Toolchain, dest: Path) -> list[tuple[str, list[str], Path | None]]:
    """(what, argv, cwd) of every command `install` runs for what is missing (shell-free; `sh -c` only where a build needs `cd` + several commands)."""
    steps: list[tuple[str, list[str], Path | None]] = []
    if not tc.gcc:
        tar = dest / "riscv-gcc.tar.gz"
        steps += [("GCC: download", ["curl", "-fL", "-o", str(tar), GCC_URL], None),
                  ("GCC: unpack", ["sh", "-c", f'mkdir -p "{dest}/riscv-gcc" && tar -xzf "{tar}" -C "{dest}/riscv-gcc" --strip-components=1 && rm -f "{tar}"'], None)]
    if not tc.dtc:
        steps += [("dtc: clone", ["git", "clone", "--depth", "1", DTC_REPO, str(dest / "src" / "dtc")], None),
                  ("dtc: build", ["make", "-j8", "NO_PYTHON=1", "NO_YAML=1", f"PREFIX={dest / 'dtc'}", "install-bin"], dest / "src" / "dtc")]
    if not tc.spike:
        src = dest / "src" / "riscv-isa-sim"
        steps += [("Spike: clone", ["git", "clone", "--depth", "1", SPIKE_REPO, str(src)], None),
                  ("Spike: build", ["sh", "-c", f'mkdir -p build && cd build && ../configure --prefix="{dest / "spike"}" && make -j8 && make install'], src)]
    if not tc.tests:
        steps.append(("riscv-tests: clone", ["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules", TESTS_REPO, str(dest / "riscv-tests")], None))
    return steps


def install(tc: Toolchain, dest: Path | None = None, log=None) -> list[str]:
    """Install what `tc` lacks into `dest` (default ~/.q3tui/tools). Returns what was done; raises RuntimeError with the output's end on a failed command."""
    dest = dest or home_tools()
    dest.mkdir(parents=True, exist_ok=True)
    done = []
    for what, argv, cwd in install_steps(tc, dest):
        if log:
            log(f"{what} …")
        env = {**os.environ, "PATH": os.pathsep.join([str(dest / "dtc" / "bin"), os.environ.get("PATH", "")])}  # (Spike's configure looks for dtc)
        res = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=3600, env=env)
        if res.returncode:
            raise RuntimeError(f"{what} failed: " + (res.stderr or res.stdout).strip()[-600:])
        done.append(what)
    return done
