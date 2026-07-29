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
from core.tools.service_manifest import EmailSearch, Service


@pytest.fixture
def registry(monkeypatch):
    """A fake manifest — never the operator's real services.yaml."""
    services = [
        Service(key="epic", label="Epic Property Management",
                parser="core/parsers/epic.py", status="implemented"),
        Service(key="unbuilt", label="Something with no parser yet"),
        # An inbox: a shared way in, connected once under Settings.
        Service(key="inbox", label="Email", input_type="email_trigger", provider="gmail"),
    ]

    class FakeManifest:
        def add(self, service):
            services.append(service)

        def update(self, key, **fields):
            for i, s in enumerate(services):
                if s.key == key:
                    services[i] = s.model_copy(update=fields)

        def set_email_search(self, key, search):
            for i, s in enumerate(services):
                if s.key == key:
                    services[i] = s.model_copy(update={"email_search": search})

    monkeypatch.setattr(mcp_tools, "_load_services", lambda: list(services))
    monkeypatch.setattr(mcp_tools, "ServiceManifest", FakeManifest)
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: True)
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


# ── Arriving by email: the source is the document, the inbox is the way in ───

def test_an_emailed_source_records_which_inbox_carries_it(registry):
    """The new source IS the statement that arrives. It is not an inbox, and it
    doesn't deliver to anything: it searches an inbox that already exists."""
    mcp_tools.add_source("Epic statement", "email", carrier="inbox",
                         from_address="mail@managebuilding.com",
                         subject_contains="Owner Statement")

    added = _find(registry, "epic_statement")
    assert added.input_type == "document", "what arrives is a document to parse"
    assert isinstance(added.email_search, EmailSearch)
    assert added.email_search.carrier == "inbox"
    assert added.email_search.from_address == "mail@managebuilding.com"


def test_an_emailed_source_with_no_inbox_named_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="which inbox"):
        mcp_tools.add_source("Orphan statement", "email")
    assert not any(s.key == "orphan_statement" for s in registry), "nothing half-created"


def test_pointing_a_new_source_at_a_non_inbox_is_refused(registry):
    with pytest.raises(mcp_tools.ToolError, match="which inbox"):
        mcp_tools.add_source("Confused statement", "email", carrier="epic")


def test_an_inbox_that_is_not_signed_in_yet_is_not_a_working_route(registry, monkeypatch):
    """THE trap: naming an inbox is not the same as being able to read it.
    Advertising the route would make "Get latest" pick one that cannot run."""
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: True)
    mcp_tools.add_source("Epic statement", "email", carrier="inbox")
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: False)

    added = next(s for s in mcp_tools.list_sources() if s["key"] == "epic_statement")
    email_route = next(r for r in added["transports"] if r["id"] == "email")
    assert email_route["available"] is False
    assert "signed in" in email_route["reason"]
    assert added["default_transport"] != "email"


def test_once_it_is_signed_in_the_route_works(registry, monkeypatch):
    mcp_tools.add_source("Epic statement", "email", carrier="inbox")

    added = next(s for s in mcp_tools.list_sources() if s["key"] == "epic_statement")
    assert next(r for r in added["transports"] if r["id"] == "email")["available"] is True


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


def test_a_source_can_be_demonstrated_before_it_has_a_name(registry, monkeypatch, tmp_path):
    """The whole flow depends on this: the agent looks FIRST, and what it saw is
    how the source gets named. So the staging key needs no manifest entry."""
    started = {}

    class _Proc:
        pid = 7

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        started["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(mcp_tools.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    assert mcp_tools.start_demo(mcp_tools.STAGING_KEY)["status"] == "running"
    assert mcp_tools.STAGING_KEY in started["cmd"]


def test_a_staged_demonstration_moves_onto_the_real_key(registry, monkeypatch, tmp_path):
    """Otherwise the operator demonstrates it once, names it, and the harness
    asks them to demonstrate it again — with a second sign-in."""
    monkeypatch.chdir(tmp_path)
    demos = tmp_path / "data" / "demos"
    demos.mkdir(parents=True)
    (demos / "_new-demonstration.json").write_text("{}")
    (demos / "_new-demo.har").write_text("{}")
    profile = tmp_path / ".browser_profiles" / "_new"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("session")

    result = mcp_tools.add_source("Some Portal", "website", adopt_staged=True,
                                  login_url="https://portal.example.com/reports")

    assert result["demo_path"].endswith("some_portal-demonstration.json")
    assert (demos / "some_portal-demonstration.json").exists()
    assert (demos / "some_portal-demo.har").exists()
    assert not (demos / "_new-demonstration.json").exists()
    # The signed-in session lives in the profile directory — losing it means
    # signing in again for nothing.
    assert (tmp_path / ".browser_profiles" / "some_portal" / "Cookies").read_text() == "session"
    assert result["session_kept"] is True


def test_the_url_it_landed_on_is_stored_when_the_source_is_named(registry, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mcp_tools.add_source("Some Portal", "website", login_url="https://portal.example.com/reports")
    assert _find(registry, "some_portal").login_url == "https://portal.example.com/reports"


def test_a_staged_sample_moves_across_too(registry, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    samples = tmp_path / "data" / "samples"
    samples.mkdir(parents=True)
    (samples / "_new-sample.pdf").write_bytes(b"%PDF")

    result = mcp_tools.add_source("Shellpoint Mortgage", "document", adopt_staged=True)

    assert (samples / "shellpoint_mortgage-sample.pdf").exists()
    assert result["sample_path"].endswith("shellpoint_mortgage-sample.pdf")


def test_adding_without_adopting_leaves_the_staged_files_alone(registry, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    demos = tmp_path / "data" / "demos"
    demos.mkdir(parents=True)
    (demos / "_new-demonstration.json").write_text("{}")

    mcp_tools.add_source("Typed By Hand", "document")

    assert (demos / "_new-demonstration.json").exists()


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
