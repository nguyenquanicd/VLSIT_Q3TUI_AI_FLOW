import pytest

from q3tui.hdl.filelist import FilelistError, parse_filelist


def test_example_filelist(example_dir):
    fl = parse_filelist(example_dir / "fifo.f", base_dir=example_dir)
    assert [f.name for f in fl.files] == ["sync_fifo.sv", "fifo_top.sv"]
    assert fl.incdirs == [example_dir / "rtl"]
    assert fl.other_args == ["-sverilog"]
    assert not fl.missing


def test_dialect(tmp_path, monkeypatch):
    monkeypatch.setenv("IP", str(tmp_path / "ip"))
    (tmp_path / "ip").mkdir()
    (tmp_path / "ip" / "ip.sv").write_text("module ip; endmodule\n")
    (tmp_path / "ip" / "ip.f").write_text("ip.sv  // relative to this .f because of -F\n")
    (tmp_path / "a.sv").write_text("")
    (tmp_path / "lib.v").write_text("")
    (tmp_path / "top.f").write_text(
        "/* block\n comment */\n"
        "# hash comment\n"
        "+incdir+inc1+inc2\n"
        "+define+A+B=2\n"
        "-v lib.v -y libdir +libext+.v+.sv\n"
        "-F ${IP}/ip.f\n"
        "a.sv a.sv\n"
        "-timescale=1ns/1ps\n"
    )
    fl = parse_filelist(tmp_path / "top.f", base_dir=tmp_path)
    assert fl.defines == {"A": None, "B": "2"}
    assert [d.name for d in fl.incdirs] == ["inc1", "inc2"]
    assert [f.name for f in fl.files] == ["ip.sv", "a.sv"]  # de-duplicated, -F relative to ip.f
    assert fl.lib_files == [tmp_path / "lib.v"]
    assert fl.lib_exts == [".v", ".sv"]
    assert fl.other_args == ["-timescale=1ns/1ps"]


def test_recursion_and_missing(tmp_path):
    (tmp_path / "a.f").write_text("-f a.f\n")
    with pytest.raises(FilelistError, match="recursive"):
        parse_filelist(tmp_path / "a.f", base_dir=tmp_path)
    (tmp_path / "b.f").write_text("nothere.sv\n")
    assert parse_filelist(tmp_path / "b.f", base_dir=tmp_path).missing


def test_undefined_env(tmp_path):
    (tmp_path / "c.f").write_text("$NOPE_NOT_SET/x.sv\n")
    with pytest.raises(FilelistError, match="NOPE_NOT_SET"):
        parse_filelist(tmp_path / "c.f", base_dir=tmp_path)
