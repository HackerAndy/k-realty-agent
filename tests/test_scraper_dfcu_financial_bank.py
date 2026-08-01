"""Tests for core.scrapers.dfcu_financial_bank — _extract() pure function."""

from copy import deepcopy
from datetime import date, datetime

from core.models import Transaction
from core.reconcile import channel, read
from core.scrapers.dfcu_financial_bank import _extract, _reconcile_against_account_balance


# ── sample payload (shape matches the real API response) ───────────────

SAMPLE_PAYLOAD = {
    "data": {
        "transactions": [
            {
                "transactionType": "DEBIT",
                "transactionId": "TXN001",
                "accountId": 1730767,
                "postedDate": "2026-07-15T00:00:00.000-04:00",
                "amount": "-200.00",
                "checkNumber": "CHK001",
                "description": "ATM Withdrawal",
                "hostTranNumber": "HTN001",
                "isDebit": True,
                "imageNumber": "IMG001",
                "showImage": False,
                "tranCode": "TC01",
                "runningBalance": "4800.00",
                "extendedDescription": [],
                "hasTransactionDetails": False,
                "showDetail": False,
                "statementDescription": "ATM WD",
                "hostTransactionDataItems": [],
                "disputeStatus": None,
                "memo": None,
                "hostPostDate": "2026-07-15T00:00:00.000-04:00",
            },
            {
                "transactionType": "CREDIT",
                "transactionId": "TXN002",
                "accountId": 1730767,
                "postedDate": "2026-07-16T00:00:00.000-04:00",
                "amount": "3000.00",
                "checkNumber": "",
                "description": "Salary Deposit",
                "hostTranNumber": "HTN002",
                "isDebit": False,
                "imageNumber": "",
                "showImage": False,
                "tranCode": "TC02",
                "runningBalance": "7800.00",
                "extendedDescription": [],
                "hasTransactionDetails": False,
                "showDetail": False,
                "statementDescription": "SALARY DEP",
                "hostTransactionDataItems": [],
                "disputeStatus": None,
                "memo": None,
                "hostPostDate": "2026-07-16T00:00:00.000-04:00",
            },
            {
                "transactionType": "DEBIT",
                "transactionId": "TXN003",
                "accountId": 1730767,
                "postedDate": "2026-07-18T00:00:00.000-04:00",
                "amount": "-75.50",
                "checkNumber": "CHK003",
                "description": "Grocery Store",
                "hostTranNumber": "HTN003",
                "isDebit": True,
                "imageNumber": "IMG003",
                "showImage": False,
                "tranCode": "TC01",
                "runningBalance": "7724.50",
                "extendedDescription": [],
                "hasTransactionDetails": False,
                "showDetail": False,
                "statementDescription": "GROCERY",
                "hostTransactionDataItems": [],
                "disputeStatus": None,
                "memo": None,
                "hostPostDate": "2026-07-18T00:00:00.000-04:00",
            },
            {
                "transactionType": "DEBIT",
                "transactionId": "TXN004",
                "accountId": 1730767,
                "postedDate": "2026-07-19T00:00:00.000-04:00",
                "amount": "-120.00",
                "checkNumber": "",
                "description": "Electric Bill Payment",
                "hostTranNumber": "HTN004",
                "isDebit": True,
                "imageNumber": "",
                "showImage": False,
                "tranCode": "TC03",
                "runningBalance": "7604.50",
                "extendedDescription": [],
                "hasTransactionDetails": False,
                "showDetail": False,
                "statementDescription": "ELEC BILL",
                "hostTransactionDataItems": [],
                "disputeStatus": None,
                "memo": None,
                "hostPostDate": "2026-07-19T00:00:00.000-04:00",
            },
            {
                "transactionType": "CREDIT",
                "transactionId": "TXN005",
                "accountId": 1730767,
                "postedDate": "2026-07-21T00:00:00.000-04:00",
                "amount": "500.00",
                "checkNumber": "",
                "description": "Online Transfer In",
                "hostTranNumber": "HTN005",
                "isDebit": False,
                "imageNumber": "",
                "showImage": False,
                "tranCode": "TC04",
                "runningBalance": "8104.50",
                "extendedDescription": [],
                "hasTransactionDetails": False,
                "showDetail": False,
                "statementDescription": "ONLINE TRANSFER",
                "hostTransactionDataItems": [],
                "disputeStatus": None,
                "memo": None,
                "hostPostDate": "2026-07-21T00:00:00.000-04:00",
            },
            {
                "transactionType": "CREDIT",
                "transactionId": "TXN006",
                "accountId": 1730767,
                "postedDate": "2026-07-29T00:00:00.000-04:00",
                "amount": "150.00",
                "checkNumber": "",
                "description": "ATM Deposit",
                "hostTranNumber": "HTN006",
                "isDebit": False,
                "imageNumber": "",
                "showImage": False,
                "tranCode": "TC05",
                "runningBalance": "8254.50",
                "extendedDescription": [],
                "hasTransactionDetails": False,
                "showDetail": False,
                "statementDescription": "ATM DEP",
                "hostTransactionDataItems": [],
                "disputeStatus": None,
                "memo": None,
                "hostPostDate": "2026-07-29T00:00:00.000-04:00",
            },
        ]
    }
}


