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


def test_source_transactions_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    result = mcp_tools.source_transactions("epic_property_management")
    assert result["source_key"] == "epic_property_management"
    assert result["count"] == 0 and result["transactions"] == []


def test_source_transactions_scopes_to_one_source(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="epic_property_management", label="Epic",
                         parser="buildium_owner_statement", status="implemented"))
    ingest.ingest_source("epic_property_management", FIXTURE, manifest=manifest)

    # A run for a DIFFERENT source must not leak into this source's view.
    other = Transaction(source_key="dfcu_financial_bank", date=datetime(2026, 7, 1),
                        amount=99.0, description="x", fields={"Amount": "99.00"})
    ingest._persist("dfcu_financial_bank", [other], FIXTURE, "test", None)

    result = mcp_tools.source_transactions("epic_property_management")
    assert result["source_key"] == "epic_property_management"
    assert result["count"] == 6
    assert all("Property" in t["fields"] for t in result["transactions"])


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


@pytest.mark.parametrize(
    "args, message",
    [
        ({"provider": "bogus"}, "Unknown provider"),
        ({"provider": "anthropic"}, "API key is required"),
        ({"provider": "openai_compatible", "model": "m"}, "base URL is required"),
        ({"provider": "openai_compatible", "base_url": "http://x/v1"}, "model is required"),
    ],
)
def test_set_llm_provider_rejects_incomplete_config(monkeypatch, args, message):
    """Guards must fire BEFORE anything is written — a half-configured provider
    would silently break every agent run."""
    def boom(*a, **k):
        raise AssertionError("must not store an incomplete provider")

    monkeypatch.setattr(mcp_tools.llm_provider, "store_llm_credential", boom)
    with pytest.raises(mcp_tools.ToolError, match=message):
        mcp_tools.set_llm_provider(**args)


def test_set_llm_provider_stores_and_loads(monkeypatch):
    stored = {}
    monkeypatch.setattr(mcp_tools.llm_provider, "store_llm_credential",
                        lambda provider, **kw: stored.update({"provider": provider, **kw}))
    monkeypatch.setattr(mcp_tools.llm_provider, "load_into_env", lambda: True)
    # Never read the operator's real vault from a test — a failure would print
    # their actual key into pytest output.
    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key", lambda provider=None: None)
    monkeypatch.setattr(mcp_tools, "llm_status", lambda: {"configured": True})

    result = mcp_tools.set_llm_provider("openai_compatible", base_url=" http://x/v1 ", model=" m ")
    assert result["saved"] is True
    # Whitespace trimmed so a stray paste can't produce an unreachable URL.
    assert stored == {"provider": "openai_compatible", "api_key": None,
                      "base_url": "http://x/v1", "model": "m", "activate": True}


def test_set_llm_provider_keeps_stored_key_when_field_left_blank(monkeypatch):
    """CredentialStore.set() replaces the whole record, so saving with a blank key
    field used to WIPE the stored key. Blank must mean 'keep what's saved'."""
    stored = {}
    monkeypatch.setattr(mcp_tools.llm_provider, "store_llm_credential",
                        lambda provider, **kw: stored.update({"provider": provider, **kw}))
    monkeypatch.setattr(mcp_tools.llm_provider, "load_into_env", lambda: True)
    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key",
                        lambda provider=None: "sk-from-vault" if provider == "anthropic" else None)
    monkeypatch.setattr(mcp_tools, "llm_status", lambda: {"configured": True})

    result = mcp_tools.set_llm_provider("anthropic", api_key=None, model="claude-opus-4-8")
    assert stored["api_key"] == "sk-from-vault"
    assert result["reused_stored_api_key"] is True


def test_set_llm_provider_does_not_reuse_a_key_across_providers(monkeypatch):
    """An Anthropic key must never be handed to a local OpenAI-compatible server."""
    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key",
                        lambda provider=None: None)  # guard says: different provider

    with pytest.raises(mcp_tools.ToolError, match="API key is required"):
        mcp_tools.set_llm_provider("anthropic")


