"""Adding a data source from the app, instead of hand-editing services.yaml.

The operator's part is three answers at most — how the data arrives, what to call
it, and (for an inbox) which source it feeds. The harness's part is everything
after: its agent writes the parser or scraper, tested, and nothing activates
without approval.

What these pin is the boundary between those two. A new source must land in the
registry in an honest state — planned, no parser, no working route — because the
Ingest screen reads that state to decide what to show. A route that doesn't work
yet must not be advertised as one.
"""

import json

import pytest

from interfaces import mcp_tools
from core.tools.service_manifest import FetchConfig, Service


@pytest.fixture
def registry(monkeypatch):
    """A fake manifest — never the operator's real services.yaml."""
    services = [
        Service(key="epic", label="Epic Property Management",
                parser="core/parsers/epic.py", status="implemented"),
        Service(key="unbuilt", label="Something with no parser yet"),
    ]

    class FakeManifest:
        def add(self, service):
            services.append(service)

        def update(self, key, **fields):
            for i, s in enumerate(services):
                if s.key == key:
                    services[i] = s.model_copy(update=fields)

        def set_fetch(self, key, config):
            for i, s in enumerate(services):
                if s.key == key:
                    services[i] = s.model_copy(update={"fetch": config})

    monkeypatch.setattr(mcp_tools, "_load_services", lambda: list(services))
    monkeypatch.setattr(mcp_tools, "ServiceManifest", FakeManifest)
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: False)
    return services


def _find(services, key):
    return next(s for s in services if s.key == key)


# ── Creating one ─────────────────────────────────────────────────────────────

def test_a_new_document_source_starts_with_nothing_built(registry):
    """The screen decides what to offer from this state, so it must be honest:
    planned, no parser, no route that works."""
    result = mcp_tools.add_source("Chase Business Checking", "document")

    added = _find(registry, "chase_business_checking")
    assert (added.status, added.parser) == ("planned", None)
    assert added.input_type == "document"
    assert result["source_key"] == "chase_business_checking"
    assert result["next"], "the operator is told what happens next"


def test_the_key_is_derived_from_the_name_so_nobody_types_one(registry):
    mcp_tools.add_source("DFCU Financial — Savings (2026)", "document")
    assert any(s.key == "dfcu_financial_savings_2026" for s in registry)


def test_a_website_source_is_marked_as_one(registry):
    """It decides which build the panel offers — a demonstration, not a sample."""
    mcp_tools.add_source("Some Portal", "website")
    assert _find(registry, "some_portal").input_type == "html_scrape"


def test_adding_the_same_source_twice_is_refused(registry):
    mcp_tools.add_source("Chase Business Checking", "document")
    with pytest.raises(mcp_tools.ToolError, match="already here"):
        mcp_tools.add_source("chase business checking", "document")


def test_a_nameless_source_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="name"):
        mcp_tools.add_source("   ", "document")


def test_a_name_with_no_letters_or_digits_is_refused(registry):
    """It would slug to an empty key and quietly collide with the next one."""
    with pytest.raises(mcp_tools.ToolError, match="another name"):
        mcp_tools.add_source("!!! ---", "document")


def test_an_unknown_method_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="Unknown method"):
        mcp_tools.add_source("Whatever", "carrier_pigeon")


# ── An inbox is a route to an existing source, not a source ──────────────────

def test_an_email_route_records_what_it_delivers_to(registry):
    mcp_tools.add_source("Epic inbox", "email", delivers_to="epic")

    carrier = _find(registry, "epic_inbox")
    assert carrier.input_type == "email_trigger"
    assert isinstance(carrier.fetch, FetchConfig) and carrier.fetch.delivers_to == "epic"


def test_an_email_route_to_nowhere_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="delivers to"):
        mcp_tools.add_source("Orphan inbox", "email")
    assert not any(s.key == "orphan_inbox" for s in registry), "nothing half-created"


def test_an_email_route_to_a_source_that_cannot_read_it_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="no parser"):
        mcp_tools.add_source("Pointless inbox", "email", delivers_to="unbuilt")


def test_an_inbox_that_is_not_signed_in_yet_is_not_a_working_route(registry, monkeypatch):
    """THE trap this opens up: routing an inbox to a source is not the same as
    being able to read that inbox. Advertising it would make "Get latest" pick a
    route that cannot run."""
    mcp_tools.add_source("Epic inbox", "email", delivers_to="epic")

    epic = next(s for s in mcp_tools.list_sources() if s["key"] == "epic")
    email_route = next(r for r in epic["transports"] if r["id"] == "email")
    assert email_route["available"] is False
    assert "signed in" in email_route["reason"]
    assert epic["default_transport"] != "email"


