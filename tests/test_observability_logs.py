"""Logging that stays small, stays findable, and stays the operator's.

Four field problems, one per section:

- The log reached 3.6 MB / 7,816 records with nothing to bound it, and every
  read parsed all of it to return fifteen records.
- 525 of those records were the same HOT_RELOAD_FAILED and 1,883 the same
  SETTINGS_SAVED, so "the last 15 errors" could be fifteen copies of one line
  with the real cause pushed off the end.
- A live 403 logged `{"url": ..., "status_code": 403}` and nothing to say WHICH
  source it belonged to, so "what went wrong with this source" was unanswerable.
- The test suite appended 788 records to the operator's real log — the same file
  `read_logs` hands the embedded agent.
"""

import json
import os

import pytest

from core import observability as obs
from core import progress


@pytest.fixture
def log(tmp_path, monkeypatch):
    """A logger writing to an isolated file, with the module pointed at it."""
    monkeypatch.setattr(obs, "LOG_DIR", tmp_path)
    monkeypatch.setattr(obs, "LOG_FILE", tmp_path / "agent.jsonl")
    return obs.get_logger("core.test")


def _fail(log, code="BOOM", message="it broke", **context):
    return log.failure(operation="op", code=code, message=message,
                       remediation="do something", context=context)


# --- the operator's log is not the suite's ----------------------------------

def test_the_log_path_is_absolute():
    """Relative meant 'wherever this process happens to be' — a second log the
    operator never found, the same defect the credential store had."""
    assert obs.REPO_ROOT.is_absolute()
    assert (obs.REPO_ROOT / "data" / "logs").is_absolute()


def test_the_log_directory_can_be_redirected_by_environment():
    """How the suite (and any subprocess it spawns) stays out of the real log."""
    assert "AGENT_LOG_DIR" in os.environ, "conftest must redirect the log"
    assert "data/logs/agent.jsonl" not in str(obs._log_file())


def test_records_written_during_tests_do_not_reach_the_repo_log(log):
    _fail(log)
    assert not (obs.REPO_ROOT / "data" / "logs" / "agent.jsonl").samefile(obs._log_file()) \
        if (obs.REPO_ROOT / "data" / "logs" / "agent.jsonl").exists() else True


# --- rotation, and reading across it ----------------------------------------

def test_the_log_rotates_instead_of_growing_forever(log, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "MAX_LOG_BYTES", 2_000)
    for i in range(60):
        _fail(log, message=f"failure number {i}")
    assert (tmp_path / "agent.1.jsonl").exists()
    assert (tmp_path / "agent.jsonl").stat().st_size < 2_000 * 2


def test_history_survives_a_rotation(log, tmp_path, monkeypatch):
    """The bug this caught: reads tailed only the LIVE file, so the moment it
    rolled over the whole history looked gone — while sitting one file over.

    Sized to cross the boundary exactly once. Rotating repeatedly with
    KEEP_ROTATIONS=1 discards older files by design, which is a different
    property (bounded growth) and is tested above."""
    monkeypatch.setattr(obs, "MAX_LOG_BYTES", 10_000)
    path = tmp_path / "agent.jsonl"
    i = 0
    while not (tmp_path / "agent.1.jsonl").exists():
        _fail(log, message=f"failure number {i}")
        i += 1
    _fail(log, message="written after the rotation")

    live = obs._tail_one_file(path, 500)
    both = obs._tail_lines(path, 500)
    assert len(both) > len(live), "the rotated file must still be reachable"
    assert "failure number 0" in "\n".join(both)      # the oldest record
    assert "failure number 0" not in "\n".join(live)  # and not in the live file
    assert "written after the rotation" in "\n".join(both)


def test_rotation_keeps_the_records_it_moves(log, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "MAX_LOG_BYTES", 2_000)
    for i in range(60):
        _fail(log, message=f"failure number {i}")
    rotated = [json.loads(x) for x in (tmp_path / "agent.1.jsonl").read_text().splitlines()]
    assert rotated and all(r["code"] == "BOOM" for r in rotated)


def test_a_tail_reads_only_what_it_needs(log, tmp_path):
    for i in range(500):
        _fail(log, message=f"failure number {i}")
    lines = obs._tail_lines(tmp_path / "agent.jsonl", 5)
    assert len(lines) == 5
    assert json.loads(lines[-1])["message"] == "failure number 499"


# --- repeats collapse -------------------------------------------------------

