# Template candidate: client-specific (tier 3) — K-Realty's own account and
# portal content. Not a promotion candidate. (The recon→scrape *pattern* and the
# portal_scrape fetch provider it feeds are the promotable parts — see
# agent-harness-template/docs/promotion-log.md.)
"""K-Realty's Epic Property Management portal — daily data retrieval by scrape.

Epic (a Buildium-powered owner portal) is a SECOND way to get Epic's data,
alongside the monthly Owner Statement PDF that arrives by email. Where the PDF
is monthly, the portal can be read on a daily cadence.

In the harness's fetch/parse model, a portal scrape is a FETCH method (like the
Gmail fetch), not a parser. Login is handled by core.tools.buildium_owner_portal
and is verified end-to-end against the real login form. Everything PAST login —
which page holds the financial data, whether it's an on-screen table or a
downloadable export — is NOT knowable without a live authenticated session, so
this module does recon first (explore()) and leaves retrieve() as an honest seam
to build against what recon actually finds. No invented selectors.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from core.models import Transaction
from core.observability import get_logger
from core.tools import buildium_owner_portal
from core.tools.browser_session import bootstrap_login, launch

log = get_logger("core.tools.epic_property_management")

SERVICE_KEY = "epic_property_management"
# Portal-scraped data gets its OWN source key so it's distinguishable from the
# emailed-PDF data (same columns, different retrieval method).
PORTAL_SOURCE_KEY = "epic_property_management_portal"
PORTAL_URL = "https://epicpropertymanagement.managebuilding.com/Manager"
RECON_PATH = Path("data/debug/epic_portal_structure.json")
SCRAPE_DEBUG_PATH = Path("data/debug/epic_scrape_debug.json")

# The detail-transactions table's columns (confirmed by recon 2026-07-16). Same
# as the PDF statement's detail table; Balance is a running total we don't keep
# as a field (it's derivable), matching the PDF parser's faithful field set.
DETAIL_HEADERS = ["Date", "Property", "Unit", "Account", "Name", "Memo", "Amount"]
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_AMOUNT_RE = re.compile(r"^\(?-?\$?[\d,]+\.\d{2}\)?$")


def bootstrap() -> None:
    """One-time setup: opens a VISIBLE browser to log in manually (including any
    2FA/verification prompt). Run this first, from a terminal:

        poetry run python -c "from core.tools.epic_property_management import bootstrap; bootstrap()"

    The session is saved to .browser_profiles/ (gitignored) so later headless
    runs reuse it without re-authenticating. Requires Epic credentials already
    stored via scripts/manage_secrets.py.
    """
    bootstrap_login(SERVICE_KEY, PORTAL_URL)


def explore(url: str = PORTAL_URL, out_path: Path = RECON_PATH) -> Path:
    """Reconnaissance, NOT extraction — the "go look before writing selectors" step.

    After bootstrap() has established a session, navigate to `url` and dump the
    authenticated page's REAL structure — links, tables (with their header
    cells), and download controls — to a JSON file, so retrieve() can be built
    against what the portal actually exposes instead of a guess.

    Read-only: it reads the DOM and clicks nothing. Run:

        poetry run python -c "from core.tools.epic_property_management import explore; print(explore())"
    """
    with launch(SERVICE_KEY, headless=True) as page:
        page.goto(url, wait_until="domcontentloaded")
        # Bounced back to the login form → the persisted session isn't valid.
        if page.get_by_label("Email address").count():
            raise RuntimeError(
                "Not logged in (hit the login form). Run bootstrap() first to "
                "establish a session, then explore() again."
            )
        structure = _dump_structure(page)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(structure, indent=2))
    return out_path


def explore_interactive(url: str = PORTAL_URL, out_path: Path = RECON_PATH) -> Path:
    """Headed recon you can WATCH — the "step me through the screens" flow.

    Opens a visible browser, you log in (handles 2FA) and navigate to the page
    you want scraped daily, then press Enter and the harness dumps THAT page's
    real structure (read-only — it clicks nothing, changes nothing). This is the
    version the TUI drives so you can see it working against the live portal.
    """
    with launch(SERVICE_KEY, headless=False) as page:
        page.goto(url, wait_until="domcontentloaded")
        input(
            "\nA browser window is open. Log in if needed, navigate to the financial "
            "page you want scraped daily, then press Enter here to capture it... "
        )
        structure = _dump_structure(page)
        structure["captured_url"] = page.url
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(structure, indent=2))
    return out_path


def _dump_structure(page) -> dict:
    """Read (never click) the current page's links, tables, and likely download
    controls. Pure reconnaissance so a human/agent can see the real layout."""
    links = page.eval_on_selector_all(
        "a",
        "els => els.map(e => ({text: (e.innerText||'').trim().slice(0,80), href: e.href}))"
        ".filter(l => l.text || l.href)",
    )
    tables = page.eval_on_selector_all(
        "table",
        "els => els.map(t => ({"
        "headers: Array.from(t.querySelectorAll('th')).map(h => (h.innerText||'').trim()).filter(Boolean),"
        "row_count: t.querySelectorAll('tr').length}))",
    )
    download_hint = ("download", "statement", "export", "report", "ledger", "financ")
    downloads = [
        l for l in links
        if any(k in (l["text"] or "").lower() for k in download_hint)
        or (l["href"] or "").lower().endswith((".pdf", ".csv", ".xlsx"))
    ]
    return {
        "title": page.title(),
        "url": page.url,
        "link_count": len(links),
        "links": links,
        "tables": tables,
        "download_candidates": downloads,
    }


def read_captured_url() -> str | None:
    """The report URL the last explore() captured — so retrieve() targets the
    exact page you confirmed, not a guess."""
    try:
        return json.loads(RECON_PATH.read_text()).get("captured_url")
    except Exception:
        return None


def _parse_amount(raw: str) -> float:
    """Signed amount from a rendered cell: handles $, commas, and (parentheses)
    for negatives — the running Balance column means Amounts are signed per row."""
    s = raw.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    value = float(s)
    return -value if negative else value


def _clean(raw: str) -> str:
    """Collapse whitespace and drop asterisk-wrapped manager markers (*EPM*),
    matching the PDF parser's property/cell normalization."""
    return re.sub(r"\s+", " ", re.sub(r"\*[^*]*\*", "", raw or "")).strip()


