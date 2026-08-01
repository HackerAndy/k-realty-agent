# Template candidate: generic (tier 1, pattern) — thin tool functions over core/,
# the seam a Claude Desktop / Cowork front-end calls. See promotion-log.md.
"""The MCP tool surface — plain functions the MCP server exposes to a Claude host.

Each is a THIN wrapper over an existing core/ capability, returning JSON-friendly
dicts. Kept separate from the MCP transport (interfaces/mcp_server.py) so the tool
logic is unit-testable without standing up a server. No new business logic lives
here; it all delegates to core/.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from core import hot_reload, progress, readers, reconcile, settings, source_status, transports
from core.observability import get_logger
from core.fetch_ingest import fetch_and_ingest, persist_scraped
from core.ingest import (
    ingest_source,
    load_latest_parsed,
    load_latest_parsed_for,
    runs_by_transport,
    transactions_from_run,
)
from core.models import Transaction
from core.scrapers import get_scraper, has_scraper
from core.scrapers import method_of as scraper_method
from core.tools import llm_provider
from core.tools.browser_session import reset_profile
from core.tools.service_manifest import ServiceManifest, ServiceManifestError


log = get_logger("interfaces.mcp_tools")


class ToolError(RuntimeError):
    """Surfaced to the MCP client as a tool error."""


_LOGIN_RECOVERY_PROCS: dict[str, subprocess.Popen] = {}
_LOGIN_RECOVERY_META: dict[str, dict[str, str]] = {}

# One in-flight agent build per source; its run file carries the streamed progress.
_BUILD_PROCS: dict[str, subprocess.Popen] = {}
_BUILD_META: dict[str, dict[str, str]] = {}

# The label an unnamed mailbox wears until consent tells us its address.
PROVISIONAL_INBOX_LABEL = "New mailbox"

# Gmail consent blocks on a browser, so it runs out-of-process too.
_CONSENT_PROCS: dict[str, subprocess.Popen] = {}
_CONSENT_META: dict[str, dict[str, str]] = {}

# So does a portal demonstration — it waits for a human to finish clicking.
_DEMO_PROCS: dict[str, subprocess.Popen] = {}
_DEMO_META: dict[str, dict[str, str]] = {}


def _tail_text(path: Path, max_lines: int = 25) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:]).strip()


def _load_services() -> list:
    """Load manifest services with client-friendly error mapping."""
    try:
        return ServiceManifest().load()
    except ServiceManifestError as exc:
        raise ToolError(str(exc)) from exc


def _summary(txns: list[Transaction]) -> dict:
    return {
        "count": len(txns),
        "money_in": round(sum(t.amount for t in txns if t.amount > 0), 2),
        "money_out": round(sum(t.amount for t in txns if t.amount < 0), 2),
    }


def _rows(txns: list[Transaction], limit: int) -> list[dict]:
    return [
        {
            "date": t.date.date().isoformat(),
            "amount": t.amount,
            "description": t.description,
            "fields": t.fields,
        }
        for t in txns[:limit]
    ]


# --- read tools -------------------------------------------------------------

def _inbox_connected(carrier_key: str) -> bool:
    """Whether an inbox can actually be read — i.e. consent was granted."""
    try:
        from core.tools import email_oauth

        return email_oauth.is_configured(carrier_key)
    except Exception:
        return False


def _inbox_account(carrier_key: str) -> str | None:
    """Which mailbox a connected inbox actually is, so the operator can tell two
    of them apart by address rather than by our label for them."""
    try:
        from core.tools import email_oauth

        return email_oauth.account_email(carrier_key)
    except Exception:
        return None


def _attach_reader_and_run(service, routes: list[dict]) -> None:
    """Give every route its own reader and its own last run.

    Getting the data and reading it are separate acts, so the graph draws them as
    separate nodes — and each route reports what IT has done, never what some
    other route did. A route with no run of its own carries `last_run: None`,
    which is what the screen prints as "Not run"; borrowing a sibling's count
    would put a number under a route that has never produced one.
    """
    history = runs_by_transport(service.key)
    for route in routes:
        run = history.get(route["id"])
        route["last_run"] = run
        route["reader"] = readers.reader_for(
            route["id"], service,
            has_scraper=has_scraper, scraper_method=scraper_method, run=run,
        )


def list_sources(include_carriers: bool = False) -> list[dict]:
    """Every financial SOURCE, with the transports its data can arrive by.

    An inbox is not a source — it is some source's email transport — so carriers
    are folded into their target's transports and left out of this list. Pass
    include_carriers=True to see them as their own rows (setup screens need that).
    """
    services = _load_services()
    out = []
    for s in services:
        if source_status.is_trigger(s) and not include_carriers:
            continue
        routes = transports.transports_for(
            s, services, has_scraper=has_scraper, parser_built=source_status.parser_built,
            carrier_ready=_inbox_connected,
        )
        _attach_reader_and_run(s, routes)
        # No "default_transport" and no "can_automate": each route reports its own
        # `available` / `unattended` / `last_run`, and a summary over them was a
        # second answer that went stale. A caller wanting the automatable ways in
        # filters `transports`, and can then say WHICH one.
        out.append({
            "key": s.key,
            "label": s.label,
            "status": s.status,
            "input_type": s.input_type,
            "access": s.access,
            "parser": s.parser,
            "is_trigger": source_status.is_trigger(s),
            "parser_built": source_status.parser_built(s.key),
            "has_scraper": has_scraper(s.key),
            "transports": routes,
        })
    return out


def credential_status() -> list[dict]:
    """Which sign-ins the harness holds — NEVER the values.

    Reports presence only (`has_username` / `has_password`), because a screen that
    can display a stored password is a screen that can leak one. The operator can
    replace a credential; nothing can read one back out.
    """
    from core.tools.credential_store import CredentialStore, CredentialStoreError

    store = CredentialStore()
    try:
        stored = set(store.list_services())
    except CredentialStoreError:
        stored = set()

    out = []
    for s in _load_services():
        # Only things you actually sign in to: a portal, or an inbox.
        if not s.login_url and not source_status.is_trigger(s):
            continue
        fields = {}
        if s.key in stored:
            try:
                fields = store.get(s.key)
            except CredentialStoreError:
                fields = {}
        out.append({
            "key": s.key,
            "label": s.label,
            "login_url": s.login_url,
            "kind": "inbox" if source_status.is_trigger(s) else "portal",
            "has_username": bool(fields.get("username")),
            "has_password": bool(fields.get("password")),
            "username": fields.get("username") or "",   # not a secret; shown so you know WHICH account
        })
    return out


def email_status(source_key: str) -> dict:
    """Whether an inbox is connected, and which sources search it.

    An inbox is ACCESS, nothing else: a signed-in mailbox the harness can read.
    What to look for inside it belongs to each source that arrives that way
    (Service.email_search), because one mailbox carries many sources — so this
    reports who uses it, not what to search for.
    """
    from core.tools import email_oauth

    services = _load_services()
    service = next((s for s in services if s.key == source_key), None)
    if service is None:
        raise ToolError(f"Unknown source '{source_key}'.")

    return {
        "source_key": source_key,
        "label": service.label,
        "provider": service.provider or "gmail",
        # Still wearing the placeholder name: the caller fills it in from the
        # address once consent has told us what that is.
        "provisional": service.label == PROVISIONAL_INBOX_LABEL,
        "connected": email_oauth.is_configured(source_key),
        "account_email": email_oauth.account_email(source_key),
        # Sources whose documents arrive through THIS inbox, with what each looks
        # for — read-only here; it's edited on the source.
        "searched_by": [
            {"key": s.key, "label": s.label,
             "search": s.email_search.model_dump(exclude_none=True)}
            for s in services
            if s.email_search and s.email_search.carrier == source_key
        ],
    }


def add_inbox(label: str = "") -> dict:
    """Add another mailbox for the harness to read.

    An inbox is a way IN, shared by whatever sources arrive through it — so
    having several is normal (a business mailbox and a personal one, say). It
    starts unconnected; the Gmail walkthrough in Settings does the sign-in.

    No name needed. Being made to invent one BEFORE signing in is busywork: the
    address is the obvious name and Google tells us what it is at consent, so the
    label is filled in then (rename_inbox gives it a nickname afterwards).
    """
    from core.tools.service_manifest import Service, ServiceManifestError

    services = _load_services()
    label = (label or "").strip()
    if label:
        key = _slug(label)
        if not key:
            raise ToolError(f"'{label}' has no letters or digits to make a key from — try another name.")
        if any(s.key == key for s in services):
            raise ToolError(f"'{label}' is already here (key '{key}'). Pick a different name.")
    else:
        # A key is an internal identifier, never shown; only the label is. So an
        # unnamed mailbox gets a free key and a placeholder label to replace.
        label = PROVISIONAL_INBOX_LABEL
        taken = {s.key for s in services}
        key = next(k for k in (f"mailbox{'' if n == 1 else f'_{n}'}" for n in range(1, 999))
                   if k not in taken)

    try:
        ServiceManifest().add(Service(key=key, label=label, input_type="email_trigger",
                                      access="api", provider="gmail", status="planned"))
    except ServiceManifestError as exc:
        raise ToolError(str(exc)) from exc

    log.event(operation="add_inbox", code="INBOX_ADDED",
              message=f"Added inbox '{label}'.", context={"source_key": key})
    return email_status(key)


def rename_inbox(source_key: str, label: str) -> dict:
    """Rename a mailbox — its address by default, or a nickname if you have
    several. Only the label changes; the key, the token, and every source
    pointing at it are untouched."""
    label = (label or "").strip()
    if not label:
        raise ToolError("A mailbox needs a name — its address is a fine one.")
    inbox = next((s for s in _load_services() if s.key == source_key), None)
    if inbox is None:
        raise ToolError(f"Unknown inbox '{source_key}'.")
    if not source_status.is_trigger(inbox):
        raise ToolError(f"'{inbox.label}' isn't an inbox.")

    ServiceManifest().update(source_key, label=label)
    return email_status(source_key)


def delete_inbox(source_key: str) -> dict:
    """Remove an inbox: forget its token and drop it from the registry.

    Refused while a source still arrives through it — deleting it would leave
    that source with a route to nowhere. Stop those sources arriving by email
    first, which is a decision about them, not about this mailbox.
    """
    from core.tools import email_oauth

    services = _load_services()
    inbox = next((s for s in services if s.key == source_key), None)
    if inbox is None:
        raise ToolError(f"Unknown inbox '{source_key}'.")
    if not source_status.is_trigger(inbox):
        raise ToolError(f"'{inbox.label}' isn't an inbox.")

    users = [s.label for s in services
             if s.email_search and s.email_search.carrier == source_key]
    if users:
        raise ToolError(
            f"'{inbox.label}' is still used by {', '.join(users)}. Open each of those under Data "
            "ingestion and stop it arriving by email first — otherwise they'd have no route."
        )

    email_oauth.forget(source_key)
    for leftover in (Path(".secrets") / f"oauth-client-{source_key}.json",
                     Path(".secrets") / f"oauth-client-{source_key}.reapprove.json",
                     Path(".secrets") / f"consent-{source_key}.status.json"):
        leftover.unlink(missing_ok=True)
    ServiceManifest().remove(source_key)
    _CONSENT_PROCS.pop(source_key, None)
    _CONSENT_META.pop(source_key, None)

    log.event(operation="delete_inbox", code="INBOX_DELETED",
              message=f"Deleted inbox '{inbox.label}'.", context={"source_key": source_key})
    return {"source_key": source_key, "deleted": True, "label": inbox.label}


def reapprove_inbox(source_key: str) -> dict:
    """Get a fresh token for an inbox that's already set up.

    For a token that expired or was revoked at myaccount.google.com. The OAuth
    client is already in the vault (it has to be, to refresh tokens), so this is
    one click — no going back to Google Cloud for the file.
    """
    from core.tools import email_oauth
    from core.tools.credential_store import CredentialStoreError

    inbox = next((s for s in _load_services() if s.key == source_key), None)
    if inbox is None:
        raise ToolError(f"Unknown inbox '{source_key}'.")
    if not source_status.is_trigger(inbox):
        raise ToolError(f"'{inbox.label}' isn't an inbox.")

    try:
        client_json = email_oauth.client_config_file(source_key)
    except CredentialStoreError as exc:
        raise ToolError(str(exc)) from exc
    return start_gmail_consent(source_key, str(client_json))


def start_gmail_consent(source_key: str, client_secret_path: str) -> dict:
    """Open Google's consent screen so the harness can read this inbox.

    Runs in its own process: the flow blocks on a browser until you click Allow,
    which no web request can wait for. Poll gmail_consent_status()."""
    from core.tools import email_oauth  # noqa: F401  (fail early if deps are missing)

    service = next((s for s in _load_services() if s.key == source_key), None)
    if service is None:
        raise ToolError(f"Unknown source '{source_key}'.")
    client_json = Path(client_secret_path).expanduser()
    if not client_json.exists():
        raise ToolError("Upload the OAuth client JSON from Google Cloud first.")

    existing = _CONSENT_PROCS.get(source_key)
    if existing and existing.poll() is None:
        return {"source_key": source_key, "status": "running", "pid": existing.pid}

    status_path = Path(".secrets") / f"consent-{source_key}.status.json"
    status_path.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.tools.oauth_consent_worker",
         source_key, str(client_json), str(status_path)],
        start_new_session=True,
    )
    _CONSENT_PROCS[source_key] = proc
    _CONSENT_META[source_key] = {"status_path": str(status_path)}
    return {"source_key": source_key, "status": "running", "pid": proc.pid,
            "message": "A browser is opening — choose the account and click Allow."}


def gmail_consent_status(source_key: str) -> dict:
    """Progress of a consent run: idle | running | completed | failed."""
    from core.tools import email_oauth

    proc = _CONSENT_PROCS.get(source_key)
    meta = _CONSENT_META.get(source_key, {})
    if proc is None:
        return {"source_key": source_key,
                "status": "completed" if email_oauth.is_configured(source_key) else "idle",
                "account_email": email_oauth.account_email(source_key)}

    payload = {}
    status_path = Path(meta.get("status_path", ""))
    if status_path.exists():
        try:
            payload = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}

    exit_code = proc.poll()
    if exit_code is None:
        return {"source_key": source_key, "status": "running",
                "message": payload.get("message", "Waiting for you to approve access…")}

    _CONSENT_PROCS.pop(source_key, None)
    status_path.unlink(missing_ok=True)
    if payload.get("status") == "completed":
        return {"source_key": source_key, "status": "completed",
                "account_email": payload.get("account_email"),
                "message": payload.get("message", "Connected.")}
    return {"source_key": source_key, "status": "failed",
            "message": payload.get("error") or "Consent didn't complete.",
            "remediation": "Confirm the JSON is a Desktop OAuth client, and that you clicked Allow."}


def save_email_search(source_key: str, carrier: str, from_address: str = "",
                      subject_contains: str = "", attachment_suffix: str = ".pdf",
                      newer_than_days: int | None = None) -> dict:
    """Set up a source's email route: which inbox carries its document, and how to
    find that message.

    This is ingestion configuration, and it lives on the SOURCE. The inbox itself
    only holds the sign-in — so the same connected mailbox can carry the Epic
    statement, a bank export, and anything else, each with its own search.
    """
    from core.tools.service_manifest import EmailSearch

    services = _load_services()
    source = next((s for s in services if s.key == source_key), None)
    if source is None:
        raise ToolError(f"Unknown source '{source_key}'.")
    inbox = next((s for s in services if s.key == carrier), None)
    if inbox is None:
        raise ToolError(f"Unknown inbox '{carrier}'.")
    if not source_status.is_trigger(inbox):
        raise ToolError(f"'{inbox.label}' isn't an inbox, so there is nothing to search there.")
    if source_status.is_trigger(source):
        raise ToolError(
            f"'{source.label}' is an inbox — a way in, not a body of data. Set the email route on "
            "the source whose document arrives there."
        )
    if not _inbox_connected(carrier):
        raise ToolError(
            f"'{inbox.label}' isn't signed in yet. Connect it under Settings → Sign-ins first; "
            "that's the access, and it's shared by every source that arrives through it."
        )

    ServiceManifest().set_email_search(source_key, EmailSearch(
        carrier=carrier,
        from_address=from_address.strip() or None,
        subject_contains=subject_contains.strip() or None,
        attachment_suffix=(attachment_suffix or "").strip() or None,
        newer_than_days=int(newer_than_days) if newer_than_days else None,
    ))
    return source_email_route(source_key)


def remove_email_search(source_key: str) -> dict:
    """Stop this source arriving by email. The inbox stays connected — other
    sources may come through the same one."""
    if not any(s.key == source_key for s in _load_services()):
        raise ToolError(f"Unknown source '{source_key}'.")
    ServiceManifest().clear_email_search(source_key)
    return source_email_route(source_key)


def source_email_route(source_key: str) -> dict:
    """A source's email route: which inbox it searches, what it looks for, and
    which inboxes are available to point it at."""
    services = _load_services()
    source = next((s for s in services if s.key == source_key), None)
    if source is None:
        raise ToolError(f"Unknown source '{source_key}'.")
    search = source.email_search
    inboxes = [
        {"key": s.key, "label": s.label, "connected": _inbox_connected(s.key),
         "account_email": _inbox_account(s.key)}
        for s in services if source_status.is_trigger(s)
    ]
    return {
        "source_key": source_key,
        "label": source.label,
        "search": search.model_dump(exclude_none=True) if search else None,
        "connected": bool(search and _inbox_connected(search.carrier)),
        # Every inbox the harness knows about, so the source can be pointed at one.
        "inboxes": inboxes,
    }


# How the operator describes a new source, and what that means in the manifest.
# The wizard offers exactly these three because they're the three the harness can
# actually handle end-to-end; anything else would be a dead end on screen.
NEW_SOURCE_METHODS = {
    "website": {
        "label": "A website behind a login",
        "detail": "Show it once; it signs in and pulls the data after that.",
        "icon": "↻", "cost": "Runs unattended", "unattended": True,
        "input_type": "html_scrape", "access": "portal_login",
        "act": "Open a browser and show it",
        "doing": "A browser is open. Sign in, set your filters, bring your data up on screen — "
                 "then close the window.",
        "next": "You demonstrate it in a browser; the agent writes a scraper from that.",
    },
    "document": {
        "label": "A document you already have",
        "detail": "A statement or an export — PDF or CSV.",
        "icon": "↑", "cost": "You hand it the file", "unattended": False,
        "input_type": "document", "access": "download",
        "act": "Choose a document",
        "doing": "Reading your document…",
        "next": "The agent writes a parser from that sample, and tests it.",
    },
    "email": {
        "label": "An email that delivers it",
        "detail": "It watches an inbox and takes the attachment off the message.",
        "icon": "✉", "cost": "Runs unattended", "unattended": True,
        # The source is the DOCUMENT that arrives, not the inbox: email is how it
        # gets here. The inbox stays a separate, shared sign-in.
        "input_type": "document", "access": "email_attachment",
        "act": "Connect the inbox",
        "doing": "Looking through the inbox for messages that carry a document…",
        "next": "The agent writes a parser from the attachment it found, and tests it.",
        "caveat": "An email delivers a document — something still has to read it. "
                  "It will use the newest attachment it finds as its sample.",
    },
}

# Where a source is learned before it has a name. The wizard asks the agent to
# read the source FIRST and suggest what to call it, so the artifacts (a sample,
# a demonstration, a browser profile with a live session) exist before there is a
# key to file them under. They are moved to the real key when the source is saved.
STAGING_KEY = "_new"


def source_methods() -> list[dict]:
    """The ways a new source's data can arrive — the wizard's first question.

    Ordered best-first, and each says what it will cost the operator afterwards,
    because that is the actual difference between them: a scrape and an inbox run
    unattended, a document means picking a file every month.
    """
    skip = {"input_type", "access"}
    return [{"id": key, **{k: v for k, v in spec.items() if k not in skip}}
            for key, spec in NEW_SOURCE_METHODS.items()]


def preview_document(sample_path: str, filename: str = "") -> dict:
    """Read a document the harness has never seen, and report what's in it.

    This is what the operator judges before naming anything: the source's own
    columns, how many rows, over what dates. It runs the model once (no parser
    exists yet, by definition) and persists nothing.

    Best-effort by design — if the model can't be reached or can't read the
    layout, the answer is a smaller panel plus a name suggested from the
    filename, never a blocked flow.
    """
    doc = Path(sample_path).expanduser()
    if not doc.exists():
        raise ToolError(f"No file at {doc}")
    name = filename or doc.name

    text = ""
    try:
        from core.tools.llm_extractor import read_document_text

        text = read_document_text(doc)
    except Exception as exc:
        log.event(operation="preview_document", code="PREVIEW_TEXT_FAILED",
                  message=f"Couldn't read text from {name}.", context={"error": str(exc)})

    if not llm_provider.is_configured():
        return {"method": "document", "where": name, "sample_path": str(doc),
                "how": "Reads this document each time you upload one",
                "rows": 0, "span": "", "columns": [], "unattended": False,
                "rows_estimated": False,
                "trouble": "No LLM provider is set up, so it can only go by the file name.",
                **_suggest_name("", name)}

    from orchestration.naming import describe_document

    look = describe_document(text, name)
    trouble = None
    if not look["columns"]:
        trouble = ("Couldn't make out a transaction table yet — the agent looks much harder "
                   "when it writes the parser.")
    return {
        "method": "document", "where": name, "sample_path": str(doc),
        "how": "Reads this document each time you upload one",
        "rows": look["rows"], "span": look["span"], "columns": look["columns"],
        # An estimate from one quick look, NOT a parse. The screen says so.
        "rows_estimated": True,
        "unattended": False, "trouble": trouble,
        "suggested_name": look["label"], "name_source": look["label_source"],
        "suggested_key": _slug(look["label"]),
    }


def preview_demo(demo_path: str) -> dict:
    """Report what a recorded browser demonstration taught the harness.

    No model needed: the operator left their data on screen, so the final page's
    biggest table is the answer, and the largest data request they triggered is
    how it would fetch that again.
    """
    from core import preview

    path = Path(demo_path).expanduser()
    if not path.exists():
        raise ToolError(f"No demonstration at {path} — record one first.")
    try:
        demo = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"That demonstration file is unreadable: {exc}") from exc

    summary = preview.summarise_demo(demo)
    # Name it from what the page called itself, falling back to the host.
    from urllib.parse import urlparse

    host = urlparse(summary["where"]).hostname or ""
    hint = "\n".join(filter(None, [
        f"Page title: {summary['title']}", f"URL: {summary['where']}",
        "Table headings: " + ", ".join(summary["columns"]) if summary["columns"] else "",
    ]))
    suggestion = _suggest_name(hint, summary["title"] or host.replace(".", " "))
    return {"method": "website", "demo_path": str(path), "unattended": True,
            "trouble": None if summary["columns"] else
                       "It recorded your clicks but saw no table on the final page.",
            **summary, **suggestion}


def preview_inbox(carrier_key: str, from_address: str = "", subject_contains: str = "",
                  attachment_suffix: str = ".pdf", newer_than_days: int | None = None) -> dict:
    """Search a connected inbox and read the newest document it carries.

    An inbox is only half a source: it delivers a document, and something still
    has to read it. So the preview is the delivery (how many messages match, the
    newest one) AND the document itself, previewed exactly like an uploaded one —
    that attachment is what the parser would be built from.
    """
    from core.tools import email_fetcher, email_oauth
    from core.tools.service_manifest import EmailSearch

    if not email_oauth.is_configured(carrier_key):
        raise ToolError(f"'{carrier_key}' isn't signed in to Google yet — connect it in Settings first.")

    cfg = EmailSearch(carrier=carrier_key,
                      from_address=from_address.strip() or None,
                      subject_contains=subject_contains.strip() or None,
                      attachment_suffix=(attachment_suffix or "").strip() or None,
                      newer_than_days=int(newer_than_days) if newer_than_days else None)
    try:
        found = email_fetcher.GmailFetcher(carrier_key).search_and_fetch(cfg, limit=5)
    except Exception as exc:
        raise ToolError(str(exc)) from exc

    if not found:
        return {"method": "email", "carrier_key": carrier_key, "messages": 0,
                "where": "no matching messages", "how": "Nothing matched that search",
                "rows": 0, "span": "", "columns": [], "unattended": True,
                "trouble": "No message matched — loosen the sender or subject and look again.",
                "suggested_name": "", "name_source": "", "suggested_key": ""}

    newest = found[0]
    staged = Path("data/samples")
    staged.mkdir(parents=True, exist_ok=True)
    sample = staged / f"{STAGING_KEY}-sample{Path(newest.filename).suffix}"
    sample.write_bytes(newest.data)

    doc = preview_document(str(sample), newest.filename)
    return {**doc, "method": "email", "carrier_key": carrier_key,
            "messages": len(found), "received": newest.received,
            "where": f"{newest.filename} · {len(found)} matching message"
                     f"{'s' if len(found) != 1 else ''}",
            "how": f"Takes the attachment off the newest match ({newest.received or 'undated'})",
            "unattended": True}


def _suggest_name(text: str, fallback_name: str) -> dict:
    from orchestration.naming import suggest_label

    suggestion = suggest_label(text, fallback_name)
    return {"suggested_name": suggestion["label"], "name_source": suggestion["source"],
            "suggested_key": _slug(suggestion["label"])}


def _slug(label: str) -> str:
    """A manifest key from a human label: lowercase, underscores, no punctuation."""
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return slug[:48]


def suggest_source_name(sample_path: str = "", filename: str = "") -> dict:
    """Propose a name for a new source by reading the top of its document.

    Never fails: with no model, no readable text, or a model that can't tell, it
    falls back to a title derived from the filename. `source` says which, so the
    screen doesn't imply the model saw something it didn't."""
    from orchestration.naming import label_from_filename, suggest_label

    doc = Path(sample_path).expanduser() if sample_path else None
    name = filename or (doc.name if doc else "")
    if doc is None or not doc.exists():
        return {"label": label_from_filename(name), "source": "filename", "key": _slug(label_from_filename(name))}

    try:
        from core.tools.llm_extractor import read_document_text

        text = read_document_text(doc)
    except Exception:
        text = ""
    suggestion = suggest_label(text, name)
    return {**suggestion, "key": _slug(suggestion["label"])}


def add_source(label: str, method: str, adopt_staged: bool = False, login_url: str = "",
               carrier: str = "", from_address: str = "", subject_contains: str = "",
               attachment_suffix: str = "") -> dict:
    """Add a new data source to the registry.

    Nothing is built here — this creates the entry the rest of the harness hangs
    off (the agent's parser/scraper, its credentials, its transports). It starts
    `planned`, with no parser and no default route, which is exactly what the
    Ingest screen should show until the agent has written something that works.

    adopt_staged: take everything learned under STAGING_KEY — the demonstration,
    and the browser profile holding the session they just signed into — and file
    it under the real key. Without this the operator would sign in twice.
    """
    from core.tools.service_manifest import Service, ServiceManifestError

    spec = NEW_SOURCE_METHODS.get(method)
    if spec is None:
        raise ToolError(f"Unknown method '{method}'. Use one of: {', '.join(NEW_SOURCE_METHODS)}.")

    label = (label or "").strip()
    if not label:
        raise ToolError("Give the source a name — it's how you'll recognise it on this screen.")
    key = _slug(label)
    if not key:
        raise ToolError(f"'{label}' has no letters or digits to make a key from — try another name.")

    services = _load_services()
    if any(s.key == key for s in services):
        raise ToolError(f"'{label}' is already here (key '{key}'). Pick a different name.")

    # Arriving by email means: this source's document is in that inbox, found by
    # that search. The inbox is a shared sign-in, so it must already exist.
    if method == "email":
        inbox = next((s for s in services if s.key == carrier), None)
        if inbox is None or not source_status.is_trigger(inbox):
            raise ToolError(
                "Say which inbox carries this source's document. Inboxes are connected under "
                "Settings → Sign-ins, and one can carry several sources."
            )

    service = Service(key=key, label=label, input_type=spec["input_type"],
                      access=spec["access"], status="planned",
                      login_url=login_url.strip() or None)
    try:
        ServiceManifest().add(service)
    except ServiceManifestError as exc:
        raise ToolError(str(exc)) from exc

    adopted = _adopt_staged(key) if adopt_staged else {}

    if method == "email":
        save_email_search(key, carrier=carrier, from_address=from_address,
                          subject_contains=subject_contains,
                          attachment_suffix=attachment_suffix or ".pdf")

    log.event(
        operation="add_source",
        code="SOURCE_ADDED",
        message=f"Added source '{label}' ({method}).",
        context={"source_key": key, "method": method, "carrier": carrier or None},
    )
    return {"source_key": key, "label": label, "method": method,
            "next": spec["next"], **adopted, **_source_row(key)}


def _adopt_staged(source_key: str) -> dict:
    """Move what was learned before the source had a name onto its real key.

    The demonstration is one file; the browser profile is a directory holding the
    session the operator just signed into. Losing either would mean doing the
    demonstration (and the sign-in) a second time for no reason.
    """
    import shutil

    adopted: dict = {}
    demo = Path("data/demos") / f"{STAGING_KEY}-demonstration.json"
    if demo.exists():
        target = demo.with_name(f"{source_key}-demonstration.json")
        shutil.move(str(demo), target)
        adopted["demo_path"] = str(target)
        har = demo.with_name(f"{STAGING_KEY}-demo.har")
        if har.exists():
            shutil.move(str(har), har.with_name(f"{source_key}-demo.har"))

    profile = Path(".browser_profiles") / STAGING_KEY
    if profile.is_dir():
        target = profile.with_name(source_key)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(profile), target)
        adopted["session_kept"] = True

    sample = next(Path("data/samples").glob(f"{STAGING_KEY}-sample.*"), None)
    if sample is not None:
        target = sample.with_name(f"{source_key}-sample{sample.suffix}")
        shutil.move(str(sample), target)
        adopted["sample_path"] = str(target)
    return adopted


def _source_row(source_key: str) -> dict:
    """The list_sources row for one source — so a caller that just changed
    something sees the same shape the screen renders from."""
    row = next((s for s in list_sources(include_carriers=True) if s["key"] == source_key), None)
    return row or {}


def start_demo(source_key: str, url: str = "") -> dict:
    """Open a browser so the operator can DEMONSTRATE how to reach a portal's data.

    Runs in its own process: it waits for a human to sign in, set filters, and get
    their data on screen, which no web request can wait for. Poll demo_status().
    The demonstration is what the agent writes the scraper from.

    Accepts the staging key: a source being added is demonstrated BEFORE it has a
    name, because what the agent sees is how it gets named."""
    service = next((s for s in _load_services() if s.key == source_key), None)
    if service is None and source_key != STAGING_KEY:
        raise ToolError(f"Unknown source '{source_key}'.")

    existing = _DEMO_PROCS.get(source_key)
    if existing and existing.poll() is None:
        return {"source_key": source_key, "status": "running", "pid": existing.pid}

    status_path = Path("data/demos") / f"{source_key}.status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.tools.demo_worker",
         source_key, (url or (service.login_url if service else "") or "-"), str(status_path)],
        start_new_session=True,
    )
    _DEMO_PROCS[source_key] = proc
    _DEMO_META[source_key] = {"status_path": str(status_path)}
    return {"source_key": source_key, "status": "running", "pid": proc.pid,
            "message": "A browser is opening — get your data on screen, then close the window."}


def demo_status(source_key: str) -> dict:
    """Progress of a demonstration: idle | running | completed | failed.

    On success the source's login_url is set from where the operator actually
    ended up — the harness learns the URL instead of asking for it."""
    proc = _DEMO_PROCS.get(source_key)
    meta = _DEMO_META.get(source_key, {})
    if proc is None:
        return {"source_key": source_key, "status": "idle"}

    payload = {}
    status_path = Path(meta.get("status_path", ""))
    if status_path.exists():
        try:
            payload = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}

    if proc.poll() is None:
        return {"source_key": source_key, "status": "running",
                "message": payload.get("message", "Waiting for your demonstration…")}

    _DEMO_PROCS.pop(source_key, None)
    if payload.get("status") != "completed":
        return {"source_key": source_key, "status": "failed",
                "message": payload.get("error") or "The demonstration wasn't captured.",
                "remediation": "Start it again, and close the browser window once your data is on screen."}

    landed = payload.get("final_url") or payload.get("start_url") or ""
    # A staged demonstration has no manifest entry to write to yet — add_source
    # stores the URL when the operator names it.
    if landed and source_key != STAGING_KEY:
        ServiceManifest().update(source_key, login_url=landed)
    return {"source_key": source_key, "status": "completed",
            "demo_path": payload.get("demo_path"),
            "login_url": landed,
            "captured_requests": payload.get("captured_requests", 0),
            "recorded_actions": payload.get("recorded_actions", 0),
            "message": "Demonstration captured — the agent can write the scraper now."}


def set_credentials(source_key: str, username: str | None = None,
                    password: str | None = None) -> dict:
    """Store a sign-in for a source, encrypted in .secrets/.

    A blank password KEEPS the stored one — CredentialStore.set() replaces the
    whole record, so omitting it would silently wipe the password while the
    operator thought they were only correcting a username.
    """
    from core.tools.credential_store import CredentialStore, CredentialStoreError

    service = next((s for s in _load_services() if s.key == source_key), None)
    if service is None:
        raise ToolError(f"Unknown source '{source_key}'.")

    username = (username or "").strip()
    password = password or ""      # not stripped: spaces can be part of a password

    store = CredentialStore()
    try:
        existing = dict(store.get(source_key))
    except CredentialStoreError:
        existing = {}

    if not password:
        password = existing.get("password", "")
    if not username:
        username = existing.get("username", "")
    if not username or not password:
        raise ToolError("Both a username and a password are needed to sign in.")

    store.set(source_key, **{**existing, "username": username, "password": password})
    log.event(
        operation="set_credentials",
        code="CREDENTIALS_SAVED",
        message=f"Stored a sign-in for '{source_key}'.",
        context={"source_key": source_key},   # never the values
    )
    return {"source_key": source_key, "has_username": True, "has_password": True,
            "username": username}


def forget_credentials(source_key: str) -> dict:
    """Remove a stored sign-in."""
    from core.tools.credential_store import CredentialStore, CredentialStoreError

    store = CredentialStore()
    try:
        data = store._load()
    except CredentialStoreError as exc:
        raise ToolError(str(exc)) from exc
    if source_key in data:
        data.pop(source_key)
        store._save(data)
    return {"source_key": source_key, "has_username": False, "has_password": False, "username": ""}


def source_settings(source_key: str) -> dict:
    """The options this source lets you adjust, and their current values.

    `schema` is declared by the source's own module, so the caller can render a
    form without knowing anything about the portal. Empty schema = no options.
    """
    schema = settings.schema_for(source_key)
    return {
        "source_key": source_key,
        "schema": schema,
        "values": settings.values_for(source_key),
        "overridden": sorted(settings.stored_for(source_key)),
        # A choice the portal has stopped offering — a property removed from the
        # account. Left unsaid, the next run quietly falls back to everything and
        # the numbers change with no explanation.
        "stale": settings.stale_values(source_key),
    }


def save_source_settings(source_key: str, values: dict) -> dict:
    """Store adjusted options for a source. They take effect on the next run —
    no code change, no rebuild."""
    try:
        effective = settings.save_for(source_key, values or {})
    except settings.SettingsError as exc:
        raise ToolError(str(exc)) from exc
    return {"source_key": source_key, "values": effective,
            "overridden": sorted(settings.stored_for(source_key))}


def reset_source_settings(source_key: str) -> dict:
    """Drop the operator's overrides, back to what the source declares."""
    return {"source_key": source_key, "values": settings.reset_for(source_key), "overridden": []}


# No get_latest(). "Run whichever route is the default" only meant anything while
# a default existed, and it made a control on one route run another: ⏵ on Mailbox
# went through here and answered "there's nothing to fetch, choose a document"
# because the default was file upload. Each route runs itself — run_scraper for
# the website, fetch_source for a mailbox, ingest_document for a file the operator
# picks — and the screen says which one it ran.


def latest_transactions(limit: int = 200) -> dict:
    """The most recent ingested transactions, with money-in/out totals."""
    run = load_latest_parsed()
    if run is None:
        return {"source_key": None, "count": 0, "money_in": 0, "money_out": 0, "transactions": []}
    txns = transactions_from_run(run)
    return {"source_key": run.get("source_key"), "month": run.get("month"),
            **_summary(txns), "transactions": _rows(txns, limit)}


def source_transactions(source_key: str, limit: int = 500, transport: str = "") -> dict:
    """The most recent ingested transactions for ONE source, with money-in/out
    totals. Powers the per-source input-validation view. Empty (not an error) when
    the source has no persisted run yet.

    With a transport, the rows THAT ROUTE last produced — so selecting a route in
    the graph shows its own data rather than whichever route happened to run last.
    An empty result then means that route has never run, which the screen says
    outright instead of showing another route's rows under it.
    """
    run = load_latest_parsed_for(source_key, transport or None)
    if run is None:
        return {"source_key": source_key, "count": 0, "money_in": 0, "money_out": 0,
                "transactions": [], "transport": transport or None, "never_run": True}
    txns = transactions_from_run(run)
    return {"source_key": run.get("source_key"), "month": run.get("month"),
            "parsed_at": run.get("parsed_at"), "run_path": run.get("run_path"),
            # Which route actually delivered this data — the funnel draws it solid.
            "last_transport": run.get("transport"),
            # HOW it was read: a verified parser, or the model. Model-read data is
            # unverified, and the screen has to say so rather than look identical.
            "extraction_method": run.get("extraction_method"),
            # WHICH model, when a model read it. "the model" is not an answer the
            # operator can audit; the name of the one that ran is.
            "model": run.get("model"),
            **_summary(txns), "transactions": _rows(txns, limit)}


def action_progress(source_key: str) -> dict:
    """The phases of a long action currently running for this source (browser
    launch, sign-in, each API call), with elapsed times.

    Safe to poll WHILE the action runs — that's the point: it turns a silent
    30-60s "Run scraper" into named steps the operator can watch."""
    steps = progress.read(source_key)
    running = next((s for s in steps if s.get("status") == "in-progress"), None)
    return {
        "source_key": source_key,
        "steps": steps,
        "current": running.get("label") if running else None,
        "current_elapsed_s": round(time.time() - running["started_at"], 1)
        if running and running.get("started_at") else None,
    }


def pending_approvals() -> list[dict]:
    """Sources whose parser is built but not yet activated — awaiting the operator's yes."""
    services = _load_services()
    return [{"key": s.key, "label": s.label} for s in source_status.pending_approvals(services)]


def llm_status() -> dict:
    """Which LLM provider and model the harness will actually use. `api_key_set`
    says whether a key is in the vault — never the key itself.

    The model reported here is the RESOLVED one (llm_provider.resolve()), not
    just the stored field: a provider saved without a model still runs something,
    and a screen that showed a blank while a default ran is the black box this
    exists to close. `model_source` says where the answer came from.
    """
    cfg = llm_provider.current_config() or {}
    choice = llm_provider.resolve()
    return {"configured": llm_provider.is_configured(),
            "provider": cfg.get("provider") or choice.provider,
            "model": choice.model,
            "model_source": choice.model_source,
            "base_url": cfg.get("base_url") or choice.base_url,
            "api_key_set": bool(cfg.get("api_key")),
            # Where a document's text would actually GO. Asking "may I send this
            # off your machine?" when the model runs on the LAN would be a lie,
            # and the answer differs per provider, so it's computed, not assumed.
            "destination": _llm_destination(cfg),
            "offsite": _llm_is_offsite(cfg),
            # What each provider would run if the operator doesn't name a model,
            # so the form can show the truth instead of its own copy of a constant.
            "defaults": {"anthropic": llm_provider.DEFAULT_MODEL,
                         "openai_compatible": llm_provider.DEFAULT_OMLX_MODEL}}


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _llm_is_offsite(cfg: dict) -> bool:
    """True if document text would leave this machine's own network."""
    if cfg.get("provider") != "openai_compatible":
        return True     # the Claude API, or nothing configured yet
    from urllib.parse import urlparse

    host = (urlparse(cfg.get("base_url") or "").hostname or "").lower()
    if not host:
        return True
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return False
    # Private ranges — a model on the LAN, e.g. another machine in the house.
    return not re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host)


