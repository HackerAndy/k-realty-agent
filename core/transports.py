# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""How a source's data can arrive — its transports.

A SOURCE is one body of financial data (an owner statement, a bank account). A
TRANSPORT is a route that data takes to get here. They are different things, and
conflating them is the modelling error this module fixes: the email inbox that
carries the Epic statement was listed as a peer of Epic itself, so the operator
saw "Email" and "Epic Property Management" as two sources when there is one
source with two ways in.

One source, several doors:

    Epic owner statement  <- email attachment   (unattended)
                          <- portal scrape      (unattended)
                          <- file upload        (needs a human)

Transports are DERIVED, not stored: a source can be uploaded to if it has a
parser, scraped if it has a scraper, and emailed if some inbox routes to it. The
only thing worth persisting is which one is the DEFAULT.

Default vs automated — deliberately separate:
  - the DEFAULT is what "Get latest" runs. Every source with any working route
    has one.
  - AUTOMATION is a further step that runs the default unattended. It is only
    possible when the default can run with nobody watching, so a source whose
    only route is a file upload has a default but can never be automated. Zero
    automation is a perfectly good state; zero default is not.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from typing import Any

UPLOAD = "upload"
SCRAPE = "scrape"
EMAIL = "email"

# Ordered best-first: when nothing is pinned, prefer the route that needs the
# least from the operator.
_PREFERENCE = (EMAIL, SCRAPE, UPLOAD)

# Named for the WAY IN, not for what happens afterwards: "Portal scrape" packed
# getting at the data and reading it into one word, and they are two stages that
# fail separately. The website is the door; the API call that reads it is its own
# node, one stage along (core/readers.py).
_LABELS = {
    UPLOAD: ("File upload", "a document you already have"),
    SCRAPE: ("Website", "sign in and pull the data"),
    EMAIL: ("Mailbox", "arrives as an attachment"),
}


def transports_for(
    service: Any,
    services: list[Any],
    has_scraper: Any,
    parser_built: Any,
    carrier_ready: Any = None,
) -> list[dict]:
    """Every route this source's data could take, whether or not it works today.

    Unavailable routes are returned too, with a `reason` — "DFCU has no scraper
    yet" is information the operator needs in order to decide to build one, and
    hiding it makes the source look less capable than it could be.
    """
    key = service.key
    routes: list[dict] = []

    # Upload works when the source has an ACTIVE parser — which is not the same
    # question as "did the agent write a file named after this source". Epic's
    # parser is registered as `buildium_owner_statement`, so a filename check
    # would report Epic as unable to accept the very PDF it parses every month.
    # A built-but-unapproved parser is its own state, and worth saying out loud:
    # the fix is one click, not a build.
    has_active_parser = bool(getattr(service, "parser", None))
    awaiting_approval = not has_active_parser and bool(parser_built(key))
    routes.append(_route(
        UPLOAD,
        available=has_active_parser,
        unattended=False,   # no unattended version of "pick a file off my desk"
        reason=None if has_active_parser else (
            "a parser is built but not approved yet — approve it to use this route"
            if awaiting_approval else "no parser built for this source yet"
        ),
    ))

    can_scrape = bool(has_scraper(key))
    routes.append(_route(
        SCRAPE,
        available=can_scrape,
        unattended=True,
        reason=None if can_scrape else "no scraper built yet — the agent can write one",
    ))

    # An inbox is not a source; it is a way IN. The source says which inbox to
    # search and what to look for (Service.email_search) — the inbox itself only
    # holds the access, so one connected account can carry many sources.
    search = getattr(service, "email_search", None)
    if search is not None:
        carrier = next((s for s in services if s.key == search.carrier), None)
        # Naming an inbox is not the same as being able to READ it: a source
        # pointed at an inbox nobody has signed into would otherwise show as a
        # working route, and "Get latest" would pick it and fail.
        connected = (carrier is not None
                     and (True if carrier_ready is None else bool(carrier_ready(carrier.key))))
        carrier_label = carrier.label if carrier is not None else search.carrier
        if carrier is None:
            reason = f"the inbox '{search.carrier}' is gone — point this source at one that exists"
        elif not connected:
            reason = f"{carrier_label} isn't signed in yet — connect it in Settings"
        else:
            reason = None
        routes.append(_route(
            EMAIL,
            available=connected,
            unattended=True,
            reason=reason,
            detail=f"via {carrier_label}"
                   + (f" · from {search.from_address}" if search.from_address else ""),
            carrier_key=search.carrier,
        ))

    return routes


def _route(route_id: str, *, available: bool, unattended: bool,
           reason: str | None = None, detail: str | None = None, **extra) -> dict:
    label, default_detail = _LABELS[route_id]
    return {
        "id": route_id,
        "label": label,
        "detail": detail or default_detail,
        "available": available,
        "unattended": unattended,
        "reason": reason,
        **extra,
    }


def default_transport(service: Any, routes: list[dict]) -> str | None:
    """Which route "Get latest" runs.

    Honours the operator's pinned choice when it still works, so a stored default
    silently degrades rather than silently lying: if the pinned route stops being
    available (a scraper was deleted), fall back instead of offering a dead
    button. Returns None only when NO route works at all.
    """
    working = [r for r in routes if r["available"]]
    if not working:
        return None

    pinned = getattr(service, "default_transport", None)
    if pinned and any(r["id"] == pinned for r in working):
        return pinned

    by_id = {r["id"]: r for r in working}
    for candidate in _PREFERENCE:
        if candidate in by_id:
            return candidate
    return working[0]["id"]


def can_automate(routes: list[dict], default_id: str | None) -> bool:
    """Automation runs the DEFAULT unattended, so it's only possible when the
    default route can run with nobody watching."""
    if default_id is None:
        return False
    route = next((r for r in routes if r["id"] == default_id), None)
    return bool(route and route["available"] and route["unattended"])
