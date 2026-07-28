"""The code-gen gate: what counts as success, and who has to notice a failure.

Both behaviours here come from the same field session:

  1. Asked to add missing test coverage, the agent wrote NO files, re-ran the
     existing suite, and the harness reported ok — because the untested-code net
     only fires when code IS written. A no-op scored a perfect green, which is
     worse than a failure because it ends the conversation.
  2. The agent skipped writing a test at all, and the operator had to notice and
     ask "why didn't you test that?". That is the harness's job, not theirs.
"""

import pytest

from orchestration import codegen


class _Result:
    def __init__(self, tool_calls, text="done"):
        self.tool_calls = tool_calls
        self.final_text = text
        self.turns = 1


def _wrote(*paths):
    return [("write_file", {"path": p}) for p in paths]


# --- what counts as a change ------------------------------------------------

def test_fold_noop_rejects_a_run_that_wrote_nothing():
    v = codegen.fold_noop({"ok": True, "test": {"ok": True}}, [])
    assert v["ok"] is False and v["no_changes"] is True


def test_fold_noop_accepts_a_run_that_wrote_something():
    v = codegen.fold_noop({"ok": True}, _wrote("core/scrapers/x.py"))
    assert v["ok"] is True and "no_changes" not in v


def test_reading_files_is_not_changing_them():
    v = codegen.fold_noop({"ok": True}, [("read_file", {"path": "core/scrapers/x.py"}),
                                         ("run_command", {"command": "pytest"})])
    assert v["ok"] is False


# --- the auto-retry ---------------------------------------------------------

def _runner(monkeypatch, sequence):
    """Feed run_agent a scripted sequence of results; record the tasks it got."""
    seen = {"tasks": []}
    calls = iter(sequence)

    def fake_run_agent(task, system, on_event=print, **kw):
        seen["tasks"].append(task)
        return next(calls)

    monkeypatch.setattr(codegen, "run_agent", fake_run_agent)
    return seen


def test_missing_test_triggers_one_automatic_retry(monkeypatch):
    """The operator should never be the one to say 'you didn't test that'."""
    seen = _runner(monkeypatch, [
        _Result(_wrote("core/scrapers/epic.py")),                      # no test
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),  # fixed
    ])
    verifications = iter([{"ok": True}, {"ok": True}])

    result, v = codegen.run_codegen_gated(
        "fix the scraper", "SYSTEM", lambda: dict(next(verifications)), on_event=lambda m: None
    )

    assert len(seen["tasks"]) == 2, "should have retried exactly once"
    assert "wrote no test" in seen["tasks"][1]
    assert "Original task" in seen["tasks"][1], "retry must keep the original context"
    assert v["ok"] is True and "untested_code" not in v


def test_a_noop_revise_triggers_a_retry(monkeypatch):
    seen = _runner(monkeypatch, [
        _Result([]),                               # today's failure: changed nothing
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),
    ])
    verifications = iter([{"ok": True}, {"ok": True}])

    _, v = codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: dict(next(verifications)),
        on_event=lambda m: None, require_changes=True,
    )

    assert len(seen["tasks"]) == 2
    assert "without writing any file" in seen["tasks"][1]
    assert v["ok"] is True


def test_a_failing_test_is_NOT_retried(monkeypatch):
    """A red test is a real engineering problem for the operator to see and
    direct — not a rule the agent forgot. Retrying would just burn tokens and
    hide it."""
    seen = _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])

    _, v = codegen.run_codegen_gated(
        "build it", "SYSTEM", lambda: {"ok": False, "test": {"ok": False, "output": "1 failed"}},
        on_event=lambda m: None,
    )

    assert len(seen["tasks"]) == 1, "must not retry a genuine test failure"
    assert v["ok"] is False


def test_the_retry_happens_at_most_once(monkeypatch):
    """A stubborn agent must not loop forever against the operator's LLM."""
    seen = _runner(monkeypatch, [_Result([]), _Result([]), _Result([])])
    verifications = iter([{"ok": True}] * 3)

    _, v = codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: dict(next(verifications)),
        on_event=lambda m: None, require_changes=True,
    )

    assert len(seen["tasks"]) == 2, "one attempt + one retry, then report honestly"
    assert v["ok"] is False and v["no_changes"] is True


