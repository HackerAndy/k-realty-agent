"""Deterministic core of the agent-authored Epic GL scraper.

The live parts (login + Buildium API calls) need a real session and are the
operator's job to run. This guards the pure `_extract()` — the risky bit — against
a response shaped like Buildium's /manager/api/generalLedger/transactions payload
(a list of account wrappers, each with a Transactions array). Kept here so the
agent-built scraper is covered like the hand-written parsers are.
"""

from core import reconcile
from core.scrapers.epic_property_management import SERVICE_KEY, SETTINGS, _extract

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


def test_settings_declares_adjustable_dropdowns():
    """SETTINGS must declare the portal's dropdown choices as adjustable."""
    assert isinstance(SETTINGS, list) and len(SETTINGS) > 0

    keys = {s["key"] for s in SETTINGS}
    # Must include the accounting basis dropdown
    assert "accounting_basis" in keys, "accounting_basis must be a configurable setting"
    # Must include lookback days
    assert "lookback_days" in keys, "lookback_days must be a configurable setting"
    # Must include property_id (replaces the old property_selection)
    assert "property_id" in keys, "property_id must be a configurable setting"

    # Verify each setting has the required shape
    for s in SETTINGS:
        assert "key" in s
        assert "label" in s
        assert "type" in s
        assert "default" in s

    # Verify accounting_basis has proper options
    basis_setting = next(s for s in SETTINGS if s["key"] == "accounting_basis")
    assert basis_setting["type"] == "choice"
    options = {o["value"] for o in basis_setting["options"]}
    assert "cash" in options, "cash basis must be an option"
    assert "accrual" in options, "accrual basis must be an option"

    # Verify property_id has proper structure
    prop_setting = next(s for s in SETTINGS if s["key"] == "property_id")
    assert prop_setting["type"] == "choice"
    # Must include "all" as an option
    prop_options = {o["value"] for o in prop_setting["options"]}
    assert "all" in prop_options, '"all" must be a property option'


def test_settings_no_longer_has_property_selection_or_unit_selection():
    """The old property_selection and unit_selection settings were replaced by property_id."""
    keys = {s["key"] for s in SETTINGS}
    assert "property_selection" not in keys, "property_selection was replaced by property_id"
    assert "unit_selection" not in keys, "unit_selection is no longer needed"


def test_property_id_default_is_all():
    """The property_id setting should default to 'all'."""
    prop_setting = next(s for s in SETTINGS if s["key"] == "property_id")
    assert prop_setting["default"] == "all"


# ── The XSRF token: where every API call succeeds or 403s ────────────────────
#
# The token is the whole of the scraper's authorisation. Nothing about it can be
# checked by reading the code — a renamed header or a missing cookie fails only
# against the live portal, minutes into a run — so it is pinned here:
#
#   * the token is found among the other cookies a real session carries;
#   * a session without it fails LOUDLY, with the structured record and the
#     remediation the operator needs, rather than sending an empty token;
#   * both calls send it as `X-XSRF-TOKEN`. Buildium rejects any other spelling,
#     and a well-meaning rename to X-CSRF-Token would break every run;
#   * a non-200 is an error, not an empty result — an empty ledger would look
#     like "a quiet month" and silently under-report the books.

import json as _json

import pytest

from core import observability
from core.scrapers.base import ScrapeError
from core.scrapers.epic_property_management import (
    ALL_PROPERTIES,
    SINGLE_RENTAL,
    _extract_xsrf_token,
    _fetch_json,
    _fetch_properties,
    _get_gl_account_ids,
    _post_json,
    _property_rows,
    _property_selection,
)


class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeRequest:
    """Records what the scraper would have sent over the wire."""

    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers or {}, "data": None})
        return _FakeResponse(self.status, self.payload)

    def post(self, url, headers=None, data=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers or {}, "data": data})
        return _FakeResponse(self.status, self.payload)


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return self._cookies


class _FakePage:
    def __init__(self, cookies=(), status=200, payload=None):
        self.context = _FakeContext(list(cookies))
        self.request = _FakeRequest(status, payload)


def _log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(observability, "LOG_FILE", tmp_path / "logs" / "agent.jsonl")
    return tmp_path / "logs" / "agent.jsonl"


def test_the_token_is_found_among_the_other_session_cookies():
    """A real session carries a dozen cookies; the token is one of them."""
    page = _FakePage(cookies=[
        {"name": "ASP.NET_SessionId", "value": "abc"},
        {"name": "XSRF-TOKEN", "value": "the-token"},
        {"name": "ai_user", "value": "telemetry"},
    ])

    assert _extract_xsrf_token(page) == "the-token"


