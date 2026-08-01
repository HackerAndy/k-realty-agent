"""Signing in to a Q2 bank, and calling its API once signed in.

Field failure this guards: the DFCU scraper called the account-history endpoint
with a valid session and got 403 on every attempt. The endpoint was right, the
cookies were right, and the operator was asked to re-authenticate over and over
because a 403 reads as "not signed in".

Q2 does double-submit CSRF: the session arrives as the `q2token` cookie and
every XHR must echo it back in a `q2token` HEADER. The browser context replays
the cookie automatically, which is the trap — the request looks authenticated
and is rejected anyway. Nothing about the failure points at the missing header.
"""

import pytest

from core.tools import q2_online_banking as q2


class _FakeContext:
    def __init__(self, cookies=()):
        self._cookies = list(cookies)

    def cookies(self):
        return list(self._cookies)


class _FakeLocator:
    """One selector's worth of page. `visible` is what decides sign-in state."""

    def __init__(self, page, visible: bool):
        self._page = page
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self._visible

    def wait_for(self, state=None, timeout=None):
        # Playwright raises on timeout rather than returning; the code under test
        # relies on that, and a fake that returned quietly would let a broken
        # "is the form there?" check pass.
        if not self._visible:
            raise TimeoutError("not visible")

    def inner_text(self, timeout=None):
        return self._page._body


class _FakePage:
    def __init__(self, cookies=(), body="", sign_in_form=False):
        self.context = _FakeContext(cookies)
        self._body = body
        # Whether the sign-in form is on the page. This is the ground truth for
        # "are we signed in" — a session cookie can outlive the session it names,
        # so the form being visible beats any cookie.
        self._sign_in_form = sign_in_form

    def locator(self, selector):
        return _FakeLocator(self, self._sign_in_form)


def _signed_in(token="tok-abc"):
    """A real signed-in session: a token AND no sign-in form."""
    return _FakePage(cookies=[{"name": "q2token", "value": token}])


def _stale_session(token="tok-expired"):
    """The failure this cost a run: a token restored from disk that the bank has
    already expired, so the portal is showing its sign-in form again."""
    return _FakePage(cookies=[{"name": "q2token", "value": token}], sign_in_form=True)


# ── the header that was missing ───────────────────────────────────────


def test_the_session_token_is_echoed_into_a_header():
    """The cookie alone is what the browser sends, and what Q2 answers 403 to."""
    headers = q2.api_headers(_signed_in())

    assert headers["q2token"] == "tok-abc"


def test_the_call_is_marked_as_an_xhr():
    """Q2's gateway checks it; a call without it is rejected before it is read."""
    assert q2.api_headers(_signed_in())["x-requested-with"] == "XMLHttpRequest"


def test_the_token_is_read_live_not_remembered():
    """It dies with the session that issued it, so a value captured during a
    recording is guaranteed wrong by the time a scheduled run uses it."""
    assert q2.api_headers(_signed_in("first"))["q2token"] == "first"
    assert q2.api_headers(_signed_in("second"))["q2token"] == "second"


def test_asking_for_headers_without_a_session_says_so():
    """Better than composing a request that will come back 403 and be misread."""
    with pytest.raises(q2.Q2LoginError, match="No Q2 session cookie"):
        q2.api_headers(_FakePage())


def test_a_referer_is_sent_only_when_there_is_one():
    assert "referer" not in q2.api_headers(_signed_in())
    assert q2.api_headers(_signed_in(), "https://online.example.com/")["referer"] == (
        "https://online.example.com/"
    )


# ── where sign-in actually lives ──────────────────────────────────────


def test_a_deep_link_is_reduced_to_somewhere_that_exists_signed_out():
    """A source is configured with the page whose data the operator wants. That
    page only exists once a session does, so it is the wrong place to send a run
    that hasn't signed in yet."""
    assert q2.preferred_login_url(
        "https://online.example.com/onlinebank/uux.aspx"
        "#/account/1730767?currentTab=transactions"
    ) == "https://online.example.com/onlinebank/uux.aspx"


def test_a_tenants_own_path_prefix_is_left_alone():
    """Every Q2 institution mounts the app somewhere different; guessing at it
    would break every tenant but the one we guessed from."""
    assert q2.preferred_login_url(
        "https://online.example.com/somebank/uux.aspx"
    ) == "https://online.example.com/somebank/uux.aspx"


# ── telling a challenge apart from a bad password ─────────────────────


