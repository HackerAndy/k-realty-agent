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
import subprocess
import sys
import time
from datetime import datetime, timezone

from core import source_status
from core.fetch_ingest import fetch_and_ingest, persist_scraped
from core.ingest import (
    ingest_source,
    load_latest_parsed,
    load_latest_parsed_for,
    transactions_from_run,
)
from core.models import Transaction
from core.scrapers import get_scraper, has_scraper
from core.tools import llm_provider
from core.tools.browser_session import reset_profile
from core.tools.service_manifest import ServiceManifest, ServiceManifestError


class ToolError(RuntimeError):
    """Surfaced to the MCP client as a tool error."""


_LOGIN_RECOVERY_PROCS: dict[str, subprocess.Popen] = {}
_LOGIN_RECOVERY_META: dict[str, dict[str, str]] = {}


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
        for s in _load_services()
    ]


def latest_transactions(limit: int = 200) -> dict:
    """The most recent ingested transactions, with money-in/out totals."""
    run = load_latest_parsed()
    if run is None:
        return {"source_key": None, "count": 0, "money_in": 0, "money_out": 0, "transactions": []}
    txns = transactions_from_run(run)
    return {"source_key": run.get("source_key"), "month": run.get("month"),
            **_summary(txns), "transactions": _rows(txns, limit)}


def source_transactions(source_key: str, limit: int = 500) -> dict:
    """The most recent ingested transactions for ONE source, with money-in/out
    totals. Powers the per-source input-validation view. Empty (not an error) when
    the source has no persisted run yet."""
    run = load_latest_parsed_for(source_key)
    if run is None:
        return {"source_key": source_key, "count": 0, "money_in": 0, "money_out": 0, "transactions": []}
    txns = transactions_from_run(run)
    return {"source_key": run.get("source_key"), "month": run.get("month"),
            "parsed_at": run.get("parsed_at"), "run_path": run.get("run_path"),
            **_summary(txns), "transactions": _rows(txns, limit)}


def pending_approvals() -> list[dict]:
    """Sources whose parser is built but not yet activated — awaiting the operator's yes."""
    services = _load_services()
    return [{"key": s.key, "label": s.label} for s in source_status.pending_approvals(services)]


def llm_status() -> dict:
    """Which LLM provider the harness is configured to use."""
    cfg = llm_provider.current_config() or {}
    return {"configured": llm_provider.is_configured(),
            "provider": cfg.get("provider"), "model": cfg.get("model"), "base_url": cfg.get("base_url")}


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

    steps: list[dict] = []

    scrape_started = time.perf_counter()
    try:
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
        raise ToolError({"message": str(exc), "steps": steps}) from exc

    result = {"source_key": source_key, **_summary(txns), "transactions": _rows(txns, limit)}

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


def start_login_recovery(source_key: str) -> dict:
    """Open a visible persistent browser for manual re-login of a portal source."""
    services = _load_services()
    service = next((s for s in services if s.key == source_key), None)
    if service is None:
        raise ToolError(f"Unknown source '{source_key}'.")
    if not service.login_url:
        raise ToolError(f"Source '{source_key}' has no login_url configured.")

    existing = _LOGIN_RECOVERY_PROCS.get(source_key)
    if existing and existing.poll() is None:
        meta = _LOGIN_RECOVERY_META.get(source_key, {})
        return {
            "source_key": source_key,
            "status": "running",
            "pid": existing.pid,
            "login_url": meta.get("login_url", service.login_url),
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
    }
    return {
        "source_key": source_key,
        "status": "running",
        "pid": proc.pid,
        "login_url": service.login_url,
        "log_path": str(log_path),
        "steps": [
            {"key": "launch_browser", "label": "Launch recovery browser", "status": "success"},
            {"key": "user_login", "label": "Log in, then CLOSE the browser window to save the session", "status": "in-progress"},
        ],
    }


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
        return {
            "source_key": source_key,
            "status": "running",
            "pid": proc.pid,
            "login_url": meta.get("login_url"),
            "log_path": meta.get("log_path"),
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


def activate_parser(source_key: str) -> dict:
    """Approve a built parser — activate it so the source uses it automatically."""
    if not source_status.parser_built(source_key):
        raise ToolError(f"No parser built for '{source_key}' to activate.")
    ServiceManifest().update(source_key, parser=source_key, status="implemented")
    return {"source_key": source_key, "status": "implemented"}


# The tools the MCP server registers, in one place.
READ_TOOLS = [list_sources, latest_transactions, source_transactions, pending_approvals, llm_status, status]
ACTION_TOOLS = [
    ingest_document,
    run_scraper,
    fetch_source,
    activate_parser,
    start_login_recovery,
    login_recovery_status,
]
ALL_TOOLS = READ_TOOLS + ACTION_TOOLS