# ── tests ──────────────────────────────────────────────────────────────

class TestExtractBasic:
    """Basic extraction sanity checks."""

    def test_returns_list_of_transactions(self):
        result = _extract(SAMPLE_PAYLOAD)
        assert isinstance(result, list)
        assert all(isinstance(t, Transaction) for t in result)

    def test_correct_count(self):
        result = _extract(SAMPLE_PAYLOAD)
        assert len(result) == 6

    def test_money_in_is_positive(self):
        result = _extract(SAMPLE_PAYLOAD)
        credit_txns = [t for t in result if t.amount > 0]
        assert len(credit_txns) == 3

    def test_money_out_is_negative(self):
        result = _extract(SAMPLE_PAYLOAD)
        debit_txns = [t for t in result if t.amount < 0]
        assert len(debit_txns) == 3

    def test_date_parsed_correctly(self):
        result = _extract(SAMPLE_PAYLOAD)
        # Check that the first transaction has the right date
        assert result[0].date.date() == date(2026, 7, 15)

    def test_all_dates_are_date_objects(self):
        result = _extract(SAMPLE_PAYLOAD)
        assert all(isinstance(t.date, datetime) for t in result)

    def test_filter_with_none_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=None)
        assert len(result) == 6

    def test_filter_with_earlier_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2026, 7, 10))
        # All transactions should be included
        assert len(result) == 6

    def test_filter_with_exact_boundary_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2026, 7, 15))
        # All transactions should be included
        assert len(result) == 6

    def test_filter_with_later_boundary_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2026, 7, 29))
        # Only the last transaction should be included
        assert len(result) == 1
        assert result[0].date.date() == date(2026, 7, 29)

    def test_filter_with_middle_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2026, 7, 18))
        # Transactions from 2026-07-18 onwards should be included
        assert len(result) == 4
        assert result[0].date.date() == date(2026, 7, 18)
        assert result[1].date.date() == date(2026, 7, 19)
        assert result[2].date.date() == date(2026, 7, 21)
        assert result[3].date.date() == date(2026, 7, 29)

    def test_invalid_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2027, 7, 15))
        # No transactions should be included
        assert len(result) == 0

    def test_filter_excludes_before_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2026, 7, 20))
        # Only the last two transactions should be included
        assert len(result) == 2
        assert result[0].date.date() == date(2026, 7, 21)
        assert result[1].date.date() == date(2026, 7, 29)

    def test_filter_includes_boundary_date(self):
        result = _extract(SAMPLE_PAYLOAD, start_date=date(2026, 7, 15))
        # All transactions should be included
        assert len(result) == 6


