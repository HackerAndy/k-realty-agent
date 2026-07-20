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
from core.tools.credential_store import CredentialStoreError

log = get_logger("core.tools.epic_property_management")

SERVICE_KEY = "epic_property_management"
# Portal-scraped data gets its OWN source key so it's distinguishable from the
# emailed-PDF data (same columns, different retrieval method).
PORTAL_SOURCE_KEY = "epic_property_management_portal"
PORTAL_URL = "https://epicpropertymanagement.managebuilding.com/Manager"
# The scrape target: the General Ledger — directly URL-navigable (unlike the
# owner-statement report, which redirects when opened cold), more current than
# the monthly statement, and it carries per-row receipt links.
GENERAL_LEDGER_URL = "https://epicpropertymanagement.managebuilding.com/manager/app/accounting/generalLedger"
RECON_PATH = Path("data/debug/epic_portal_structure.json")
SCRAPE_DEBUG_PATH = Path("data/debug/epic_scrape_debug.json")

# The General Ledger table (confirmed by recon 2026-07-19). It has a header row
# of 7 <th> but data rows of 8 cells — an UNLABELED receipt column sits at index
# 5 (shows an attachment count / link, empty when none). Fixed positions:
GL_TH_MARKERS = ("DATE (CASH BASIS)", "BALANCE")  # identify the GL table by these headers
GL_IDX = {"Date": 0, "Property": 1, "Unit": 2, "Name": 3, "Description": 4,
          "Receipt": 5, "Amount": 6, "Balance": 7}
GL_MIN_CELLS = 8
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


def explore(url: str = GENERAL_LEDGER_URL, out_path: Path = RECON_PATH) -> Path:
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
        # Let the Angular SPA settle: either the login form or the data table.
        try:
            page.wait_for_selector("input[type='password'], table tr", timeout=25000)
        except Exception:
            pass
        # Bounced back to the login form → the persisted session isn't valid.
        if page.get_by_label("Email address").count():
            raise RuntimeError(
                "Not logged in (hit the login form). Run bootstrap() first to "
                "establish a session, then explore() again."
            )
        page.wait_for_timeout(2500)  # async rows load after the table shell
        structure = _dump_structure(page)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(structure, indent=2))
    return out_path


def explore_interactive(url: str = GENERAL_LEDGER_URL, out_path: Path = RECON_PATH) -> Path:
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
        """els => els.map(t => {
            const rows = Array.from(t.querySelectorAll('tr'));
            const cells = tr => Array.from(tr.querySelectorAll('th,td'))
                .map(c => (c.innerText||'').replace(/\\s+/g,' ').trim());
            return {
                th_headers: Array.from(t.querySelectorAll('th')).map(h => (h.innerText||'').trim()).filter(Boolean),
                header_row: rows.length ? cells(rows[0]) : [],
                sample_rows: rows.slice(1, 40).map(cells),
                row_count: rows.length,
                row_links: Array.from(t.querySelectorAll('a[href]'))
                    .map(a => ({text:(a.innerText||'').trim().slice(0,40), href:a.href})).slice(0, 8),
            };
        })""",
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