def test_once_it_is_signed_in_the_route_works(registry, monkeypatch):
    mcp_tools.add_source("Epic inbox", "email", delivers_to="epic")
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: True)

    epic = next(s for s in mcp_tools.list_sources() if s["key"] == "epic")
    assert next(r for r in epic["transports"] if r["id"] == "email")["available"] is True


# ── Naming, which the harness suggests rather than demands ──────────────────

def test_a_name_is_suggested_from_the_document(registry, tmp_path, monkeypatch):
    doc = tmp_path / "statement.pdf"
    doc.write_text("x")
    monkeypatch.setattr("core.tools.llm_extractor.read_document_text", lambda p: "CHASE BUSINESS…")
    monkeypatch.setattr("orchestration.naming.suggest_label",
                        lambda text, filename="": {"label": "Chase Business Checking", "source": "model"})

    assert mcp_tools.suggest_source_name(str(doc), "statement.pdf") == {
        "label": "Chase Business Checking", "source": "model", "key": "chase_business_checking"}


def test_naming_falls_back_to_the_filename_rather_than_failing(registry, tmp_path, monkeypatch):
    """A suggestion is a convenience. Losing the model must not block the wizard."""
    doc = tmp_path / "2026-07_DFCU-checking.pdf"
    doc.write_text("x")
    monkeypatch.setattr("core.tools.llm_extractor.read_document_text",
                        lambda p: (_ for _ in ()).throw(RuntimeError("unreadable")))

    result = mcp_tools.suggest_source_name(str(doc), doc.name)
    assert result["source"] == "filename"
    assert "Dfcu" in result["label"] or "DFCU" in result["label"]


def test_naming_works_with_no_file_at_all(registry):
    assert mcp_tools.suggest_source_name(filename="wells_fargo_export.csv")["label"] == "Wells Fargo Export"


# ── The demonstration that replaces a URL ───────────────────────────────────

def test_a_demonstration_teaches_the_harness_where_the_portal_is(registry, tmp_path, monkeypatch):
    """The operator was never asked for a URL — the harness records where they
    actually went, which beats what they meant to type."""
    mcp_tools.add_source("Some Portal", "website")
    status = tmp_path / "demo.status.json"
    status.write_text(json.dumps({
        "status": "completed", "demo_path": str(tmp_path / "demo.json"),
        "final_url": "https://portal.example.com/manager/reports",
        "captured_requests": 12, "recorded_actions": 5,
    }))

    class _Done:
        def poll(self):
            return 0

    monkeypatch.setitem(mcp_tools._DEMO_PROCS, "some_portal", _Done())
    monkeypatch.setitem(mcp_tools._DEMO_META, "some_portal", {"status_path": str(status)})

    result = mcp_tools.demo_status("some_portal")

    assert result["status"] == "completed"
    assert _find(registry, "some_portal").login_url == "https://portal.example.com/manager/reports"


def test_no_demonstration_running_reads_as_idle(registry):
    mcp_tools._DEMO_PROCS.pop("some_portal", None)
    assert mcp_tools.demo_status("some_portal")["status"] == "idle"


def test_a_demonstration_for_an_unknown_source_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="Unknown source"):
        mcp_tools.start_demo("nope")


def test_a_scraper_build_no_longer_demands_a_url_when_a_demo_exists(registry, tmp_path, monkeypatch):
    """A captured demonstration already contains where the operator went."""
    mcp_tools.add_source("Some Portal", "website")
    demo = tmp_path / "demo.json"
    demo.write_text("{}")
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)
    started = {}

    class _Proc:
        pid = 999

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        started["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(mcp_tools.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    mcp_tools.start_build("scraper", "some_portal", mode="build", demo_path=str(demo))

    assert "--demo-path" in started["cmd"], "the build must reuse the demonstration, not record another"


def test_a_scraper_build_with_neither_url_nor_demo_says_so(registry, monkeypatch):
    mcp_tools.add_source("Some Portal", "website")
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)

    with pytest.raises(mcp_tools.ToolError, match="no portal URL and no demonstration"):
        mcp_tools.start_build("scraper", "some_portal", mode="build")


def test_a_build_pointed_at_a_missing_demonstration_is_refused(registry, monkeypatch, tmp_path):
    mcp_tools.add_source("Some Portal", "website")
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)

    with pytest.raises(mcp_tools.ToolError, match="No demonstration at"):
        mcp_tools.start_build("scraper", "some_portal", mode="build",
                              demo_path=str(tmp_path / "gone.json"))
