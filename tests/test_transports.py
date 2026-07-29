"""Sources versus transports.

The modelling error this fixes: the inbox carrying the Epic statement was listed
as a PEER of Epic, so the operator saw two sources where there is one source with
two ways in. A source is a body of data; a transport is a route it takes.

Also pins the distinction the operator drew: the DEFAULT is what "Get latest"
runs and every working source has one, while AUTOMATION is a further step that
runs the default unattended. A source can legitimately have a default and no
possible automation.
"""

import pathlib

import pytest

from core import transports
from core.transports import EMAIL, SCRAPE, UPLOAD
from core.tools.service_manifest import EmailSearch, Service


def _epic(**kw):
    """Epic's statement arrives by email — the SOURCE says so, and says which
    inbox to look in. Pass email_search=None for a source that doesn't."""
    kw.setdefault("email_search", EmailSearch(carrier="email"))
    return Service(key="epic", label="Epic", parser="buildium_owner_statement",
                   status="implemented", **kw)


def _inbox():
    """An inbox holds the sign-in and nothing else — which source's document is in
    there is declared by that source (Service.email_search)."""
    return Service(key="email", label="Email", input_type="email_trigger", provider="gmail")


def _routes(service, services=None, scraper=False, built=False):
    return transports.transports_for(
        service, services if services is not None else [service],
        has_scraper=lambda k: scraper, parser_built=lambda k: built,
    )


def _by_id(routes):
    return {r["id"]: r for r in routes}


# --- deriving the routes -----------------------------------------------------

def test_the_source_names_the_inbox_that_carries_it():
    epic = _epic(email_search=EmailSearch(carrier="email", from_address="mail@x.com"))
    routes = _by_id(_routes(epic, [epic, _inbox()]))
    assert routes[EMAIL]["available"] is True
    assert routes[EMAIL]["carrier_key"] == "email"
    assert "mail@x.com" in routes[EMAIL]["detail"]


def test_no_email_route_when_the_source_does_not_arrive_that_way():
    epic = _epic(email_search=None)
    assert EMAIL not in _by_id(_routes(epic, [epic, _inbox()]))


def test_one_inbox_carries_several_sources():
    """The reason the search lives on the source: a mailbox is a shared way in,
    and storing 'delivers_to' on it capped a connected account at one source."""
    inbox = _inbox()
    epic = _epic(email_search=EmailSearch(carrier="email", subject_contains="Owner Statement"))
    bank = Service(key="bank", label="Bank", parser="bank", status="implemented",
                   email_search=EmailSearch(carrier="email", from_address="bank@x.com"))
    services = [inbox, epic, bank]

    for source in (epic, bank):
        assert _by_id(_routes(source, services))[EMAIL]["available"] is True


def test_an_inbox_that_was_deleted_says_so_instead_of_vanishing():
    epic = _epic(email_search=EmailSearch(carrier="gone"))
    route = _by_id(_routes(epic, [epic]))[EMAIL]
    assert route["available"] is False
    assert "gone" in route["reason"]


def test_upload_depends_on_an_ACTIVE_parser_not_a_filename():
    """Epic's parser is registered as `buildium_owner_statement`, so a filename
    check reported Epic as unable to accept the very PDF it parses every month."""
    routes = _by_id(_routes(_epic(), built=False))
    assert routes[UPLOAD]["available"] is True


def test_a_built_but_unapproved_parser_is_its_own_state():
    """Not the same as 'no parser' — the fix is one click, not a build."""
    no_parser = Service(key="dfcu", label="DFCU", status="needs_parser")
    routes = _by_id(_routes(no_parser, built=True))
    assert routes[UPLOAD]["available"] is False
    assert "not approved yet" in routes[UPLOAD]["reason"]


def test_an_unavailable_route_still_says_why():
    """'DFCU has no scraper yet' is what tells the operator to build one; hiding
    it makes the source look less capable than it could be."""
    routes = _by_id(_routes(Service(key="dfcu", label="DFCU", parser="dfcu")))
    assert routes[SCRAPE]["available"] is False
    assert "agent can write one" in routes[SCRAPE]["reason"]


def test_only_upload_needs_a_human():
    epic = _epic()
    routes = _by_id(_routes(epic, [epic, _inbox()], scraper=True))
    assert routes[UPLOAD]["unattended"] is False
    assert routes[SCRAPE]["unattended"] is True
    assert routes[EMAIL]["unattended"] is True


# --- the default -------------------------------------------------------------

def test_the_operators_pinned_choice_wins():
    epic = _epic(default_transport=SCRAPE)
    routes = _routes(epic, [epic, _inbox()], scraper=True)
    assert transports.default_transport(epic, routes) == SCRAPE


def test_a_pinned_route_that_stopped_working_falls_back_instead_of_lying():
    """A scraper can be deleted. Offering a dead button is worse than moving on."""
    epic = _epic(default_transport=SCRAPE)
    routes = _routes(epic, [epic, _inbox()], scraper=False)
    assert transports.default_transport(epic, routes) == EMAIL


def test_unpinned_prefers_the_route_needing_least_from_the_operator():
    epic = _epic()
    routes = _routes(epic, [epic, _inbox()], scraper=True)
    assert transports.default_transport(epic, routes) == EMAIL


def test_every_source_with_a_working_route_gets_a_default():
    """The operator's rule: zero automation is fine, zero default is not."""
    upload_only = Service(key="dfcu", label="DFCU", parser="dfcu")
    routes = _routes(upload_only)
    assert transports.default_transport(upload_only, routes) == UPLOAD


def test_no_default_when_nothing_works_at_all():
    nothing = Service(key="new", label="New")
    assert transports.default_transport(nothing, _routes(nothing)) is None