def test_a_clean_first_run_is_not_retried(monkeypatch):
    seen = _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])

    _, v = codegen.run_codegen_gated(
        "build it", "SYSTEM", lambda: {"ok": True}, on_event=lambda m: None
    )

    assert len(seen["tasks"]) == 1 and v["ok"] is True


def test_the_retry_is_announced_to_the_operator(monkeypatch):
    """It spends their LLM, so it must be visible in the event stream."""
    _runner(monkeypatch, [_Result([]), _Result(_wrote("core/x.py", "tests/test_x.py"))])
    verifications = iter([{"ok": True}, {"ok": True}])
    events = []

    codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: dict(next(verifications)),
        on_event=events.append, require_changes=True,
    )

    assert any("harness rejected that run" in e for e in events)
    assert any("Retrying once" in e for e in events)


def test_build_mode_does_not_require_changes(monkeypatch):
    """Only a *fix* is meaningless without a change; a build is judged on its
    verification alone."""
    seen = _runner(monkeypatch, [_Result([])])

    _, v = codegen.run_codegen_gated(
        "build it", "SYSTEM", lambda: {"ok": True}, on_event=lambda m: None
    )

    assert len(seen["tasks"]) == 1
    assert "no_changes" not in v


@pytest.mark.parametrize("calls, expected", [
    ([], []),
    (_wrote("core/a.py"), ["core/a.py"]),
    (_wrote("core/a.py", "tests/test_a.py"), ["core/a.py", "tests/test_a.py"]),
])
def test_files_written(calls, expected):
    assert codegen.files_written(calls) == expected


# --- coverage, not existence -------------------------------------------------

def test_uncovered_changes_fail_the_gate_and_trigger_a_retry(monkeypatch):
    """The hole that started all this: the Epic scraper's two tests passed for the
    entire time it was 403-broken, because they only covered a pure helper the fix
    never touched. Measured on that real commit: 22 changed executable lines, 4
    covered, 18 not."""
    seen = _runner(monkeypatch, [
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),  # stale test
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),  # extended
    ])
    coverage_results = iter([
        {"ok": False, "checked": True, "uncovered": {"core/scrapers/epic.py": [39, 40, 41]}},
        {"ok": True, "checked": True, "uncovered": {}},
    ])
    monkeypatch.setattr(codegen, "covers_changes", lambda t, c: dict(next(coverage_results)))

    _, v = codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_epic.py",
    )

    assert len(seen["tasks"]) == 2, "uncovered changes must earn a retry"
    assert "never RUNS the code you changed" in seen["tasks"][1]
    assert "lines [39, 40, 41]" in seen["tasks"][1], "name the lines, not just the file"
    assert v["ok"] is True


def test_covered_changes_pass_cleanly(monkeypatch):
    seen = _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])
    monkeypatch.setattr(codegen, "covers_changes",
                        lambda t, c: {"ok": True, "checked": True, "uncovered": {}})

    _, v = codegen.run_codegen_gated(
        "build", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_x.py",
    )

    assert len(seen["tasks"]) == 1 and v["ok"] is True


def test_coverage_is_not_second_guessed_when_no_test_was_written_at_all(monkeypatch):
    """Missing test is the clearer complaint; don't stack a confusing second one."""
    _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py"))] * 2)
    called = []
    monkeypatch.setattr(codegen, "covers_changes",
                        lambda t, c: called.append(1) or {"ok": False, "checked": True, "uncovered": {}})

    _, v = codegen.run_codegen_gated(
        "build", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_x.py",
    )

    assert not called, "no point measuring coverage of a test that doesn't exist"
    assert v["untested_code"] == ["core/parsers/x.py"]


def test_an_unmeasurable_coverage_run_does_not_block(monkeypatch):
    """If coverage can't run, fail open — the existence + pass checks still hold.
    A tooling problem must not strand the operator."""
    _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])
    monkeypatch.setattr(codegen, "covers_changes",
                        lambda t, c: {"ok": True, "checked": False, "detail": "Coverage unavailable"})

    _, v = codegen.run_codegen_gated(
        "build", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_x.py",
    )

    assert v["ok"] is True and "uncovered_changes" not in v
