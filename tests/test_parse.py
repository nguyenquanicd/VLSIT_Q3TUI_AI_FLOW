from q3tui.hdl.filelist import filelist_from_files, parse_filelist
from q3tui.hdl.parse import parse_design, summarize


def test_example_design(example_dir):
    d = parse_design(parse_filelist(example_dir / "fifo.f", base_dir=example_dir), "fifo_top")
    assert d.ok and d.top == "fifo_top"
    assert set(d.modules) == {"fifo_top", "sync_fifo"}
    assert [c["instance"] for c in d.hierarchy["children"]] == ["u_ingress", "u_egress"]

    top = d.modules["fifo_top"]
    ports = {p.name: (p.direction, p.type) for p in top.ports}
    assert ports["drop_count"] == ("output", "logic[15:0]")
    assert ports["in_data"] == ("input", "logic[31:0]")
    assert [c.name for c in top.clocks] == ["clk"]
    assert [(r.name, r.polarity, r.kind) for r in top.resets] == [("rst_n", "active_low", "async")]

    fifo = d.modules["sync_fifo"]
    assert fifo.elaborated_in == "fifo_top.u_ingress"
    assert {p.name: p.value for p in fifo.parameters}["DEPTH"] == "8"
    assert "u_egress: sync_fifo" in summarize(d)
    assert all(diag.file and diag.file.endswith(".sv") for diag in d.diagnostics)


def test_errors_and_unresolved(tmp_path):
    src = tmp_path / "bad.sv"
    src.write_text(
        "module bad(input logic clk, input logic srst_n, output logic q);\n"
        "  missing_mod u_m ();\n"
        "  always_ff @(posedge clk) if (!srst_n) q <= 0; else q <= ~q;\n"
        "endmodule\n"
    )
    d = parse_design(filelist_from_files([src]), "bad")
    assert not d.ok
    assert d.unresolved_modules == ["missing_mod"]
    assert any(e.code == "UnknownModule" and e.line == 2 for e in d.errors)
    resets = {r.name: (r.polarity, r.kind) for r in d.modules["bad"].resets}
    assert resets == {"srst_n": ("active_low", "unknown")}


def test_syntax_error_reported(tmp_path):
    src = tmp_path / "syn.sv"
    src.write_text("module syn(input a output b); endmodule\n")
    d = parse_design(filelist_from_files([src]), "syn")
    assert not d.ok and d.errors[0].line == 1


def test_errors_in_included_files_keep_their_message(tmp_path):
    from q3tui.hdl.filelist import filelist_from_files
    from q3tui.hdl.parse import parse_design

    (tmp_path / "inc.svh").write_text("  task automatic t();\n    int x;\n    x = undefined_signal;\n  endtask\n")
    (tmp_path / "top.sv").write_text('module top;\n`include "inc.svh"\nendmodule\n')
    fl = filelist_from_files([tmp_path / "top.sv"])
    fl.incdirs.append(tmp_path)
    errs = [d for d in parse_design(fl, "top").diagnostics if d.severity == "error"]
    assert errs and "undefined_signal" in errs[0].message and "included from" not in errs[0].message