def _llm_destination(cfg: dict) -> str:
    if not cfg:
        return "no LLM is set up yet"
    if cfg.get("provider") != "openai_compatible":
        return "the Anthropic API"
    from urllib.parse import urlparse

    host = urlparse(cfg.get("base_url") or "").hostname or cfg.get("base_url") or "an unknown server"
    return f"the model server at {host}"


def status() -> dict:
    """Overall harness status — LLM, source counts, pending approvals, latest ingest."""
    services = _load_services()
    run = load_latest_parsed()
    return {
        "llm": llm_status(),
        "sources_total": len(services),
        "sources_implemented": sum(1 for s in services if s.status == "implemented"),
        "pending_approvals": [s.key for s in source_status.pending_approvals(services)],
        "latest_ingest": (
            {"source_key": run["source_key"], "month": run["month"], "count": run["transaction_count"]}
            if run else None
        ),
    }


# --- action tools -----------------------------------------------------------

def ingest_document(source_key: str, path: str, allow_llm_fallback: bool = False) -> dict:
    """Parse a document you already have (PDF/CSV) for a source into transactions.

    allow_llm_fallback: if the source's committed parser can't read the layout,
    read it with the model instead. Only ever pass True on an explicit operator
    decision — it's the step where the document's text leaves the parser and goes
    to whatever LLM is configured."""
    doc = Path(path).expanduser()
    if not doc.exists():
        raise ToolError(f"No file at {doc}")
    if allow_llm_fallback:
        _require_llm()
    run = ingest_source(source_key, doc, allow_llm_fallback=allow_llm_fallback)
    return {"source_key": source_key, "run_path": run["run_path"],
            "extraction_method": run["extraction_method"],
            "model": run.get("model"),
            **_summary(transactions_from_run(run))}


