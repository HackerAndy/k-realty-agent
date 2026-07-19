# Template candidate: generic (tier 1) — the registry pattern is client-agnostic;
# the scraper modules it maps to are per-client. See
# agent-harness-template/docs/promotion-log.md.
"""Registry of portal scrapers: source_key -> a module that can log into and
read that source's login-protected portal.

Mirrors core/parsers/REGISTRY — the seam between "this source is scraped" and
"here is the code that scrapes it." A scraper module exposes:
  - bootstrap()            : one-time manual (headed) login, session persisted
  - explore_interactive()  : headed recon you can watch; dumps a page's structure
  - explore(url)           : headless recon of a specific URL
  - retrieve()             : the actual daily data pull (built against recon)
  - SERVICE_KEY, PORTAL_URL

Scraper modules are usually client-specific (a real account + portal content),
but this registry and the recon->scrape pattern are generic.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from types import ModuleType

from core.tools import epic_property_management

REGISTRY: dict[str, ModuleType] = {
    "epic_property_management": epic_property_management,
}


def get_scraper(source_key: str) -> ModuleType | None:
    """The scraper module for a source, or None if it isn't scraped."""
    return REGISTRY.get(source_key)
