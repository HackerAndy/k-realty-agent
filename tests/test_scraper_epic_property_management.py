"""Deterministic core of the agent-authored Epic GL scraper.

The live parts (login + Buildium API calls) need a real session and are the
operator's job to run. This guards the pure `_extract()` — the risky bit — against
a response shaped like Buildium's /manager/api/generalLedger/transactions payload
(a list of account wrappers, each with a Transactions array). Kept here so the
agent-built scraper is covered like the hand-written parsers are.
"""

from core.scrapers.epic_property_management import SERVICE_KEY, _extract

# Shaped like the real API: account wrappers, some with no activity.
RAW = [
    {
        "Id": 1, "Name": "Capital One", "BeginningBalance": -5081.19, "Total": -11.62,
        "Transactions": [
            {"Id": 101, "Date": "2026-06-24", "PropertyOrCompany": "1029 E. Granet Ave.",
             "Name": "Lowes", "Description": "Odor Eliminator", "Amount": -11.62,
             "Balance": -5092.81, "UnitNumber": "Property level"},
        ],
    },
    {
        "Id": 2, "Name": "Rent Income", "BeginningBalance": 0, "Total": 1421.00,
        "Transactions": [
            {"Id": 102, "Date": "2026-06-26", "PropertyOrCompany": "8095 Prospect Ave.",
             "Name": "Unit 1 - Kenneth Davis", "Description": "by Kenneth Davis",
             "Amount": 1421.00, "Balance": 7420.21, "UnitNumber": "1"},
        ],
    },
    {"Id": 3, "Name": "Advertising", "BeginningBalance": 481.24, "Total": 0, "Transactions": []},
]


def test_extract_flattens_accounts_and_tags_account_name():
    txns = _extract(RAW)

    assert len(txns) == 2  # the empty Advertising account contributes nothing
    lowes, rent = txns

    assert lowes.amount == -11.62  # API amounts are already signed
    assert rent.amount == 1421.00
    assert lowes.fields["AccountName"] == "Capital One"
    assert rent.fields["AccountName"] == "Rent Income"
    assert lowes.fields["PropertyOrCompany"] == "1029 E. Granet Ave."
    assert lowes.description == "Odor Eliminator"
    assert lowes.source_key == SERVICE_KEY
    assert str(lowes.date.date()) == "2026-06-24"


def test_extract_skips_rows_with_unparseable_date():
    raw = [{"Name": "X", "Transactions": [
        {"Id": 1, "Date": "not-a-date", "Amount": 5.0, "Description": "bad"},
        {"Id": 2, "Date": "2026-07-01", "Amount": 5.0, "Description": "good"},
    ]}]
    txns = _extract(raw)
    assert len(txns) == 1 and txns[0].description == "good"
