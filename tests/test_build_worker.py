"""The background build runner and its GUI-facing status polling.

The agent itself is never invoked here — the workflows are stubbed. What matters
is the seam: the worker's JSONL protocol, and that build_status() reports "ran"
and "passed" as SEPARATE facts. Conflating them would let the GUI imply a source
is ready when its test actually failed, which the test gate exists to prevent.
"""

import json
from pathlib import Path

import pytest

from interfaces import mcp_tools
from orchestration import build_worker
from core.tools.service_manifest import Service


def _lines(run_file: Path) -> list[dict]:
    return [json.loads(line) for line in run_file.read_text().splitlines()]


# --- worker protocol ---------------------------------------------------------

def test_worker_streams_events_then_result(tmp_path, monkeypatch):
    run_file = tmp_path / "run.jsonl"
    sample = tmp_path / "statement.pdf"
    sample.write_bytes(b"%PDF-1.4")

    def fake_build(source_key, sample_path, source_label="", on_event=print):
        on_event("studying the existing parser")
        on_event("wrote the test")
        return {"source_key": source_key, "verification": {"ok": True}}

    monkeypatch.setattr("orchestration.build_parser.build_parser_for_source", fake_build)

    rc = build_worker.run("parser", "build", "dfcu_financial_bank", run_file, sample_path=str(sample))

    assert rc == 0
    recs = _lines(run_file)
    assert [r["type"] for r in recs] == ["event", "event", "result"]
    assert recs[0]["text"] == "studying the existing parser"
    assert recs[-1]["result"]["verification"]["ok"] is True
    assert all("ts" in r for r in recs)


def test_worker_records_a_crash_as_a_failed_line(tmp_path, monkeypatch):
    """A crash must land IN the run file — the GUI only sees this file, so an
    exception that only hit stderr would look like a hung build."""
    run_file = tmp_path / "run.jsonl"
    sample = tmp_path / "s.pdf"
    sample.write_bytes(b"x")

    def boom(*a, **k):
        raise RuntimeError("model refused")

    monkeypatch.setattr("orchestration.build_parser.build_parser_for_source", boom)

    rc = build_worker.run("parser", "build", "k", run_file, sample_path=str(sample))

    assert rc == 1
    rec = _lines(run_file)[-1]
    assert rec["type"] == "failed"
    assert rec["error"] == "model refused"
    assert "RuntimeError" in rec["traceback"]


def test_worker_rejects_a_missing_sample(tmp_path):
    run_file = tmp_path / "run.jsonl"
    rc = build_worker.run("parser", "build", "k", run_file, sample_path=str(tmp_path / "nope.pdf"))
    assert rc == 1
    assert "no sample document" in _lines(run_file)[-1]["error"]


# --- start_build guards ------------------------------------------------------

@pytest.fixture
def one_source(monkeypatch, tmp_path):
    # start_build touches its run file BEFORE spawning the worker, so without an
    # isolated cwd these tests litter the operator's real data/logs/builds/ with
    # empty run files on every suite run.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_tools, "_load_services",
                        lambda: [Service(key="dfcu_financial_bank", label="DFCU")])
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)
    mcp_tools._BUILD_PROCS.clear()
    mcp_tools._BUILD_META.clear()


def test_start_build_rejects_unknown_kind_and_mode(one_source):
    with pytest.raises(mcp_tools.ToolError, match="Unknown build kind"):
        mcp_tools.start_build("fetcher", "dfcu_financial_bank")
    with pytest.raises(mcp_tools.ToolError, match="Unknown build mode"):
        mcp_tools.start_build("parser", "dfcu_financial_bank", mode="yolo")


def test_start_build_requires_a_sample_for_a_parser(one_source):
    with pytest.raises(mcp_tools.ToolError, match="sample document is required"):
        mcp_tools.start_build("parser", "dfcu_financial_bank")


def test_scraper_revise_needs_no_sample_or_feedback(one_source, monkeypatch):
    """Revise reads the harness's own failure logs, so a broken scrape is fixable
    without re-demonstrating and without the operator diagnosing it first."""
    launched = {}

    class Running:
        pid = 7
        def poll(self):
            return None

    def capture(cmd, **kwargs):
        launched["cmd"] = cmd
        return Running()

    monkeypatch.setattr(mcp_tools.subprocess, "Popen", capture)
    started = mcp_tools.start_build("scraper", "dfcu_financial_bank", mode="revise")

    assert started["status"] == "running" and started["kind"] == "scraper"
    assert "--sample-path" not in launched["cmd"]
    assert "--mode" in launched["cmd"] and "revise" in launched["cmd"]


