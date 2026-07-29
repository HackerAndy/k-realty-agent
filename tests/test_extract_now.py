"""Reading a document with the model instead of a parser.

The harness's rule is that a source is handled by verified, committed code. But
a parser takes an agent build to write, and layouts change without warning — so
there has to be an answer to "I need this month's numbers today". That answer is
the model, with two properties that must hold:

  * it is OFFERED, never substituted silently. Model output is unverified, and
    data that was guessed at must not look like data that was parsed;
  * the operator is told, accurately, where the document's text goes — which
    depends on whether the configured model runs on their own network.
"""

import json
from datetime import datetime

import pytest

from core.models import Transaction
from core.tools.llm_provider import LLMChoice
from interfaces import mcp_tools
from core.tools.service_manifest import Service

_CHOICE = LLMChoice(provider="anthropic", model="claude-opus-4-8", model_source="settings")


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(mcp_tools, "_load_services", lambda: [
        Service(key="epic", label="Epic", parser="core/parsers/epic.py", status="implemented"),
        Service(key="fresh", label="A brand new source"),
    ])


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "statement.pdf"
    path.write_bytes(b"%PDF-1.4 pretend")
    return path


@pytest.fixture
def llm_ready(monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)


def _txn(source_key="fresh", amount=-10.0):
    return Transaction(source_key=source_key, date=datetime(2026, 7, 1),
                       amount=amount, description="x")


def _run(method, count=2):
    return {"run_path": "data/parsed/fresh-2026-07.json", "extraction_method": method,
            "transactions": [_txn().model_dump(mode="json") for _ in range(count)]}


# ── extract_now: a source with no parser at all ──────────────────────────────

def test_it_reads_a_document_for_a_source_with_no_parser(sources, doc, llm_ready, monkeypatch):
    import core.ingest as ingest

    monkeypatch.setattr(ingest, "ingest_via_llm", lambda k, p, **kw: _run("llm_extract"))

    result = mcp_tools.extract_now("fresh", str(doc))

    assert result["count"] == 2
    assert result["extraction_method"] == "llm_extract", "the screen keys its warning off this"


def test_it_refuses_before_reading_anything_when_no_model_is_set_up(sources, doc, monkeypatch):
    """Failing after the document is read wastes the slow part of the work."""
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: False)
    called = []
    import core.ingest as ingest
    monkeypatch.setattr(ingest, "ingest_via_llm", lambda *a, **k: called.append(1))

    with pytest.raises(mcp_tools.ToolError, match="No LLM provider"):
        mcp_tools.extract_now("fresh", str(doc))
    assert not called


def test_a_missing_file_is_refused(sources, llm_ready, tmp_path):
    with pytest.raises(mcp_tools.ToolError, match="No file at"):
        mcp_tools.extract_now("fresh", str(tmp_path / "gone.pdf"))


def test_an_unknown_source_is_refused(sources, doc, llm_ready):
    with pytest.raises(mcp_tools.ToolError, match="Unknown source"):
        mcp_tools.extract_now("nope", str(doc))


def test_model_read_data_records_the_route_it_arrived_by(tmp_path, monkeypatch):
    """The funnel draws the last-used route solid, so an LLM read still has to
    say it came in by upload — otherwise the drawing goes back to the default."""
    import core.ingest as ingest
    from core.tools import llm_extractor

    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    monkeypatch.setattr(llm_extractor, "read_document_text", lambda p: "text", raising=False)
    monkeypatch.setattr(llm_extractor, "extract_with_model",
                        lambda *a: ([_txn(amount=1.0)], _CHOICE), raising=False)

    class FakeManifest:
        def get(self, key):
            return Service(key=key, label="Fresh")

    run = ingest.ingest_via_llm("fresh", tmp_path / "doc.pdf", manifest=FakeManifest())

    assert run["transport"] == "upload"
    assert run["extraction_method"] == "llm_extract"
    assert run["parser"] is None, "no parser was involved, and the record must not imply one"


# ── ingest_document: a parser exists but couldn't read this layout ───────────

def _spy_ingest(monkeypatch, method):
    """Record the fallback flag ingest_source was actually called with."""
    seen = {}

    def fake(key, path, allow_llm_fallback=False):
        seen["flag"] = allow_llm_fallback
        return _run(method)

    monkeypatch.setattr(mcp_tools, "ingest_source", fake)
    return seen


def test_the_fallback_is_off_unless_asked_for(sources, doc, monkeypatch):
    """The default path must never quietly hand a document to a model."""
    seen = _spy_ingest(monkeypatch, "deterministic_parser")

    mcp_tools.ingest_document("epic", str(doc))
    assert seen["flag"] is False


def test_asking_for_the_fallback_passes_it_through(sources, doc, llm_ready, monkeypatch):
    seen = _spy_ingest(monkeypatch, "llm_fallback")

    result = mcp_tools.ingest_document("epic", str(doc), allow_llm_fallback=True)

    assert seen["flag"] is True
    assert result["extraction_method"] == "llm_fallback"


def test_the_fallback_also_needs_a_model_to_be_set_up(sources, doc, monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: False)
    with pytest.raises(mcp_tools.ToolError, match="No LLM provider"):
        mcp_tools.ingest_document("epic", str(doc), allow_llm_fallback=True)


