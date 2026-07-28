"""Deterministic core of the agent-authored Epic GL scraper.

The live parts (login + Buildium API calls) need a real session and are the
operator's job to run. This guards the pure `_extract()` — the risky bit — against
a response shaped like Buildium's /manager/api/generalLedger/transactions payload
(a list of account wrappers, each with a Transactions array). Kept here so the
agent-built scraper is covered like the hand-written parsers are.
"""

from core import reconcile
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


def test_extract_records_reconciliation_when_channel_open():
    """Verify that _extract calls reconcile.record() for accounts with a Total."""
    with reconcile.channel("test_recon"):
        _extract(RAW)

    checks = reconcile.read("test_recon")
    # Capital One (Total=-11.62, one txn of -11.62) and Rent Income (Total=1421, one txn of 1421)
    # Advertising has Total=0 but no transactions, so no record.
    assert len(checks) == 2

    by_label = {c["label"]: c for c in checks}
    assert by_label["Capital One"]["expected"] == -11.62
    assert by_label["Capital One"]["actual"] == -11.62
    assert by_label["Capital One"]["balanced"] is True

    assert by_label["Rent Income"]["expected"] == 1421.00
    assert by_label["Rent Income"]["actual"] == 1421.00
    assert by_label["Rent Income"]["balanced"] is True


def test_extract_reconciliation_noop_without_channel():
    """When no reconcile channel is open, record() is a no-op — tests still pass."""
    txns = _extract(RAW)
    assert len(txns) == 2  # unaffected by reconciliation logic


def test_extract_multi_row_account_reconciliation():
    """Multiple transactions in one account should sum to the Total."""
    raw = [{
        "Name": "Test Account", "Total": 100.0,
        "Transactions": [
            {"Id": 1, "Date": "2026-07-01", "Amount": 60.0, "Description": "A"},
            {"Id": 2, "Date": "2026-07-02", "Amount": 40.0, "Description": "B"},
        ],
    }]
    with reconcile.channel("test_multi"):
        txns = _extract(raw)

    assert len(txns) == 2
    checks = reconcile.read("test_multi")
    assert len(checks) == 1
    assert checks[0]["expected"] == 100.0
    assert checks[0]["actual"] == 100.0
    assert checks[0]["balanced"] is True


def test_extract_reconciliation_mismatch():
    """If extracted sum differs from Total, reconciliation should flag it."""
    raw = [{
        "Name": "Mismatched", "Total": 200.0,
        "Transactions": [
            {"Id": 1, "Date": "2026-07-01", "Amount": 50.0, "Description": "A"},
            {"Id": 2, "Date": "2026-07-02", "Amount": 50.0, "Description": "B"},
        ],
    }]
    with reconcile.channel("test_mismatch"):
        _extract(raw)

    checks = reconcile.read("test_mismatch")
    assert len(checks) == 1
    assert checks[0]["expected"] == 200.0
    assert checks[0]["actual"] == 100.0
    assert checks[0]["balanced"] is False


def test_extract_no_reconciliation_when_total_is_none():
    """Accounts without a Total field should not produce a reconciliation record."""
    raw = [{
        "Name": "No Total", "Transactions": [
            {"Id": 1, "Date": "2026-07-01", "Amount": 50.0, "Description": "A"},
        ],
    }]
    with reconcile.channel("test_no_total"):
        _extract(raw)

    checks = reconcile.read("test_no_total")
    assert len(checks) == 0
