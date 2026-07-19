"""The logging standard itself — record schema, secret masking, retrieval."""

import json

import core.observability as obs


def _point_log_at(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "LOG_DIR", tmp_path)
    monkeypatch.setattr(obs, "LOG_FILE", tmp_path / "agent.jsonl")


def test_failure_writes_full_record_and_returns_actionable_string(tmp_path, monkeypatch):
    _point_log_at(tmp_path, monkeypatch)
    log = obs.get_logger("core.demo")

    try:
        raise ValueError("boom")
    except ValueError as exc:
        human = log.failure(
            operation="do_thing",
            code="THING_FAILED",
            message="The thing failed.",
            remediation="Try the other thing.",
            context={"source_key": "epic", "api_key": "sk-secret"},
            exc=exc,
        )

    assert human == "The thing failed. Try the other thing."
    rec = json.loads((tmp_path / "agent.jsonl").read_text().splitlines()[-1])
    # every schema field present
    for field in ("ts", "level", "component", "operation", "code", "context",
                  "message", "cause", "remediation", "traceback"):
        assert field in rec
    assert rec["level"] == "error"
    assert rec["component"] == "core.demo"
    assert rec["code"] == "THING_FAILED"
    assert rec["cause"] == {"type": "ValueError", "message": "boom"}
    assert "ValueError: boom" in rec["traceback"]


def test_secret_values_masked_but_useful_context_kept(tmp_path, monkeypatch):
    _point_log_at(tmp_path, monkeypatch)
    log = obs.get_logger("core.demo")
    log.failure(
        operation="op", code="C", message="m", remediation="r",
        context={"source_key": "epic", "input": "/p/x.pdf",
                 "api_key": "sk-abc", "client_secret": "xyz", "refresh_token": "rt"},
    )
    rec = json.loads((tmp_path / "agent.jsonl").read_text().splitlines()[-1])
    ctx = rec["context"]
    assert ctx["source_key"] == "epic"   # useful field kept in the clear
    assert ctx["input"] == "/p/x.pdf"
    assert ctx["api_key"] == "<present>"  # secrets masked
    assert ctx["client_secret"] == "<present>"
    assert ctx["refresh_token"] == "<present>"


def test_read_recent_filters_by_level(tmp_path, monkeypatch):
    _point_log_at(tmp_path, monkeypatch)
    log = obs.get_logger("core.demo")
    log.event(operation="ok", code="DONE", message="fine")
    log.failure(operation="bad", code="OOPS", message="nope", remediation="fix it")

    assert len(obs.read_recent()) == 2
    errors = obs.read_recent(level="error")
    assert len(errors) == 1 and errors[0]["code"] == "OOPS"
