from q3tui.core.config import Config
from q3tui.eda import get_adapter
from q3tui.eda.base import ToolRequest, run_command
from q3tui.eda.synopsys import parse_vcs_log
from q3tui.hdl.filelist import parse_filelist

VCS_LOG = """\
Error-[SE] Syntax error
  Following verilog source has syntax error :
  "rtl/foo.sv", 12: token is 'endmodule'
endmodule
         ^

Warning-[TFIPC] Too few instance port connections
rtl/top.sv, 40
"top", "sub u_sub( .a (a));"

UVM_ERROR tb/scoreboard.sv(88) @ 1250: uvm_test_top.env.sb [SB] mismatch exp=1 act=0
"""


def test_parse_vcs_log():
    diags = parse_vcs_log(VCS_LOG)
    assert [(d.severity, d.code, d.file, d.line) for d in diags] == [
        ("error", "SE", "rtl/foo.sv", 12),
        ("warning", "TFIPC", "rtl/top.sv", 40),
        ("error", "UVM_ERROR", "tb/scoreboard.sv", 88),
    ]


def test_vcs_command(example_dir):
    fl = parse_filelist(example_dir / "fifo.f", base_dir=example_dir)
    fl.defines = {"SIM": None, "W": "8"}
    vcs = get_adapter("simulator", Config())
    cmd = vcs.compile_command(ToolRequest(filelist=fl, top="fifo_top"))
    assert cmd[0] == "vcs" and "-top" in cmd and cmd[cmd.index("-top") + 1] == "fifo_top"
    assert f"+incdir+{example_dir / 'rtl'}" in cmd
    assert "+define+SIM" in cmd and "+define+W=8" in cmd
    assert cmd[-1].endswith("fifo_top.sv")


def test_slang_syntax_adapter(example_dir, tmp_path):
    fl = parse_filelist(example_dir / "fifo.f", base_dir=example_dir)
    result = get_adapter("syntax", Config()).run(ToolRequest(filelist=fl, top="fifo_top"), tmp_path)
    assert result.ok and not result.errors and result.vendor == "slang"


def test_run_command_missing_binary_and_timeout(tmp_path):
    rc, out, _, timed_out = run_command(["definitely-not-a-tool-xyz"], tmp_path, tmp_path / "a.log", 5)
    assert rc == 127 and not timed_out
    rc, _, _, timed_out = run_command(["sleep", "5"], tmp_path, tmp_path / "b.log", 1)
    assert timed_out and rc is None
    assert (tmp_path / "b.log").read_text().startswith("$ sleep 5")


def test_tool_env_module_load(tmp_path):
    from q3tui.eda import tool_env
    from q3tui.eda.base import ToolEnv

    bindir = tmp_path / "fakevcs" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "fakevcs"
    fake.write_text("#!/bin/sh\necho fake-vcs-ran \"$@\"\n")
    fake.chmod(0o755)
    init = tmp_path / "init.bash"  # stand-in for $MODULESHOME/init/bash
    init.write_text(f'module() {{ [ "$1" = load ] && shift && for m in "$@"; do PATH="{tmp_path}/$m/bin:$PATH"; done; }}\n')

    env = ToolEnv(modules=("fakevcs",), modules_init=str(init))
    assert env.which("fakevcs") == str(fake)
    assert ToolEnv(modules=("nothing",), modules_init=str(init)).which("fakevcs") is None
    rc, out, _, _ = run_command(["fakevcs", "-x"], tmp_path, tmp_path / "c.log", 10, env)
    assert rc == 0 and "fake-vcs-ran -x" in out

    cfg = Config.model_validate({"tools": {"modules": ["common"], "modules_init": str(init), "simulator": {"adapter": "vcs", "modules": ["fakevcs", "common"]}}})
    assert tool_env(cfg, "simulator").modules == ("common", "fakevcs")
    assert tool_env(cfg, "lint").modules == ("common",)


def test_a_compile_that_never_finishes_is_the_tools_problem(tmp_path, monkeypatch):
    """rv32im: VCS hung 600 s on mem_stage's testbench (rc=None, 0 errors) and Q3TUI failed the step for "testbench
    checks fail". It runs once more; still unfinished, it is reported, never blamed on the code."""
    from q3tui.eda.base import ToolResult, compile_settled, unfinished

    calls = []

    class Hung:
        def compile(self, request, workdir):
            calls.append(1)
            return ToolResult("simulator", "synopsys", False, [], None, 600.0, "log", [], True, "vcs compile: rc=None, 0 errors")

    r = compile_settled(Hung(), None, tmp_path)
    assert len(calls) == 2 and unfinished(r)
    killed = ToolResult("simulator", "synopsys", False, [], None, 1.0, "log", [], False, "vcs compile: rc=None, 0 errors")
    assert unfinished(killed)                                              # no exit code, not timed out: killed
    assert not unfinished(ToolResult("simulator", "synopsys", False, [], 1, 1.0, "log", [], False, "rc=1"))


def test_vcs_error_whose_long_path_wraps_still_names_its_file():
    from q3tui.eda.synopsys import parse_vcs_log

    log = ("Error-[SE] Syntax error\n  Following verilog source has syntax error :\n"
           '  "/a/very/long/path/to/the/project/src/tb/tb_top.sv", \n  407: token is \'$finish\'\n      $finish;\n             ^\n\n')
    (d,) = parse_vcs_log(log)
    assert (d.severity, d.code, d.file, d.line) == ("error", "SE", "/a/very/long/path/to/the/project/src/tb/tb_top.sv", 407)
