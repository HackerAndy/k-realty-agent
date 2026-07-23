# Template candidate: generic (tier 1) — source-state logic, front-end agnostic.
# See agent-harness-template/docs/promotion-log.md.
"""What state a source is in — shared by every front-end (TUI, MCP server).

Whether a source has a parser, is built-but-not-activated, or is a trigger/inbox
is DOMAIN logic, not UI. Keeping it here (framework-free) means the TUI and the
MCP layer read one source of truth instead of each re-deriving it.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from pathlib import Path

from core.parsers import REGISTRY
from core.tools.service_manifest import Service, ServiceManifest


def parser_built(source_key: str) -> bool:
    """True if a parser exists for this source (file written and/or registered),
    even if the source isn't marked implemented — i.e. built but awaiting approval."""
    return (Path("core/parsers") / f"{source_key}.py").exists() or source_key in REGISTRY


def is_trigger(service: Service) -> bool:
    """A trigger source (an inbox) signals a document has arrived — a delivery
    channel, not itself a document to parse. It never has a parser of its own."""
    return service.input_type == "email_trigger"


def pending_approvals(services: list[Service] | None = None) -> list[Service]:
    """Sources whose parser is built but NOT yet activated — outstanding actions
    needing the operator's explicit yes. Surfaced loudly so nothing waits in the dark."""
    services = services if services is not None else ServiceManifest().load()
    return [
        s for s in services
        if not is_trigger(s) and s.status != "implemented" and parser_built(s.key)
    ]