def test_llm_status_reports_all_three_providers_and_which_is_armed(monkeypatch):
    """The picker draws itself from this: three cards, a tick on one. A payload
    that only described the active provider is what made the Settings page label
    the Claude Code CLI 'Local / OpenAI-compatible'."""
    saved = {"anthropic": {"api_key": "sk", "model": "claude-opus-4-8"},
             "openai_compatible": {}, "claude_code": {}}
    # Both patched: resolve() reads them, and a test must never reach the
    # operator's real vault — a failure would print their actual key.
    monkeypatch.setattr(mcp_tools.llm_provider, "settings_for", lambda p: saved[p])
    monkeypatch.setattr(mcp_tools.llm_provider, "configured_provider", lambda: "anthropic")
    monkeypatch.setattr(mcp_tools.llm_provider, "current_config",
                        lambda: {"provider": "anthropic", "model": "claude-opus-4-8"})
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)

    providers = mcp_tools.llm_status()["providers"]

    assert [p["provider"] for p in providers] == list(mcp_tools.llm_provider.PROVIDERS)
    assert [p["active"] for p in providers].count(True) == 1
    assert next(p for p in providers if p["active"])["provider"] == "anthropic"
    assert {p["label"] for p in providers} == {"Claude API", "Local server", "Claude Code CLI"}
    # A provider with nothing saved says so, and says what would fix it, rather
    # than offering a switch that arms something the next run can't use.
    local = next(p for p in providers if p["provider"] == "openai_compatible")
    assert local["blocked"] and local["blocked_fix"] == "settings"
    # Never the key itself, for any provider.
    assert all("api_key" not in p for p in providers)


def test_the_screen_and_the_save_path_agree_on_what_is_configured(monkeypatch):
    """One validator, called by both. Two copies drift silently: the picker offers
    a switch, the save behind it refuses, and neither explains the other."""
    monkeypatch.setattr(mcp_tools.llm_provider, "settings_for", lambda p: {})
    monkeypatch.setattr(mcp_tools.llm_provider, "configured_provider", lambda: "anthropic")
    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key", lambda provider=None: None)

    reported = next(p for p in mcp_tools.llm_status()["providers"]
                    if p["provider"] == "anthropic")["blocked"]
    with pytest.raises(mcp_tools.ToolError) as refused:
        mcp_tools.set_llm_provider("anthropic")

    assert reported == str(refused.value)


def test_llm_status_reports_key_presence_not_the_key(monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "current_config",
                        lambda: {"provider": "anthropic", "model": "m", "api_key": "<stored>"})
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)

    st = mcp_tools.llm_status()
    assert st["api_key_set"] is True
    assert "api_key" not in st


def test_list_llm_models_falls_back_to_the_stored_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key", lambda provider=None: "local-key")

    def capture(base_url, api_key=None):
        seen["api_key"] = api_key
        return ["m1"]

    monkeypatch.setattr(mcp_tools.llm_provider, "list_models", capture)
    assert mcp_tools.list_llm_models("http://x/v1")["models"] == ["m1"]
    assert seen["api_key"] == "local-key"


def test_list_llm_models_surfaces_the_real_reason(monkeypatch):
    def unreachable(base_url, api_key=None):
        raise RuntimeError("could not reach http://x/v1/models: No route to host")

    monkeypatch.setattr(mcp_tools.llm_provider, "list_models", unreachable)
    with pytest.raises(mcp_tools.ToolError, match="No route to host"):
        mcp_tools.list_llm_models("http://x/v1")


@pytest.mark.parametrize("fn", [mcp_tools.list_sources, mcp_tools.pending_approvals, mcp_tools.status])
def test_manifest_errors_map_to_toolerror(monkeypatch, fn):
    def boom(self):
        raise ServiceManifestError("manifest broken")

    monkeypatch.setattr(ServiceManifest, "load", boom)
    with pytest.raises(mcp_tools.ToolError, match="manifest broken"):
        fn()
