"""Which failures are allowed to open a browser at the operator.

Field failure this pins, and it was wrong in BOTH directions on the same run.
The front-end decided by pattern-matching the error prose:

    /(\\b401\\b|unauthor|session|login|credential|sign[\\s-]?in|2fa|verification)/i

- An HTTP 403 from a missing CSRF header — nothing to do with signing in —
  carried the harness's own boilerplate remediation "Check session validity and
  network connectivity". It matched on the word *session* and launched a
  browser demanding a login that was neither needed nor able to help. The
  operator saw a login prompt and never saw the 403.
- "No username/password stored for X" — the one failure a person must act on —
  matched nothing, and was shown as an ordinary error.

So the failure now says whether a human at a browser can fix it, and prose about
the failure never decides.
"""

import pytest

from core.scrapers.base import ScrapeError, SessionExpired
from core.tools.buildium_owner_portal import BuildiumLoginError
from core.tools.q2_online_banking import Q2AccessCodeRequired, Q2LoginError


# --- who asks for a human ----------------------------------------------------

@pytest.mark.parametrize("exc", [
    SessionExpired("the session is gone"),
    Q2AccessCodeRequired("Q2 asked for a one-time access code"),
])
def test_a_challenge_only_a_person_can_answer_asks_for_the_browser(exc):
    assert exc.needs_browser_login is True


@pytest.mark.parametrize("exc", [
    ScrapeError("GET .../accountHistory returned 403."),
    Q2LoginError("No username/password stored for 'dfcu_financial_bank'."),
    BuildiumLoginError("Login form was not detected."),
    RuntimeError("something else entirely"),
])
def test_everything_else_does_not(exc):
    assert getattr(exc, "needs_browser_login", False) is False


def test_the_403_that_caused_this_does_not_open_a_browser():
    """The exact failure, with the exact remediation text that fooled the regex."""
    exc = ScrapeError(
        "GET https://online.dfcufinancial.com/dfcufinancialonline/mobilews/"
        "accountHistory/1730767 returned 403. "
        "Check session validity and network connectivity."
    )
    assert "session" in str(exc)                      # the word is still there
    assert getattr(exc, "needs_browser_login", False) is False   # and means nothing


def test_a_missing_password_does_not_open_a_browser_either():
    """It's fixed in Settings. A browser login would fail exactly the same way,
    having wasted the operator's time getting there."""
    assert Q2LoginError("No username/password stored.").needs_browser_login is False


# --- the type relationships callers depend on --------------------------------

def test_session_expired_is_a_scrape_error():
    """So `except ScrapeError` keeps catching it."""
    assert isinstance(SessionExpired("x"), ScrapeError)


def test_the_access_code_challenge_is_still_a_login_error():
    assert isinstance(Q2AccessCodeRequired("x"), Q2LoginError)


def test_an_instance_can_flag_itself_without_a_new_class():
    """Scrapers are agent-authored; a one-off shouldn't need a class to say this."""
    exc = ScrapeError("portal wants a code")
    exc.needs_browser_login = True
    assert exc.needs_browser_login is True
    assert ScrapeError("another").needs_browser_login is False   # not leaked


# --- what the front-end is handed --------------------------------------------

def test_run_scraper_reports_the_flag(monkeypatch):
    """The GUI must receive a boolean, not be left to read the prose again."""
    from interfaces import mcp_tools

    def boom():
        raise Q2AccessCodeRequired("Q2 asked for a one-time access code")

    monkeypatch.setattr(mcp_tools, "has_scraper", lambda key: True)
    monkeypatch.setattr(mcp_tools, "get_scraper", lambda key: boom)

    with pytest.raises(mcp_tools.ToolError) as exc:
        mcp_tools.run_scraper("dfcu_financial_bank")
    assert exc.value.args[0]["needs_login"] is True


def test_run_scraper_does_not_flag_an_ordinary_failure(monkeypatch):
    from interfaces import mcp_tools

    def boom():
        raise ScrapeError("GET ... returned 403. Check session validity.")

    monkeypatch.setattr(mcp_tools, "has_scraper", lambda key: True)
    monkeypatch.setattr(mcp_tools, "get_scraper", lambda key: boom)

    with pytest.raises(mcp_tools.ToolError) as exc:
        mcp_tools.run_scraper("dfcu_financial_bank")
    detail = exc.value.args[0]
    assert detail["needs_login"] is False
    assert "403" in detail["message"]      # and the REAL error still reaches them
