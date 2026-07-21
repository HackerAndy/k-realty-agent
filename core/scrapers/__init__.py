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

__all__ = ["ScrapeError", "Scraper", "REGISTRY", "get_scraper", "has_scraper"]


def get_scraper(source_key: str) -> Scraper:
    if source_key not in REGISTRY:
        raise KeyError(
            f"No scraper registered under '{source_key}'. Registered: {sorted(REGISTRY)}. "
            "The harness builds one via 'Build the scraper' in the TUI."
        )
    return REGISTRY[source_key]


def has_scraper(source_key: str) -> bool:
    return source_key in REGISTRY
