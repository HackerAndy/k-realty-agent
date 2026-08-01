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
from datetime import date, datetime, timedelta
from typing import Any

from playwright.sync_api import Page

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

# Buildium's PROPERTY_FILTER_STRING_VALUES, the two members this report uses.
# Protocol constants, not operator choices: they name a shape of request, and
# WHICH of them is sent is exactly what the property setting decides.
ALL_PROPERTIES = "AllProperties"
SINGLE_RENTAL = "Rental"

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
        # Only the portal knows which properties exist on this account, so the
        # list is filled in by a run (settings.record_options) rather than
        # declared here. Without this flag record_options refuses the field —
        # which is what drove an earlier version to write the settings file by
        # hand, under a key nothing declared and nothing rendered.
        "discovered": True,
        "help": "Select a specific property or all properties. Options are populated from the portal.",
    },
]


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


def _property_rows(payload: Any) -> list[dict] | None:
    """The list of property records inside whatever the endpoint returned.

    The endpoint answers 200 but is not in the recorded demonstration (the
    operator never opened the property dropdown), so its envelope is not known
    from evidence. It was assumed to be a bare list, and it is not: iterating
    the dict that actually came back yielded its KEYS, and `p["Id"]` on a string
    raised `string indices must be integers` on every run — swallowed as a
    warning, which is why the dropdown has been empty this whole time.

    So handle the two shapes an API can take — a bare list, or a list wrapped in
    an envelope — and return None for anything else rather than guessing at a
    third. The caller reports what it actually saw.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next(
            (
                value
                for value in payload.values()
                if isinstance(value, list)
                and value
                and all(isinstance(item, dict) for item in value)
            ),
            None,
        )
        if rows is None:
            return None
    else:
        return None
    return rows if all(isinstance(row, dict) for row in rows) else None


def _fetch_properties(page: Page, xsrf_token: str) -> list[dict]:
    """Fetch all properties from the Buildium API.

    Returns a list of dicts with 'id' and 'name' keys.
    """
    url = f"{BASE_URL}/manager/api/properties"
    payload = _fetch_json(page, url, xsrf_token)
    rows = _property_rows(payload)
    if rows is None:
        raise ScrapeError(
            log.failure(
                operation="fetch_properties",
                code="PROPERTIES_SHAPE_UNKNOWN",
                message=(
                    "The properties endpoint returned a shape this scraper cannot read."
                ),
                # Say what came back. The previous version reported only the
                # exception text, which named a Python error and not one fact
                # about the response — so seven identical failures taught nobody
                # what the endpoint actually sends.
                remediation=(
                    "Open the property dropdown during a fresh demonstration so the "
                    "response is recorded, then have the agent revise this parser."
                ),
                context={
                    "service_key": SERVICE_KEY,
                    "url": url,
                    "payload_type": type(payload).__name__,
                    "payload_keys": (
                        sorted(payload)[:20] if isinstance(payload, dict) else None
                    ),
                    "payload_sample": str(payload)[:500],
                },
            )
        )
    # Id/Name are Buildium's spelling throughout this module's other endpoints.
    return [
        {"id": str(row["Id"]), "name": str(row.get("Name") or "")}
        for row in rows
        if row.get("Id") is not None
    ]


def _property_selection(
    property_id: str, properties: list[dict]
) -> tuple[int | None, str]:
    """Turn the operator's chosen property into what the GL request expects.

    Buildium's own report form builds this pair, and its bundle is where these
    values come from rather than a guess::

        PropertySelectionEntityId: k(selectedProperty.filter)   // AllProperties -> null,
                                                                // otherwise parseInt(EntityId)
        PropertySelectionType:     selectedProperty.filter.Type // PROPERTY_FILTER_STRING_VALUES

    The version this replaces looked the property up, assigned its NAME to a
    local, and then sent `AllProperties` regardless — so the dropdown on the
    settings screen changed nothing at all, and the run silently returned every
    property's transactions no matter what was picked.
    """
    if property_id == "all":
        return None, ALL_PROPERTIES  # fixed: API enum — the no-filter selection

    match = next((p for p in properties if p["id"] == property_id), None)
    if match is None:
        # Falling back to every property is a real change to what the run
        # returns, so it is a warning the operator can find, not a silent one.
        log.event(
            operation="property_not_found",
            code="PROPERTY_NOT_FOUND",
            message=f"Property ID '{property_id}' not found in portal. Using all properties.",
            context={"source_key": SERVICE_KEY, "property_id": property_id},
            level="warning",
        )
        return None, ALL_PROPERTIES  # fixed: API enum — the no-filter selection

    try:
        entity_id = int(match["id"])
    except (TypeError, ValueError):
        log.event(
            operation="property_id_not_numeric",
            code="PROPERTY_ID_NOT_NUMERIC",
            message=f"Property ID '{match['id']}' is not numeric. Using all properties.",
            context={"source_key": SERVICE_KEY, "property_id": match["id"]},
            level="warning",
        )
        return None, ALL_PROPERTIES  # fixed: API enum — the no-filter selection

    return entity_id, SINGLE_RENTAL  # fixed: API enum — one rental property


def _sync_properties_to_settings(page: Page, xsrf_token: str) -> list[dict]:
    """Fetch the account's properties and publish them as this field's choices.

    `record_options` is the whole mechanism: it stores what the PORTAL offers,
    kept apart from what the operator chose, and `settings.schema_for()` merges
    the two when the GUI asks for the schema.

    The version this replaces did it by hand — it read `settings._load_all()`,
    stored the list under an undeclared `properties` key, and rewrote the file
    as `{"sources": ...}` only, which dropped the `discovered` section wholesale
    and so erased every OTHER source's recorded choices each time Epic ran.
    """
    props = _fetch_properties(page, xsrf_token)

    previous = {
        option["value"]
        for option in settings.recorded_options(SERVICE_KEY).get("property_id", [])
    }
    settings.record_options(
        SERVICE_KEY,
        "property_id",
        [{"value": prop["id"], "label": prop["name"]} for prop in props],
    )

    if previous != {prop["id"] for prop in props}:
        log.event(
            operation="properties_changed",
            code="PROPERTIES_CHANGED",
            message=f"Properties changed: {len(previous)} -> {len(props)}",
            context={
                "source_key": SERVICE_KEY,
                "old_count": len(previous),
                "new_count": len(props),
            },
        )

    log.event(
        operation="properties_synced",
        code="PROPERTIES_SYNCED",
        message=f"Synced {len(props)} properties to settings.",
        context={"source_key": SERVICE_KEY},
    )

    return props


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
        selection_entity_id, selection_type = _property_selection(property_id, properties)

        # ── fetch transactions (using configurable settings) ──────────
        body = {
            "PropertySelectionEntityId": selection_entity_id,
            "PropertySelectionType": selection_type,
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
