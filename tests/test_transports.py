"""Sources versus transports.

The modelling error this fixes: the inbox carrying the Epic statement was listed
as a PEER of Epic, so the operator saw two sources where there is one source with
two ways in. A source is a body of data; a transport is a route it takes.

Each route also answers for ITSELF. There is no default route and no per-source
"can be automated" flag: both summarised several routes into one value that then
went stale, and the screen read the stale value back out — Epic reported "needs
you — file upload" while its website was running itself unattended.
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


# --- there is no default, and no source-level automation flag ----------------
#
# Both were one answer standing in for several, and both drifted. A pinned default
# said "file upload" long after Epic's website had taken over; the automation flag,
# computed from that pin, then called a self-running source manual. What replaced
# them is per-route facts, which cannot disagree with themselves.

def test_no_route_is_singled_out_as_the_default():
    """The concept is gone from the module, not merely unused by the screen — a
    surviving helper is something the next caller reaches for."""
    assert not hasattr(transports, "default_transport")
    assert not hasattr(transports, "can_automate")


def test_a_leftover_pin_in_the_manifest_decides_nothing():
    """Someone's services.yaml may still carry the field the star wrote. It has to
    be inert, not a value the code keeps half-honouring."""
    stale = Service(key="dfcu", label="DFCU", parser="dfcu", default_transport=SCRAPE)

    assert not hasattr(stale, "default_transport")
    assert {r["id"] for r in _routes(stale)} == {UPLOAD, SCRAPE}


def test_every_route_answers_for_itself():
    """What the default and the automation flag were summarising. Epic can run
    itself two ways and needs a human for the third — one bit per source could
    never say that."""
    epic = _epic()
    routes = _by_id(_routes(epic, [epic, _inbox()], scraper=True))

    assert [r for r in routes.values() if r["available"] and r["unattended"]]
    assert routes[UPLOAD]["unattended"] is False
    assert routes[SCRAPE]["unattended"] is True and routes[EMAIL]["unattended"] is True


def test_a_source_with_nothing_working_has_no_unattended_route():
    nothing = Service(key="new", label="New")
    assert not [r for r in _routes(nothing) if r["available"] and r["unattended"]]


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


def test_list_sources_reports_the_routes_and_nothing_summarising_them(two_sources):
    epic = next(s for s in two_sources.list_sources() if s["key"] == "epic")
    assert {r["id"] for r in epic["transports"]} == {UPLOAD, SCRAPE, EMAIL}
    assert "default_transport" not in epic and "can_automate" not in epic

    dfcu = next(s for s in two_sources.list_sources() if s["key"] == "dfcu")
    assert {r["id"] for r in dfcu["transports"]} == {UPLOAD, SCRAPE}


def test_the_tools_that_only_served_the_default_are_gone(two_sources):
    """set_default_transport set it; get_latest ran it. With no default, the first
    writes something nothing reads and the second makes a control on one route run
    another — ⏵ on Mailbox used to answer "choose a document"."""
    names = {fn.__name__ for fn in two_sources.ALL_TOOLS}

    assert "set_default_transport" not in names and not hasattr(two_sources, "set_default_transport")
    assert "get_latest" not in names and not hasattr(two_sources, "get_latest")


def test_a_route_is_run_by_the_tool_that_belongs_to_it(two_sources):
    """What replaced get_latest: the mailbox route fetches the SOURCE — the inbox
    is only where we look — and no other route's tool is involved."""
    assert {"fetch_source", "run_scraper", "ingest_document"} <= {
        fn.__name__ for fn in two_sources.ALL_TOOLS
    }


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
