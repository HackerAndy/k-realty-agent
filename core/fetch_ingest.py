# Template candidate: generic (tier 1) — the fetch→route pattern is
# client-agnostic. See agent-harness-template/docs/promotion-log.md.
"""Fetch a source's document out of an inbox, then ingest it normally.

Email is a ROUTE, not a source: this retrieves the attachment that carries a
source's data (core/tools/email_fetcher.py) and hands it to that source's own
committed parser through the ordinary ingest path — so a fetched Epic PDF is
parsed by exactly the same code as one you'd drop in by hand. The fetch only
replaces the manual "point at a file" step.

Which inbox to search and what to look for comes from the SOURCE
(Service.email_search); the inbox holds nothing but the access to it, which is
why one connected account can carry several sources.

Deliberately minimal: fetch + ingest. Fetched documents are written under
data/inbox/<source_key>/ (gitignored) so there's an on-disk copy to inspect.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.ingest import IngestError, _persist, ingest_source
from core.models import Transaction
from core.observability import get_logger
from core.tools.service_manifest import ServiceManifest

INBOX_DIR = Path("data/inbox")

log = get_logger("core.fetch_ingest")


def persist_scraped(transactions: list[Transaction], source_uri: str) -> dict:
    """Persist portal-scraped transactions through the same store as parsed ones.
    The source key comes from the transactions themselves (e.g. the portal source
    key the scraper stamped), so scraped runs sit beside emailed/parsed runs in
    data/parsed/ and are distinguishable by source."""
    key = transactions[0].source_key if transactions else "portal_scrape"
    return _persist(key, transactions, source_uri, "portal_scrape", None, transport="scrape")


def fetch_and_ingest(
    source_key: str,
    manifest: ServiceManifest | None = None,
    limit: int = 5,
    on_event: Callable[[str], None] = print,
    on_step: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Fetch this source's documents from its inbox and ingest each with its own
    parser. Returns one ingest run dict per document ingested."""
    def _step(key: str, label: str, status: str = "success", **details) -> None:
        if on_step is None:
            return
        rec = {"key": key, "label": label, "status": status}
        if details:
            rec["details"] = details
        on_step(rec)

    manifest = manifest or ServiceManifest()
    source = manifest.get(source_key)
    _step("resolve_source", "Resolve source", source_key=source_key)
    search = source.email_search
    if search is None:
        raise IngestError(log.failure(
            operation="fetch_and_ingest",
            code="NO_EMAIL_SEARCH",
            message=f"'{source_key}' doesn't arrive by email.",
            remediation="Open the source and set up its email route (which inbox, and what to "
                        "look for) before fetching.",
            context={"source_key": source_key},
        ))
    if not source.parser or source.status != "implemented":
        raise IngestError(log.failure(
            operation="fetch_and_ingest",
            code="NO_PARSER",
            message=f"'{source_key}' has no active parser yet (status: {source.status}), so a "
                    f"fetched document couldn't be read.",
            remediation="Build and approve this source's parser first, then fetch.",
            context={"source_key": source_key, "status": source.status},
        ))
    _step("load_email_search", "Load email search", carrier=search.carrier, parser=source.parser)

    # The inbox is a separate service holding only the access to it.
    carrier = manifest.get(search.carrier)
    provider = carrier.provider or "gmail"

    # Provider dispatch — gmail today; imap/other providers slot in here later.
    if provider == "gmail":
        from core.tools.email_fetcher import GmailFetcher

        # Keyed by the INBOX: that's whose OAuth token opens it.
        fetcher = GmailFetcher(search.carrier)
        _step("init_provider", "Initialize provider", provider="gmail", inbox=carrier.label)
    else:
        raise IngestError(log.failure(
            operation="fetch_and_ingest",
            code="UNSUPPORTED_PROVIDER",
            message=f"Unsupported inbox provider '{provider}' for '{search.carrier}'.",
            remediation="Use a supported provider (gmail) or add an implementation for it.",
            context={"source_key": source_key, "carrier": search.carrier, "provider": provider},
        ))

    on_event(f"Searching {carrier.label} for the message carrying {source.label}'s document...")
    _step("search_messages", "Search messages", provider=provider, limit=limit)
    documents = fetcher.search_and_fetch(search, limit=limit)
    _step("messages_fetched", "Fetch message attachments", documents=len(documents))
    if not documents:
        on_event("No matching messages found. Nothing to ingest.")
        _step("fetch_complete", "Fetch complete", ingested=0)
        return []

    runs: list[dict] = []
    dest_dir = INBOX_DIR / source_key
    for doc in documents:
        saved = doc.save(dest_dir)
        _step("save_attachment", "Save attachment", filename=doc.filename, path=str(saved))
        on_event(f"Fetched '{doc.filename}' (received {doc.received or 'unknown date'}) "
                 f"→ ingesting with {source.label}'s parser.")
        run = ingest_source(source_key, saved, manifest=manifest, transport="email")
        run["fetched_from"] = search.carrier
        run["fetched_message_id"] = doc.message_id
        runs.append(run)
        _step(
            "ingest_document",
            "Ingest fetched document",
            filename=doc.filename,
            run_path=run.get("run_path"),
            count=run.get("transaction_count"),
        )
    _step("fetch_complete", "Fetch complete", ingested=len(runs))
    return runs
