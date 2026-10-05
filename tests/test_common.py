from q3tui.steps.common import scoped, split_feedback


def test_scoped_feedback():
    fb = [scoped("rtl", "sync_fifo", "fix rd"), "general change", scoped("tb", "checker", "x")]
    assert split_feedback(fb, "rtl") == ({"sync_fifo": ["fix rd"]}, ["general change", "[tb:checker] x"])