def extract_now(source_key: str, path: str) -> dict:
    """Read a document with the model, for a source that has no parser yet.

    The "don't be blocked" path: it produces transactions today, from a source
    the harness has never seen, without waiting on an agent build. Treat the
    result as lower-confidence than a parser's — nothing verified it — and build
    a parser so later runs are deterministic and free."""
    doc = Path(path).expanduser()
    if not doc.exists():
        raise ToolError(f"No file at {doc}")
    if not any(s.key == source_key for s in _load_services()):
        raise ToolError(f"Unknown source '{source_key}'.")
    _require_llm()

    from core.ingest import ingest_via_llm

    run = ingest_via_llm(source_key, doc)
    return {"source_key": source_key, "run_path": run["run_path"],
            "extraction_method": run["extraction_method"],
            # Which model read it — the screen names it rather than leaving the
            # operator to assume it was whatever Settings says today.
            "model": run.get("model"),
            **_summary(transactions_from_run(run))}


def _require_llm() -> None:
    """Fail before the document is read, not after — reading it is the slow part."""
    if not llm_provider.is_configured():
        raise ToolError("No LLM provider is set up. Choose one under Settings first.")


def run_scraper(source_key: str, save: bool = True, limit: int = 200) -> dict:
    """Run the harness-built scraper for a source (logs in + pulls its data). Saves
    the result unless save=False. Requires the source's scraper to be built already."""
    if not has_scraper(source_key):
        raise ToolError(f"No scraper built for '{source_key}'. Build one first (build_scraper).")

    steps: list[dict] = []

    scrape_started = time.perf_counter()
    try:
        # Open a live progress channel so the phases INSIDE the scraper (browser
        # launch, sign-in, each API call) are visible while it runs — poll them
        # with action_progress(). Without this the operator sees one "Run scraper"
        # step for 30-60s, indistinguishable from a hang.
        # The reconciliation channel runs alongside progress: if the scraper
        # records the source's own control totals, we can answer "did we pull
        # EVERYTHING", which neither a passing test nor a successful run can.
        with progress.channel(source_key), reconcile.channel(source_key):
            txns = get_scraper(source_key)()
        steps.append({
            "key": "run_scraper",
            "label": "Run scraper",
            "status": "success",
            "duration_ms": int((time.perf_counter() - scrape_started) * 1000),
            "details": {"count": len(txns)},
        })
    except Exception as exc:
        steps.append({
            "key": "run_scraper",
            "label": "Run scraper",
            "status": "failed",
            "duration_ms": int((time.perf_counter() - scrape_started) * 1000),
            "error": str(exc),
        })
        # Does this failure actually want a human at a browser? The FAILURE says
        # so — the front-end used to guess by matching words like "session" in the
        # message, which opened a browser for a 403 that had nothing to do with
        # signing in, and showed a plain error for a genuinely missing password.
        raise ToolError({
            "message": str(exc),
            "steps": steps,
            "needs_login": bool(getattr(exc, "needs_browser_login", False)),
        }) from exc

    # Did we get EVERYTHING? Reported as its own step so a silent shortfall can't
    # hide behind a green "Run scraper". `ok is None` means the source published
    # nothing to check against — deliberately NOT rendered as a pass.
    balance = reconcile.summary(source_key)
    if balance["checked"]:
        short = balance["discrepancies"]
        steps.append({
            "key": "reconcile",
            "label": f"Reconcile against the source's own totals ({balance['checked']} checked)",
            "status": "success" if balance["ok"] else "failed",
            "details": {"checked": balance["checked"]},
            **({} if balance["ok"] else {
                "error": "; ".join(
                    f"{d['label']}: source says {d['expected']}, we extracted {d['actual']} "
                    f"(off by {d['difference']})" for d in short[:5]
                )
            }),
        })
        if not balance["ok"]:
            log.failure(
                operation="run_scraper",
                code="RECONCILIATION_FAILED",
                message=f"{len(short)} of {balance['checked']} control totals did not balance for '{source_key}'.",
                remediation="The scrape ran but the data is INCOMPLETE or wrong — check the date "
                            "window, pagination, and whether every account was included.",
                context={"source_key": source_key, "checked": balance["checked"],
                         "failed": len(short), "labels": [d["label"] for d in short[:10]]},
            )
    else:
        # Say what is actually known. The harness cannot tell whether the SOURCE
        # publishes totals — only that this scraper recorded none. Claiming the
        # former reads as "nothing to check here", which is how a missing check
        # gets mistaken for a passing one.
        steps.append({
            "key": "reconcile",
            "label": "Not reconciled — this scraper records no control totals",
            "status": "pending",
            "details": {"hint": "If the source publishes totals (a per-account Total, a balance "
                                "line, a row count), have the agent record them so the harness can "
                                "verify every transaction was pulled."},
        })

    result = {"source_key": source_key, **_summary(txns),
              "reconciliation": balance, "transactions": _rows(txns, limit)}

    persist_started = time.perf_counter()
    try:
        if save and txns:
            result["run_path"] = persist_scraped(txns, source_key)["run_path"]
            steps.append({
                "key": "persist_scraped",
                "label": "Persist scraped transactions",
                "status": "success",
                "duration_ms": int((time.perf_counter() - persist_started) * 1000),
            })
        else:
            steps.append({
                "key": "persist_scraped",
                "label": "Persist scraped transactions",
                "status": "success",
                "duration_ms": int((time.perf_counter() - persist_started) * 1000),
                "details": {"skipped": True},
            })
    except Exception as exc:
        steps.append({
            "key": "persist_scraped",
            "label": "Persist scraped transactions",
            "status": "failed",
            "duration_ms": int((time.perf_counter() - persist_started) * 1000),
            "error": str(exc),
        })
        raise ToolError({"message": str(exc), "steps": steps}) from exc

    return {**result, "steps": steps}


