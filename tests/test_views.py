from q3tui.tui.views import split_fences


def test_split_fences_keeps_code_blocks_whole():
    text = "# A\n\nprose\n\n```text\n┌──┐ wide line\n│  │\n```\n\nmore\n\n```systemverilog\nmodule m;\nendmodule\n```"
    parts = split_fences(text)
    assert [(k, lang) for k, _, lang in parts] == [("md", ""), ("code", "text"), ("md", ""), ("code", "systemverilog")]
    assert parts[1][1] == "┌──┐ wide line\n│  │"
    assert split_fences("no code") == [("md", "no code", "")]
    assert split_fences("```\nunterminated") == [("code", "unterminated", "text")]


def test_spec_field_tables_are_drawn_without_an_empty_header():
    from io import StringIO

    from rich.console import Console
    from rich.table import Table

    from q3tui.tui.views import _prose_renderables

    body = "Intro.\n\n| | |\n|---|---|\n| **Clock domains** | 1 domain, `i_clk_pclk` |\n| **Reset** | active low |\n\nAfter.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    parts = _prose_renderables(body)
    assert [type(p).__name__ for p in parts] == ["Markdown", "Table", "Markdown"]
    out = StringIO()
    Console(file=out, width=80).print(parts[1])
    text = out.getvalue()
    assert "Clock domains" in text and "i_clk_pclk" in text and "─" not in text   # no header rule
    assert isinstance(parts[1], Table) and not parts[1].show_header


def test_crossing_wires_are_drawn_with_a_line_jump():
    from termaid.renderer import canvas as cv

    from q3tui.tui import mermaid

    put = cv.Canvas.put
    art = mermaid.render("flowchart TB\n  A[a] -->|x| C[c]\n  B[b] -->|y| D[d]\n  A -->|z| D\n  B -->|w| C")
    assert "╴│" in art and "┼" not in art                 # the crossing: the horizontal wire is broken around the vertical
    assert "" not in art and cv.Canvas.put is put    # no marker left, termaid restored
    plain = mermaid.render("flowchart TB\n  A[a] -->|x| B[b]\n  A -->|y| C[c]")
    assert "╴" not in plain                                # a fan-out is a junction, not a jump


def test_a_wire_through_a_module_boundary_is_not_a_line_jump():
    from q3tui.tui import mermaid

    art = mermaid.render('flowchart LR\n  io(i_rx)\n  subgraph top["m_top"]\n    a[m_a]\n  end\n  io -->|serial in| a\n')
    assert "╴" not in art and "╶" not in art


BLOCK = """flowchart LR
  apb_if([APB interface])
  rx([i_uart_rx])
  tx_o([o_uart_tx])
  subgraph top["m_uart"]
    subgraph d1["i_clk_apb domain"]
      apb[m_apb_slave]
      csr[m_csr]
    end
    subgraph d2["i_clk_uart domain"]
      sync[m_cdc_sync_bit]
      txm[m_uart_tx]
    end
  end
  apb_if -->|APB requests| apb
  apb -->|APB response| apb_if
  apb -->|register bus| csr
  rx -->|serial in| sync
  sync -->|CDC: async FIFO| csr
  csr -->|CDC: TX data| txm
  txm -->|serial out| tx_o
"""


def test_block_diagrams_are_drawn_as_a_schematic():
    from q3tui.tui import blockdiagram, mermaid

    art = mermaid.render(BLOCK)
    lines = art.split("\n")
    assert blockdiagram.LAST["overlaps"] == []                              # every wire on its own track: no shared lanes
    assert "╔═ m_uart " in art and "┌┄ i_clk_apb domain " in art and "┌┄ i_clk_uart domain " in art   # boundary, domains
    row = next(ln for ln in lines if "APB interface" in ln)                 # IO outside the boundary, on the left
    assert row.index("APB interface") < max(row.find("║"), row.find("╫"))
    out = next(ln for ln in lines if "o_uart_tx" in ln)                     # outputs on the right
    assert out.index("o_uart_tx") > max(out.rfind("║"), out.rfind("╫")) > 0
    for label in ("APB requests", "APB response", "register bus", "serial in", "CDC: async FIFO", "CDC: TX data", "serial out"):
        assert f" {label} " in art                                         # every connection carries its label
    assert "╫" in art                                                       # a wire through the boundary is a pin
    req = next(ln for ln in lines if " APB requests " in ln)
    assert "►│ m_apb_slave" in req                                             # arrow at the target
    resp = next(ln for ln in lines if " APB response " in ln)
    assert "│◄" in resp                                                     # a response goes back into the IO's right side


def test_block_diagram_wires_cross_with_a_jump_and_fall_back_when_not_a_block_diagram():
    from q3tui.tui import blockdiagram

    crossing = """flowchart LR
  a(i_a)
  b(i_b)
  subgraph top["m_t"]
    x[m_x]
    y[m_y]
  end
  a -->|to y| y
  b -->|to x| x
"""
    art = blockdiagram.render(crossing)
    assert art is not None and blockdiagram.LAST["overlaps"] == []
    assert blockdiagram.render("flowchart LR\n  a[m_a] -->|data| b[m_b]\n") is None   # no IO, no boundary: termaid draws it
    assert blockdiagram.render("stateDiagram-v2\n  [*] --> A\n") is None


def test_block_diagram_of_a_real_spec_has_no_shared_wires():
    """A dense block diagram (13 blocks, 32 connections, control loops) as a spec writer drew it."""
    from q3tui.tui import blockdiagram

    blocks = ["rsts", "apb", "csr", "txf", "rxf", "baud", "tx", "rx", "rxs", "err", "irq", "pwr", "cg"]
    edges = [("rst_pin", "rsts"), ("rsts", "csr"), ("clk_pin", "cg"), ("clk_pin", "csr"), ("apb_if", "apb"), ("apb", "apb_if"),
             ("apb", "csr"), ("csr", "txf"), ("txf", "tx"), ("rx", "rxf"), ("rxf", "csr"), ("csr", "baud"), ("csr", "tx"),
             ("csr", "rx"), ("baud", "tx"), ("baud", "rx"), ("tx", "tx_pin"), ("rx_pin", "rxs"), ("rxs", "rx"), ("rxs", "pwr"),
             ("rx", "err"), ("txf", "err"), ("rxf", "err"), ("err", "csr"), ("err", "irq"), ("txf", "irq"), ("rxf", "irq"),
             ("csr", "irq"), ("irq", "irq_pins"), ("csr", "pwr"), ("tx", "pwr"), ("rx", "pwr"), ("pwr", "cg"), ("cg", "baud"),
             ("cg", "tx"), ("cg", "rx")]
    src = ["flowchart LR", "  apb_if([APB interface])", "  clk_pin([i_clk port])", "  rst_pin([i_rst_n port])",
           "  rx_pin([i_rx port])", "  tx_pin([o_tx port])", "  irq_pins([Interrupts])", '  subgraph dom["i_clk domain"]']
    src += [f"    {b}[m_{b}]" for b in blocks] + ["  end"]
    src += [f"  {a} -->|{a} to {b}| {b}" for a, b in edges]
    art = blockdiagram.render("\n".join(src))
    assert art is not None and blockdiagram.LAST["overlaps"] == []
    assert all(f" {a} to {b} " in art for a, b in edges)
