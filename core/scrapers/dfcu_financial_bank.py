"""Scraper for DFCU Financial Bank.

Pulls transaction history from the DFCU Financial online banking API endpoint
``/mobilews/accountHistory/{accountId}``.  The flow is:

1. Open a persistent browser session (cookies survive across runs).
2. Sign in via the Q2 login helper (DFCU Financial is a Q2-powered bank).
3. GET the account history API endpoint with pagination, carrying the Q2 CSRF
   headers that prevent a 403.
4. Parse the JSON response into ``Transaction`` objects, filtering by date.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

METHOD = "api"  # Calls the data endpoint directly (preferred)

from datetime import date, datetime, timedelta

from core import progress, reconcile, settings
from core.models import Transaction
from core.observability import get_logger
from core.scrapers.base import ScrapeError

log = get_logger("core.scrapers.dfcu_financial_bank")

BASE_URL = "https://online.dfcufinancial.com/dfcufinancialonline"
# Where you SIGN IN, which for DFCU is not where the API is. The banking app at
# BASE_URL renders no sign-in form at all — routed to `#/login` it serves a page
# with zero inputs in any frame — because the credentials widget lives on the
# marketing site and relays into the app. Verified against the live page:
# `#loginid`, `#password`, `#rememberme`, `button.btn-login`.
LOGIN_URL = "https://www.dfcufinancial.com/"
SERVICE_KEY = "dfcu_financial_bank"

# ── configurable settings (dropdown values from the portal UI) ───────────
SETTINGS = [
    {
        "key": "lookback_days",
        "label": "Lookback days",
        "type": "number",
        "default": 30,
        "min": 1,
        "max": 365,
        "help": "Number of days before today to include.",
    },
    {
        "key": "account_id",
        "label": "Account ID",
        "type": "text",
        "default": "1730767",
        "help": (
            "The account ID to pull transactions for. Found in the URL after "
            "/account/ (e.g. https://online.dfcufinancial.com/...#/account/1730767)."
        ),
    },
]

# ── extraction (pure, testable) ──────────────────────────────────────


def _extract(raw: dict, start_date: date | None = None) -> list[Transaction]:
    """Turn the raw API response into Transactions.

    The raw response has the shape::

        {
            "data": {
                "transactions": [ { txn fields... }, ... ]
            }
        }

    Each transaction has the following source columns (all preserved verbatim
    in ``Transaction.fields``):

    - transactionType: str
    - transactionId: str
    - accountId: int
    - postedDate: ISO 8601 datetime string
    - amount: str (signed, negative for debits)
    - checkNumber: str
    - description: str
    - hostTranNumber: str
    - isDebit: bool
    - imageNumber: str
    - showImage: bool
    - tranCode: str
    - runningBalance: str
    - extendedDescription: list
    - hasTransactionDetails: bool
    - showDetail: bool
    - statementDescription: str
    - hostTransactionDataItems: list
    - disputeStatus: str | None
    - memo: str | None
    - hostPostDate: ISO 8601 datetime string

    If ``start_date`` is given, only transactions on or after that date are
    returned (the API does not support server-side date filtering).
    """
    transactions: list[Transaction] = []

    txn_list = raw.get("data", {}).get("transactions", [])

    for txn in txn_list:
        # ── date ──
        date_str = txn.get("postedDate", "")
        try:
            # Parse ISO 8601 datetime string (e.g. "2026-07-29T00:00:40.000-04:00")
            txn_date = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue  # skip rows with unparseable dates

        # Client-side date filter (API has no server-side date param)
        if start_date is not None and txn_date.date() < start_date:
            continue

        # ── amount (already signed: negative for debits, positive for credits) ──
        amount_str = txn.get("amount", "0")
        try:
            amount = float(amount_str)
        except (ValueError, TypeError):
            continue

        # ── description ──
        description = txn.get("description", "") or ""

        # ── fields (verbatim source columns — ALL of them) ──
        def _str_val(val, default: str = "") -> str:
            """Convert a value to string, returning default for None."""
            if val is None:
                return default
            return str(val)

        fields: dict[str, str] = {
            "transactionType": _str_val(txn.get("transactionType", "")),
            "transactionId": _str_val(txn.get("transactionId", "")),
            "accountId": _str_val(txn.get("accountId", "")),
            "postedDate": _str_val(txn.get("postedDate", "")),
            "amount": _str_val(txn.get("amount", "")),
            "checkNumber": _str_val(txn.get("checkNumber", "")),
            "description": description,
            "hostTranNumber": _str_val(txn.get("hostTranNumber", "")),
            "isDebit": _str_val(txn.get("isDebit", "")),
            "imageNumber": _str_val(txn.get("imageNumber", "")),
            "showImage": _str_val(txn.get("showImage", "")),
            "tranCode": _str_val(txn.get("tranCode", "")),
            "runningBalance": _str_val(txn.get("runningBalance", "")),
            "extendedDescription": _str_val(txn.get("extendedDescription", [])),
            "hasTransactionDetails": _str_val(
                txn.get("hasTransactionDetails", "")
            ),
            "showDetail": _str_val(txn.get("showDetail", "")),
            "statementDescription": _str_val(
                txn.get("statementDescription", "") or ""
            ),
            "hostTransactionDataItems": _str_val(
                txn.get("hostTransactionDataItems", [])
            ),
            "disputeStatus": _str_val(txn.get("disputeStatus", "")),
            "memo": txn.get("memo") if txn.get("memo") is not None else "",
            "hostPostDate": _str_val(txn.get("hostPostDate", "")),
        }

        transactions.append(
            Transaction(
                source_key=SERVICE_KEY,
                date=txn_date,
                amount=amount,
                description=description,
                fields=fields,
                source_uri=(
                    f"{BASE_URL}/uux.aspx#/account/{txn.get('accountId', '')}"
                ),
            )
        )

    # ── reconciliation: verify running balances are internally consistent ──
    if len(transactions) >= 2:
        # Sort by date ascending so runningBalance is in chronological order
        sorted_txns = sorted(transactions, key=lambda t: t.date)

        # Check that consecutive running balances differ by the transaction amount
        for i in range(1, len(sorted_txns)):
            prev_balance = float(sorted_txns[i - 1].fields["runningBalance"])
            curr_balance = float(sorted_txns[i].fields["runningBalance"])
            expected_diff = sorted_txns[i].amount
            actual_diff = curr_balance - prev_balance
            reconcile.record(
                f"running_balance_{sorted_txns[i].date.isoformat()}",
                expected=expected_diff,
                actual=actual_diff,
            )

    return transactions


# ── public entry point ───────────────────────────────────────────────


def retrieve() -> list[Transaction]:
    """Pull transaction history from DFCU Financial Bank."""
    from core.tools.browser_session import launch
    from core.tools.q2_online_banking import api_headers, login

    # ── read configurable settings ──────────────────────────────────
    opts = settings.values_for(SERVICE_KEY)
    lookback_days: int = opts.get("lookback_days", 30)
    account_id: str = opts.get("account_id", "1730767")

    # Compute date range from settings
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    with launch(SERVICE_KEY) as page:
        # ── sign in (Q2-powered portal) ───────────────────────────────
        progress.step("sign_in", "Sign in to DFCU Financial")
        login(page, LOGIN_URL, SERVICE_KEY, api_base=BASE_URL)
        progress.done("sign_in")  # fixed: removed status="success" kwarg

        # ── fetch transactions (API call) ─────────────────────────────
        progress.step(
            "fetch_transactions",
            f"Fetch transactions for account {account_id}",
        )

        # Build the API URL with pagination
        all_transactions: list[dict] = []
        page_number = 1
        page_size = 100

        while True:
            api_url = (
                f"{BASE_URL}/mobilews/accountHistory/{account_id}"
                f"?page[number]={page_number}"
                f"&page[size]={page_size}"
                f"&sort=postedDate%1Fd"
            )

            # Q2 requires the q2token cookie echoed back as a header, plus
            # x-requested-with.  Without these the gateway returns 403.
            q2_headers = api_headers(page, referer=BASE_URL)

            try:
                resp = page.request.get(api_url, headers=q2_headers, timeout=30_000)
                if resp.status == 401:
                    raise ScrapeError(
                        log.failure(
                            operation="fetch_transactions",
                            code="NOT_LOGGED_IN",
                            message=(
                                "Not logged in to DFCU Financial. Run bootstrap_login first."
                            ),
                            remediation=(
                                "Run: python -m core.tools.browser_session bootstrap_login "
                                "dfcu_financial_bank https://online.dfcufinancial.com/dfcufinancialonline"
                            ),
                            context={"service_key": SERVICE_KEY},
                        )
                    )
                if resp.status != 200:
                    raise ScrapeError(
                        log.failure(
                            operation="fetch_transactions",
                            code="HTTP_ERROR",
                            message=f"GET {api_url} returned {resp.status}.",
                            remediation="Check session validity and network connectivity.",
                            context={"url": api_url, "status_code": resp.status},
                        )
                    )
                data = resp.json()
                txns = data.get("data", {}).get("transactions", [])
                all_transactions.extend(txns)

                # Check if there are more pages
                if len(txns) < page_size:
                    break  # No more pages
                page_number += 1
            except ScrapeError:
                raise
            except Exception as exc:
                raise ScrapeError(
                    log.failure(
                        operation="fetch_transactions",
                        code="FETCH_FAILED",
                        message=f"Could not fetch transactions: {exc}",
                        remediation="Check session validity and network connectivity.",
                        context={"service_key": SERVICE_KEY},
                        exc=exc,
                    )
                ) from exc

        progress.done("fetch_transactions", details={"rows": len(all_transactions)})

        # ── extract transactions ──────────────────────────────────────
        progress.step("extract", "Parse rows into transactions")
        raw_response = {"data": {"transactions": all_transactions}}
        transactions = _extract(raw_response, start_date=start_date)
        progress.done("extract", details={"transactions": len(transactions)})

        # ── the anchored half of reconciliation ───────────────────────
        _reconcile_against_account_balance(page, account_id, all_transactions)

        return transactions


def _account_balance(page, account_id: str) -> float | None:
    """The account's CURRENT balance, from the portal's own accounts list.

    `balance2` is the field the portal labels "Current Balance" (`balance1` is
    "Available", which includes pending activity that has no posted row yet, so
    it would not agree with transaction history even on a perfect pull).

    Returns None rather than raising: a scrape that pulled its rows should not be
    thrown away because a second, purely corroborating call failed.
    """
    # Imported here, as retrieve() does: q2_online_banking pulls in playwright,
    # which must not be a cost of importing this module (the registry imports
    # every scraper, and the tests import _extract).
    from core.tools.q2_online_banking import api_headers

    url = f"{BASE_URL}/mobilews/accounts"
    try:
        resp = page.request.get(url, headers=api_headers(page, referer=BASE_URL), timeout=30_000)
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        rows = (resp.json() or {}).get("data") or []
    except Exception as exc:
        log.failure(
            operation="fetch_account_balance",
            code="BALANCE_UNAVAILABLE",
            message="Could not read the account's current balance, so the pull could "
                    "not be checked against it.",
            remediation="The transactions themselves are unaffected. If this persists, "
                        "the accounts endpoint may have moved.",
            context={"service_key": SERVICE_KEY, "url": url, "account_id": account_id},
            exc=exc, level="warning",
        )
        return None

    for row in rows:
        if str(row.get("accountId")) != str(account_id):
            continue
        try:
            return float((row.get("extended") or {}).get("balance2"))
        except (TypeError, ValueError):
            return None
    return None


def _reconcile_against_account_balance(page, account_id: str, raw_rows: list[dict]) -> None:
    """Check the newest row's running balance against the account's real balance.

    The running-balance CHAIN (in `_extract`) proves the rows are internally
    consistent, which catches a row dropped between two others. It cannot catch
    truncation: lose the newest rows, or stop paginating early, and what remains
    is a perfectly consistent chain of the wrong length. That is the likelier
    failure — a date window that clipped, a page that never arrived — and it is
    precisely the one an internal check is blind to.

    The account balance is an EXTERNAL number the portal states independently, so
    comparing it to where the chain ends closes that gap at the newest end.

    Uses the RAW rows, not the extracted ones: the newest row must be the newest
    the portal returned, before any `start_date` filtering of our own.
    """
    if not raw_rows:
        return
    balance = _account_balance(page, account_id)
    if balance is None:
        return

    newest = max(raw_rows, key=lambda r: str(r.get("postedDate") or ""))
    try:
        newest_running = float(newest.get("runningBalance"))
    except (TypeError, ValueError):
        return

    reconcile.record(
        "account balance vs newest running balance",
        expected=balance,
        actual=newest_running,
    )