def fetch_source(source_key: str) -> dict:
    """Fetch a source's document from its inbox and ingest it (e.g. the 'email' source)."""
    steps: list[dict] = []
    def _on_step(step: dict) -> None:
        steps.append(step)

    fetch_started = time.perf_counter()
    try:
        runs = fetch_and_ingest(source_key, on_step=_on_step)
        steps.append({
            "key": "fetch_and_ingest",
            "label": "Fetch and ingest",
            "status": "success",
            "duration_ms": int((time.perf_counter() - fetch_started) * 1000),
            "details": {"documents": len(runs)},
        })
    except Exception as exc:
        steps.append({
            "key": "fetch_and_ingest",
            "label": "Fetch and ingest",
            "status": "failed",
            "duration_ms": int((time.perf_counter() - fetch_started) * 1000),
            "error": str(exc),
        })
        raise ToolError({"message": str(exc), "steps": steps}) from exc

    return {
        "source_key": source_key,
        "ingested": [{"run_path": r["run_path"], "count": r["transaction_count"]} for r in runs],
        "steps": steps,
    }


# Why a human had to step in. Ordered most-specific first — the first match wins,
# so "verification code" isn't swallowed by the broader session/sign-in pattern.
# Shown to the operator: a browser opening unexplained is exactly the black box
# this project forbids.
_AUTH_REASONS: tuple[tuple[str, str, str], ...] = (
    ("two_factor",
     r"2fa|two[\s-]?factor|verification code|verify (it'?s )?you|one[\s-]?time (code|pass)|otp|authenticator",
     "The portal asked for a 2FA / verification code. That code only reaches you, so the "
     "harness can't complete this sign-in on its own."),
    ("captcha",
     r"captcha|recaptcha|hcaptcha|are you a human|bot (check|detection)",
     "The portal presented a CAPTCHA. The harness will not solve those, so you need to "
     "sign in once yourself."),
    ("bad_credentials",
     r"incorrect password|invalid (password|credential|login)|wrong password|bad credential|\b403\b",
     "The stored credentials were rejected. Sign in manually to confirm them — and update "
     "them via scripts/manage_secrets.py if they've changed."),
    # The OBSERVATION is "no sign-in form found" — do not assert a redesign, which
    # is the least likely of several causes. Ordered by real-world likelihood.
    ("login_form_not_found",
     r"login form .*not detected|was not detected|no such element|selector",
     "The harness couldn't find the sign-in form on the page. Usually that means the page "
     "hadn't finished rendering, or you were already signed in and it didn't recognize the "
     "page; a redesigned login form is possible but less likely. Signing in once captures a "
     "fresh session either way."),
    ("page_timeout",
     r"timeout .*(waiting for|exceeded)|timed out",
     "The portal didn't respond in time. That's usually slowness or a redirect rather than a "
     "login problem — signing in once confirms which."),
    ("session_expired",
     r"\b401\b|unauthor|session|expired|signed out|logged out|sign[\s-]?in|login",
     "The saved browser session expired, so the portal wants a fresh interactive sign-in."),
)