def test_worker_dispatches_a_scraper_revise_to_the_right_workflow(tmp_path, monkeypatch):
    run_file = tmp_path / "run.jsonl"
    seen = {}

    def fake_revise(source_key, feedback="", on_event=print):
        seen["source_key"] = source_key
        seen["feedback"] = feedback
        on_event("read_logs: HTTP_ERROR 403 on the transactions POST")
        return {"source_key": source_key, "verification": {"ok": True, "registered": True}}

    monkeypatch.setattr("orchestration.build_scraper.revise_scraper_for_source", fake_revise)

    rc = build_worker.run("scraper", "revise", "epic_property_management", run_file,
                          feedback="the transactions POST returns 403")

    assert rc == 0
    assert seen["source_key"] == "epic_property_management"
    assert "403" in seen["feedback"]
    assert _lines(run_file)[-1]["result"]["verification"]["ok"] is True


def test_start_build_requires_a_configured_llm(one_source, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: False)
    sample = tmp_path / "s.pdf"
    sample.write_bytes(b"x")
    with pytest.raises(mcp_tools.ToolError, match="No LLM provider"):
        mcp_tools.start_build("parser", "dfcu_financial_bank", sample_path=str(sample))


def test_start_build_refuses_a_second_concurrent_build(one_source, tmp_path, monkeypatch):
    """Two agents editing the same parser file would race and corrupt it."""
    sample = tmp_path / "s.pdf"
    sample.write_bytes(b"x")

    class Running:
        pid = 1
        def poll(self):
            return None

    monkeypatch.setattr(mcp_tools.subprocess, "Popen", lambda *a, **k: Running())
    mcp_tools.start_build("parser", "dfcu_financial_bank", sample_path=str(sample))

    with pytest.raises(mcp_tools.ToolError, match="already running"):
        mcp_tools.start_build("parser", "dfcu_financial_bank", sample_path=str(sample))


# --- build_status ------------------------------------------------------------

class _Proc:
    pid = 42
    def __init__(self, code):
        self._code = code
    def poll(self):
        return self._code


def _seed(tmp_path, monkeypatch, lines, exit_code):
    run_file = tmp_path / "run.jsonl"
    run_file.write_text("".join(json.dumps(x) + "\n" for x in lines))
    monkeypatch.setitem(mcp_tools._BUILD_PROCS, "k", _Proc(exit_code))
    monkeypatch.setitem(mcp_tools._BUILD_META, "k", {
        "kind": "parser", "mode": "build",
        "run_file": str(run_file), "log_path": str(tmp_path / "run.log"),
    })
    return run_file


def test_build_status_idle_when_nothing_started():
    mcp_tools._BUILD_PROCS.pop("nobody", None)
    assert mcp_tools.build_status("nobody")["status"] == "idle"


def test_build_status_running_streams_events(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [
        {"type": "event", "text": "one"}, {"type": "event", "text": "two"},
    ], exit_code=None)

    st = mcp_tools.build_status("k")
    assert st["status"] == "running"
    assert st["events"] == ["one", "two"] and st["event_count"] == 2
    # Tailing: only what's new after the offset.
    assert mcp_tools.build_status("k", event_offset=1)["events"] == ["two"]


def test_build_status_completed_and_passed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [
        {"type": "result", "result": {"verification": {"ok": True}}},
    ], exit_code=0)

    st = mcp_tools.build_status("k")
    assert st["status"] == "completed" and st["passed"] is True
    assert [s["status"] for s in st["steps"]] == ["success", "success"]


def test_build_status_completed_but_test_failed_is_not_passed(tmp_path, monkeypatch):
    """The critical distinction: the build RAN, the code is NOT acceptable."""
    _seed(tmp_path, monkeypatch, [
        {"type": "result", "result": {"verification": {"ok": False, "test": {"output": "1 failed"}}}},
    ], exit_code=0)

    st = mcp_tools.build_status("k")
    assert st["status"] == "completed"
    assert st["passed"] is False
    verify = next(s for s in st["steps"] if s["key"] == "verify")
    assert verify["status"] == "failed"
    assert "test failed" in verify["error"], "the step says WHICH gate refused it"
    assert st["blockers"] == ["Its test failed when the harness re-ran it independently."]