def test_a_signed_in_session_is_recognised():
    assert q2.looks_authenticated(_signed_in()) is True
    assert q2.looks_authenticated(_FakePage()) is False


# ── the stale session that cost a live run ────────────────────────────
#
# Persisting session cookies between runs (so the operator stops re-logging-in
# every time) made a `q2token` immortal LOCALLY — it has no expiry of its own —
# while the bank expired it server-side minutes after issuing it. The cookie was
# present and dead. Sign-in was skipped as "already authenticated", the stored
# password was never read, and the scrape sent the dead token and got a 403 that
# reads exactly like a permissions problem.


def test_a_restored_but_expired_token_is_not_treated_as_signed_in():
    assert q2.looks_authenticated(_stale_session()) is False


def test_the_form_beats_the_cookie_even_when_the_token_looks_fine():
    """The whole point: presence of a token proves nothing on its own."""
    stale = _stale_session()
    assert q2.session_token(stale) == "tok-expired"   # the cookie IS there
    assert q2.looks_authenticated(stale) is False     # and it means nothing


def test_a_stale_session_still_yields_a_token_for_headers():
    """api_headers must not start guessing at session validity — that belongs to
    login(). It reports the token it has; a 403 is the portal's answer to give."""
    assert q2.api_headers(_stale_session())[q2.SESSION_COOKIE] == "tok-expired"


def test_an_access_code_prompt_is_detected():
    """It has to be told apart from a wrong password: no stored credential can
    answer it, so 'check your password' would send the operator after a password
    that was in fact correct."""
    page = _FakePage(body="Enter the Secure Access Code we just sent you")

    assert q2._challenged_for_access_code(page) is True


def test_an_ordinary_page_is_not_mistaken_for_a_challenge():
    page = _FakePage(body="Welcome back. Your accounts are shown below.")

    assert q2._challenged_for_access_code(page) is False


def test_the_challenge_is_its_own_kind_of_failure():
    """Callers route it to a browser the operator is sitting in front of, so it
    must be catchable separately — while still being a login failure."""
    assert issubclass(q2.Q2AccessCodeRequired, q2.Q2LoginError)


# ── asking the server, not the page ───────────────────────────────────
#
# The stale-session fix went through two wrong answers before this one. The
# cookie alone was trusted first (a dead token looks identical to a live one).
# Then the DOM: token present AND no sign-in form. That still failed live — the
# SPA had not finished booting inside the probe window, so "no form yet" read as
# "signed in", the dead token went to the bank, and the run 403'd in six seconds
# with the stored password never touched.
#
# Nothing on the client can tell a dead session from a live one. Only the server
# knows, so only the server is asked.

class _Resp:
    def __init__(self, status):
        self.status = status


class _ProbePage(_FakePage):
    """A page whose authenticated API call answers with `status`."""

    def __init__(self, status=200, cookies=(("q2token", "tok"),), raises=False):
        super().__init__(cookies=[{"name": n, "value": v} for n, v in cookies])
        self._status, self._raises = status, raises
        self.request = self
        self.asked = []

    def get(self, url, headers=None, timeout=None):
        self.asked.append((url, headers))
        if self._raises:
            raise RuntimeError("connection reset")
        return _Resp(self._status)


PORTAL = "https://online.example.com/onlinebanking/uux.aspx#/account/1?tab=x"
API_BASE = "https://online.example.com/onlinebanking"


def test_a_live_session_is_confirmed_by_the_server():
    assert q2.session_is_live(_ProbePage(200), API_BASE) is True


def test_a_dead_session_is_caught_however_healthy_the_cookie_looks():
    """The exact live failure: a restored token, and the bank says no."""
    page = _ProbePage(403)
    assert q2.session_token(page) == "tok"        # the cookie is right there
    assert q2.session_is_live(page, API_BASE) is False


def test_a_401_is_also_not_live():
    assert q2.session_is_live(_ProbePage(401), API_BASE) is False


def test_no_cookie_means_no_probe_at_all():
    page = _ProbePage(200, cookies=())
    assert q2.session_is_live(page, API_BASE) is False
    assert page.asked == []


def test_an_unreachable_server_is_treated_as_not_live():
    """Signing in again is cheap and always safe; continuing on a dead session
    fails the whole run."""
    assert q2.session_is_live(_ProbePage(raises=True), API_BASE) is False


