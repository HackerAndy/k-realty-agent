"""Deterministic parts of the Epic portal scrape.

The live browser/session parts need a real login and can only be exercised by
the operator (via the TUI preview). These cover the pure cell→Transaction
mapping — the risky bit — plus amount/cleanup helpers, using cells shaped like
what recon found (headers Date/Property/Unit/Account/Name/Memo/Amount/Balance).
"""

import pytest

from core.tools.epic_property_management import (
    PORTAL_SOURCE_KEY,
    _clean,
    _parse_amount,
    _rows_to_transactions,
)

COLS = ["Date", "Property", "Unit", "Account", "Name", "Memo", "Amount", "Balance"]


def _row(date, prop, unit, account, name, memo, amount, balance):
    return [date, prop, unit, account, name, memo, amount, balance]


def test_parse_amount_signs():
    assert _parse_amount("$1,234.56") == 1234.56
    assert _parse_amount("(1,234.56)") == -1234.56
    assert _parse_amount("-45.00") == -45.0
    assert _parse_amount("  $0.00 ") == 0.0


def test_clean_drops_epm_markers():
    assert _clean("8095 Prospect Ave.  *EPM*") == "8095 Prospect Ave."
    assert _clean("Management  Fees") == "Management Fees"


def test_rows_to_transactions_faithful_and_filters_nondata_rows():
    rows = [
        # a section/header-ish row with no date → skipped
        ["Additions to cash", "", "", "", "", "", "", ""],
        _row("05/01/2026", "1029 E. Granet Ave. *EPM*", "1", "Rent Income", "Jane Tenant", "May rent", "$1,500.00", "$1,500.00"),
        _row("05/03/2026", "8095 Prospect Ave. *EPM*", "2", "Management Fees", "Epic PM", "mgmt", "(150.00)", "$1,350.00"),
        # subtotal row with no valid date → skipped
        ["Total from Additions", "", "", "", "", "", "$1,350.00", ""],
    ]
    txns = _rows_to_transactions(COLS, rows, "https://portal/report")

    assert len(txns) == 2
    rent, fee = txns
    assert rent.amount == 1500.00
    assert fee.amount == -150.00  # parentheses → negative
    assert rent.source_key == PORTAL_SOURCE_KEY
    assert rent.fields["Property"] == "1029 E. Granet Ave."  # *EPM* stripped
    assert rent.description == "Rent Income | Jane Tenant | May rent"
    assert rent.fields["Amount"] == "1500.00"
    # Balance is shown by the portal but not kept as a field (matches PDF parser)
    assert "Balance" not in rent.fields


def test_rows_to_transactions_raises_on_missing_columns():
    with pytest.raises(RuntimeError, match="missing expected columns"):
        _rows_to_transactions(["Date", "Amount"], [], "uri")
