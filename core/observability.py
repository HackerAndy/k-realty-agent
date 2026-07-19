# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Project logging standard for deterministic logic.

Every failure in deterministic code is recorded as ONE structured, actionable
record — enough for a human (or the harness's own agent) to diagnose and fix
WITHOUT re-running it. Records are appended as JSON lines to
data/logs/agent.jsonl (gitignored, local-only).

Record schema (every field present on a failure):

    ts           ISO-8601 UTC timestamp
    level        "error" | "warning" | "info"
    component    module reporting it              e.g. "core.ingest"
    operation    the action attempted             e.g. "ingest_source", "portal_scrape"
    code         stable UPPER_SNAKE slug to grep  e.g. "PARSER_FAILED", "SCRAPE_TABLE_NOT_FOUND"
    context      the inputs that matter (source_key, input path, url, counts).
                 NEVER secret values or dollar amounts — keys that look secret are
                 masked to <present>/<absent>; call sites must pass counts/paths,
                 not financial values.
    message      one human sentence: what happened
    cause        {type, message} of the underlying exception, or null
    remediation  one human sentence: what to DO about it (this is what makes a
                 log actionable — always answer "so what do I do now?")
    traceback    full traceback string (in the file record), or null

The one-call pattern at a failure site — catch, log, raise; never swallow:

    from core.observability import get_logger
    log = get_logger("core.ingest")
    ...
    except ParseError as exc:
        raise IngestError(log.failure(
            operation="ingest_source",
            code="PARSER_FAILED",
            message=f"Parser '{parser}' could not read {path.name}.",
            remediation="Inspect the dump in data/debug/, or retry with the AI fallback.",
            context={"source_key": source_key, "parser": parser, "input": str(path)},
            exc=exc,
        )) from exc

log.failure() writes the full record AND returns the human-facing string
("message — remediation"), so one call serves both audiences: the JSONL record
for debugging/the agent, and a clear, actionable string for the operator (raise
it in a domain error, or print it in the TUI). This keeps user-facing output
clean while the complete diagnostic lands on disk.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
import traceback as _tb
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_DIR = Path("data/logs")
LOG_FILE = LOG_DIR / "agent.jsonl"

# Context keys whose VALUES must never be logged — recorded as present/absent
# instead. Deliberately specific: a bare "key" substring would wrongly mask
# "source_key" (the most useful debugging field), so match genuine-secret terms.
_SECRET_HINTS = ("password", "secret", "token", "credential", "apikey", "api_key", "access_key")


def _safe(context: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (context or {}).items():
        if any(hint in k.lower() for hint in _SECRET_HINTS):
            out[k] = "<present>" if v else "<absent>"
        else:
            out[k] = v
    return out


def _write(record: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


class _Logger:
    def __init__(self, component: str):
        self.component = component

    def _record(self, level, operation, code, message, remediation, exc, context) -> dict:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "level": level,
            "component": self.component,
            "operation": operation,
            "code": code,
            "context": _safe(context),
            "message": message,
            "cause": {"type": type(exc).__name__, "message": str(exc)} if exc else None,
            "remediation": remediation,
            "traceback": (
                "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)) if exc else None
            ),
        }

    def failure(
        self,
        *,
        operation: str,
        code: str,
        message: str,
        remediation: str,
        exc: BaseException | None = None,
        context: dict[str, Any] | None = None,
        level: str = "error",
    ) -> str:
        """Log a structured failure record; return the actionable human string
        ('message — remediation') for the caller to raise or display."""
        _write(self._record(level, operation, code, message, remediation, exc, context))
        return f"{message} {remediation}".strip()

    def event(
        self,
        *,
        operation: str,
        code: str,
        message: str,
        context: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        """Log a non-failure event (a successful milestone worth an audit trail)."""
        _write(self._record(level, operation, code, message, None, None, context))


def get_logger(component: str) -> _Logger:
    return _Logger(component)


def read_recent(limit: int = 20, level: str | None = None) -> list[dict]:
    """Most recent records (optionally filtered by level), newest last — so the
    TUI or the harness's agent can surface 'what went wrong lately' on demand."""
    if not LOG_FILE.exists():
        return []
    records = []
    for line in LOG_FILE.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level is None or rec.get("level") == level:
            records.append(rec)
    return records[-limit:]


def format_record(rec: dict) -> str:
    """One-line human rendering of a record, for TUI display."""
    loc = f"{rec.get('component', '?')}.{rec.get('operation', '?')}"
    cause = rec.get("cause")
    tail = f" (cause: {cause['type']}: {cause['message']})" if cause else ""
    return f"[{rec.get('level', '?').upper()}] {loc} [{rec.get('code', '?')}]: {rec.get('message', '')}{tail}"