class TestExtractEdgeCases:
    """Tests for edge cases in extraction."""

    def test_empty_payload(self):
        result = _extract({"data": {"transactions": []}})
        assert len(result) == 0

    def test_missing_date_field(self):
        # deepcopy, NOT .copy(): a shallow copy shares payload["data"], so
        # mutating a transaction below corrupts the module-level fixture for
        # every test that runs after this one. That is what made the
        # reconciliation test pass alone and fail in the suite.
        payload = deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["transactions"][0]["postedDate"] = None
        result = _extract(payload)
        # Should skip the transaction with missing date
        assert len(result) == 5

    def test_invalid_date_format(self):
        # deepcopy, NOT .copy(): a shallow copy shares payload["data"], so
        # mutating a transaction below corrupts the module-level fixture for
        # every test that runs after this one. That is what made the
        # reconciliation test pass alone and fail in the suite.
        payload = deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["transactions"][0]["postedDate"] = "invalid-date"
        result = _extract(payload)
        # Should skip the transaction with invalid date
        assert len(result) == 5

    def test_missing_amount_field(self):
        # deepcopy, NOT .copy(): a shallow copy shares payload["data"], so
        # mutating a transaction below corrupts the module-level fixture for
        # every test that runs after this one. That is what made the
        # reconciliation test pass alone and fail in the suite.
        payload = deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["transactions"][0]["amount"] = None
        result = _extract(payload)
        # Should skip the transaction with missing amount
        assert len(result) == 5

    def test_invalid_amount_format(self):
        # deepcopy, NOT .copy(): a shallow copy shares payload["data"], so
        # mutating a transaction below corrupts the module-level fixture for
        # every test that runs after this one. That is what made the
        # reconciliation test pass alone and fail in the suite.
        payload = deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["transactions"][0]["amount"] = "invalid"
        result = _extract(payload)
        # Should skip the transaction with invalid amount
        assert len(result) == 5


