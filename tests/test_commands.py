from q3tui.tui.commands import candidates, complete, hint

SOURCE = {"step": ["spec", "parse", "config"], "gate": ["spec", "parse", "config", "all"],
          "question": ["Q-S1", "Q-P2"]}.get


def source(kind):
    return SOURCE(kind) or []


def test_command_names():
    assert complete("/app", source) == "/approve"
    assert complete("/approve", source) is None  # nothing left to add
    assert set(candidates("/s", source)) == {"/stop", "/step", "/skill", "/specreview", "/status", "/settings", "/stats"}
    assert complete("/re", source) == "/reset"
    assert complete("/nope", source) is None


def test_arguments():
    assert complete("/approve p", source) == "/approve parse"
    assert complete("/approve all --f", source) == "/approve all --force"
    assert complete("/answer Q-P", source) == "/answer Q-P2"
    assert complete("/autoapprove o", source) == "/autoapprove on"
    assert candidates("/approve ", source) == ["spec", "parse", "config", "all"]
    assert complete("/change spec some text", source) is None  # free text


def test_hint():
    assert hint("hello", source) == ""
    assert hint("/appr", source).startswith("/approve <step>|all [--force] — ")
    assert "/stop" in hint("/st", source) and "/status" in hint("/st", source)
    assert "▸ spec · parse · config · all" in hint("/approve ", source)
    assert hint("/xyz", source).startswith("unknown command")