def _gl_table(page) -> list[list[str]] | None:
    """Find the General Ledger table by its header cells and return ALL rows in
    DOM order (each a list of cell text). Order matters — the account each
    transaction belongs to comes from the section-header row above it. Read-only."""
    return page.evaluate(
        """(markers) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            for (const t of document.querySelectorAll('table')) {
                const ths = Array.from(t.querySelectorAll('th')).map(h => norm(h.innerText));
                if (!markers.every(m => ths.includes(m))) continue;
                const rows = [];
                for (const tr of t.querySelectorAll('tr')) {
                    const cells = Array.from(tr.querySelectorAll('th,td')).map(c => norm(c.innerText));
                    if (cells.length) rows.push(cells);
                }
                return rows;
            }
            return null;
        }""",
        list(GL_TH_MARKERS),
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
    rows = _gl_table(page)
    if not rows:
        diagnostics = _diagnostics(page, target)
        SCRAPE_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCRAPE_DEBUG_PATH.write_text(json.dumps(diagnostics, indent=2))
        raise RuntimeError(log.failure(
            operation="portal_scrape",
            code="SCRAPE_TABLE_NOT_FOUND",
            message=(
                "Couldn't find the General Ledger table on this page. What the page actually was: "
                f"title={diagnostics['title']!r}, final_url={diagnostics['final_url']}, "
                f"login_form={diagnostics['login_form']}, tables={diagnostics['table_summary']}."
            ),
            remediation=f"See the full dump at {SCRAPE_DEBUG_PATH}. If login_form is true the session "
                        "wasn't authenticated; if tables is empty the ledger hadn't rendered (set the "
                        "filters and Search first).",
            context={"source_key": SERVICE_KEY, "requested_url": target,
                     "final_url": diagnostics["final_url"], "login_form": diagnostics["login_form"],
                     "table_count": diagnostics["table_count"], "debug_dump": str(SCRAPE_DEBUG_PATH)},
        ))
    transactions = _gl_rows_to_transactions(rows, target)
    if not transactions:
        raise RuntimeError(log.failure(
            operation="portal_scrape",
            code="SCRAPE_ZERO_ROWS",
            message="Found the General Ledger table but extracted zero transaction rows.",
            remediation="The ledger may be showing a period with no activity — set the date range and "
                        "Search, then scrape. Or re-capture with 'Explore the portal' so we can adjust.",
            context={"source_key": SERVICE_KEY, "requested_url": target, "table_rows": len(rows)},
        ))
    return transactions


def retrieve_interactive(url: str | None = None) -> list[Transaction]:
    """WORKS NOW: scrape the page you're looking at. Opens a visible browser; you
    log in and open the owner-statement report so its table is on screen, then
    press Enter and the harness scrapes THAT rendered page. Sidesteps the
    session/headless problems — the page is authenticated and fully rendered
    because you just loaded it. This is the path that proves the extraction is
    right before we tackle unattended automation."""
    target = url or GENERAL_LEDGER_URL
    with launch(SERVICE_KEY, headless=False) as page:
        page.goto(target, wait_until="domcontentloaded")
        input(
            "\nA browser window is open on the General Ledger. Log in if needed, set the date "
            "range/filters and click Search so the ledger is populated, then press Enter here "
            "to scrape it... "
        )
        return _scrape_page(page, target)


def _login(page) -> None:
    """Sign in with STORED Epic credentials, then record where the login landed —
    so an email-verification or challenge screen is visible in the log."""
    try:
        buildium_owner_portal.login(page, PORTAL_URL, SERVICE_KEY)
    except CredentialStoreError as exc:
        raise RuntimeError(log.failure(
            operation="portal_login",
            code="SCRAPE_NO_CREDENTIALS",
            message="No Epic credentials stored to log in with.",
            remediation="Store your Epic username/password first: Manage services & credentials "
                        "→ epic_property_management.",
            context={"source_key": SERVICE_KEY},
            exc=exc,
        )) from exc
    except buildium_owner_portal.BuildiumLoginError as exc:
        raise RuntimeError(log.failure(
            operation="portal_login",
            code="SCRAPE_LOGIN_INCOMPLETE",
            message=f"Automated login didn't complete — landed on {page.title()!r}. Likely an "
                    "email-verification step, a CAPTCHA, or a bot-check after the password.",
            remediation="If it's the 'check your email' step, we can automate that next (Gmail "
                        "reading already works). If it's a CAPTCHA/bot-block, headless login is out.",
            context={"source_key": SERVICE_KEY, "final_url": page.url, "title": page.title()},
            exc=exc,
        )) from exc
    # Record the post-login landing — reveals a verification/challenge page if one appeared.
    log.event(
        operation="portal_login",
        code="POST_LOGIN_STATE",
        message=f"After automated login, page is {page.title()!r}.",
        context={"source_key": SERVICE_KEY, "url": page.url,
                 "still_on_login": bool(page.get_by_label("Email address").count())},
    )


def retrieve(url: str | None = None, headless: bool = True) -> list[Transaction]:
    """Log in with stored credentials, then scrape the report — the unattended
    (no-human) path. Works headless (the daily-automation target) or headed
    (watch it happen / dodge headless bot-detection, e.g. via xvfb on a Pi).

    If a session already persists, the login step is skipped. If Buildium answers
    the password with email verification or a CAPTCHA, that surfaces in the log
    (SCRAPE_LOGIN_INCOMPLETE, or a post-login page still showing the login form)
    so we learn whether the automatable email step is what's in the way."""
    target = url or GENERAL_LEDGER_URL
    with launch(SERVICE_KEY, headless=headless) as page:
        page.goto(target, wait_until="domcontentloaded")
        # The Angular SPA may still be redirecting to login OR rendering content —
        # wait until one actually appears before deciding whether to log in. (The
        # old code checked too early, saw neither, and skipped login entirely.)
        try:
            page.wait_for_selector("input[type='password'], table th", timeout=20000)
        except Exception:
            pass
        if page.get_by_label("Email address").count():  # genuinely not logged in → sign in
            _login(page)
            page.goto(target, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("table th", timeout=20000)
            except Exception:
                pass
        return _scrape_page(page, target)


def _gl_rows_to_transactions(rows: list[list[str]], source_uri: str) -> list[Transaction]:
    """Pure mapping: General Ledger rows → faithful Transactions. The GL is grouped
    by account, so we walk rows IN ORDER and track the current account (from its
    section-header row) to stamp each transaction with the GL account it posts to.

    Row kinds:
      • transaction  → a date in cell 0 (8 cells: Date/Property/Unit/Name/Description/
                       Receipt/Amount/Balance). Emitted, tagged with current account.
      • section head → a single non-empty cell that's an account name. Sets the account.
      • PRIOR BALANCE / Total <account> / blank → skipped (don't change the account).

    Kept browser-free so it's unit-testable against real captured rows."""
    transactions: list[Transaction] = []
    current_account = ""
    for cells in rows:
        first = cells[0].strip() if cells else ""
        nonempty = [c for c in cells if c.strip()]

        if _DATE_RE.match(first):  # transaction row
            if len(cells) < GL_MIN_CELLS:
                continue
            amount_cell = cells[GL_IDX["Amount"]].strip()
            if not _AMOUNT_RE.match(amount_cell):
                continue
            name = _clean(cells[GL_IDX["Name"]])
            desc = _clean(cells[GL_IDX["Description"]])
            description = " | ".join(part for part in (current_account, name, desc) if part)
            transactions.append(
                Transaction(
                    source_key=PORTAL_SOURCE_KEY,
                    source_uri=source_uri,
                    date=datetime.strptime(first, "%m/%d/%Y"),
                    amount=_parse_amount(amount_cell),
                    description=description,
                    fields={
                        "Date": first,
                        "Account": current_account,  # the GL account (from the section header)
                        "Property": _clean(cells[GL_IDX["Property"]]),
                        "Unit": _clean(cells[GL_IDX["Unit"]]),
                        "Name": name,
                        "Description": desc,
                        "Amount": f"{_parse_amount(amount_cell):.2f}",
                    },
                )
            )
        elif first.upper().startswith("PRIOR BALANCE") or first.startswith("Total "):
            continue  # subtotal / carried balance — not a transaction, keep the account
        elif len(nonempty) == 1:
            current_account = first  # account section header
    return transactions
