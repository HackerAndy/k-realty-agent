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
    oak = [t for t in txns if t.property_id == "456_oak_ave"]  # "456 Oak Av"+"e." and *SPM* stripped
    assert len(oak) == 2

    # units: real units kept, "Property level" -> None
    rent = next(t for t in txns if "Rent Income" in t.description)
    assert rent.unit_id == "1"
    draw = next(t for t in txns if "Owner Draw" in t.description)
    assert draw.unit_id is None

    # dates parse (single-digit month/day form)
    assert rent.transaction_date.month == 5 and rent.transaction_date.day == 24
