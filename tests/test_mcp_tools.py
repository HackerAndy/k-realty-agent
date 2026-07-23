"""The MCP tool surface — the thin functions a Claude host calls.

Covers the read tools against real/seeded state and the action guards. The live
network actions (run_scraper, fetch_source) are the operator's to exercise; here
we verify their guard logic. The MCP transport (mcp_server.py) is just
registration over these.
"""

from pathlib import Path

import pytest

import core.ingest as ingest
from interfaces import mcp_tools
from core.tools.service_manifest import Service, ServiceManifest

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
