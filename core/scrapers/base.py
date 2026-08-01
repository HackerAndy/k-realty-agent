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
    """Raised when a scraper cannot reach or read its data.

    `needs_browser_login` is how a failure ASKS for the operator, and the only
    thing that should ever open a browser at them. It exists because the front-end
    used to decide by pattern-matching the error prose for words like "session"
    and "login" — which got it wrong in both directions at once. A plain HTTP 403
    (a missing CSRF header, nothing to do with the operator) carried the harness's
    own generic advice "Check session validity and network connectivity", matched
    on the word *session*, and opened a browser demanding a sign-in that was
    neither needed nor able to help. Meanwhile "No username/password stored"  —
    the one failure a human genuinely must act on — matched nothing and was shown
    as a plain error.

    A failure knows whether a human at a browser can fix it. Prose about the
    failure does not.
    """

    needs_browser_login: bool = False


class SessionExpired(ScrapeError):
    """The portal's session is gone and only a person at a browser can restore it.

    For challenges no stored credential can answer — a one-time access code, a
    device-verification prompt, a CAPTCHA. NOT for a missing password: that one is
    fixed in Settings, and opening a browser for it wastes the operator's time on
    a login that will fail the same way.
    """

    needs_browser_login = True