# --- default is not automation ----------------------------------------------

def test_upload_can_be_the_default_but_never_automated():
    """The operator's correction: 'Get latest' on an upload source means 'hand me
    the file'. That is a fine default; it just cannot run unattended."""
    upload_only = Service(key="dfcu", label="DFCU", parser="dfcu")
    routes = _routes(upload_only)
    default = transports.default_transport(upload_only, routes)

    assert default == UPLOAD
    assert transports.can_automate(routes, default) is False


def test_an_unattended_default_can_be_automated():
    epic = _epic()
    routes = _routes(epic, [epic, _inbox()])
    assert transports.can_automate(routes, transports.default_transport(epic, routes)) is True


def test_nothing_working_cannot_be_automated():
    nothing = Service(key="new", label="New")
    assert transports.can_automate(_routes(nothing), None) is False


# --- the tool surface --------------------------------------------------------

@pytest.fixture
def two_sources(monkeypatch):
    from interfaces import mcp_tools
    epic = _epic()
    dfcu = Service(key="dfcu", label="DFCU", parser="dfcu", status="implemented")
    services = [_inbox(), epic, dfcu]
    monkeypatch.setattr(mcp_tools, "_load_services", lambda: services)
    monkeypatch.setattr(mcp_tools, "has_scraper", lambda k: k == "epic")
    monkeypatch.setattr(mcp_tools.source_status, "parser_built", lambda k: False)
    # Whether the inbox is signed in decides whether EMAIL is even a route, so
    # the test has to state it. It used to be answered by the operator's real
    # .secrets/ — the assertions below passed on this machine and nowhere else.
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda k: True)
    return mcp_tools


def test_list_sources_hides_the_inbox_because_it_is_not_a_source(two_sources):
    keys = [s["key"] for s in two_sources.list_sources()]
    assert keys == ["epic", "dfcu"], "the carrier must not appear as a peer source"


def test_carriers_are_still_reachable_when_asked_for(two_sources):
    """Setup screens need the inbox itself."""
    keys = [s["key"] for s in two_sources.list_sources(include_carriers=True)]
    assert "email" in keys


def test_list_sources_reports_routes_and_the_default(two_sources):
    epic = next(s for s in two_sources.list_sources() if s["key"] == "epic")
    assert {r["id"] for r in epic["transports"]} == {UPLOAD, SCRAPE, EMAIL}
    assert epic["default_transport"] == EMAIL and epic["can_automate"] is True

    dfcu = next(s for s in two_sources.list_sources() if s["key"] == "dfcu")
    assert dfcu["default_transport"] == UPLOAD and dfcu["can_automate"] is False


def test_setting_a_default_to_an_unusable_route_is_refused(two_sources):
    with pytest.raises(two_sources.ToolError, match="isn't usable"):
        two_sources.set_default_transport("dfcu", SCRAPE)


def test_get_latest_on_an_upload_source_asks_for_the_file(two_sources):
    """There is nothing to fetch — say so instead of failing obscurely."""
    with pytest.raises(two_sources.ToolError, match="choose a document"):
        two_sources.get_latest("dfcu")


def test_get_latest_fetches_the_source_itself(two_sources, monkeypatch):
    """The source is what gets fetched and ingested; the inbox is only where we
    look. It used to be handed the carrier's key, which then had to bounce the
    document back through a delivers_to indirection."""
    seen = {}

    def fake_fetch(key):
        seen["key"] = key
        return {"ingested": []}

    monkeypatch.setattr(two_sources, "fetch_source", fake_fetch)

    result = two_sources.get_latest("epic")

    assert result["transport"] == EMAIL
    assert seen["key"] == "epic"


# --- which route actually delivered the data ---------------------------------

def test_a_run_records_the_route_that_delivered_it(tmp_path, monkeypatch):
    """The funnel draws the route the CURRENT data arrived by as a solid line, so
    it has to be recorded — it cannot be inferred from the parser used, since an
    uploaded file and an emailed one run the very same parser."""
    import core.ingest as ingest
    from core.tools.service_manifest import ServiceManifest

    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="epic_property_management", label="Epic",
                         parser="buildium_owner_statement", status="implemented"))
    sample = pathlib.Path("tests/fixtures/sample_owner_statement.pdf")

    uploaded = ingest.ingest_source("epic_property_management", sample, manifest=manifest)
    assert uploaded["transport"] == "upload", "an upload defaults to the upload route"

    emailed = ingest.ingest_source("epic_property_management", sample,
                                   manifest=manifest, transport="email")
    assert emailed["transport"] == "email", "the same parser, a different route"


def test_a_scrape_records_itself_as_the_scrape_route(tmp_path, monkeypatch):
    import core.ingest as ingest
    from core.fetch_ingest import persist_scraped
    from core.models import Transaction
    from datetime import datetime

    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    txn = Transaction(source_key="epic", date=datetime(2026, 7, 1), amount=1.0,
                      description="x", fields={"Amount": "1.00"})

    assert persist_scraped([txn], "https://example.com")["transport"] == "scrape"


def test_source_transactions_reports_the_last_route_used(tmp_path, monkeypatch):
    import core.ingest as ingest
    from interfaces import mcp_tools
    from core.tools.service_manifest import ServiceManifest

    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="epic_property_management", label="Epic",
                         parser="buildium_owner_statement", status="implemented"))
    ingest.ingest_source("epic_property_management",
                         pathlib.Path("tests/fixtures/sample_owner_statement.pdf"),
                         manifest=manifest, transport="email")

    assert mcp_tools.source_transactions("epic_property_management")["last_transport"] == "email"
