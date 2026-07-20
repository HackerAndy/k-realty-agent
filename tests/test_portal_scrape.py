"""Deterministic parts of the Epic General Ledger scrape.

The live browser/session parts need a real login and can only be exercised by
the operator (via the TUI). These cover the pure row->Transaction mapping — the
risky bit — using rows shaped exactly like the real recon capture
(data/debug/epic_portal_structure.json, 2026-07-19): account-grouped, with
section headers, PRIOR BALANCE / Total rows, an unlabeled receipt column, and
parenthesized negatives.
"""

import pytest

from core.tools.epic_property_management import (
    PORTAL_SOURCE_KEY,
    _clean,
    _gl_rows_to_transactions,
    _parse_amount,
)

# Real-shaped GL rows in DOM order: an account section, prior/total, and txns.
GL_ROWS = [
    ["Capital One"],  # account section header
    ["PRIOR BALANCE", "($5,081.19)"],
    ["6/24/2026", "1029 E. Granet Ave. *EPM*", "Property level", "Lowes", "Odor Eliminator", "1", "($11.62)", "($5,092.81)"],
    ["Total Capital One", "($11.62)", "($5,092.81)"],
    ["Rent Income"],  # next account section header
    ["6/26/2026", "8095 Prospect Ave. *EPM*", "1", "Unit 1 - Kenneth Davis", "by Kenneth Davis", "", "$1,421.00", "$7,420.21"],
    ["7/8/2026", "1029 E. Granet Ave. *EPM*", "Lower", "", "Rent Deposit", "", "$2,000.00", "$9,374.46"],
    ["Total Rent Income", "$3,421.00", "$9,374.46"],
]


def test_parse_amount_signs():
    assert _parse_amount("($11.62)") == -11.62   # parentheses = negative
    assert _parse_amount("$1,421.00") == 1421.00
    assert _parse_amount("$2,000.00") == 2000.00


def test_clean_drops_epm_markers():
    assert _clean("1029 E. Granet Ave. *EPM*") == "1029 E. Granet Ave."


def test_gl_mapping_tags_account_and_filters_nondata_rows():
    txns = _gl_rows_to_transactions(GL_ROWS, "https://portal/generalLedger")

    # 3 transactions; section headers, PRIOR BALANCE and Total rows all skipped
    assert len(txns) == 3
    lowes, rent1, rent2 = txns

    # account comes from the section header above each transaction
    assert lowes.fields["Account"] == "Capital One"
    assert rent1.fields["Account"] == "Rent Income"
    assert rent2.fields["Account"] == "Rent Income"

    # signs, cleanup, and faithful columns
    assert lowes.amount == -11.62
    assert rent1.amount == 1421.00
    assert lowes.fields["Property"] == "1029 E. Granet Ave."  # *EPM* stripped
    assert lowes.fields["Name"] == "Lowes"
    assert lowes.fields["Description"] == "Odor Eliminator"
    assert lowes.source_key == PORTAL_SOURCE_KEY
    assert rent2.fields["Name"] == ""  # empty Name preserved, not shifted
    assert rent2.fields["Description"] == "Rent Deposit"


def test_gl_mapping_returns_empty_when_no_transactions():
    rows = [["Advertising"], ["PRIOR BALANCE", "$481.24"], ["Total Advertising", "$0.00", "$481.24"]]
    assert _gl_rows_to_transactions(rows, "uri") == []
