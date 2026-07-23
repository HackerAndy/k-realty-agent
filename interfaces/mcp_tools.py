# Template candidate: generic (tier 1, pattern) — thin tool functions over core/,
# the seam a Claude Desktop / Cowork front-end calls. See promotion-log.md.
"""The MCP tool surface — plain functions the MCP server exposes to a Claude host.

Each is a THIN wrapper over an existing core/ capability, returning JSON-friendly
dicts. Kept separate from the MCP transport (interfaces/mcp_server.py) so the tool
logic is unit-testable without standing up a server. No new business logic lives
here; it all delegates to core/.
"""

from __future__ import annotations

from pathlib import Path

from core import source_status
from core.fetch_ingest import fetch_and_ingest, persist_scraped
from core.ingest import ingest_source, load_latest_parsed, transactions_from_run
from core.models import Transaction
from core.scrapers import get_scraper, has_scraper
from core.tools import llm_provider
from core.tools.service_manifest import ServiceManifest


class ToolError(RuntimeError):
    """Surfaced to the MCP client as a tool error."""


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

def list_sources() -> list[dict]:
    """List every financial source and its state (parser/scraper built, status)."""
    return [
        {
            "key": s.key,
            "label": s.label,
            "status": s.status,
            "input_type": s.input_type,
            "access": s.access,
            "parser": s.parser,
            "is_trigger": source_status.is_trigger(s),
            "parser_built": source_status.parser_built(s.key),
            "has_scraper": has_scraper(s.key),
        }
        for s in ServiceManifest().load()
    ]


def latest_transactions(limit: int = 200) -> dict:
    """The most recent ingested transactions, with money-in/out totals."""
    run = load_latest_parsed()
    if run is None:
        return {"source_key": None, "count": 0, "money_in": 0, "money_out": 0, "transactions": []}
    txns = transactions_from_run(run)
    return {"source_key": run.get("source_key"), "month": run.get("month"),
            **_summary(txns), "transactions": _rows(txns, limit)}


def pending_approvals() -> list[dict]:
    """Sources whose parser is built but not yet activated — awaiting the operator's yes."""
    return [{"key": s.key, "label": s.label} for s in source_status.pending_approvals()]


def llm_status() -> dict:
    """Which LLM provider the harness is configured to use."""
    cfg = llm_provider.current_config() or {}
    return {"configured": llm_provider.is_configured(),
            "provider": cfg.get("provider"), "model": cfg.get("model"), "base_url": cfg.get("base_url")}


def status() -> dict:
    """Overall harness status — LLM, source counts, pending approvals, latest ingest."""
    services = ServiceManifest().load()
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

def ingest_document(source_key: str, path: str) -> dict:
    """Parse a document you already have (PDF/CSV) for a source into transactions."""
    doc = Path(path).expanduser()
    if not doc.exists():
        raise ToolError(f"No file at {doc}")
    run = ingest_source(source_key, doc)
    return {"source_key": source_key, "run_path": run["run_path"], **_summary(transactions_from_run(run))}


def run_scraper(source_key: str, save: bool = True, limit: int = 200) -> dict:
    """Run the harness-built scraper for a source (logs in + pulls its data). Saves
    the result unless save=False. Requires the source's scraper to be built already."""
    if not has_scraper(source_key):
        raise ToolError(f"No scraper built for '{source_key}'. Build one first (build_scraper).")
    txns = get_scraper(source_key)()
    result = {"source_key": source_key, **_summary(txns), "transactions": _rows(txns, limit)}
    if save and txns:
        result["run_path"] = persist_scraped(txns, source_key)["run_path"]
    return result


def fetch_source(source_key: str) -> dict:
    """Fetch a source's document from its inbox and ingest it (e.g. the 'email' source)."""
    runs = fetch_and_ingest(source_key)
    return {"source_key": source_key,
            "ingested": [{"run_path": r["run_path"], "count": r["transaction_count"]} for r in runs]}


def activate_parser(source_key: str) -> dict:
    """Approve a built parser — activate it so the source uses it automatically."""
    if not source_status.parser_built(source_key):
        raise ToolError(f"No parser built for '{source_key}' to activate.")
    ServiceManifest().update(source_key, parser=source_key, status="implemented")
    return {"source_key": source_key, "status": "implemented"}


# The tools the MCP server registers, in one place.
READ_TOOLS = [list_sources, latest_transactions, pending_approvals, llm_status, status]
ACTION_TOOLS = [ingest_document, run_scraper, fetch_source, activate_parser]
ALL_TOOLS = READ_TOOLS + ACTION_TOOLS
