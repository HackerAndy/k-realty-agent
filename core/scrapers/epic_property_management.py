"""Scraper for Epic Property Management (Buildium-powered portal).

Pulls general-ledger transactions from the Buildium API endpoint
`/manager/api/generalLedger/transactions`.  The flow is:

1. Open a persistent browser session (cookies survive across runs).
2. Log in via the shared Buildium login helper.
3. Extract the XSRF token from cookies (required for all API calls).
4. Fetch the list of GL account IDs.
5. Fetch the list of properties (for dynamic property selection).
6. POST the GL account IDs + date range to the transactions endpoint.
7. Parse the JSON response into ``Transaction`` objects.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from typing import Any

from playwright.sync_api import Page
from ruamel.yaml import YAML

from core import progress, reconcile, settings
from core.models import Transaction
from core.observability import get_logger
from core.scrapers.base import ScrapeError

log = get_logger("core.scrapers.epic_property_management")

BASE_URL = "https://epicpropertymanagement.managebuilding.com"
SERVICE_KEY = "epic_property_management"

# ── configurable settings (dropdown values from the portal UI) ───────────
# These mirror the dropdowns the operator selects on the General Ledger page.
# Defaults match what was demonstrated so the first run reproduces that result.

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
        "key": "accounting_basis",
        "label": "Accounting basis",
        "type": "choice",
        "default": "cash",
        "options": [
            {"value": "accrual", "label": "Accrual"},
            {"value": "cash", "label": "Cash"},
        ],
        "help": "Which accounting basis the report uses.",
    },
    {
        "key": "property_id",
        "label": "Property",
        "type": "choice",
        "default": "all",
        "options": [
            {"value": "all", "label": "All Properties"},
        ],
        "help": "Select a specific property or all properties. Options are populated from the portal.",
    },
]

# ── internal YAML helper for storing properties (not a declared setting) ─
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096


def _write_properties_to_settings(props: list[dict]) -> None:
    """Write property list to the settings YAML file directly.

    'properties' is not a declared setting key, so we can't use
    settings.save_for(). Instead we write directly to the YAML file.
    """
    from pathlib import Path

    SETTINGS_PATH = Path("core/policies/source_settings.yaml")
    data = settings._load_all()  # type: ignore[attr-defined]

    # Merge properties into the existing source entry
    entry = data.get(SERVICE_KEY, {})
    entry["properties"] = props
    data[SERVICE_KEY] = entry

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    buf = StringIO()
    _yaml.dump({"sources": data}, buf)
    SETTINGS_PATH.write_text(
        "# Operator-adjustable options per source — edited from the app, not by hand.\n"
        "# The available fields are declared by each source's own module (SETTINGS);\n"
        "# see core/settings.py. Values here override those declared defaults.\n"
        + buf.getvalue()
    )

    log.event(
        operation="properties_synced",
        code="PROPERTIES_SYNCED",
        message=f"Synced {len(props)} properties to settings.",
        context={"source_key": SERVICE_KEY},
    )


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


def _fetch_properties(page: Page, xsrf_token: str) -> list[dict]:
    """Fetch all properties from the Buildium API.

    Returns a list of dicts with 'id' and 'name' keys.
    """
    url = f"{BASE_URL}/manager/api/properties"
    props = _fetch_json(page, url, xsrf_token)
    return [{"id": str(p["Id"]), "name": p.get("Name", "")} for p in props]


def _sync_properties_to_settings(page: Page, xsrf_token: str) -> list[dict]:
    """Fetch properties from API and sync to settings.

    Reads stored properties, compares with current API properties,
    updates if changed, and returns the list.
    """
    props = _fetch_properties(page, xsrf_token)

    # Read stored properties from settings
    current = settings.values_for(SERVICE_KEY)
    stored = current.get("properties", [])

    # Check if properties have changed
    stored_ids = {p["id"] for p in stored}
    current_ids = {p["id"] for p in props}

    if stored_ids != current_ids:
        log.event(
            operation="properties_changed",
            code="PROPERTIES_CHANGED",
            message=f"Properties changed: {len(stored)} -> {len(props)}",
            context={
                "source_key": SERVICE_KEY,
                "old_count": len(stored),
                "new_count": len(props),
            },
        )
        # Update settings with new properties (write directly to YAML)
        try:
            _write_properties_to_settings(props)
        except Exception as exc:
            log.event(
                operation="properties_sync_failed",
                code="PROPERTIES_SYNC_FAILED",
                message=f"Could not sync properties to settings: {exc}",
                context={"source_key": SERVICE_KEY},
                level="warning",
            )

    return props


def _get_property_options() -> list[dict]:
    """Get current property options from settings (for the GUI).

    Reads stored properties and returns them as choice options.
    Called at module level so schema_for() picks up the updated list.
    """
    opts = settings.values_for(SERVICE_KEY)
    properties = opts.get("properties", [])
    options = [{"value": "all", "label": "All Properties"}]
    for prop in properties:
        options.append({"value": prop["id"], "label": prop["name"]})
    return options


# ── Dynamic SETTINGS update at module level ───────────────────────────
# Read stored properties and update the property_id options so the GUI
# shows actual property names. This runs when the module is imported,
# before schema_for() reads SETTINGS.

try:
    _current_options = _get_property_options()
    # Find the property_id setting and update its options in place
    for _field in SETTINGS:
        if _field["key"] == "property_id":
            _field["options"] = _current_options
            break
except Exception:
    # If anything goes wrong (no stored properties yet, etc.), keep defaults
    pass


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

    # ── read configurable settings (dropdown values) ────────────────
    opts = settings.values_for(SERVICE_KEY)
    lookback_days: int = opts.get("lookback_days", 30)
    accounting_basis_label: str = opts.get("accounting_basis", "cash")
    property_id: str = opts.get("property_id", "all")

    # Map human-readable labels to the API's expected values
    accounting_basis_map = {"accrual": 0, "cash": 1}

    accounting_basis = accounting_basis_map.get(accounting_basis_label, 1)

    # Compute date range from settings
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

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

        # ── fetch & sync properties (self-improvement) ────────────────
        progress.step("properties", "Fetch and sync property list")
        try:
            properties = _sync_properties_to_settings(page, xsrf_token)
            progress.done("properties", details={"count": len(properties)})
        except ScrapeError:
            raise
        except Exception as exc:
            log.event(
                operation="fetch_properties_failed",
                code="FETCH_PROPERTIES_FAILED",
                message=f"Could not fetch properties: {exc}",
                context={"service_key": SERVICE_KEY},
                level="warning",
            )
            properties = []

        # ── build property filter ─────────────────────────────────────
        if property_id == "all":
            # No property filter — include all properties in results
            property_filter = None
        else:
            # Find the matching property
            matching = [p for p in properties if p["id"] == property_id]
            if not matching:
                log.event(
                    operation="property_not_found",
                    code="PROPERTY_NOT_FOUND",
                    message=f"Property ID '{property_id}' not found in portal. Using all properties.",
                    context={"source_key": SERVICE_KEY, "property_id": property_id},
                    level="warning",
                )
                property_filter = None
            else:
                property_filter = matching[0]["name"]

        # ── fetch transactions (using configurable settings) ──────────
        body = {
            "PropertySelectionEntityId": None,
            "PropertySelectionType": "AllProperties",  # fixed: API protocol constant — always AllProperties for GL transactions
            "StartDate": start_date.isoformat(),
            "EndDate": end_date.isoformat(),
            "AccountingBasis": accounting_basis,
            "GlAccountIds": gl_account_ids,
            "IncludeFundType": True,  # fixed: API protocol flag — always True for GL transactions
            "SelectedFundType": None,
            "UnitSelectionType": 0,  # fixed: API protocol constant — 0 means no unit filter for GL transactions
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
