# Template candidate: generic (tier 1) — the registry pattern is client-agnostic;
# See agent-harness-template/docs/promotion-log.md.
"""Scraper registry: maps a source key to the callable that scrapes that source's
portal into transactions.

Mirrors core/parsers/REGISTRY. Entries here are added by the embedded agent when
it builds a scraper (orchestration/build_scraper.py) — the seam between "this
source is scraped" and "here is the agent-written code that scrapes it." Starts
empty; the harness fills it.
"""

from __future__ import annotations

from core.scrapers.base import ScrapeError, Scraper
from core.scrapers.epic_property_management import retrieve as _epic_retrieve

# source_key -> retrieve() callable. Populated by the agent via build_scraper.
REGISTRY: dict[str, Scraper] = {
    "epic_property_management": _epic_retrieve,
}

__all__ = ["ScrapeError", "Scraper", "REGISTRY", "get_scraper", "has_scraper", "method_of"]

# What a scraper DOES, in the two shapes the builder prompt offers: call the
# endpoint the site's own button fires, or replay the recorded clicks. The screen
# has to say which — they fail differently and cost differently, and "scraper" on
# its own hides that. Agent-written scrapers declare it as a module-level METHOD;
# the ones written before that declaration existed are read off their source.
API = "api"
CLICKS = "clicks"
UNKNOWN = "unknown"

_API_MARKERS = ("api_request", "page.request", "request.post", "request.get", "/api/")
_CLICK_MARKERS = ("page.click", ".click(", "select_option", "fill(")


def method_of(source_key: str) -> str:
    """Whether this source's scraper calls an endpoint or replays clicks."""
    scraper = REGISTRY.get(source_key)
    if scraper is None:
        return UNKNOWN

    import inspect

    module = inspect.getmodule(scraper)
    declared = getattr(module, "METHOD", None)
    if declared in (API, CLICKS):
        return declared
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):
        return UNKNOWN
    # An API scraper still signs in through the browser, so clicks alone prove
    # nothing — the endpoint call is the distinguishing act.
    if any(marker in source for marker in _API_MARKERS):
        return API
    if any(marker in source for marker in _CLICK_MARKERS):
        return CLICKS
    return UNKNOWN


def get_scraper(source_key: str) -> Scraper:
    if source_key not in REGISTRY:
        raise KeyError(
            f"No scraper registered under '{source_key}'. Registered: {sorted(REGISTRY)}. "
            "The harness builds one via 'Build the scraper' in the TUI."
        )
    return REGISTRY[source_key]


def has_scraper(source_key: str) -> bool:
    return source_key in REGISTRY