def _detail_table(page) -> dict | None:
    """Find the detail-transactions table by its header cells and return
    {cols, rows} — cols in DOM order, rows as lists of cell text. Read-only."""
    return page.evaluate(
        """(expected) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            for (const t of document.querySelectorAll('table')) {
                const ths = Array.from(t.querySelectorAll('th')).map(h => norm(h.innerText));
                if (!expected.every(e => ths.includes(e))) continue;
                const rows = [];
                for (const tr of t.querySelectorAll('tr')) {
                    const tds = Array.from(tr.querySelectorAll('td')).map(d => norm(d.innerText));
                    if (tds.length) rows.push(tds);
                }
                return {cols: ths, rows};
            }
            return null;
        }""",
        DETAIL_HEADERS,
    )


def _diagnostics(page, target: str) -> dict:
    """What did the headless load actually get? Captured on scrape failure so we
    can see whether it's a login bounce, an empty SPA shell, or a different view
    — read-only, clicks nothing."""
    tables = page.eval_on_selector_all(
        "table",
        "els => els.map(t => ({"
        "headers: Array.from(t.querySelectorAll('th')).map(h => (h.innerText||'').trim()).filter(Boolean),"
        "row_count: t.querySelectorAll('tr').length}))",
    )
    return {
        "requested_url": target,
        "final_url": page.url,
        "title": page.title(),
        "login_form": bool(page.get_by_label("Email address").count()),
        "table_count": len(tables),
        "table_summary": [f"{len(t['headers'])} header-cells / {t['row_count']} rows" for t in tables] or "none",
        "tables": tables,
    }


def _scrape_page(page, target: str) -> list[Transaction]:
    """Extract transactions from a live, rendered page. Shared by the interactive
    and headless entry points. On failure, dumps diagnostics so we can see what
    the page actually was."""
    table = _detail_table(page)
    if table is None:
        diagnostics = _diagnostics(page, target)
        SCRAPE_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCRAPE_DEBUG_PATH.write_text(json.dumps(diagnostics, indent=2))
        raise RuntimeError(log.failure(
            operation="portal_scrape",
            code="SCRAPE_TABLE_NOT_FOUND",
            message=(
                "Couldn't find the detail table on this page. What the page actually was: "
                f"title={diagnostics['title']!r}, final_url={diagnostics['final_url']}, "
                f"login_form={diagnostics['login_form']}, tables={diagnostics['table_summary']}."
            ),
            remediation=f"See the full dump at {SCRAPE_DEBUG_PATH}. If login_form is true the "
                        "session wasn't authenticated; if tables is empty the report hadn't rendered.",
            context={"source_key": SERVICE_KEY, "requested_url": target,
                     "final_url": diagnostics["final_url"], "login_form": diagnostics["login_form"],
                     "table_count": diagnostics["table_count"], "debug_dump": str(SCRAPE_DEBUG_PATH)},
        ))
    transactions = _rows_to_transactions(table["cols"], table["rows"], target)
    if not transactions:
        raise RuntimeError(log.failure(
            operation="portal_scrape",
            code="SCRAPE_ZERO_ROWS",
            message="Found the detail table but extracted zero transaction rows.",
            remediation="Re-capture with 'Explore the portal' so we can adjust the row mapping.",
            context={"source_key": SERVICE_KEY, "requested_url": target, "columns": table["cols"]},
        ))
    return transactions