def test_a_session_without_the_token_fails_loudly_and_says_what_to_do(tmp_path, monkeypatch):
    """The alternative is calling the API with no authorisation and reporting the
    403 as if the portal were broken."""
    log_file = _log_file(tmp_path, monkeypatch)
    page = _FakePage(cookies=[{"name": "ASP.NET_SessionId", "value": "abc"}])

    with pytest.raises(ScrapeError):
        _extract_xsrf_token(page)

    record = _json.loads(log_file.read_text().splitlines()[-1])
    assert record["code"] == "XSRF_TOKEN_MISSING"
    assert record["operation"] == "extract_xsrf_token"
    assert "bootstrap_login" in record["remediation"], "the fix is re-establishing the session"
    assert record["context"]["service_key"] == SERVICE_KEY


def test_both_calls_send_the_token_in_the_header_buildium_requires():
    """X-XSRF-TOKEN exactly. Any other spelling 403s every call, and only live."""
    page = _FakePage(status=200, payload={"ok": True})

    _fetch_json(page, "https://x/api/thing", "tok-1")
    _post_json(page, "https://x/api/thing", {"a": 1}, "tok-2")

    get_call, post_call = page.request.calls
    assert get_call["headers"]["X-XSRF-TOKEN"] == "tok-1"
    assert post_call["headers"]["X-XSRF-TOKEN"] == "tok-2"
    assert get_call["headers"]["Content-Type"] == "application/json"
    assert post_call["headers"]["Content-Type"] == "application/json"


def test_a_posted_body_goes_as_a_json_string():
    """Buildium reads the body as JSON; handing it a dict would send form data."""
    page = _FakePage(payload=[])

    _post_json(page, "https://x/api/thing", {"AccountIds": ["1", "2"]}, "tok")

    sent = page.request.calls[0]["data"]
    assert isinstance(sent, str)
    assert _json.loads(sent) == {"AccountIds": ["1", "2"]}


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_rejected_get_is_an_error_not_an_empty_ledger(tmp_path, monkeypatch, status):
    """Returning [] here would read as "a quiet month" and under-report the books."""
    log_file = _log_file(tmp_path, monkeypatch)
    page = _FakePage(status=status, payload=None)

    with pytest.raises(ScrapeError):
        _fetch_json(page, "https://x/api/thing", "tok")

    record = _json.loads(log_file.read_text().splitlines()[-1])
    assert record["code"] == "HTTP_ERROR"
    assert record["context"]["status"] == status


def test_a_rejected_post_is_an_error_too(tmp_path, monkeypatch):
    log_file = _log_file(tmp_path, monkeypatch)
    page = _FakePage(status=403, payload=None)

    with pytest.raises(ScrapeError):
        _post_json(page, "https://x/api/thing", {"a": 1}, "tok")

    record = _json.loads(log_file.read_text().splitlines()[-1])
    assert record["code"] == "HTTP_ERROR"
    assert record["operation"] == "post_json"


def test_the_token_reaches_the_account_lookup():
    """The wiring between extraction and use: the account list is the first call
    of every run, so a token that doesn't get passed through fails immediately."""
    page = _FakePage(payload=[{"Id": 7}, {"Id": 9}])

    ids = _get_gl_account_ids(page, "tok")

    assert ids == ["7", "9"], "ids come back as strings — the payload wants strings"
    assert page.request.calls[0]["headers"]["X-XSRF-TOKEN"] == "tok"
    assert "excludeBankAccounts=true" in page.request.calls[0]["url"]


def test_fetch_properties_returns_id_and_name():
    """_fetch_properties extracts id and name from the API response."""
    page = _FakePage(payload=[
        {"Id": 10, "Name": "1029 E. Granet Ave."},
        {"Id": 20, "Name": "8095 Prospect Ave."},
    ])

    props = _fetch_properties(page, "tok")

    assert len(props) == 2
    assert props[0] == {"id": "10", "name": "1029 E. Granet Ave."}
    assert props[1] == {"id": "20", "name": "8095 Prospect Ave."}


# ── the property the operator picked ──────────────────────────────────
#
# Field failure this guards: the scrape looked the chosen property up, assigned
# its NAME to a local variable, and then sent "AllProperties" anyway. The
# dropdown on the settings screen did nothing whatsoever — every run returned
# every property's transactions, and nothing on screen said so. Only ruff
# noticed, as an unused variable.
#
# The pair below is Buildium's own, read out of the bundle its report form ships
# rather than guessed: AllProperties carries a null entity id, and a single
# rental carries the integer id with type "Rental".