# ── What the screen is told ──────────────────────────────────────────────────

def test_the_view_reports_how_its_data_was_read(tmp_path, monkeypatch):
    """Without this the table can't warn that nothing verified these rows."""
    import core.ingest as ingest

    run = {"source_key": "fresh", "month": "2026-07", "parsed_at": "2026-07-28T00:00:00Z",
           "transport": "upload", "extraction_method": "llm_extract",
           "transactions": [_txn(amount=-5.0).model_dump(mode="json")]}
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path)
    (tmp_path / "fresh-2026-07.json").write_text(json.dumps(run))
    monkeypatch.setattr(mcp_tools, "load_latest_parsed_for", ingest.load_latest_parsed_for)

    assert mcp_tools.source_transactions("fresh")["extraction_method"] == "llm_extract"


@pytest.mark.parametrize("cfg,offsite", [
    ({"provider": "anthropic"}, True),
    ({"provider": "openai_compatible", "base_url": "http://127.0.0.1:9090/v1"}, False),
    ({"provider": "openai_compatible", "base_url": "http://klabss-macbook-pro.local:9090/v1"}, False),
    ({"provider": "openai_compatible", "base_url": "http://192.168.1.40:9090/v1"}, False),
    ({"provider": "openai_compatible", "base_url": "https://api.together.xyz/v1"}, True),
    ({}, True),
])
def test_where_the_text_goes_is_computed_not_assumed(cfg, offsite, monkeypatch):
    """Telling the operator "this leaves your machine" when the model runs on
    their own LAN would be a lie; so would the reverse."""
    monkeypatch.setattr(mcp_tools.llm_provider, "current_config", lambda: cfg)
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: bool(cfg))

    status = mcp_tools.llm_status()
    assert status["offsite"] is offsite
    assert status["destination"]


# ── The upload that failed keeps its document ────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from interfaces import rest_server

    monkeypatch.setattr(rest_server, "REPO_ROOT", tmp_path)
    return TestClient(rest_server.app), tmp_path, rest_server


def _upload(api_client, key="epic"):
    return api_client.post(f"/api/upload_ingest/{key}",
                           files={"file": ("july.pdf", b"%PDF-1.4 statement", "application/pdf")})


def test_a_document_the_parser_cannot_read_is_kept_for_a_retry(api, monkeypatch):
    """Otherwise the temp file is deleted and the offer to read it with the
    model has nothing to point at — the operator must re-upload."""
    from core.parsers import ParseError

    client, root, _ = api

    def boom(source_key, path):
        raise ParseError("could not read this layout")

    monkeypatch.setattr(mcp_tools, "ingest_document", boom)

    res = _upload(client)

    assert res.status_code >= 400
    detail = res.json()["detail"]
    kept = root / "data" / "samples" / "epic-unparsed.pdf"
    assert detail["retry_path"] == str(kept)
    assert kept.read_bytes() == b"%PDF-1.4 statement", "the operator's own document, intact"
    assert detail["filename"] == "july.pdf", "so the offer can name the file"


def test_a_failure_the_model_cannot_help_with_keeps_nothing(api, monkeypatch):
    """A bad source key isn't a retry case, and a stray financial document on
    disk is a cost with no benefit."""
    from core.ingest import IngestError

    client, root, _ = api

    def boom(source_key, path):
        raise IngestError("Unknown source 'typo'.")

    monkeypatch.setattr(mcp_tools, "ingest_document", boom)

    detail = _upload(client, key="typo").json()["detail"]

    assert "retry_path" not in detail
    assert not (root / "data" / "samples").exists()


def test_a_parser_that_crashes_outright_is_still_a_retry_case(api, monkeypatch):
    """Real parsers fail with IndexError as often as with ParseError, and the
    operator's position is identical either way: their document wasn't read."""
    client, root, _ = api

    def boom(source_key, path):
        raise IndexError("list index out of range")

    monkeypatch.setattr(mcp_tools, "ingest_document", boom)

    assert "retry_path" in _upload(client).json()["detail"]


def test_a_source_with_no_parser_yet_is_a_retry_case(api, monkeypatch):
    """That's exactly what extract_now is for."""
    from core.ingest import IngestError

    client, root, _ = api

    def boom(source_key, path):
        raise IngestError("No parser built for 'fresh' yet (status: planned).")

    monkeypatch.setattr(mcp_tools, "ingest_document", boom)

    assert "retry_path" in _upload(client, key="fresh").json()["detail"]


def test_a_successful_upload_leaves_no_copy_behind(api, monkeypatch):
    client, root, _ = api
    monkeypatch.setattr(mcp_tools, "ingest_document",
                        lambda source_key, path: {"count": 3, "run_path": "x"})

    assert _upload(client).status_code == 200
    assert not (root / "data" / "samples").exists(), "financial documents aren't kept without a reason"


def test_the_destination_names_the_server_the_operator_configured(monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "current_config",
                        lambda: {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9090/v1"})
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)

    assert "127.0.0.1" in mcp_tools.llm_status()["destination"]
