# Template candidate: generic (tier 1) — the fetch→route pattern is
# client-agnostic. See agent-harness-template/docs/promotion-log.md.
"""Fetch a document from a source, then ingest it through the source it delivers to.

This is the seam that makes an inbox a real ingest source: it retrieves the
attachment (core/tools/email_fetcher.py) and hands each document to the committed
parser of `fetch.delivers_to` via the ordinary ingest path — so a fetched Epic
PDF is parsed by exactly the same code as one you'd drop in by hand. No new
parser; the fetch just replaces the manual "point at a file" step.

Deliberately minimal: fetch + route. Fetched documents are written under
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
    """Fetch documents for `source_key` and ingest each through its delivers_to
    source. Returns one ingest run dict per document ingested."""
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
    cfg = source.fetch
    if cfg is None:
        raise IngestError(log.failure(
            operation="fetch_and_ingest",
            code="NO_FETCH_CONFIG",
            message=f"'{source_key}' has no fetch config.",
            remediation="Set up the fetch for this source in the TUI first.",
            context={"source_key": source_key},
        ))
    _step("load_fetch_config", "Load fetch config", provider=cfg.provider, delivers_to=cfg.delivers_to)

    target = manifest.get(cfg.delivers_to)
    if not target.parser or target.status != "implemented":
        raise IngestError(log.failure(
            operation="fetch_and_ingest",
            code="DELIVERS_TO_NO_PARSER",
            message=f"'{source_key}' delivers to '{cfg.delivers_to}', but that source has no "
                    f"active parser yet (status: {target.status}).",
            remediation="Build/activate the delivered source's parser first.",
            context={"source_key": source_key, "delivers_to": cfg.delivers_to, "status": target.status},
        ))
    _step("validate_delivery_target", "Validate delivery target", target=cfg.delivers_to, parser=target.parser)

    # Provider dispatch — gmail today; imap/other providers slot in here later.
    if cfg.provider == "gmail":
        from core.tools.email_fetcher import GmailFetcher

        fetcher = GmailFetcher(source_key)
        _step("init_provider", "Initialize provider", provider="gmail")
    else:
        raise IngestError(log.failure(
            operation="fetch_and_ingest",
            code="UNSUPPORTED_PROVIDER",
            message=f"Unsupported fetch provider '{cfg.provider}' for '{source_key}'.",
            remediation="Use a supported provider (gmail) or add an implementation for it.",
            context={"source_key": source_key, "provider": cfg.provider},
        ))

    on_event(f"Searching {source.label} for messages matching the configured criteria...")
    _step("search_messages", "Search messages", provider=cfg.provider, limit=limit)
    documents = fetcher.search_and_fetch(cfg, limit=limit)
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
                 f"→ ingesting via {target.label}'s parser.")
        run = ingest_source(cfg.delivers_to, saved, manifest=manifest, transport="email")
        run["fetched_from"] = source_key
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