def test_choosing_all_properties_sends_no_filter():
    assert _property_selection("all", []) == (None, ALL_PROPERTIES)


def test_choosing_one_property_actually_filters_the_request():
    """The whole point. If this pair doesn't change, the setting is decorative."""
    properties = [{"id": "10", "name": "1029 E. Granet Ave."},
                  {"id": "20", "name": "8095 Prospect Ave."}]

    assert _property_selection("20", properties) == (20, SINGLE_RENTAL)


def test_the_entity_id_is_sent_as_a_number():
    """Buildium's form does parseInt on it; a string is not the same request."""
    entity_id, _ = _property_selection("10", [{"id": "10", "name": "A"}])

    assert isinstance(entity_id, int)


def test_a_property_that_no_longer_exists_says_so(tmp_path, monkeypatch):
    """Falling back to every property changes what the run returns, so it can't
    be silent — a sold property would otherwise quietly widen the books."""
    log_file = _log_file(tmp_path, monkeypatch)

    assert _property_selection("999", [{"id": "10", "name": "A"}]) == (None, ALL_PROPERTIES)

    record = _json.loads(log_file.read_text().splitlines()[-1])
    assert record["code"] == "PROPERTY_NOT_FOUND"


# ── reading the portal's property list ────────────────────────────────
#
# Field failure this guards: the response was assumed to be a bare list. It is
# not — iterating the dict that came back yielded its keys, so `p["Id"]` raised
# `string indices must be integers` on all seven runs in the log. It was caught
# and logged as a warning, so the list silently stayed empty and the dropdown
# never had anything in it to pick.


def test_a_list_of_properties_is_read():
    assert _property_rows([{"Id": 1, "Name": "A"}]) == [{"Id": 1, "Name": "A"}]


def test_a_list_wrapped_in_an_envelope_is_read():
    """The shape that actually broke it."""
    assert _property_rows({"Data": [{"Id": 1, "Name": "A"}]}) == [{"Id": 1, "Name": "A"}]


def test_an_unrecognisable_shape_is_refused_rather_than_guessed_at():
    assert _property_rows({"Message": "Not authorised"}) is None
    assert _property_rows("nope") is None


def test_an_unreadable_response_reports_what_it_actually_got(tmp_path, monkeypatch):
    """Seven identical failures taught nobody anything, because the record named
    a Python exception and not one fact about the response. The next run has to
    be able to say what the endpoint sends."""
    log_file = _log_file(tmp_path, monkeypatch)
    page = _FakePage(payload={"Message": "Not authorised"})

    with pytest.raises(ScrapeError):
        _fetch_properties(page, "tok")

    record = _json.loads(log_file.read_text().splitlines()[-1])
    assert record["code"] == "PROPERTIES_SHAPE_UNKNOWN"
    assert record["context"]["payload_type"] == "dict"
    assert record["context"]["payload_keys"] == ["Message"]


def test_the_portals_property_list_is_published_the_declared_way(tmp_path, monkeypatch):
    """record_options is the sanctioned path, and it only accepts a field that
    declared itself discoverable. The version this replaces couldn't use it —
    so it hand-wrote the settings file instead, under a key nothing declared,
    and dropped every other source's recorded choices in the process."""
    import core.settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "source_settings.yaml")

    _settings.record_options(SERVICE_KEY, "property_id", [
        {"value": "10", "label": "1029 E. Granet Ave."},
        {"value": "20", "label": "8095 Prospect Ave."},
    ])

    options = next(
        f for f in _settings.schema_for(SERVICE_KEY) if f["key"] == "property_id"
    )["options"]
    values = [o["value"] for o in options]

    assert values[0] == "all", "the declared catch-all is not displaced by the portal's list"
    assert "10" in values and "20" in values


def test_publishing_the_list_leaves_other_sources_alone(tmp_path, monkeypatch):
    """The hand-rolled writer dumped {"sources": ...} only, so the whole
    `discovered` section went with it — every Epic run silently erased what
    every other source had learned."""
    import core.settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "source_settings.yaml")
    _settings._write(
        {}, {"another_source": {"some_field": [{"value": "keep", "label": "Keep me"}]}}
    )

    _settings.record_options(SERVICE_KEY, "property_id", [{"value": "10", "label": "A"}])

    survived = _settings.recorded_options("another_source").get("some_field")
    assert survived == [{"value": "keep", "label": "Keep me"}]
