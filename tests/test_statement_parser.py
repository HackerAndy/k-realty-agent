from pathlib import Path

from core.parsers.buildium_owner_statement import parse_statement

FIXTURE = Path(__file__).parent / "fixtures" / "sample_owner_statement.pdf"


def test_parses_columnar_fixture():
    txns = parse_statement(FIXTURE)
    assert len(txns) == 6

    # summary page (no detail header) skipped; balances/totals/footers skipped
    assert all("cash balance" not in t.description.lower() for t in txns)
    assert all("total from" not in t.description.lower() for t in txns)

    # additions positive, subtractions negative — and the subtraction sign
    # carries across the page break (page 3 has no section line)
    assert sum(t.amount for t in txns if t.amount > 0) == 1250.00
    assert sum(t.amount for t in txns if t.amount < 0) == -850.00
    mgmt = next(t for t in txns if "Management Fees" in t.description)
    assert mgmt.amount == -99.00  # on page 3, after the break

    # wrapped cells merge correctly
    assert any("Non-Refundable Pet Deposit" in t.description for t in txns)  # acct over 4 lines
    assert any("Owner Contribution" in t.description for t in txns)  # "Contributio"+"n"

    # FAITHFUL model: the statement's own columns live in fields, verbatim —
    # nothing invented. This source has Property/Unit because the statement does.
    rent = next(t for t in txns if "Rent Income" in t.description)
    assert set(rent.fields) >= {"Date", "Property", "Unit", "Account", "Name", "Memo", "Amount"}
    assert rent.fields["Unit"] == "1"

    oak = [t for t in txns if t.fields["Property"] == "456 Oak Ave."]  # "456 Oak Av"+"e." and *SPM* stripped
    assert len(oak) == 2

    # universal normalized fields drawn from the source, not fabricated
    assert rent.date.month == 5 and rent.date.day == 24
    assert rent.source_key == "epic_property_management_statement"


# --- the statement's own arithmetic -----------------------------------------
#
# Added after the codegen bench approved a parser that had extracted ONE
# transaction out of six. Every other gate was satisfied: its test asserted a
# count of one and passed, coverage was real, nothing was hardcoded. A test
# written from a parser's own output agrees with whatever that parser does, so
# only the document's own totals can tell a complete read from a partial one.

import pytest

from core.parsers.base import ParseError
from core.parsers.buildium_owner_statement import _reconcile_sections, _stated_total


def test_the_real_statement_balances_to_its_stated_totals():
    transactions = parse_statement(FIXTURE)

    assert round(sum(t.amount for t in transactions if t.amount > 0), 2) == 1250.00
    assert round(sum(-t.amount for t in transactions if t.amount < 0), 2) == 850.00


def test_a_partial_read_is_refused_rather_than_returned():
    """The failure this exists for: rows lost to a page break or a layout change."""
    transactions = parse_statement(FIXTURE)
    missing_one = [t for t in transactions if t.amount != 900.00]

    with pytest.raises(ParseError) as excinfo:
        _reconcile_sections(missing_one, [("Total from additions to cash", +1, 1250.00)],
                            FIXTURE)

    message = str(excinfo.value)
    assert "1,250.00" in message and "350.00" in message, "name both numbers"
    assert "partial read" in message


def test_a_section_that_balances_passes_quietly():
    transactions = parse_statement(FIXTURE)

    _reconcile_sections(transactions, [("Total from subtractions from cash", -1, 850.00)],
                        FIXTURE)  # must not raise


def test_a_statement_stating_no_totals_is_not_forced_to_reconcile():
    """Nothing to check against is a fact about the document, not a failure."""
    _reconcile_sections(parse_statement(FIXTURE), [], FIXTURE)  # must not raise


@pytest.mark.parametrize("line,expected", [
    ("Total from additions to cash $1,250.00", 1250.00),
    ("Total from subtractions from cash 850.00", 850.00),
    ("Total from additions to cash ($42.10)", -42.10),
    ("Total from additions to cash", None),
])
def test_the_stated_total_is_read_off_the_line(line, expected):
    assert _stated_total(line) == expected