def test_the_probe_carries_the_csrf_header():
    """Without it Q2 answers 403 to everything, and the probe would report every
    session dead — turning a session check into a permanent re-login loop."""
    page = _ProbePage(200)
    q2.session_is_live(page, API_BASE)
    _, headers = page.asked[0]
    assert headers[q2.SESSION_COOKIE] == "tok"
    assert headers["x-requested-with"] == "XMLHttpRequest"


def test_the_probe_hits_the_api_base_it_was_given():
    """The API base is passed in, not guessed from the sign-in URL: DFCU signs in
    on the marketing site and relays into the banking app on another host."""
    page = _ProbePage(200)
    q2.session_is_live(page, "https://online.example.com/onlinebanking")
    url, _ = page.asked[0]
    assert url == "https://online.example.com/onlinebanking/mobilews/accounts"


def test_a_trailing_slash_on_the_api_base_does_not_double_up():
    page = _ProbePage(200)
    q2.session_is_live(page, "https://online.example.com/onlinebanking/")
    assert page.asked[0][0] == "https://online.example.com/onlinebanking/mobilews/accounts"


@pytest.mark.parametrize("portal,expected", [
    # The DFCU case, exactly as configured.
    ("https://online.dfcufinancial.com/dfcufinancialonline/uux.aspx",
     "https://online.dfcufinancial.com/dfcufinancialonline"),
    # With the deep link the operator's source actually carries.
    ("https://online.dfcufinancial.com/dfcufinancialonline/uux.aspx#/account/1730767?t=x",
     "https://online.dfcufinancial.com/dfcufinancialonline"),
    # Already the app directory — nothing to strip.
    ("https://online.example.com/somebank",
     "https://online.example.com/somebank"),
    ("https://online.example.com/somebank/",
     "https://online.example.com/somebank"),
    # A different SPA entry filename must be handled the same way.
    ("https://online.example.com/somebank/index.html",
     "https://online.example.com/somebank"),
    # A bare host has nothing to strip and must not become empty.
    ("https://online.example.com", "https://online.example.com"),
])
def test_the_api_root_sits_beside_the_spa_entry_page(portal, expected):
    """Keeping the filename builds .../uux.aspx/mobilews/accounts, which 404s —
    and a probe that always fails reports every session dead, re-logging in on
    every run. Caught by a test, not in the field."""
    assert q2.api_root(portal) == expected


# ── when the bank refuses the robot ───────────────────────────────────
#
# Observed live at DFCU: correct credentials, correct form, correct submit — and
# the click lands on "Access Denied — You are unauthorized to access this
# resource. Reference ID is: b8d69b57b0". No session cookie is ever issued.
#
# This is the institution's bot detection. It is not a bad password and not a
# broken selector, and every other reading of it sends the operator somewhere
# useless: the generic timeout said "check the stored credentials", which invites
# re-entering a password that is perfectly correct.

BLOCK_PAGE = ("Access Denied\n\nYou are unauthorized to access this resource.\n\n"
              "Reference ID is: b8d69b57b0\n\nTroubleshooting Steps\n"
              "1. Reload the home page and try again.")


def test_the_block_page_is_recognised():
    assert q2._blocked_by_automation_defence(_FakePage(body=BLOCK_PAGE)) is True


def test_an_ordinary_page_is_not_mistaken_for_a_block():
    assert q2._blocked_by_automation_defence(
        _FakePage(body="Welcome back. Your accounts are shown below.")) is False


def test_a_plain_permission_error_is_not_a_block():
    """"Access denied" alone is ordinary; turning it into "go sign in" would send
    the operator to a browser that cannot help."""
    assert q2._blocked_by_automation_defence(
        _FakePage(body="Access denied: this account cannot view statements.")) is False


def test_a_block_is_not_confused_with_an_access_code_challenge():
    """Different failures, different remedies — a code can be typed, a block
    cannot be answered at all."""
    page = _FakePage(body=BLOCK_PAGE)
    assert q2._blocked_by_automation_defence(page) is True
    assert q2._challenged_for_access_code(page) is False


def test_the_block_asks_for_a_human_at_a_browser():
    """The one thing that does work: the operator signing in themselves."""
    assert q2.Q2AutomationBlocked("x").needs_browser_login is True
    assert issubclass(q2.Q2AutomationBlocked, q2.Q2LoginError)


def test_an_unreadable_page_is_not_reported_as_a_block():
    class _Dead(_FakePage):
        def locator(self, selector):
            raise RuntimeError("page closed")
    assert q2._blocked_by_automation_defence(_Dead()) is False
