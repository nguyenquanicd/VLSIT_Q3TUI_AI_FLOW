from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sync_fifo"


@pytest.fixture
def example_dir() -> Path:
    return EXAMPLE


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.delenv("Q3TUI_CONFIG", raising=False)
    monkeypatch.setenv("Q3TUI_HOME", str(tmp_path / "home"))
    from q3tui.core.icons import set_style

    set_style("unicode")  # icon style is process-wide; a test that switches it must not leak


@pytest.fixture(autouse=True)
def _reads_kind():
    """The `reads` step kind of the test flow (tests/fakes.py `use_test_flow`)."""
    from q3tui.steps import KINDS, register_kind

    register_kind("reads", "tests.fakes:ReadsStep")
    yield
    KINDS.pop("reads", None)


@pytest.fixture(autouse=True)
def _fresh_session_totals():
    """Session running totals (llm/runtime.py) are per process: never carried from one test into the next."""
    from q3tui.llm import runtime

    runtime._TOTALS.clear()
    yield
    runtime._TOTALS.clear()
