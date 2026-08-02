# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Reconciliation against a source's OWN control totals.

The strongest correctness signal a financial harness can get, and the only one
that answers the question that actually matters: *did we pull everything?*

A unit test says the parsing logic is unchanged. A successful run says the portal
answered. Neither notices that a date window clipped ten rows, that an account
was silently skipped, or that pagination stopped early — the run still "succeeds"
and the numbers are quietly wrong. Financial sources almost always publish their
own totals (a statement's balance line, an API's per-account `Total`), so the
harness can check its extraction against the source's own arithmetic.

Same shape as core/progress.py, and for the same reason: scrapers are
agent-authored and must stay `retrieve() -> list[Transaction]`, so there is
nowhere in the signature to return control totals. The caller opens a channel and
the extraction code records into it:

    # inside a scraper or parser
    reconcile.record("Rent Income", expected=1450.00, actual=sum_of_rows)

    # interface layer, after the run
    reconcile.summary("epic_property_management")
    # -> {"checked": 12, "ok": False, "discrepancies": [...]}

Recorders are no-ops when no channel is open, so extraction code still runs fine
from tests, the agent, or a script.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

# Money compared to the cent. Floats accumulate error across a few hundred rows,
# so this is a tolerance, not equality — but it is tight enough that a single
# missing transaction always trips it.
DEFAULT_TOLERANCE = 0.005

_CHANNELS: dict[str, list[dict[str, Any]]] = {}
_LOCK = threading.Lock()
_ACTIVE = threading.local()


def _active() -> str | None:
    return getattr(_ACTIVE, "name", None)


@contextmanager
def channel(name: str):
    """Open `name` as this thread's reconciliation channel, clearing anything a
    previous run recorded."""
    with _LOCK:
        _CHANNELS[name] = []
    previous = _active()
    _ACTIVE.name = name
    try:
        yield
    finally:
        _ACTIVE.name = previous


def record(label: str, expected: float, actual: float, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    """Record one control-total check, and say whether it balanced.

    The return value is the REAL comparison, always — with no active channel the
    recording is skipped but the arithmetic still happens. That is what makes

        if not reconcile.record("balance chain", expected=stated, actual=summed):
            raise ParseError(...)

    behave the same inside a run and inside a unit test, which is the only way a
    parser can both report its check to the operator and refuse to hand back
    numbers it knows are wrong. The docstring previously claimed it returns True
    without a channel; that would have silently disabled exactly that pattern
    everywhere except a live run.
    """
    try:
        expected_f, actual_f = float(expected), float(actual)
    except (TypeError, ValueError):
        expected_f, actual_f = float("nan"), float("nan")

    difference = actual_f - expected_f
    balanced = abs(difference) <= tolerance if difference == difference else False  # NaN => not balanced

    name = _active()
    if name is None:
        return balanced
    with _LOCK:
        _CHANNELS.setdefault(name, []).append({
            "label": label,
            "expected": round(expected_f, 2) if expected_f == expected_f else None,
            "actual": round(actual_f, 2) if actual_f == actual_f else None,
            "difference": round(difference, 2) if difference == difference else None,
            "balanced": balanced,
        })
    return balanced


def read(name: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(c) for c in _CHANNELS.get(name, [])]


def summary(name: str) -> dict[str, Any]:
    """Verdict for a run.

    `checked == 0` means the source published no control totals to check against
    — reported as `ok: None`, NOT as a pass. "We didn't look" and "we looked and
    it balanced" must never render the same way; that is exactly the false green
    this module exists to prevent.
    """
    checks = read(name)
    discrepancies = [c for c in checks if not c["balanced"]]
    return {
        "checked": len(checks),
        "ok": None if not checks else not discrepancies,
        "discrepancies": discrepancies,
        "checks": checks,
    }


def clear(name: str) -> None:
    with _LOCK:
        _CHANNELS.pop(name, None)