class TestReconciliation:
    """Tests for reconciliation of running balances."""

    def test_balanced_running_balances(self):
        """Test that balanced running balances are recorded correctly."""
        with channel("test_account"):
            _extract(SAMPLE_PAYLOAD)          # these tests assert on the
            checks = read("test_account")    # recorded checks, not the rows
        
        # Should have 5 checks (for 6 transactions, checking consecutive pairs)
        assert len(checks) == 5

        # Check that all checks are balanced
        for check in checks:
            assert check["balanced"] is True

    def test_unbalanced_running_balances(self):
        """Test that unbalanced running balances are detected correctly."""
        # Create a payload with an error in running balance
        payload_with_error = {
            "data": {
                "transactions": [
                    {
                        "transactionType": "DEBIT",
                        "transactionId": "TXN001",
                        "accountId": 1730767,
                        "postedDate": "2026-07-15T00:00:00.000-04:00",
                        "amount": "-200.00",
                        "checkNumber": "CHK001",
                        "description": "ATM Withdrawal",
                        "hostTranNumber": "HTN001",
                        "isDebit": True,
                        "imageNumber": "IMG001",
                        "showImage": False,
                        "tranCode": "TC01",
                        "runningBalance": "4800.00",
                        "extendedDescription": [],
                        "hasTransactionDetails": False,
                        "showDetail": False,
                        "statementDescription": "ATM WD",
                        "hostTransactionDataItems": [],
                        "disputeStatus": None,
                        "memo": None,
                        "hostPostDate": "2026-07-15T00:00:00.000-04:00",
                    },
                    {
                        "transactionType": "CREDIT",
                        "transactionId": "TXN002",
                        "accountId": 1730767,
                        "postedDate": "2026-07-16T00:00:00.000-04:00",
                        "amount": "3000.00",
                        "checkNumber": "",
                        "description": "Salary Deposit",
                        "hostTranNumber": "HTN002",
                        "isDebit": False,
                        "imageNumber": "",
                        "showImage": False,
                        "tranCode": "TC02",
                        "runningBalance": "7800.00",
                        "extendedDescription": [],
                        "hasTransactionDetails": False,
                        "showDetail": False,
                        "statementDescription": "SALARY DEP",
                        "hostTransactionDataItems": [],
                        "disputeStatus": None,
                        "memo": None,
                        "hostPostDate": "2026-07-16T00:00:00.000-04:00",
                    },
                    {
                        "transactionType": "DEBIT",
                        "transactionId": "TXN003",
                        "accountId": 1730767,
                        "postedDate": "2026-07-18T00:00:00.000-04:00",
                        "amount": "-75.50",
                        "checkNumber": "CHK003",
                        "description": "Grocery Store",
                        "hostTranNumber": "HTN003",
                        "isDebit": True,
                        "imageNumber": "IMG003",
                        "showImage": False,
                        "tranCode": "TC01",
                        "runningBalance": "7724.00",  # This is wrong - should be 7724.50
                        "extendedDescription": [],
                        "hasTransactionDetails": False,
                        "showDetail": False,
                        "statementDescription": "GROCERY",
                        "hostTransactionDataItems": [],
                        "disputeStatus": None,
                        "memo": None,
                        "hostPostDate": "2026-07-18T00:00:00.000-04:00",
                    },
                    {
                        "transactionType": "DEBIT",
                        "transactionId": "TXN004",
                        "accountId": 1730767,
                        "postedDate": "2026-07-19T00:00:00.000-04:00",
                        "amount": "-120.00",
                        "checkNumber": "",
                        "description": "Electric Bill Payment",
                        "hostTranNumber": "HTN004",
                        "isDebit": True,
                        "imageNumber": "",
                        "showImage": False,
                        "tranCode": "TC03",
                        "runningBalance": "7604.50",
                        "extendedDescription": [],
                        "hasTransactionDetails": False,
                        "showDetail": False,
                        "statementDescription": "ELEC BILL",
                        "hostTransactionDataItems": [],
                        "disputeStatus": None,
                        "memo": None,
                        "hostPostDate": "2026-07-19T00:00:00.000-04:00",
                    },
                    {
                        "transactionType": "CREDIT",
                        "transactionId": "TXN005",
                        "accountId": 1730767,
                        "postedDate": "2026-07-21T00:00:00.000-04:00",
                        "amount": "500.00",
                        "checkNumber": "",
                        "description": "Online Transfer In",
                        "hostTranNumber": "HTN005",
                        "isDebit": False,
                        "imageNumber": "",
                        "showImage": False,
                        "tranCode": "TC04",
                        "runningBalance": "8104.50",
                        "extendedDescription": [],
                        "hasTransactionDetails": False,
                        "showDetail": False,
                        "statementDescription": "ONLINE TRANSFER",
                        "hostTransactionDataItems": [],
                        "disputeStatus": None,
                        "memo": None,
                        "hostPostDate": "2026-07-21T00:00:00.000-04:00",
                    },
                    {
                        "transactionType": "CREDIT",
                        "transactionId": "TXN006",
                        "accountId": 1730767,
                        "postedDate": "2026-07-29T00:00:00.000-04:00",
                        "amount": "150.00",
                        "checkNumber": "",
                        "description": "ATM Deposit",
                        "hostTranNumber": "HTN006",
                        "isDebit": False,
                        "imageNumber": "",
                        "showImage": False,
                        "tranCode": "TC05",
                        "runningBalance": "8254.50",
                        "extendedDescription": [],
                        "hasTransactionDetails": False,
                        "showDetail": False,
                        "statementDescription": "ATM DEP",
                        "hostTransactionDataItems": [],
                        "disputeStatus": None,
                        "memo": None,
                        "hostPostDate": "2026-07-29T00:00:00.000-04:00",
                    },
                ]
            }
        }
        
        with channel("test_account"):
            _extract(payload_with_error)     # these tests assert on the
            checks = read("test_account")    # recorded checks, not the rows

        # The first check (between TXN001 and TXN002) should be balanced
        assert checks[0]["balanced"] is True
        assert checks[0]["difference"] == 0.0

        # The second check (between TXN002 and TXN003) should be unbalanced
        # The difference should be -0.50 (7724.00 - 7800.00 = -76.00, but expected was -75.50)
        # Actually, the difference is 7724.00 - 7800.00 = -76.00, expected is -75.50, so difference is -0.50
        assert checks[1]["balanced"] is False
        assert checks[1]["difference"] == -0.50

        # The third check (between TXN003 and TXN004) should be unbalanced
        # The difference should be +0.50 (7604.50 - 7724.00 = -119.50, but expected was -120.00)
        # The difference is 7604.50 - 7724.00 = -119.50, expected is -120.00, so difference is +0.50
        assert checks[2]["balanced"] is False
        assert checks[2]["difference"] == 0.50

