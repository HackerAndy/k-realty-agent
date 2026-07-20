# Template candidate: generic (tier 1) — the scraper contract, client-agnostic.
# See agent-harness-template/docs/promotion-log.md.
"""The Scraper contract.

A scraper pulls a login-protected portal's data into transactions on its own:
it establishes an authenticated session, reaches the data (preferably by calling
the endpoint the site's "Generate" button fires, else by replaying recorded
clicks), and returns faithful Transactions.

Scraper modules under core/scrapers/ are AUTHORED BY THE EMBEDDED AGENT from an
operator demonstration (see orchestration/build_scraper.py) — not hand-written by
a developer. Each exposes a module-level:

    def retrieve() -> list[Transaction]: ...

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from collections.abc import Callable

from core.models import Transaction

# A scraper is any zero-arg callable returning transactions.
Scraper = Callable[[], list[Transaction]]


class ScrapeError(RuntimeError):
    """Raised when a scraper cannot reach or read its data."""