def classify_auth_failure(message: str) -> dict:
    """Map a portal failure message to WHY a human is needed. Returns
    {reason, explanation} — `reason` is 'unknown' when nothing matches, and the
    explanation says so rather than inventing a cause."""
    text = (message or "").lower()
    for reason, pattern, explanation in _AUTH_REASONS:
        if re.search(pattern, text):
            return {"reason": reason, "explanation": explanation}
    return {
        "reason": "unknown",
        "explanation": "The portal blocked automated sign-in, but the harness couldn't tell why "
                       "from the error. Signing in once manually captures a fresh session.",
    }


def start_login_recovery(source_key: str, trigger_error: str = "") -> dict:
    """Open a visible persistent browser for manual re-login of a portal source.

    Pass `trigger_error` (the failure that prompted this) so the operator is told
    WHY a human is needed rather than just seeing a browser appear."""
    services = _load_services()
    service = next((s for s in services if s.key == source_key), None)
    if service is None:
        raise ToolError(f"Unknown source '{source_key}'.")
    if not service.login_url:
        raise ToolError(f"Source '{source_key}' has no login_url configured.")

    why = classify_auth_failure(trigger_error)

    existing = _LOGIN_RECOVERY_PROCS.get(source_key)
    if existing and existing.poll() is None:
        meta = _LOGIN_RECOVERY_META.get(source_key, {})
        return {
            "source_key": source_key,
            "status": "running",
            "pid": existing.pid,
            "login_url": meta.get("login_url", service.login_url),
            **{k: meta.get(k, why[k]) for k in ("reason", "explanation")},
            "steps": [
                {"key": "launch_browser", "label": "Launch recovery browser", "status": "success"},
                {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session", "status": "in-progress"},
            ],
        }

    # Reap any orphaned worker/browser left over from a prior attempt (e.g. a
    # worker that outlived a server restart) before starting a fresh one —
    # launching against an already-open profile just piles up blank tabs.
    reset_profile(source_key)

    cmd = [sys.executable, "-m", "core.tools.login_recovery_worker", source_key, service.login_url]
    logs_dir = Path("data/logs/login_recovery")
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"{source_key}-{stamp}.log"
    log_fh = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    log_fh.close()
    _LOGIN_RECOVERY_PROCS[source_key] = proc
    _LOGIN_RECOVERY_META[source_key] = {
        "login_url": service.login_url,
        "log_path": str(log_path),
        "started_at": time.monotonic(),
        **why,
    }
    return {
        "source_key": source_key,
        "status": "running",
        "pid": proc.pid,
        "login_url": service.login_url,
        "log_path": str(log_path),
        **why,
        "steps": [
            {"key": "launch_browser", "label": "Launch recovery browser", "status": "success"},
            {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session", "status": "in-progress"},
        ],
    }


# A wedged worker (locked profile, phantom tab) leaves the process alive with no
# browser. Give the browser this long to appear before calling it stuck.
_RECOVERY_BROWSER_GRACE_S = 45.0


def _recovery_browser_alive(source_key: str) -> bool:
    """Is a Chromium actually running against this source's profile? A live worker
    with no browser is the signature of a wedged launch."""
    from core.tools.browser_session import DEFAULT_PROFILE_ROOT, _find_pids
    profile_dir = DEFAULT_PROFILE_ROOT / source_key
    return bool(_find_pids(f"user-data-dir={profile_dir}"))


def cancel_login_recovery(source_key: str) -> dict:
    """Kill a running/wedged recovery worker so the operator can retry. The escape
    hatch for a browser that never opened or never registered as closed."""
    proc = _LOGIN_RECOVERY_PROCS.get(source_key)
    if proc is None:
        return {"source_key": source_key, "status": "idle", "cancelled": False}
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _LOGIN_RECOVERY_PROCS.pop(source_key, None)
    _LOGIN_RECOVERY_META.pop(source_key, None)
    reset_profile(source_key)   # release the profile lock so a retry can launch
    return {"source_key": source_key, "status": "cancelled", "cancelled": True}


def login_recovery_status(source_key: str) -> dict:
    """Check whether a login recovery browser session is still active."""
    proc = _LOGIN_RECOVERY_PROCS.get(source_key)
    meta = _LOGIN_RECOVERY_META.get(source_key, {})
    if proc is None:
        return {
            "source_key": source_key,
            "status": "idle",
            "steps": [
                {"key": "launch_browser", "label": "Launch recovery browser", "status": "pending"},
                {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session", "status": "pending"},
                {"key": "session_saved", "label": "Session saved", "status": "pending"},
            ],
        }

    exit_code = proc.poll()
    if exit_code is None:
        elapsed = time.monotonic() - meta.get("started_at", time.monotonic())
        browser_alive = _recovery_browser_alive(source_key)
        # Worker alive, past the grace period, but no browser against this
        # profile: the launch wedged. Say so instead of spinning forever.
        if not browser_alive and elapsed > _RECOVERY_BROWSER_GRACE_S:
            message = (
                "The recovery browser isn't running (it either never opened or has already "
                "quit), but the worker is still waiting. Cancel the recovery and retry — "
                "close any other Chromium windows using this source's profile first."
            )
            log.failure(
                operation="login_recovery_status",
                code="LOGIN_RECOVERY_WEDGED",
                message=f"Recovery worker for '{source_key}' is alive with no browser.",
                remediation="Cancel the recovery (cancel_login_recovery) and retry.",
                context={"source_key": source_key, "pid": proc.pid, "elapsed_s": round(elapsed, 1),
                         "log_path": meta.get("log_path")},
            )
            return {
                "source_key": source_key,
                "status": "stuck",
                "pid": proc.pid,
                "elapsed_s": round(elapsed, 1),
                "message": message,
                "browser_running": False,
                **{k: meta[k] for k in ("reason", "explanation") if k in meta},
                "steps": [
                    {"key": "launch_browser", "label": "Launch recovery browser",
                     "status": "failed", "error": message},
                    {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session",
                     "status": "pending"},
                    {"key": "session_saved", "label": "Session saved", "status": "pending"},
                ],
            }
        return {
            "source_key": source_key,
            "status": "running",
            "pid": proc.pid,
            "elapsed_s": round(elapsed, 1),
            "browser_running": browser_alive,
            "login_url": meta.get("login_url"),
            "log_path": meta.get("log_path"),
            **{k: meta[k] for k in ("reason", "explanation") if k in meta},
            "steps": [
                {"key": "launch_browser", "label": "Launch recovery browser", "status": "success"},
                {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session", "status": "in-progress"},
                {"key": "session_saved", "label": "Session saved", "status": "pending"},
            ],
        }

    _LOGIN_RECOVERY_PROCS.pop(source_key, None)
    _LOGIN_RECOVERY_META.pop(source_key, None)
    ok = exit_code == 0
    failure_detail = None
    log_path_str = meta.get("log_path")
    if not ok and log_path_str:
        tail = _tail_text(Path(log_path_str))
        failure_detail = tail or None
    return {
        "source_key": source_key,
        "status": "completed" if ok else "failed",
        "exit_code": exit_code,
        "login_url": meta.get("login_url"),
        "log_path": log_path_str,
        **({"message": failure_detail} if failure_detail else {}),
        "steps": [
            {"key": "launch_browser", "label": "Launch recovery browser", "status": "success"},
            {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session", "status": "success" if ok else "failed"},
            {"key": "session_saved", "label": "Session saved", "status": "success" if ok else "failed"},
        ],
    }


def list_llm_models(base_url: str, api_key: str | None = None) -> dict:
    """Ask an OpenAI-compatible server which models it actually has, so the
    operator picks a real one instead of guessing. Surfaces the real reason on
    failure rather than silently returning nothing."""
    if not (base_url or "").strip():
        raise ToolError("A base URL is required to list models.")
    # Blank field = "use the key I already stored", so it never has to be retyped.
    api_key = (api_key or "").strip() or llm_provider.stored_api_key("openai_compatible")
    try:
        return {"base_url": base_url, "models": llm_provider.list_models(base_url.strip(), api_key)}
    except RuntimeError as exc:
        # Already a logged, actionable "message remediation" string — don't bury it.
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Couldn't list models from {base_url}: {exc}") from exc


def set_llm_provider(
    provider: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict:
    """Store which LLM the harness uses (encrypted in .secrets/) and load it into
    the environment immediately. 'anthropic' needs an api_key; 'openai_compatible'
    needs base_url + model, key optional."""
    if provider not in llm_provider.PROVIDERS:
        raise ToolError(f"Unknown provider '{provider}'. Known: {list(llm_provider.PROVIDERS)}")

    api_key = (api_key or "").strip() or None
    base_url = (base_url or "").strip() or None
    model = (model or "").strip() or None

    # CredentialStore.set() REPLACES the whole record, so an omitted key would
    # wipe the stored one. Blank field means "keep the key I already saved" —
    # the operator only retypes it to change it.
    reused_key = False
    if not api_key:
        api_key = llm_provider.stored_api_key(provider)
        reused_key = api_key is not None

    if provider == "anthropic":
        if not api_key:
            raise ToolError("An Anthropic API key is required.")
    else:
        if not base_url:
            raise ToolError("A base URL is required for an OpenAI-compatible server.")
        if not model:
            raise ToolError("A model is required — list the server's models and pick one.")

    try:
        llm_provider.store_llm_credential(provider, api_key=api_key, base_url=base_url, model=model)
        llm_provider.load_into_env()
    except Exception as exc:
        raise ToolError(f"Couldn't save the LLM provider: {exc}") from exc
    # Report the reuse so it's visible, not silent — the operator should know the
    # saved key came from the vault rather than from what they just typed.
    return {"saved": True, "reused_stored_api_key": reused_key, **llm_status()}


def start_build(
    kind: str,
    source_key: str,
    mode: str = "build",
    sample_path: str | None = None,
    feedback: str = "",
    portal_url: str = "",
    demo_path: str = "",
) -> dict:
    """Start the embedded agent building (or revising) a parser/scraper for a
    source, in the background. An agent build takes minutes, so this returns
    immediately — poll build_status() for progress and the result.

    Nothing is activated: the operator reviews the verification and approves via
    activate_parser."""
    if kind not in ("parser", "scraper"):
        raise ToolError(f"Unknown build kind '{kind}'. Use 'parser' or 'scraper'.")
    if mode not in ("build", "revise"):
        raise ToolError(f"Unknown build mode '{mode}'. Use 'build' or 'revise'.")

    services = _load_services()
    service = next((s for s in services if s.key == source_key), None)
    if service is None:
        raise ToolError(f"Unknown source '{source_key}'.")

    if kind == "parser":
        if not sample_path:
            raise ToolError("A sample document is required — upload one first.")
        sample = Path(sample_path).expanduser()
        if not sample.exists():
            raise ToolError(f"No file at {sample}")
        sample_path = str(sample)
    else:
        portal_url = (portal_url or service.login_url or "").strip()
        demo_path = (demo_path or "").strip()
        if demo_path and not Path(demo_path).expanduser().exists():
            raise ToolError(f"No demonstration at {demo_path} — record one first.")
        # A captured demonstration is a URL and then some: it already contains
        # where the operator went and what they clicked. Only demand a URL when
        # there's no demonstration yet, so the build has *something* to open.
        if mode == "build" and not portal_url and not demo_path:
            raise ToolError(
                f"Source '{source_key}' has no portal URL and no demonstration yet. "
                "Show the harness how to reach the data first."
            )
    if mode == "revise" and not (feedback or "").strip() and kind == "parser":
        raise ToolError("Say what needs changing — revise needs feedback.")

    if not llm_provider.is_configured():
        raise ToolError("No LLM provider is configured. Set one in Settings first.")

    existing = _BUILD_PROCS.get(source_key)
    if existing and existing.poll() is None:
        raise ToolError(f"A build is already running for '{source_key}'. Wait for it to finish.")

    run_dir = Path("data/logs/builds")
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_file = run_dir / f"{kind}-{source_key}-{stamp}.jsonl"
    run_file.touch()

    cmd = [
        sys.executable, "-m", "orchestration.build_worker",
        "--kind", kind, "--mode", mode,
        "--source-key", source_key,
        "--run-file", str(run_file),
        "--source-label", service.label or "",
    ]
    if sample_path:
        cmd += ["--sample-path", sample_path]
    if feedback:
        cmd += ["--feedback", feedback]
    if portal_url:
        cmd += ["--portal-url", portal_url]
    if demo_path:
        cmd += ["--demo-path", demo_path]

    log_path = run_file.with_suffix(".log")
    with log_path.open("w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, start_new_session=True)
    _BUILD_PROCS[source_key] = proc
    _BUILD_META[source_key] = {
        "kind": kind, "mode": mode, "run_file": str(run_file), "log_path": str(log_path),
    }
    return {
        "source_key": source_key, "kind": kind, "mode": mode, "status": "running",
        "pid": proc.pid, "run_file": str(run_file),
        "steps": _build_steps(kind, mode, "running"),
    }


def _build_steps(kind: str, mode: str, status: str) -> list[dict]:
    """The build's stages, for the same step timeline the other actions render."""
    verb = "Build" if mode == "build" else "Revise"
    running, done = ("in-progress", "pending"), ("success", "success")
    agent_state, verify_state = running if status == "running" else done
    if status == "failed":
        agent_state, verify_state = "failed", "pending"
    return [
        {"key": "agent_codegen", "label": f"{verb} the {kind} (agent writes code + a test)",
         "status": agent_state},
        {"key": "verify", "label": "Re-run the agent's test independently", "status": verify_state},
    ]


def _read_run_file(path: Path) -> tuple[list[str], dict | None, dict | None]:
    """Parse the worker's JSONL protocol into (events, result, failure)."""
    events: list[str] = []
    result: dict | None = None
    failure: dict | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events, result, failure
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially-written final line while the worker is mid-flush
        kind = rec.get("type")
        if kind == "event":
            events.append(rec.get("text", ""))
        elif kind == "result":
            result = rec.get("result")
        elif kind == "failed":
            failure = {"error": rec.get("error", "build failed"), "traceback": rec.get("traceback")}
    return events, result, failure


def build_status(source_key: str, event_offset: int = 0) -> dict:
    """Progress of the running/last build for a source. `event_offset` returns only
    events after that index, so the GUI can tail a long build cheaply.

    status: idle | running | completed | failed. `completed` means the build RAN —
    read `result.verification.ok` to see whether the code actually passed."""
    proc = _BUILD_PROCS.get(source_key)
    meta = _BUILD_META.get(source_key, {})
    if proc is None or not meta:
        return {"source_key": source_key, "status": "idle", "events": [],
                "event_count": 0, "steps": []}

    run_file = Path(meta["run_file"])
    events, result, failure = _read_run_file(run_file)
    exit_code = proc.poll()
    kind, mode = meta.get("kind", "parser"), meta.get("mode", "build")

    if exit_code is None:
        status = "running"
    elif failure or result is None or exit_code != 0:
        status = "failed"
    else:
        status = "completed"

    payload = {
        "source_key": source_key, "kind": kind, "mode": mode, "status": status,
        "events": events[event_offset:], "event_count": len(events),
        "steps": _build_steps(kind, mode, status),
    }
    if status == "running":
        # A spinner cannot tell "thinking" from "wedged". The run file's mtime is
        # exactly when the worker last said anything, so the screen can show the
        # silence and its own last words instead of leaving the operator guessing.
        try:
            idle = max(0.0, time.time() - run_file.stat().st_mtime)
        except OSError:
            idle = 0.0
        payload["idle_seconds"] = round(idle, 1)
        payload["last_event"] = events[-1] if events else ""
        # A local model can legitimately go quiet for ~3 minutes (measured); past
        # that, say so out loud rather than implying all is well. The worker's own
        # watchdog is what eventually ends it.
        payload["stalled"] = idle > 180
        return payload

    if status == "failed":
        detail = failure or {}
        message = detail.get("error") or _tail_text(Path(meta["log_path"])) or "The build failed."
        payload["message"] = message
        payload["steps"] = [
            {**s, "status": "failed", "error": message} if s["key"] == "agent_codegen" else s
            for s in payload["steps"]
        ]
        return payload

    from orchestration import verify as _verify

    verification = (result or {}).get("verification") or {}
    payload["result"] = result
    payload["passed"] = bool(verification.get("ok"))
    # WHY it was refused, named specifically. "Its test did NOT pass" was reported
    # for every refusal, including runs whose test passed.
    payload["blockers"] = _verify.blockers(verification)
    # What was seen and deliberately not treated as fatal. Shown because the
    # alternative to classifying a finding as advisory is deleting the rule, and
    # that makes it invisible to everyone forever — which is the masking this
    # project doesn't do.
    payload["notes"] = _verify.notes(verification)
    payload["no_change_reason"] = verification.get("no_change_reason") or ""
    # WHAT it did, as files and commands. The agent's own prose can run to tens of
    # thousands of characters; the list of files it wrote cannot.
    payload["did"] = _what_it_did(result or {})

    # The agent just rewrote this source's module on disk, but THIS process still
    # has the version it imported at startup — so without a reload the operator
    # runs the old code and the fix looks like it did nothing. Do it once, on the
    # transition to completed.
    if not meta.get("reloaded"):
        payload["reload"] = hot_reload.reload_source_code(kind, source_key)
        meta["reloaded"] = True
    if not payload["passed"]:
        # Ran, but the code isn't acceptable — say so plainly instead of implying
        # success, and put the REASON on the step rather than the test output (which
        # may well read "17 passed" and send the operator the wrong way).
        reason = "; ".join(payload["blockers"]) or "The build was refused."
        payload["steps"] = [
            {**s, "status": "failed", "error": reason} if s["key"] == "verify" else s
            for s in payload["steps"]
        ]
    return payload


def _what_it_did(result: dict) -> dict:
    """The agent's run as a short list of acts: files written, commands run, files
    read. Derived from the recorded tool calls, so it can't drift from what it
    actually did — and it stays readable however long the model's prose gets."""
    # Imported rather than spelled out: the agent has several ways to change a
    # file, and a screen that only knows about one of them tells the operator
    # "files written: none" about a run that edited three. Deferred like the
    # verify import below, to keep this module importable on its own.
    from orchestration.agent_tools import WRITE_TOOLS

    files: list[str] = []
    commands: list[str] = []
    reads = 0
    for call in result.get("tool_calls") or []:
        try:
            name, args = call[0], call[1] or {}
        except (IndexError, TypeError):
            continue
        if name in WRITE_TOOLS:
            path = str(args.get("path") or "")
            if path and path not in files:
                files.append(path)
        elif name == "run_command":
            commands.append(str(args.get("command") or ""))
        else:
            reads += 1
    return {"files": files, "commands": commands, "reads": reads}


def stop_build(source_key: str) -> dict:
    """Stop a running build.

    There was no way to do this from the app: a build that wedged holding a socket
    to the model could only be killed from a terminal, which is exactly the black
    box this project forbids. Whatever the agent already wrote stays on disk — the
    files are the point, and the operator can look at the diff.
    """
    proc = _BUILD_PROCS.get(source_key)
    meta = _BUILD_META.get(source_key, {})
    if proc is None or not meta:
        raise ToolError(f"No build is running for '{source_key}'.")
    if proc.poll() is not None:
        return {"source_key": source_key, "stopped": False,
                "message": "That build had already finished."}

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()          # blocked in a syscall; terminate alone won't land

    # Record it in the run file ourselves: the worker may die before it can, and a
    # bare non-zero exit would read as a crash rather than a decision.
    run_file = Path(meta.get("run_file", ""))
    if run_file.name:
        try:
            with run_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "failed",
                    "error": "You stopped this build. Anything it had already written is still on disk.",
                }) + "\n")
        except OSError:
            pass

    log.event(operation="stop_build", code="BUILD_STOPPED",
              message=f"Operator stopped the build for '{source_key}'.",
              context={"source_key": source_key, "pid": proc.pid})
    return {"source_key": source_key, "stopped": True,
            "message": "Stopped. Anything it already wrote is still on disk."}


def activate_parser(source_key: str) -> dict:
    """Approve a built parser — activate it so the source uses it automatically."""
    if not source_status.parser_built(source_key):
        raise ToolError(f"No parser built for '{source_key}' to activate.")
    ServiceManifest().update(source_key, parser=source_key, status="implemented")
    return {"source_key": source_key, "status": "implemented"}


# The tools the MCP server registers, in one place.
READ_TOOLS = [list_sources, source_methods, latest_transactions, source_transactions,
              action_progress, source_settings, credential_status, email_status,
              pending_approvals, llm_status, status]
ACTION_TOOLS = [
    add_source,
    suggest_source_name,
    preview_document,
    preview_demo,
    preview_inbox,
    start_demo,
    demo_status,
    ingest_document,
    extract_now,
    run_scraper,
    fetch_source,
    activate_parser,
    start_login_recovery,
    login_recovery_status,
    cancel_login_recovery,
    list_llm_models,
    set_llm_provider,
    start_build,
    stop_build,
    build_status,
    save_source_settings,
    reset_source_settings,
    set_credentials,
    forget_credentials,
    start_gmail_consent,
    gmail_consent_status,
    add_inbox,
    rename_inbox,
    delete_inbox,
    reapprove_inbox,
    save_email_search,
    remove_email_search,
    source_email_route,
]
ALL_TOOLS = READ_TOOLS + ACTION_TOOLS