def retrieve_interactive(url: str | None = None) -> list[Transaction]:
    """WORKS NOW: scrape the page you're looking at. Opens a visible browser; you
    log in and open the owner-statement report so its table is on screen, then
    press Enter and the harness scrapes THAT rendered page. Sidesteps the
    session/headless problems — the page is authenticated and fully rendered
    because you just loaded it. This is the path that proves the extraction is
    right before we tackle unattended automation."""
    target = url or read_captured_url() or PORTAL_URL
    with launch(SERVICE_KEY, headless=False) as page:
        page.goto(target, wait_until="domcontentloaded")
        input(
            "\nA browser window is open. Log in if needed, open the owner-statement report "
            "so its transactions table is visible, then press Enter here to scrape it... "
        )
        return _scrape_page(page, target)


def retrieve(url: str | None = None) -> list[Transaction]:
    """Unattended (headless) scrape — the daily-automation target. Currently
    blocked: Buildium bounces the headless session to the login page (the headed
    login doesn't carry over, and/or headless is refused). Kept as the goal;
    use retrieve_interactive() until the session problem is solved."""
    target = url or read_captured_url() or PORTAL_URL
    with launch(SERVICE_KEY, headless=True) as page:
        page.goto(target, wait_until="domcontentloaded")
        if page.get_by_label("Email address").count():
            raise RuntimeError(log.failure(
                operation="portal_scrape_headless",
                code="SCRAPE_NOT_AUTHENTICATED",
                message="Headless scrape bounced to Buildium's login page — not authenticated.",
                remediation="Unattended scraping isn't wired up yet; use the interactive scrape.",
                context={"source_key": SERVICE_KEY, "requested_url": target, "final_url": page.url},
            ))
        try:  # the report renders its table client-side — wait for it
            page.wait_for_selector("table th", timeout=20000)
        except Exception:
            pass
        return _scrape_page(page, target)


def _rows_to_transactions(cols: list[str], rows: list[list[str]], source_uri: str) -> list[Transaction]:
    """Pure mapping: table cells → faithful Transactions. Rows without a date in
    the Date column (section headers, subtotals, blanks) are skipped. Kept
    separate from the browser so it's unit-testable against real captured cells."""
    idx = {c: cols.index(c) for c in DETAIL_HEADERS if c in cols}
    if not all(c in idx for c in DETAIL_HEADERS):
        missing = [c for c in DETAIL_HEADERS if c not in idx]
        raise RuntimeError(f"Detail table is missing expected columns: {missing}. Found: {cols}")

    transactions: list[Transaction] = []
    for cells in rows:
        if len(cells) <= max(idx.values()):
            continue
        date_cell = cells[idx["Date"]].strip()
        amount_cell = cells[idx["Amount"]].strip()
        if not _DATE_RE.match(date_cell) or not _AMOUNT_RE.match(amount_cell):
            continue  # section header / subtotal / blank row — not a transaction
        account, name, memo = _clean(cells[idx["Account"]]), _clean(cells[idx["Name"]]), _clean(cells[idx["Memo"]])
        description = " | ".join(part for part in (account, name, memo) if part)
        transactions.append(
            Transaction(
                source_key=PORTAL_SOURCE_KEY,
                source_uri=source_uri,
                date=datetime.strptime(date_cell, "%m/%d/%Y"),
                amount=_parse_amount(amount_cell),
                description=description,
                fields={
                    "Date": date_cell,
                    "Property": _clean(cells[idx["Property"]]),
                    "Unit": _clean(cells[idx["Unit"]]),
                    "Account": account,
                    "Name": name,
                    "Memo": memo,
                    "Amount": f"{_parse_amount(amount_cell):.2f}",
                },
            )
        )
    return transactions
