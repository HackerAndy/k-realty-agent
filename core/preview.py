# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""What the harness learned about a source, before it writes any code for it.

Adding a source asks the operator for one thing — the source itself, as a
document or a demonstration — and then has to report back what it made of it,
because nobody can name a source, or judge whether the harness understood it,
from a filename.

So this turns either artifact into the same small summary:

    where   what it came from (a file name, the URL they ended up on)
    how     how it would get it again
    rows    how many transactions it could see
    span    the dates they cover
    columns the source's OWN column names, verbatim

That summary is the entire content of the wizard's last step. It is deliberately
cheap and best-effort: a preview that fails is a smaller panel, never a blocked
flow, so every function here degrades to fewer facts rather than an exception.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from typing import Any


def summarise_transactions(txns: list[Any]) -> dict:
    """Rows, date span, and the source's own columns — from parsed transactions.

    Columns come from `fields`, which preserves each source's actual headings,
    so this reports what the source calls things rather than what we call them.
    """
    if not txns:
        return {"rows": 0, "span": "", "columns": []}

    columns: list[str] = []
    for t in txns:
        for key in (getattr(t, "fields", None) or {}):
            if key not in columns:
                columns.append(key)

    dates = sorted(t.date for t in txns if getattr(t, "date", None))
    span = ""
    if dates:
        span = (f"{dates[0]:%-d %b %Y}" if dates[0] == dates[-1]
                else f"{dates[0]:%-d %b} – {dates[-1]:%-d %b %Y}")
    return {"rows": len(txns), "span": span, "columns": columns}


def summarise_demo(demo: dict) -> dict:
    """The same summary, from a recorded browser demonstration.

    No model involved: the operator left their data on screen, so the final
    page's biggest table IS the answer, and the largest data response they
    triggered is how the harness would fetch it again.
    """
    page = demo.get("final_page") or {}
    tables = sorted(page.get("tables") or [], key=lambda t: t.get("row_count") or 0, reverse=True)
    biggest = tables[0] if tables else {}
    columns = [c for c in (biggest.get("th_headers") or biggest.get("header_row") or []) if c]

    requests = demo.get("candidate_requests") or []
    top = requests[0] if requests else None
    if top:
        how = f"Found the request behind it — {top.get('method', 'GET')} {_short_url(top.get('url', ''))}"
    elif columns:
        how = "Would replay the clicks you recorded"
    else:
        how = "Recorded your clicks, but saw no data table on the final page"

    return {
        "where": demo.get("final_url") or demo.get("start_url") or "",
        "how": how,
        # A header row isn't a transaction, so don't count it as one.
        "rows": max((biggest.get("row_count") or 0) - 1, 0),
        "span": "",
        "columns": columns,
        "title": page.get("title") or demo.get("title") or "",
        "requests": len(requests),
        "actions": len(demo.get("recorded_actions") or []),
    }


def _short_url(url: str, limit: int = 60) -> str:
    """Enough of the URL to recognise, not enough to wrap the panel."""
    trimmed = url.split("?")[0]
    for prefix in ("https://", "http://"):
        if trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix):]
            break
    return trimmed if len(trimmed) <= limit else trimmed[:limit] + "…"
