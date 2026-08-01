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
import os
import threading
import traceback as _tb
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Anchored to the repo, not the working directory — the same defect the credential
# store had: a process started elsewhere quietly wrote a SECOND log, so the
# records you needed were in a file nobody thought to look at.
REPO_ROOT = Path(__file__).resolve().parent.parent
# `AGENT_LOG_DIR` exists so the test suite can point somewhere else. It has to:
# the suite was appending to the operator's real diagnostic log — 788 records of
# fixtures named 'epic', 'portal', 'some_bank' — which is the same file
# `read_logs` hands the embedded agent. An agent debugging a live failure was
# reading invented ones.
LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR") or (REPO_ROOT / "data" / "logs"))
LOG_FILE = LOG_DIR / "agent.jsonl"

# Rotation. The log had reached 3.6 MB / 7,816 records with nothing to stop it,
# and every read parsed all of it. One rotation is kept: enough to span a
# session that crossed the boundary, and bounded.
MAX_LOG_BYTES = 2_000_000
KEEP_ROTATIONS = 1


def _log_file() -> Path:
    """Resolved at CALL time, so a test that redirects LOG_DIR is obeyed by code
    that imported this module earlier."""
    return LOG_DIR / "agent.jsonl"


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
            oldest = path.with_suffix(f".{KEEP_ROTATIONS}.jsonl")
            if oldest.exists():
                oldest.unlink()
            for n in range(KEEP_ROTATIONS, 1, -1):
                prev = path.with_suffix(f".{n - 1}.jsonl")
                if prev.exists():
                    prev.rename(path.with_suffix(f".{n}.jsonl"))
            path.rename(path.with_suffix(".1.jsonl"))
    except OSError:
        pass  # a log we can't rotate is not a reason to fail the operation

# Context keys whose VALUES must never be logged — recorded as present/absent
# instead. Deliberately specific: a bare "key" substring would wrongly mask
# "source_key" (the most useful debugging field), so match genuine-secret terms.
_SECRET_HINTS = ("password", "secret", "token", "credential", "apikey", "api_key", "access_key")


# Which source the current run belongs to, per thread (FastAPI runs sync
# endpoints in a threadpool). Set by progress.channel(), so every failure raised
# during a run is stamped with what it was running.
_SCOPE = threading.local()


def current_scope() -> str | None:
    return getattr(_SCOPE, "source_key", None)


@contextmanager
def scope(source_key: str):
    """Stamp every record written in this block with `source_key`.

    Without it a record only carries whatever the call site chose to pass, and
    call sites forget: the DFCU 403 that cost a day of debugging logged
    `{"url": ..., "status_code": 403}` and nothing to say WHICH source it was.
    That makes "show me what went wrong with this source" unanswerable, which is
    the first question anyone asks.
    """
    previous = current_scope()
    _SCOPE.source_key = source_key
    try:
        yield
    finally:
        _SCOPE.source_key = previous


