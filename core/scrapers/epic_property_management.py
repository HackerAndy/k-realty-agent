"""Scraper for Epic Property Management (Buildium-powered portal).

Pulls general-ledger transactions from the Buildium API endpoint
`/manager/api/generalLedger/transactions`.  The flow is:

1. Open a persistent browser session (cookies survive across runs).
2. Log in via the shared Buildium login helper.
3. Extract the XSRF token from cookies (required for all API calls).
4. Fetch the list of GL account IDs.
5. POST the GL account IDs + date range to the transactions endpoint.
6. Parse the JSON response into ``Transaction`` objects.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from playwright.sync_api import Page

from core import progress, reconcile
from core.models import Transaction
from core.observability import get_logger
from core.scrapers.base import ScrapeError

log = get_logger("core.scrapers.epic_property_management")

BASE_URL = "https://epicpropertymanagement.managebuilding.com"
SERVICE_KEY = "epic_property_management"

# ── helpers ──────────────────────────────────────────────────────────


def _extract_xsrf_token(page: Page) -> str:
    """Extract the XSRF-TOKEN cookie value from the browser session."""
    cookies = page.context.cookies()
    for cookie in cookies:
        if cookie["name"] == "XSRF-TOKEN":
            return cookie["value"]
    raise ScrapeError(
        log.failure(
            operation="extract_xsrf_token",
            code="XSRF_TOKEN_MISSING",
            message="XSRF-TOKEN cookie not found after login.",
            remediation="Re-run bootstrap_login to refresh the session.",
            context={"service_key": SERVICE_KEY},
        )
    )


def _fetch_json(page: Page, url: str, xsrf_token: str) -> Any:
    """GET a JSON endpoint and return the parsed body."""
    resp = page.request.get(
        url,
        headers={
            "X-XSRF-TOKEN": xsrf_token,
            "Content-Type": "application/json",
        },
    )
    if resp.status != 200:
        raise ScrapeError(
            log.failure(
                operation="fetch_json",
                code="HTTP_ERROR",
                message=f"GET {url} returned {resp.status}.",
                remediation="Check credentials / session validity.",
                context={"url": url, "status": resp.status},
            )
        )
    return resp.json()


def _post_json(page: Page, url: str, body: dict, xsrf_token: str) -> Any:
    """POST JSON to an endpoint and return the parsed response body."""
    http_resp = page.request.post(
        url,
        headers={
            "Content-Type": "application/json",
            "X-XSRF-TOKEN": xsrf_token,
        },
        data=json.dumps(body),
    )
    if http_resp.status != 200:
        raise ScrapeError(
            log.failure(
                operation="post_json",
                code="HTTP_ERROR",
                message=f"POST {url} returned {http_resp.status}.",
                remediation="Check credentials / session validity.",
                context={"url": url, "status": http_resp.status},
            )
        )
    return http_resp.json()


def _get_gl_account_ids(page: Page, xsrf_token: str) -> list[str]:
    """Fetch all GL account IDs from the Buildium API."""
    url = (
        f"{BASE_URL}/manager/api/glAccounts"
        "?types=5&types=4&types=3&types=2&types=1"
        "&excludeBankAccounts=true&excludeCreditCardAccounts=true"
    )
    accounts = _fetch_json(page, url, xsrf_token)
    return [str(a["Id"]) for a in accounts]


# ── extraction (pure, testable) ──────────────────────────────────────


def _extract(raw: list[dict]) -> list[Transaction]:
    """Turn the raw API response (list of account wrappers) into Transactions.

    Each account wrapper has the shape::

        {
            "Id": ...,
            "Name": "Account Name",
            "FullName": "...",
            "BeginningBalance": ...,
            "Total": ...,
            "Transactions": [ { txn fields... }, ... ]
        }

    We flatten across accounts, skipping accounts with no transactions.
    For every account that has a ``Total`` control total, we record a
    reconciliation check (no-op when no reconcile channel is open).
    """
    transactions: list[Transaction] = []

    for acct in raw:
        acct_name = acct.get("Name", "")
        acct_total = acct.get("Total")  # may be None for some accounts

        acct_amounts: list[float] = []

        for txn in acct.get("Transactions", []):
            # ── date ──
            date_str = txn.get("Date", "")
            try:
                txn_date = datetime.strptime(date_str, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue  # skip rows with unparseable dates

            # ── amount ──
            amount = txn.get("Amount", 0)
            try:
                amount = float(amount)
            except (ValueError, TypeError):
                continue

            # ── description ──
            description = txn.get("Description", "") or ""

            # ── fields (verbatim source columns) ──
            fields: dict[str, str] = {
                "Id": str(txn.get("Id", "")),
                "Date": str(txn.get("Date", "")),
                "PropertyOrCompany": str(txn.get("PropertyOrCompany", "") or ""),
                "Name": str(txn.get("Name", "") or ""),
                "Description": str(txn.get("Description", "") or ""),
                "Amount": str(txn.get("Amount", "")),
                "Balance": str(txn.get("Balance", "")),
                "JournalCode": str(txn.get("JournalCode", "")),
                "PrimaryPayeeName": str(txn.get("PrimaryPayeeName", "") or ""),
                "UnitNumber": str(txn.get("UnitNumber", "") or ""),
                "AccountName": acct_name,
            }

            transactions.append(
                Transaction(
                    source_key=SERVICE_KEY,
                    date=txn_date,
                    amount=amount,
                    description=description,
                    fields=fields,
                    source_uri=f"{BASE_URL}/manager/app/accounting/generalLedger",
                )
            )

            acct_amounts.append(amount)

        # ── reconciliation: compare extracted sum against source's Total ──
        if acct_total is not None and acct_amounts:
            reconcile.record(
                acct_name,
                expected=float(acct_total),
                actual=sum(acct_amounts),
            )

    return transactions


# ── public entry point ───────────────────────────────────────────────


def retrieve() -> list[Transaction]:
    """Pull general-ledger transactions from Epic Property Management."""
    from core.tools.browser_session import launch
    from core.tools.buildium_owner_portal import login, BuildiumLoginError

    # Compute a 30-day lookback window (matching the demo's pattern)
    end_date = date.today()
    start_date = end_date - timedelta(days=30)

    with launch(SERVICE_KEY) as page:
        # ── login ──
        try:
            login(page, BASE_URL, SERVICE_KEY)
        except BuildiumLoginError as exc:
            raise ScrapeError(
                log.failure(
                    operation="login",
                    code="LOGIN_FAILED",
                    message=str(exc),
                    remediation="Re-run bootstrap_login to refresh the session.",
                    context={"service_key": SERVICE_KEY},
                    exc=exc,
                )
            ) from exc

        # ── extract XSRF token (required for all API calls) ──
        progress.step("xsrf_token", "Extract XSRF token from cookies")
        try:
            xsrf_token = _extract_xsrf_token(page)
            progress.done("xsrf_token")
        except ScrapeError:
            raise
        except Exception as exc:
            raise ScrapeError(
                log.failure(
                    operation="extract_xsrf_token",
                    code="XSRF_TOKEN_FAILED",
                    message="Could not extract XSRF token from cookies.",
                    remediation="Re-run bootstrap_login to refresh the session.",
                    context={"service_key": SERVICE_KEY},
                    exc=exc,
                )
            ) from exc

        # ── fetch GL account IDs ──
        progress.step("gl_accounts", "Fetch the chart of accounts")
        try:
            gl_account_ids = _get_gl_account_ids(page, xsrf_token)
            progress.done("gl_accounts", details={"accounts": len(gl_account_ids)})
        except ScrapeError:
            raise
        except Exception as exc:
            raise ScrapeError(
                log.failure(
                    operation="fetch_gl_accounts",
                    code="GL_ACCOUNTS_FAILED",
                    message="Could not fetch GL account IDs.",
                    remediation="Check session validity and network connectivity.",
                    context={"service_key": SERVICE_KEY},
                    exc=exc,
                )
            ) from exc

        # ── fetch transactions ──
        body = {
            "PropertySelectionEntityId": None,
            "PropertySelectionType": "AllProperties",
            "StartDate": start_date.isoformat(),
            "EndDate": end_date.isoformat(),
            "AccountingBasis": 1,
            "GlAccountIds": gl_account_ids,
            "IncludeFundType": True,
            "SelectedFundType": None,
            "UnitSelectionType": 0,
            "UnitIds": None,
        }

        progress.step(
            "gl_transactions",
            f"Fetch general-ledger transactions ({start_date} to {end_date})",
        )
        try:
            raw = _post_json(
                page, f"{BASE_URL}/manager/api/generalLedger/transactions", body, xsrf_token
            )
            progress.done("gl_transactions", details={"rows": len(raw) if raw else 0})
        except ScrapeError:
            raise
        except Exception as exc:
            raise ScrapeError(
                log.failure(
                    operation="fetch_transactions",
                    code="TRANSACTIONS_FAILED",
                    message="Could not fetch general ledger transactions.",
                    remediation="Check session validity and network connectivity.",
                    context={"service_key": SERVICE_KEY},
                    exc=exc,
                )
            ) from exc

        progress.step("extract", "Parse rows into transactions")
        transactions = _extract(raw)
        progress.done("extract", details={"transactions": len(transactions)})
        return transactions