class TestAccountBalanceAnchor:
    """The half of reconciliation the running-balance chain cannot do.

    The chain proves consecutive rows are internally consistent, which catches a
    row dropped BETWEEN two others. It cannot catch truncation: lose the newest
    rows, or stop paginating early, and what remains is a perfectly consistent
    chain of the wrong length. That is the likelier failure — a date window that
    clipped, a page that never arrived — and an internal check is blind to it.

    So the newest row's running balance is compared against the account's own
    stated Current Balance, which the portal publishes independently.
    """

    class _FakeResponse:
        def __init__(self, payload, status=200):
            self._payload, self.status = payload, status

        def json(self):
            return self._payload

    class _FakePage:
        """Answers the accounts endpoint; records what it was asked for."""

        def __init__(self, payload, status=200):
            self._payload, self._status = payload, status
            self.urls = []
            self.request = self
            self.context = self

        def cookies(self):                      # for q2 api_headers
            return [{"name": "q2token", "value": "tok"}]

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            return TestAccountBalanceAnchor._FakeResponse(self._payload, self._status)

    @staticmethod
    def _accounts(balance, account_id=1730767):
        return {"data": [{"accountId": account_id,
                          "extended": {"balance1": "9999.99", "balance2": balance}}]}

    ROWS = [
        {"postedDate": "2026-07-15T00:00:00.000-04:00", "runningBalance": "4800.00"},
        {"postedDate": "2026-07-29T00:00:00.000-04:00", "runningBalance": "8254.50"},
    ]

    def test_a_complete_pull_balances(self):
        page = self._FakePage(self._accounts("8254.50"))
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", self.ROWS)
            checks = read("anchor")
        assert len(checks) == 1 and checks[0]["balanced"] is True

    def test_a_truncated_pull_does_NOT_balance(self):
        """The whole point: drop the newest row and the chain is still perfect."""
        page = self._FakePage(self._accounts("8254.50"))
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", self.ROWS[:1])
            checks = read("anchor")
        assert checks[0]["balanced"] is False
        assert checks[0]["difference"] == -3454.50

    def test_it_uses_current_balance_not_available(self):
        """balance1 is Available and includes pending activity with no posted row,
        so it would disagree with history even on a perfect pull."""
        page = self._FakePage(self._accounts("8254.50"))
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", self.ROWS)
            checks = read("anchor")
        assert checks[0]["expected"] == 8254.50      # balance2, not 9999.99

    def test_the_newest_row_is_chosen_regardless_of_order(self):
        page = self._FakePage(self._accounts("8254.50"))
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", list(reversed(self.ROWS)))
            checks = read("anchor")
        assert checks[0]["actual"] == 8254.50

    def test_another_accounts_balance_is_not_used(self):
        payload = {"data": [
            {"accountId": 999, "extended": {"balance2": "1.00"}},
            {"accountId": 1730767, "extended": {"balance2": "8254.50"}},
        ]}
        page = self._FakePage(payload)
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", self.ROWS)
            checks = read("anchor")
        assert checks[0]["expected"] == 8254.50

    def test_a_failed_balance_call_does_not_lose_the_scrape(self):
        """A corroborating call that fails must not discard rows already pulled."""
        page = self._FakePage({}, status=500)
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", self.ROWS)
            checks = read("anchor")
        assert checks == []

    def test_an_unknown_account_records_nothing_rather_than_guessing(self):
        page = self._FakePage(self._accounts("8254.50", account_id=42))
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", self.ROWS)
            checks = read("anchor")
        assert checks == []

    def test_no_rows_means_no_check(self):
        page = self._FakePage(self._accounts("8254.50"))
        with channel("anchor"):
            _reconcile_against_account_balance(page, "1730767", [])
            checks = read("anchor")
        assert checks == [] and page.urls == []      # and no needless call
