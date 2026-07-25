"""The MCP tool surface — the thin functions a Claude host calls.

Covers the read tools against real/seeded state and the action guards. The live
network actions (run_scraper, fetch_source) are the operator's to exercise; here
we verify their guard logic. The MCP transport (mcp_server.py) is just
registration over these.
"""

from pathlib import Path
from datetime import datetime

import pytest

import core.ingest as ingest
from interfaces import mcp_tools
from core.models import Transaction
from core.tools.service_manifest import Service, ServiceManifest, ServiceManifestError

FIXTURE = Path(__file__).parent / "fixtures" / "sample_owner_statement.pdf"


def test_list_sources_shape():
    sources = mcp_tools.list_sources()
    assert isinstance(sources, list) and sources
    s = sources[0]
    for key in ("key", "label", "status", "is_trigger", "parser_built", "has_scraper"):
        assert key in s


def test_latest_transactions_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    result = mcp_tools.latest_transactions()
    assert result["count"] == 0 and result["transactions"] == []


def test_latest_transactions_after_ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="epic_property_management", label="Epic",
                         parser="buildium_owner_statement", status="implemented"))
    ingest.ingest_source("epic_property_management", FIXTURE, manifest=manifest)

    result = mcp_tools.latest_transactions()
    assert result["count"] == 6
    assert result["money_in"] + abs(result["money_out"]) > 0
    t = result["transactions"][0]
    assert "date" in t and "amount" in t and "fields" in t
    assert set(t["fields"]) >= {"Date", "Property", "Amount"}


def test_run_scraper_requires_a_built_scraper():
    with pytest.raises(mcp_tools.ToolError, match="No scraper built"):
        mcp_tools.run_scraper("first_federal_loan")


def test_run_scraper_returns_steps(monkeypatch):
    monkeypatch.setattr(mcp_tools, "has_scraper", lambda source_key: True)

    txn = Transaction(
        source_key="epic_property_management",
        date=datetime(2026, 7, 24),
        amount=50.0,
        description="Rent",
        fields={"Amount": "50.00"},
        source_uri="https://example.com",
    )

    monkeypatch.setattr(mcp_tools, "get_scraper", lambda source_key: (lambda: [txn]))
    monkeypatch.setattr(mcp_tools, "persist_scraped", lambda txns, source_uri: {"run_path": "data/parsed/demo.json"})

    result = mcp_tools.run_scraper("epic_property_management")
    assert isinstance(result.get("steps"), list)
    assert any(s.get("key") == "run_scraper" and s.get("status") == "success" for s in result["steps"])
    assert any(s.get("key") == "persist_scraped" and s.get("status") == "success" for s in result["steps"])


def test_run_scraper_failure_returns_structured_toolerror(monkeypatch):
    monkeypatch.setattr(mcp_tools, "has_scraper", lambda source_key: True)

    def bad_scraper():
        raise RuntimeError("scrape boom")

    monkeypatch.setattr(mcp_tools, "get_scraper", lambda source_key: bad_scraper)
    with pytest.raises(mcp_tools.ToolError) as exc:
        mcp_tools.run_scraper("epic_property_management")
    detail = exc.value.args[0]
    assert detail["message"] == "scrape boom"
    assert any(s.get("key") == "run_scraper" and s.get("status") == "failed" for s in detail["steps"])


def test_activate_parser_requires_a_built_parser():
    with pytest.raises(mcp_tools.ToolError, match="No parser built"):
        mcp_tools.activate_parser("first_federal_loan")


def test_ingest_document_missing_file():
    with pytest.raises(mcp_tools.ToolError, match="No file at"):
        mcp_tools.ingest_document("epic_property_management", "/nope/missing.pdf")


def test_status_and_llm_status_shape():
    st = mcp_tools.status()
    assert "llm" in st and "sources_total" in st and "pending_approvals" in st
    assert "configured" in mcp_tools.llm_status()


def test_fetch_source_returns_steps(monkeypatch):
    def fake_fetch_and_ingest(source_key, on_step=None):
        if on_step:
            on_step({"key": "search_messages", "label": "Search messages", "status": "success"})
            on_step({"key": "fetch_complete", "label": "Fetch complete", "status": "success"})
        return [{"run_path": "data/parsed/x.json", "transaction_count": 3}]

    monkeypatch.setattr(
        mcp_tools,
        "fetch_and_ingest",
        fake_fetch_and_ingest,
    )
    result = mcp_tools.fetch_source("email")
    assert isinstance(result.get("steps"), list)
    assert any(s.get("key") == "search_messages" and s.get("status") == "success" for s in result["steps"])
    assert any(s.get("key") == "fetch_and_ingest" and s.get("status") == "success" for s in result["steps"])


def test_fetch_source_failure_returns_structured_toolerror(monkeypatch):
    def boom(source_key, on_step=None):
        if on_step:
            on_step({"key": "search_messages", "label": "Search messages", "status": "success"})
        raise RuntimeError("fetch boom")

    monkeypatch.setattr(mcp_tools, "fetch_and_ingest", boom)
    with pytest.raises(mcp_tools.ToolError) as exc:
        mcp_tools.fetch_source("email")
    detail = exc.value.args[0]
    assert detail["message"] == "fetch boom"
    assert any(s.get("key") == "fetch_and_ingest" and s.get("status") == "failed" for s in detail["steps"])


def test_start_login_recovery_missing_login_url(monkeypatch):
    monkeypatch.setattr(
        mcp_tools,
        "_load_services",
        lambda: [Service(key="epic_property_management", label="Epic")],
    )
    with pytest.raises(mcp_tools.ToolError, match="no login_url"):
        mcp_tools.start_login_recovery("epic_property_management")


def test_start_login_recovery_and_status(monkeypatch):
    monkeypatch.setattr(
        mcp_tools,
        "_load_services",
        lambda: [Service(key="epic_property_management", label="Epic", login_url="https://example.com")],
    )

    class DummyProc:
        def __init__(self):
            self.pid = 12345
            self._poll_count = 0

        def poll(self):
            self._poll_count += 1
            return None if self._poll_count == 1 else 0

    proc = DummyProc()
    monkeypatch.setattr(mcp_tools.subprocess, "Popen", lambda *args, **kwargs: proc)
    mcp_tools._LOGIN_RECOVERY_PROCS.clear()

    started = mcp_tools.start_login_recovery("epic_property_management")
    assert started["status"] == "running"
    assert started["pid"] == 12345

    running = mcp_tools.login_recovery_status("epic_property_management")
    assert running["status"] == "running"
    assert any(s.get("key") == "user_login" and s.get("status") == "in-progress" for s in running["steps"])

    done = mcp_tools.login_recovery_status("epic_property_management")
    assert done["status"] == "completed"
    assert any(s.get("key") == "session_saved" and s.get("status") == "success" for s in done["steps"])


@pytest.mark.parametrize("fn", [mcp_tools.list_sources, mcp_tools.pending_approvals, mcp_tools.status])
def test_manifest_errors_map_to_toolerror(monkeypatch, fn):
    def boom(self):
        raise ServiceManifestError("manifest broken")

    monkeypatch.setattr(ServiceManifest, "load", boom)
    with pytest.raises(mcp_tools.ToolError, match="manifest broken"):
        fn()
