# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Live progress channel for long deterministic operations.

`core/observability.py` records what already happened, for diagnosis after the
fact. This is its complement: what is happening RIGHT NOW, so a front-end can
show it while the operation is still running.

The problem it solves: a portal scrape spends 30-60s launching a browser, signing
in, and calling two API endpoints, and the operator saw a single "Run scraper"
step the whole time — indistinguishable from a hang. Per the project's "nothing
hidden" rule, those phases have to be visible as they happen.

Deliberately NOT a callback parameter threaded through every signature: scrapers
are agent-authored and must stay `retrieve() -> list[Transaction]` (see
core/scrapers/base.py). Instead the caller opens a named channel, and any code
running under it reports into that channel:

    # interface layer (the reader)
    with progress.channel("epic"):
        transactions = get_scraper("epic")()
    ...
    progress.read("epic")      # another thread can poll this WHILE it runs

    # deep inside browser_session / a scraper (the writer)
    progress.step("login", "Signing in")
    progress.done("login")

Writers are no-ops when no channel is open, so nothing breaks outside a request
(tests, the agent, a plain script).

The active channel is per-thread (FastAPI runs sync endpoints in a threadpool),
while the recorded steps live in a module-level dict so a concurrent poll on
another thread can read them.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any

# channel name -> ordered steps. Kept small: one entry per named channel, reset
# each time that channel is opened.
_CHANNELS: dict[str, list[dict[str, Any]]] = {}
_LOCK = threading.Lock()
_ACTIVE = threading.local()

MAX_STEPS = 200  # a runaway loop must not grow this without bound


def _active() -> str | None:
    return getattr(_ACTIVE, "name", None)


@contextmanager
def channel(name: str):
    """Open `name` as this thread's progress channel, clearing anything from a
    previous run so a poller never sees two runs interleaved."""
    with _LOCK:
        _CHANNELS[name] = []
    previous = _active()
    _ACTIVE.name = name
    try:
        yield
    finally:
        _ACTIVE.name = previous


def step(key: str, label: str, status: str = "in-progress", **details: Any) -> None:
    """Report a phase starting (or update one). No-op with no channel open."""
    name = _active()
    if name is None:
        return
    with _LOCK:
        steps = _CHANNELS.setdefault(name, [])
        for existing in steps:
            if existing["key"] == key:
                existing.update(label=label, status=status, **details)
                return
        if len(steps) >= MAX_STEPS:
            return
        steps.append({
            "key": key,
            "label": label,
            "status": status,
            "started_at": time.time(),
            **details,
        })


def done(key: str, **details: Any) -> None:
    """Mark a phase finished, recording how long it took — the number that tells
    the operator whether 'slow' means working or wedged."""
    _finish(key, "success", **details)


def failed(key: str, error: str = "", **details: Any) -> None:
    _finish(key, "failed", error=error, **details)


def _finish(key: str, status: str, **details: Any) -> None:
    name = _active()
    if name is None:
        return
    with _LOCK:
        for existing in _CHANNELS.get(name, []):
            if existing["key"] == key:
                started = existing.get("started_at")
                if started:
                    existing["duration_ms"] = int((time.time() - started) * 1000)
                existing["status"] = status
                existing.update(details)
                return


def read(name: str) -> list[dict[str, Any]]:
    """Snapshot of a channel's steps — safe to call from another thread while the
    operation runs. Returns copies so a caller can't mutate live state."""
    with _LOCK:
        return [dict(s) for s in _CHANNELS.get(name, [])]


def clear(name: str) -> None:
    with _LOCK:
        _CHANNELS.pop(name, None)
