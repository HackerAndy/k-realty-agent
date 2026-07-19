# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Fetch a document out of an email inbox.

A fetched source (an inbox) doesn't parse anything itself — it locates the one
message carrying the document and pulls the attachment, which is then routed to
another source's committed parser (see core/fetch_ingest.py). This module is the
retrieval half.

Multi-provider by design: FetchConfig + build_gmail_query() express the search in
provider-neutral terms; GmailFetcher implements it against the Gmail API (the path
Google requires post-2025). An ImapFetcher for other providers (still allowing app
passwords) can implement the same shape later — the interview picks which.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from core.observability import get_logger
from core.tools.service_manifest import FetchConfig

log = get_logger("core.tools.email_fetcher")


@dataclass
class FetchedDocument:
    """One attachment pulled from a message, held in memory until routed."""

    filename: str
    data: bytes
    message_id: str
    received: str = ""  # RFC-2822 date header, for the operator's audit

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.filename
        path.write_bytes(self.data)
        return path


def build_gmail_query(cfg: FetchConfig) -> str:
    """Translate FetchConfig into a Gmail search string. Deterministic and
    unit-tested — the live Gmail call is a thin wrapper around this."""
    parts: list[str] = ["has:attachment"]
    if cfg.from_address:
        parts.append(f"from:{cfg.from_address}")
    if cfg.subject_contains:
        parts.append(f'subject:"{cfg.subject_contains}"')
    if cfg.attachment_suffix:
        # Gmail's filename: matches the extension without the dot.
        parts.append(f"filename:{cfg.attachment_suffix.lstrip('.')}")
    if cfg.newer_than_days:
        parts.append(f"newer_than:{cfg.newer_than_days}d")
    return " ".join(parts)


def _matches_suffix(filename: str, suffix: str | None) -> bool:
    return not suffix or filename.lower().endswith(suffix.lower())


class GmailFetcher:
    """Pull attachments from a Gmail/Workspace inbox over the Gmail API using the
    stored OAuth token (core/tools/email_oauth.py)."""

    def __init__(self, source_key: str):
        self.source_key = source_key

    def _service(self):
        from googleapiclient.discovery import build

        from core.tools import email_oauth

        creds = email_oauth.load_credentials(self.source_key)
        return build("gmail", "v1", credentials=creds)

    def search_and_fetch(self, cfg: FetchConfig, limit: int = 5) -> list[FetchedDocument]:
        """Find messages matching cfg and return their matching attachments,
        newest first. `limit` bounds how many messages we pull (the target
        statement is normally the most recent match)."""
        query = build_gmail_query(cfg)
        try:
            service = self._service()
            listing = service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        except Exception as exc:
            raise RuntimeError(log.failure(
                operation="gmail_search",
                code="GMAIL_API_ERROR",
                message=f"Gmail search failed for '{self.source_key}'.",
                remediation="Check the network and that the OAuth token is still valid "
                            "(re-run email setup to refresh it if needed).",
                context={"source_key": self.source_key, "query": query},
                exc=exc,
            )) from exc
        messages = listing.get("messages", [])

        found: list[FetchedDocument] = []
        for msg_ref in messages:
            msg = service.users().messages().get(userId="me", id=msg_ref["id"], format="full").execute()
            received = _header(msg, "Date")
            for filename, attachment_id in _attachment_parts(msg.get("payload", {})):
                if not _matches_suffix(filename, cfg.attachment_suffix):
                    continue
                att = (
                    service.users().messages().attachments()
                    .get(userId="me", messageId=msg_ref["id"], id=attachment_id)
                    .execute()
                )
                data = base64.urlsafe_b64decode(att["data"])
                found.append(FetchedDocument(filename=filename, data=data,
                                             message_id=msg_ref["id"], received=received))
        return found


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _attachment_parts(payload: dict) -> list[tuple[str, str]]:
    """Walk a message payload tree, yielding (filename, attachment_id) for every
    part that is a real attachment."""
    out: list[tuple[str, str]] = []
    filename = payload.get("filename")
    body = payload.get("body", {})
    if filename and body.get("attachmentId"):
        out.append((filename, body["attachmentId"]))
    for part in payload.get("parts", []) or []:
        out.extend(_attachment_parts(part))
    return out