def test_identical_records_collapse_to_one_with_a_count(log):
    for _ in range(12):
        _fail(log, code="HOT_RELOAD_FAILED", message="Could not reload core.parsers.k")
    records, summary = obs.read_relevant(limit=15, level="error")
    assert len(records) == 1
    assert records[0]["_count"] == 12
    assert summary["collapsed"] == 11


def test_the_real_cause_is_not_pushed_out_by_repeats(log):
    """The whole point. Twelve copies of noise must not evict the one record
    that explains the failure."""
    for _ in range(12):
        _fail(log, code="HOT_RELOAD_FAILED", message="Could not reload core.parsers.k")
    _fail(log, code="HTTP_ERROR", message="GET .../accountHistory returned 403.")

    records, _ = obs.read_relevant(limit=2, level="error")
    assert {r["code"] for r in records} == {"HOT_RELOAD_FAILED", "HTTP_ERROR"}


def test_a_collapsed_record_keeps_the_newest_detail(log):
    _fail(log, code="SAME", message="same message", attempt=1)
    _fail(log, code="SAME", message="same message", attempt=2)
    records, _ = obs.read_relevant(limit=5, level="error")
    assert records[0]["context"]["attempt"] == 2


def test_different_messages_are_not_collapsed(log):
    _fail(log, code="HTTP_ERROR", message="returned 403.")
    _fail(log, code="HTTP_ERROR", message="returned 500.")
    records, _ = obs.read_relevant(limit=5, level="error")
    assert len(records) == 2


# --- a run stamps its own source --------------------------------------------

def test_a_run_stamps_its_source_on_every_record(log):
    """The 403 that cost a day logged its URL and status and nothing else."""
    with progress.channel("dfcu_financial_bank"):
        _fail(log, code="HTTP_ERROR", message="returned 403.", url="https://x", status_code=403)
    records, _ = obs.read_relevant(limit=5, level="error")
    assert records[0]["context"]["source_key"] == "dfcu_financial_bank"


def test_an_explicit_source_key_wins_over_the_run(log):
    with progress.channel("outer_source"):
        _fail(log, source_key="the_caller_knows_better")
    records, _ = obs.read_relevant(limit=5, level="error")
    assert records[0]["context"]["source_key"] == "the_caller_knows_better"


def test_the_scope_does_not_leak_past_the_run(log):
    with progress.channel("dfcu_financial_bank"):
        pass
    assert obs.current_scope() is None
    _fail(log)
    records, _ = obs.read_relevant(limit=5, level="error")
    assert "source_key" not in records[0]["context"]


def test_records_can_be_filtered_to_one_source(log):
    with progress.channel("dfcu_financial_bank"):
        _fail(log, code="DFCU_ONE", message="dfcu failed")
    with progress.channel("epic_property_management"):
        _fail(log, code="EPIC_ONE", message="epic failed")

    records, _ = obs.read_relevant(limit=5, level="error", source_key="dfcu")
    assert [r["code"] for r in records] == ["DFCU_ONE"]


def test_a_time_window_excludes_older_records(log):
    _fail(log, code="OLD", message="an old failure")
    records, _ = obs.read_relevant(limit=5, level="error", since_minutes=1)
    assert [r["code"] for r in records] == ["OLD"]      # just written, so inside

    stale, _ = obs.read_relevant(limit=5, level="error", since_minutes=0)
    assert stale, "since_minutes=0 means no window, not an empty result"


# --- what the agent is handed ------------------------------------------------

def test_read_logs_reports_repeats_as_a_count_not_as_copies(log):
    from orchestration import agent_tools

    for _ in range(12):
        _fail(log, code="HOT_RELOAD_FAILED", message="Could not reload core.parsers.k")
    out = agent_tools.read_logs(level="error", limit=15)

    assert out.count("Could not reload core.parsers.k") == 1
    assert "×12" in out
    assert "collapsed" in out


def test_read_logs_says_so_when_a_narrow_filter_finds_nothing(log):
    from orchestration import agent_tools

    _fail(log)
    out = agent_tools.read_logs(level="error", source_key="no_such_source")
    assert "No log records" in out and "no_such_source" in out


def test_read_logs_includes_a_traceback_only_once(log):
    from orchestration import agent_tools

    for i in range(3):
        try:
            raise ValueError(f"underlying cause {i}")
        except ValueError as exc:
            log.failure(operation="op", code=f"CODE_{i}", message=f"failure {i}",
                        remediation="fix it", exc=exc)
    out = agent_tools.read_logs(level="error", limit=5)
    assert out.count("traceback (newest only)") == 1
