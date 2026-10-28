"""A board KiCad's parser rejects fails once, says why, and is not rebuilt on a timer.

On the live instance a KiCad 8 board holding a zone ``(placement (expr ...))``
made pcbnew.LoadBoard answer None; the upgrade then segfaulted with no output,
and each of seven commits was retried three times.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import kicad_tools
from app.services.design_bundle import jobs, kicad_steps

PARSE_ABORT = (
    "terminate called after throwing an instance of 'PARSE_ERROR'\n"
    "  what():  Expecting enabled, sheetname, component_class, or group. Got ''symbol'' "
    "in 'board.kicad_pcb', line 1453222, offset 5.\n"
)


def _python_answers(monkeypatch: pytest.MonkeyPatch, result: kicad_tools.ToolResult) -> None:
    class FakePython:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, args, *, timeout):
            return result

    monkeypatch.setattr(kicad_tools, "LocalTool", FakePython)


def test_the_load_script_surfaces_the_parse_error_instead_of_crashing() -> None:
    for script in (kicad_steps.UPGRADE_BOARD_SCRIPT, kicad_steps.REFILL_SCRIPT):
        compile(script, "<kicad-python>", "exec")
        assert "if board is None:" in script
        assert script.index("if board is None:") < script.index("SaveBoard")


def test_a_parse_error_is_an_unreadable_source_with_kicads_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _python_answers(monkeypatch, kicad_tools.ToolResult(returncode=-6, stderr=PARSE_ABORT))
    with pytest.raises(kicad_steps.KicadSourceUnreadable) as caught:
        kicad_steps._run_python(kicad_steps.REFILL_SCRIPT, Path("board.kicad_pcb"), timeout=10, cancel=None)
    message = str(caught.value)
    assert message.startswith(f"{kicad_steps.UNREADABLE_PREFIX}board.kicad_pcb: Expecting enabled")
    assert "line 1453222" in message


def test_a_nightly_placement_board_says_how_to_fix_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    board = tmp_path / "board.kicad_pcb"
    board.write_text(
        "(kicad_pcb (version 20240108)\n"
        "  (zone (placement\n      (expr \"A.memberOfSheet('/io/bank0/')\")))\n"
        "  (zone (placement (expr \"A.memberofsheet('/io/bank1/')\"))))\n"
    )
    _python_answers(monkeypatch, kicad_tools.ToolResult(returncode=-6, stderr=PARSE_ABORT))
    with pytest.raises(kicad_steps.KicadSourceUnreadable) as caught:
        kicad_steps._run_python(kicad_steps.REFILL_SCRIPT, board, timeout=10, cancel=None)
    message = str(caught.value)
    assert "line 1453222" in message
    assert "offset 5. The board has 2 rule-area placement(s)" in message
    assert "no released KiCad reads" in message
    assert '(placement (enabled yes) (sheetname "<sheet path>"))' in message


def test_an_unrelated_parse_error_gets_no_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb (version 20240108))\n")
    _python_answers(monkeypatch, kicad_tools.ToolResult(returncode=-6, stderr=PARSE_ABORT))
    with pytest.raises(kicad_steps.KicadSourceUnreadable) as caught:
        kicad_steps._run_python(kicad_steps.REFILL_SCRIPT, board, timeout=10, cancel=None)
    assert "rule-area" not in str(caught.value)


def test_a_silent_crash_names_its_signal_and_stays_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _python_answers(monkeypatch, kicad_tools.ToolResult(returncode=-11))
    with pytest.raises(kicad_steps.KicadStepFailed) as caught:
        kicad_steps._run_python(kicad_steps.REFILL_SCRIPT, Path("board.kicad_pcb"), timeout=10, cancel=None)
    assert not isinstance(caught.value, kicad_steps.KicadSourceUnreadable)
    assert "crashed with signal 11" in str(caught.value)


def test_an_unreadable_source_ends_the_job_without_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unreadable(*args, **kwargs):
        raise kicad_steps.KicadSourceUnreadable(f"{kicad_steps.UNREADABLE_PREFIX}board.kicad_pcb: Expecting ...")

    monkeypatch.setattr(jobs.materialize, "materialize", lambda *args, **kwargs: None)
    monkeypatch.setattr(kicad_steps, "upgrade_sources", unreadable)

    class Key:
        config: dict = {}

        def record(self) -> dict:
            return {}

    class Generator:
        kicad_file_formats: dict = {}

    with pytest.raises(jobs.PermanentJobError) as caught:
        jobs.prepare_sources(tmp_path, Key(), Generator(), tmp_path, cancel=lambda: None)
    assert caught.value.code == "source-unreadable"


def test_an_unreadable_failure_holds_past_the_retry_window() -> None:
    long_ago = datetime.now(timezone.utc) - timedelta(days=2)
    unreadable = {"state": "failed", "error": f"{kicad_steps.UNREADABLE_PREFIX}board.kicad_pcb: x", "updated_at": long_ago}
    transient = {"state": "failed", "error": "KiCad's Python failed on board.kicad_pcb: timed out", "updated_at": long_ago}
    assert jobs._failure_holds(unreadable)
    assert not jobs._failure_holds(transient)
