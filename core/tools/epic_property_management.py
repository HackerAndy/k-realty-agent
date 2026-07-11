# Template candidate: client-specific (tier 3) — K-Realty's own account and
# portal content. Not a promotion candidate.
# See agent-harness-template/docs/promotion-log.md.
"""K-Realty's Epic Property Management extraction.

Login is handled by core.tools.buildium_owner_portal and has been verified
end-to-end against the real login page (see that module + git history).
Everything below login is UNVERIFIED: I don't have live credentials or
visibility into the authenticated pages, so extract_transactions() below is
a best-effort skeleton, not working code. Run bootstrap() once from a
terminal to log in manually, then iterate the extraction logic against the
real, authenticated pages.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from core.models import Transaction
from core.tools import buildium_owner_portal
from core.tools.browser_session import bootstrap_login, launch

SERVICE_KEY = "epic_property_management"
PORTAL_URL = "https://epicpropertymanagement.managebuilding.com/Manager"


def bootstrap() -> None:
    """One-time setup: opens a visible browser for manual login (including
    any 2FA prompt). Run this first, from a terminal:

        poetry run python -c "from core.tools.epic_property_management import bootstrap; bootstrap()"

    Requires credentials already stored via scripts/manage_secrets.py.
    """
    bootstrap_login(SERVICE_KEY, PORTAL_URL)


def extract_transactions() -> list[Transaction]:
    """Log in and extract transaction records from the owner-statement pages.

    TODO — needs a real, authenticated session to build against:
    - confirm the post-login landing page and how to reach the financial/
      owner-statement view per property
    - identify the actual table/list structure holding transactions
    - map each row to a Transaction (property/unit, amount, transaction
      date, description, category)
    """
    with launch(SERVICE_KEY, headless=True) as page:
        buildium_owner_portal.login(page, PORTAL_URL, SERVICE_KEY)
        raise NotImplementedError(
            "Post-login extraction not yet built. Run bootstrap() first, "
            "inspect the real owner-statement pages, then fill this in."
        )