def test_build_status_untested_code_is_not_passed(tmp_path, monkeypatch):
    """fold_untested marks ok False when the agent wrote code with no test."""
    _seed(tmp_path, monkeypatch, [
        {"type": "result", "result": {"verification": {"ok": False, "untested_code": ["core/parsers/k.py"]}}},
    ], exit_code=0)
    assert mcp_tools.build_status("k")["passed"] is False


def test_build_status_failed_surfaces_the_cause(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [
        {"type": "event", "text": "starting"},
        {"type": "failed", "error": "model refused", "traceback": "..."},
    ], exit_code=1)

    st = mcp_tools.build_status("k")
    assert st["status"] == "failed"
    assert st["message"] == "model refused"
    assert next(s for s in st["steps"] if s["key"] == "agent_codegen")["status"] == "failed"


def test_build_status_tolerates_a_half_written_line(tmp_path, monkeypatch):
    """The GUI polls while the worker is mid-flush; a truncated final line must
    not blow up the whole status call."""
    run_file = _seed(tmp_path, monkeypatch, [{"type": "event", "text": "ok"}], exit_code=None)
    with run_file.open("a") as fh:
        fh.write('{"type": "even')

    st = mcp_tools.build_status("k")
    assert st["status"] == "running" and st["events"] == ["ok"]


def test_build_status_nonzero_exit_without_a_result_is_failed(tmp_path, monkeypatch):
    """A killed worker (no result, no failed line) must not read as completed."""
    _seed(tmp_path, monkeypatch, [{"type": "event", "text": "started"}], exit_code=-9)
    assert mcp_tools.build_status("k")["status"] == "failed"


def test_a_refusal_names_the_gate_that_refused_it_not_the_test(tmp_path, monkeypatch):
    """THE misreport, from a real run: verification failed because the agent's last
    turn wrote no file, while its test passed 17/17 — and the screen said "its test
    did NOT pass", sending the operator to debug a test that was fine."""
    _seed(tmp_path, monkeypatch, [
        {"type": "result", "result": {
            "verification": {"ok": False, "no_changes": True,
                             "test": {"ok": True, "output": "17 passed"}},
            "tool_calls": [["read_file", {"path": "core/scrapers/x.py"}],
                           ["read_file", {"path": "core/settings.py"}]],
        }},
    ], exit_code=0)

    st = mcp_tools.build_status("k")

    assert st["passed"] is False
    assert st["blockers"], "a refusal always says why"
    assert "without writing any file" in st["blockers"][0]
    assert not any("test" in b and "failed" in b for b in st["blockers"]), \
        "the test passed; nothing may claim otherwise"
    verify = next(s for s in st["steps"] if s["key"] == "verify")
    assert "17 passed" not in verify["error"], "passing output is not an error message"


def test_a_run_reports_what_it_did_as_acts_not_prose(tmp_path, monkeypatch):
    """The model's own account of a run reached 82,000 characters — unreadable, and
    it buried the outcome. What it DID is a short list, taken from its tool calls."""
    _seed(tmp_path, monkeypatch, [
        {"type": "result", "result": {
            "verification": {"ok": True, "test": {"ok": True}},
            "agent_summary": "x" * 50_000,
            "tool_calls": [
                ["read_file", {"path": "core/scrapers/x.py"}],
                ["write_file", {"path": "core/scrapers/x.py"}],
                ["write_file", {"path": "core/scrapers/x.py"}],
                ["write_file", {"path": "tests/test_scraper_x.py"}],
                ["run_command", {"command": "poetry run pytest tests/test_scraper_x.py"}],
            ],
        }},
    ], exit_code=0)

    did = mcp_tools.build_status("k")["did"]

    assert did["files"] == ["core/scrapers/x.py", "tests/test_scraper_x.py"], "written once each"
    assert did["commands"] == ["poetry run pytest tests/test_scraper_x.py"]
    assert did["reads"] == 1
