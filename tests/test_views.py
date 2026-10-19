from q3tui.tui.views import split_fences


def test_split_fences_keeps_code_blocks_whole():
    text = "# A\n\nprose\n\n```text\n┌──┐ wide line\n│  │\n```\n\nmore\n\n```systemverilog\nmodule m;\nendmodule\n```"
    parts = split_fences(text)
    assert [(k, lang) for k, _, lang in parts] == [("md", ""), ("code", "text"), ("md", ""), ("code", "systemverilog")]
    assert parts[1][1] == "┌──┐ wide line\n│  │"
    assert split_fences("no code") == [("md", "no code", "")]
    assert split_fences("```\nunterminated") == [("code", "unterminated", "text")]
