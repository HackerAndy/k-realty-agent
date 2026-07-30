"""The one reader on each route, and what it is allowed to claim.

The screen drew getting the data and reading the data as a single arrow, so an
inbox that won't connect and a PDF the parser can't read looked identical. These
pin the distinction — and the one rule that matters most: rows a model produced
must never be presented as rows a verified parser produced.
"""

import pytest

from core import readers


class _Service:
    def __init__(self, key="epic_property_management", parser=None):
        self.key = key
        self.parser = parser


def _reader(route, service=None, *, scraper=False, method="api", run=None):
    return readers.reader_for(
        route,
        service or _Service(),
        has_scraper=lambda k: scraper,
        scraper_method=lambda k: method,
        run=run,
    )


@pytest.mark.parametrize("route", ["upload", "email"])
def test_a_file_route_is_read_by_the_source_s_parser(route):
    """Both routes deliver a document, and the SAME parser reads it — a fetched
    Epic PDF goes through exactly the code an uploaded one does."""
    node = _reader(route, _Service(parser="buildium_owner_statement"))

    assert node["kind"] == readers.PARSER
    assert node["label"] == "Parser · buildium_owner_statement"
    assert node["built"] and node["verified"]


def test_a_portal_that_calls_an_endpoint_says_so():
    node = _reader("scrape", scraper=True, method="api")

    assert node["kind"] == readers.API
    assert node["label"] == "API call"


def test_a_portal_that_replays_clicks_says_that_instead():
    """They fail differently and cost differently; "scraper" hides which you have."""
    node = _reader("scrape", scraper=True, method="clicks")

    assert node["kind"] == readers.CLICKS
    assert "clicks" in node["label"].lower()


def test_a_scraper_of_unknown_shape_still_gets_a_node():
    node = _reader("scrape", scraper=True, method="unknown")

    assert node["built"] and node["label"] == "Scraper"


# --- the unbuilt node -------------------------------------------------------
#
# A brand-new source and a website with no scraper are the SAME state, which is
# why there is one empty node rather than two designs.

def test_a_source_with_no_parser_shows_an_empty_reader_not_a_gap():
    node = _reader("upload", _Service(parser=None))

    assert node["kind"] == readers.NONE
    assert not node["built"]
    assert "No parser yet" in node["label"]


def test_a_website_with_no_scraper_shows_the_same_empty_reader():
    node = _reader("scrape", scraper=False)

    assert node["kind"] == readers.NONE
    assert "No scraper yet" in node["label"]


def test_the_empty_parser_node_carries_both_ways_out():
    """It's where the offer lives: build one, or read it once with the model."""
    note = _reader("upload", _Service(parser=None))["note"]

    assert "agent can write one" in note
    assert "model" in note


def test_the_empty_scraper_node_does_not_offer_the_model():
    """There is no document for a model to read — a portal's data needs code to
    reach at all, and that starts with a demonstration. Offering the model here
    would be a button that cannot work."""
    note = _reader("scrape", scraper=False)["note"]

    assert "model" not in note
    assert "show the agent" in note


# --- what actually ran beats what is configured -----------------------------

def test_a_run_that_the_model_read_names_the_model():
    """"The model" is not an answer anyone can audit. The one that ran is."""
    node = _reader("upload", _Service(parser="buildium_owner_statement"),
                   run={"extraction_method": "llm_extract", "model": "qwen3-coder-30b"})

    assert node["kind"] == readers.MODEL
    assert "qwen3-coder-30b" in node["label"]


def test_model_read_rows_are_never_marked_verified():
    """The whole reason the reader is drawn at all: unverified rows must not look
    identical to rows a tested parser produced."""
    node = _reader("upload", run={"extraction_method": "llm_fallback", "model": "gpt-oss-120b"})

    assert node["verified"] is False
    assert "unverified" in node["note"]


def test_a_fallback_says_the_parser_failed_and_a_direct_read_says_there_was_none():
    fell_back = _reader("upload", run={"extraction_method": "llm_fallback", "model": "m"})
    no_parser = _reader("upload", run={"extraction_method": "llm_extract", "model": "m"})

    assert "couldn't read it" in fell_back["note"]
    assert "no parser has been built" in no_parser["note"]


def test_a_model_read_with_no_recorded_name_still_admits_it_was_a_model():
    node = _reader("upload", run={"extraction_method": "llm_extract"})

    assert node["kind"] == readers.MODEL and node["verified"] is False


def test_the_run_s_own_parser_wins_over_the_source_s_current_one():
    """Rows on screen were read by whatever ran THEN, even if the source has since
    been pointed at a different parser."""
    node = _reader("upload", _Service(parser="new_parser"),
                   run={"extraction_method": "deterministic_parser", "parser": "old_parser"})

    assert node["label"] == "Parser · old_parser"


def test_an_unrecognised_extraction_method_falls_back_to_what_is_configured():
    node = _reader("upload", _Service(parser="buildium_owner_statement"),
                   run={"extraction_method": "something_new"})

    assert node["label"] == "Parser · buildium_owner_statement"


# --- reading the shape off the real registry --------------------------------

def test_the_real_epic_scraper_is_recognised_as_an_api_call():
    """Not a hypothetical: Epic's agent-written scraper calls Buildium's GL
    endpoint rather than clicking through the report page, and the node has to
    say so. Scrapers written from now on declare METHOD outright."""
    from core import scrapers

    assert scrapers.method_of("epic_property_management") == scrapers.API


def test_a_source_with_no_scraper_has_no_method():
    from core import scrapers

    assert scrapers.method_of("dfcu_financial_bank") == scrapers.UNKNOWN


def test_a_declared_method_beats_what_the_source_looks_like(monkeypatch):
    """The declaration is the point; the source scan is only for code written
    before it existed."""
    import sys
    import types

    from core import scrapers

    module = types.ModuleType("core.scrapers._fake")
    module.METHOD = scrapers.CLICKS

    def retrieve():
        # looks like an API scraper by its text, but says otherwise
        return []
    retrieve.__module__ = module.__name__
    module.retrieve = retrieve
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(scrapers.REGISTRY, "fake", retrieve)

    assert scrapers.method_of("fake") == scrapers.CLICKS