def _safe(context: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (context or {}).items():
        if any(hint in k.lower() for hint in _SECRET_HINTS):
            out[k] = "<present>" if v else "<absent>"
        else:
            out[k] = v
    return out


def _write(record: dict) -> None:
    path = _log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


class _Logger:
    def __init__(self, component: str):
        self.component = component

    def _record(self, level, operation, code, message, remediation, exc, context) -> dict:
        safe = _safe(context)
        # The run says which source it is, so the call site doesn't have to
        # remember. An explicit source_key from the caller always wins.
        active = current_scope()
        if active and not safe.get("source_key"):
            safe["source_key"] = active
        return {
            "ts": datetime.now(UTC).isoformat(),
            "level": level,
            "component": self.component,
            "operation": operation,
            "code": code,
            "context": safe,
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


def _tail_one_file(path: Path, max_lines: int, chunk: int = 65_536) -> list[str]:
    """The last `max_lines` lines of one file, read backwards from its end.

    The old read parsed the WHOLE log to return fifteen records — 3.6 MB and
    7,816 JSON decodes per call, on a file that only grows. Seeking from the end
    makes the cost proportional to what's wanted, not to how long the harness has
    been running.
    """
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            buf = b""
            while end > 0 and buf.count(b"\n") <= max_lines:
                step = min(chunk, end)
                end -= step
                f.seek(end)
                buf = f.read(step) + buf
    except OSError:
        return []
    return buf.decode("utf-8", "replace").splitlines()[-max_lines:]


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """The last `max_lines` lines, reaching back through rotated files.

    Rotation without this is a history that vanishes: the moment the live file
    rolls over, "the last 15 errors" comes back nearly empty even though every
    one of them is on disk, one file over. Caught the hard way — a rotation fired
    mid-session and the whole log appeared to be gone.
    """
    lines = _tail_one_file(path, max_lines)
    n = 1
    while len(lines) < max_lines and n <= KEEP_ROTATIONS:
        older = path.with_suffix(f".{n}.jsonl")
        if not older.exists():
            break
        lines = _tail_one_file(older, max_lines - len(lines)) + lines
        n += 1
    return lines


def read_recent(limit: int = 20, level: str | None = None) -> list[dict]:
    """Most recent records (optionally filtered by level), newest last — so the
    GUI or the harness's agent can surface 'what went wrong lately' on demand."""
    # Over-read, because filtering by level can discard most of what's tailed.
    lines = _tail_lines(_log_file(), max_lines=max(limit * 20, 200))
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level is None or rec.get("level") == level:
            records.append(rec)
    return records[-limit:]


def _fingerprint(rec: dict) -> tuple:
    """What makes two records "the same problem again"."""
    return (rec.get("level"), rec.get("component"), rec.get("operation"),
            rec.get("code"), rec.get("message"))


def read_relevant(
    limit: int = 15,
    level: str | None = "error",
    since_minutes: int | None = None,
    source_key: str | None = None,
) -> tuple[list[dict], dict]:
    """The records worth reading, plus what was left out.

    Recency alone is a poor filter, and the log proves it: 525 of the records on
    disk are the same HOT_RELOAD_FAILED and 1,883 the same SETTINGS_SAVED. Ask
    for "the last 15 errors" during a real failure and you can get fifteen copies
    of one line, with the actual cause pushed off the end.

    So identical records collapse to one, carrying how many times it happened and
    when it last did — which is better evidence than fifteen copies, because a
    repeat count is itself a symptom. Returns (records, summary) where summary
    reports the collapsing, so nothing is hidden, only compressed.
    """
    cutoff = None
    if since_minutes:
        cutoff = (datetime.now(UTC) - timedelta(minutes=since_minutes)).isoformat()

    lines = _tail_lines(_log_file(), max_lines=max(limit * 40, 400))
    seen: dict[tuple, dict] = {}
    scanned = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level and level != "all" and rec.get("level") != level:
            continue
        if cutoff and (rec.get("ts") or "") < cutoff:
            continue
        if source_key:
            ctx = rec.get("context") or {}
            haystack = f"{ctx.get('source_key', '')} {ctx.get('service_key', '')}"
            if source_key not in haystack:
                continue
        scanned += 1
        key = _fingerprint(rec)
        if key in seen:
            seen[key]["_count"] += 1
            seen[key]["_last_ts"] = rec.get("ts")
            # Keep the NEWEST instance's detail: a cause and traceback from the
            # most recent occurrence beat one from an hour ago.
            seen[key].update({k: v for k, v in rec.items() if k not in ("_count", "_last_ts")})
        else:
            rec["_count"] = 1
            rec["_last_ts"] = rec.get("ts")
            seen[key] = rec

    records = list(seen.values())[-limit:]
    return records, {
        "matched": scanned,
        "distinct": len(seen),
        "shown": len(records),
        "collapsed": scanned - len(seen),
    }


def format_record(rec: dict) -> str:
    """One-line human rendering of a record, for TUI display."""
    loc = f"{rec.get('component', '?')}.{rec.get('operation', '?')}"
    cause = rec.get("cause")
    tail = f" (cause: {cause['type']}: {cause['message']})" if cause else ""
    return f"[{rec.get('level', '?').upper()}] {loc} [{rec.get('code', '?')}]: {rec.get('message', '')}{tail}"
