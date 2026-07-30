# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""What READS a route's data, once the route has delivered it.

Getting the data and reading the data are two different acts that fail in two
different ways, and the screen used to draw them as one arrow. An email that
arrives fine but whose PDF the parser can't read is not the same problem as an
inbox that won't connect, and the operator was left to work out which from a
single number.

So every route has exactly one READER, and it is named for what it actually is:

    File upload ──▶ Parser · buildium_owner_statement ──▶ 23 rows
    Website     ──▶ API call                          ──▶ 33 rows
    Mailbox     ──▶ No reader yet                      ──▶ Not run

One node rather than a fixed row of Parser/API/Model boxes, because a route only
ever has one reader — and because the reader that RAN is the fact worth showing.
Rows a model produced must never look like rows a verified parser produced, so
when a run exists the node reports what read THAT run, model name and all;
with no run, it reports what is configured to read the next one.

An unbuilt reader is still a node. "No reader yet" is where the offer lives —
have the agent write one, or read it once with the model — which is how a brand
new source and a portal with no scraper are the same shape, not two designs.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from typing import Any

PARSER = "parser"
API = "api"
CLICKS = "clicks"
MODEL = "model"
NONE = "none"

# A route delivers a file, or it delivers rows. Which reader a route implies.
_FILE_ROUTES = ("upload", "email")

_METHOD_KINDS = {
    "deterministic_parser": PARSER,
    "llm_fallback": MODEL,
    "llm_extract": MODEL,
    "portal_scrape": API,          # refined to CLICKS below when that's what it is
}


def reader_for(
    route_id: str,
    service: Any,
    *,
    has_scraper: Any,
    scraper_method: Any = None,
    run: dict | None = None,
) -> dict:
    """The one reader on this route, as configured — or as it actually ran.

    `run` is that route's own last run (None if it has never run). When present it
    wins: what read the rows on screen is a fact, where the configured reader is
    only an intention.
    """
    if run:
        actual = _from_run(run, service, scraper_method)
        if actual is not None:
            return actual
    if route_id in _FILE_ROUTES:
        return _configured_parser(service)
    if route_id == "scrape":
        return _configured_scraper(service, has_scraper, scraper_method)
    return _none("No reader yet")


def _from_run(run: dict, service: Any, scraper_method: Any) -> dict | None:
    kind = _METHOD_KINDS.get(run.get("extraction_method") or "")
    if kind is None:
        return None
    if kind == MODEL:
        # Never "the model". Which one, because that is the whole of how far the
        # rows can be trusted, and it is not recoverable later from Settings.
        name = run.get("model") or "an unnamed model"
        fell_back = run.get("extraction_method") == "llm_fallback"
        return _reader(MODEL, name, f"Read by the model · {name}", built=True, verified=False,
                       note=("the parser couldn't read it, so the model did — these rows are "
                             "unverified" if fell_back else
                             "no parser has been built yet — these rows are unverified"))
    if kind == PARSER:
        name = run.get("parser") or getattr(service, "parser", None) or "a parser"
        return _reader(PARSER, name, f"Parser · {name}", built=True, verified=True)
    return _scraper_reader(scraper_method, getattr(service, "key", ""))


def _configured_parser(service: Any) -> dict:
    name = getattr(service, "parser", None)
    if not name:
        return _none("No parser yet")
    return _reader(PARSER, name, f"Parser · {name}", built=True, verified=True)


def _configured_scraper(service: Any, has_scraper: Any, scraper_method: Any) -> dict:
    key = getattr(service, "key", "")
    if not has_scraper(key):
        return _none("No scraper yet")
    return _scraper_reader(scraper_method, key)


def _scraper_reader(scraper_method: Any, key: str) -> dict:
    method = scraper_method(key) if scraper_method else "unknown"
    if method == CLICKS:
        return _reader(CLICKS, key, "Replays your clicks", built=True, verified=True,
                       note="drives the browser the way you demonstrated it")
    if method == API:
        return _reader(API, key, "API call", built=True, verified=True,
                       note="calls the endpoint the site's own button fires")
    return _reader(API, key, "Scraper", built=True, verified=True)


def _none(label: str) -> dict:
    return _reader(NONE, None, label, built=False, verified=False,
                   note="the agent can write one — or read it once with the model")


def _reader(kind: str, name: str | None, label: str, *, built: bool, verified: bool,
            note: str = "") -> dict:
    return {
        "kind": kind,
        "name": name,
        "label": label,
        "built": built,
        # Whether the rows this produces have been checked by a test. A model's
        # have not, and the screen must not present them as if they had.
        "verified": verified,
        "note": note,
    }
