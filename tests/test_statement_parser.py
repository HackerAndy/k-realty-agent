from pathlib import Path

from core.tools.buildium_owner_statement import parse_statement

FIXTURE = Path(__file__).parent / "fixtures" / "sample_owner_statement.pdf"


def test_parses_fixture_statement():
    transactions = parse_statement(FIXTURE)

    # 8 real rows: the 4 beginning/ending balance lines skipped, headers ignored
    assert len(transactions) == 8

    maple = [t for t in transactions if t.property_id == "123_maple_st_duplex"]
    oak = [t for t in transactions if t.property_id == "456_oak_ave"]
    assert len(maple) == 4
    assert len(oak) == 4

    rent_a = next(t for t in maple if "Unit A" in t.description and "Rent" in t.description)
    assert rent_a.amount == 1200.00
    assert rent_a.unit_id == "A"
    assert rent_a.transaction_date.month == 6

    mgmt = next(t for t in oak if "Management fee" in t.description)
    assert mgmt.amount == -140.00
    assert mgmt.unit_id is None

    assert all("cash balance" not in t.description.lower() for t in transactions)
