# Template candidate: platform-reusable (tier 2) — reusable for any future
# client whose bank or credit union also runs on Q2 (Q2 Holdings' digital
# banking platform), not universally generic.
# See agent-harness-template/docs/promotion-log.md.
"""Login + API-header helper for Q2-powered online banking.

Q2 (q2.com) is the digital-banking platform behind a large number of US credit
unions and community banks, white-labelled onto each institution's own domain.
This module handles the two things that are true of EVERY Q2 tenant — how you
sign in, and what an authenticated API call has to carry — so a scraper for any
one of them doesn't have to rediscover them. Anything past that (which account,
which endpoint, which columns) is that institution's business and belongs in an
agent-authored scraper.

The header rule is the part worth knowing, because getting it wrong does not
look like getting it wrong: Q2's gateway does double-submit CSRF. The session
token arrives as the `q2token` cookie, and every XHR must ECHO it back in a
`q2token` HEADER. Send the cookie alone — which is what a bare
``page.request.get()`` does, since the context replays cookies automatically —
and the gateway answers **403**. Not 401. A 403 with a perfectly valid session
reads as "not logged in", so the natural response is to go re-authenticate,
which changes nothing, and the loop repeats.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import time
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import Page

from core import progress
from core.tools.credential_store import CredentialStore, CredentialStoreError

SESSION_COOKIE = "q2token"


class Q2LoginError(RuntimeError):
    # Most sign-in failures are NOT fixed by opening a browser: a missing
    # password belongs in Settings, a changed form belongs in a revise. Only the
    # subclass below asks for a human. See core/scrapers/base.ScrapeError.
    needs_browser_login: bool = False


class Q2AutomationBlocked(Q2LoginError):
    """The institution refused the automated browser itself.

    Observed at DFCU: correct credentials, correct form, and the submit lands on
    "Access Denied — You are unauthorized to access this resource. Reference ID
    is: …". No session cookie is ever issued. This is the bank's bot detection,
    not a bad password and not a broken selector.

    It gets its own type because every other reading sends the operator somewhere
    useless — "check the stored credentials" invites them to re-enter a password
    that is perfectly correct, which is exactly what the generic timeout message
    did. A human signing in themselves is the only thing that works, and it is
    the operator's own bank and their own browser, so that is what we route to.
    """

    needs_browser_login = True


class Q2AccessCodeRequired(Q2LoginError):
    """Q2 answered the password with a one-time access code challenge.

    A DISTINCT failure from a bad password, because the remedy is distinct and
    the operator can only be told the right one if we can tell them apart: no
    stored credential completes this. It needs a code delivered out of band
    (email, SMS, voice, push), so the run has to hand off to a browser the
    operator is sitting in front of.
    """

    needs_browser_login = True


def preferred_login_url(url: str) -> str:
    """Reduce a Q2 deep link to the app root, which is where sign-in lives.

    A source's configured URL is naturally the page whose data the operator
    wants — ``.../uux.aspx#/account/1730767?currentTab=transactions``. That is a
    signed-IN location: it only exists once a session does. Sent there without
    one, the SPA loads, finds no session, and bounces to sign-in anyway, but the
    fragment it was carrying is lost in the process, so the "did the login land
    where I aimed?" check has nothing stable to compare against.

    Dropping the fragment and query leaves the app root, which serves sign-in
    when unauthenticated and the dashboard when not — correct either way. The
    host and path are left exactly as given; a tenant's own path prefix
    (``/dfcufinancialonline``) is not ours to guess at.
    """
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def session_token(page: Page) -> str | None:
    """The live `q2token`, read off the browser session. None if not signed in.

    Read at RUN time, never carried over from a recording — this value dies with
    the session that issued it.
    """
    try:
        cookies = page.context.cookies()
    except Exception:
        return None
    for cookie in cookies:
        if cookie.get("name") == SESSION_COOKIE and cookie.get("value"):
            return cookie["value"]
    return None


def api_headers(page: Page, referer: str | None = None) -> dict[str, str]:
    """The headers a Q2 `mobilews/...` call must carry, or Q2 answers 403.

    Pass the result straight to ``page.request.get(url, headers=...)``. The
    cookie itself is replayed by the browser context; what is NOT automatic is
    the echo of it into a header, plus the XHR marker the gateway checks.
    """
    token = session_token(page)
    if not token:
        raise Q2LoginError(
            "No Q2 session cookie — sign in first (q2_online_banking.login)."
        )
    headers = {
        SESSION_COOKIE: token,
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "cache-control": "no-store",
    }
    if referer:
        headers["referer"] = referer
    return headers


# How long to wait for the sign-in form before concluding it isn't there. Short:
# this runs on the already-signed-in path too, where every second is spent
# waiting for something that will never appear.
_FORM_PROBE_MS = 3_000


def looks_authenticated(page: Page, probe_ms: int = _FORM_PROBE_MS) -> bool:
    """Cheap DOM heuristic: a token, and no sign-in form on screen.

    Useful for deciding whether a sign-in has COMPLETED, where the form was
    demonstrably there a moment ago and its disappearance is real evidence.

    Not sufficient for deciding whether a RESTORED session is still good — see
    `session_is_live`, which asks the server instead. Absence of a form within a
    few seconds is weak evidence on a SPA that may simply still be booting, and
    reading it as "signed in" is what sent a dead token to the bank and produced
    a 403 in six seconds flat, with the stored password never touched.
    """
    if session_token(page) is None:
        return False
    return _first_visible(page, _LOGIN_ID_SELECTORS, probe_ms) is None


def session_is_live(page: Page, api_base: str) -> bool:
    """Ask the SERVER whether this session still works. The authoritative check.

    Q2's `q2token` is a session cookie with no expiry of its own, so a token
    restored from disk looks perfectly valid locally long after the bank has
    dropped the session behind it. Nothing on the client can tell the difference
    — not the cookie, not the DOM — so the only honest answer comes from making a
    real authenticated request and seeing what comes back.

    `mobilews/accounts` is the probe: small, read-only, and the same endpoint the
    scraper needs anyway. A 200 means the session is genuinely live; 401/403 mean
    it is gone and the stored credentials must be used. Anything else (a network
    blip, a 500) is treated as NOT live — signing in again is cheap and always
    safe, whereas continuing on a dead session fails the whole run.

    `api_base` is where the tenant's `mobilews/...` endpoints live, which is NOT
    always derivable from where you sign in: DFCU's sign-in widget is on the
    marketing site (www.dfcufinancial.com) and relays into the banking app on a
    different host entirely.
    """
    if session_token(page) is None:
        return False
    root = api_base.rstrip("/")
    try:
        resp = page.request.get(
            f"{root}/mobilews/accounts",
            headers=api_headers(page, referer=root),
            timeout=15_000,
        )
        return resp.status == 200
    except Exception:
        return False


def api_root(portal_url: str) -> str:
    """Where a tenant's `mobilews/...` endpoints live.

    The SPA entry page is a FILE sitting in the app directory
    (`.../dfcufinancialonline/uux.aspx`), and the API sits beside it, not under
    it. Keeping the filename builds `.../uux.aspx/mobilews/accounts`, which 404s
    — and a probe that always fails would report every session dead and re-login
    on every single run.

    Only a final segment that looks like a file is dropped; a tenant's own path
    prefix is never ours to guess at.
    """
    parsed = urlparse(preferred_login_url(portal_url))
    segments = [s for s in parsed.path.split("/") if s]
    # Only a final segment that looks like a file, and only when there is a real
    # path to trim. Splitting on the last "/" of the whole URL would cut a bare
    # host down to "https:/".
    if segments and "." in segments[-1]:
        segments = segments[:-1]
    return urlunparse(parsed._replace(path="/" + "/".join(segments) if segments else ""))


# Ordered most-specific first. Q2 tenants ship two shapes of sign-in: the app's
# own form on the banking host, and a login widget embedded in the institution's
# marketing site that relays into it. Both are legitimate front doors, and which
# one a source is pointed at is the operator's choice, not ours.
_LOGIN_ID_SELECTORS = (
    "#loginid",
    "input[name='loginid']",
    "#UserID",
    "input[name='UserID']",
    "input[autocomplete='username']",
)
_PASSWORD_SELECTORS = (
    "#password",
    "input[name='password']",
    "input[type='password']",
)
_SUBMIT_SELECTORS = (
    # Verified against DFCU's live widget: class is
    # "btn btn-primary btn-lg btn-block mt-5 btn-login", text "Log In".
    "button.btn-login",
    "button[type='submit']",
    "input[type='submit']",
)
_REMEMBER_SELECTORS = (
    "#rememberme",
    "input[name='rememberme']",
)

# Wording Q2 uses when it wants a one-time code. Matched case-insensitively
# against the visible page.
_ACCESS_CODE_HINTS = (
    "access code",
    "one-time",
    "verification code",
    "secure access code",
)


def _first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int):
    """The first of `selectors` to become visible, or None if none does.

    Deliberately knows nothing about sign-in state. It used to stop early when a
    session token appeared, which was a shortcut for "the form will never show
    up now" — but `looks_authenticated` is now defined in terms of THIS function
    (the form is the honest signal, not the cookie), and the two calling each
    other is unbounded recursion. Waiting out the timeout is the correct
    behaviour anyway: the caller passes a short probe when it can't afford to.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=500)
                return locator
            except Exception:
                continue
    return None


def _challenged_for_access_code(page: Page) -> bool:
    return any(hint in _page_text(page) for hint in _ACCESS_CODE_HINTS)


# What the institution's bot detection says when it refuses the automated
# browser. Both halves must match: "access denied" alone appears on ordinary
# permission errors, and this must not turn one of those into "go sign in".
_BLOCKED_HINTS = (
    ("access denied", "unauthorized to access this resource"),
    ("request unsuccessful", "incapsula"),
    ("attention required", "cloudflare"),
)


def _blocked_by_automation_defence(page: Page) -> bool:
    """Has the institution refused the automated browser itself?

    Distinct from every other sign-in failure because the remedy is distinct: no
    password, selector, or retry changes it. Reported as its own error so the
    operator is not sent to re-check a credential that is correct — which is what
    the generic "sign-in did not complete, check the stored credentials" message
    did, for the one failure a credential cannot fix.
    """
    text = _page_text(page)
    return any(all(part in text for part in hints) for hints in _BLOCKED_HINTS)


def _page_text(page: Page) -> str:
    try:
        return (page.locator("body").inner_text(timeout=2_000) or "").lower()
    except Exception:
        return ""


def login(page: Page, portal_url: str, service_key: str, timeout_ms: int = 45_000,
          api_base: str | None = None) -> None:
    """Sign in to a Q2-powered banking portal using stored credentials.

    `service_key` is the key the credentials were stored under (Settings →
    Sign-ins, or scripts/manage_secrets.py).

    Raises `Q2AccessCodeRequired` — not a generic failure — when Q2 challenges
    for a one-time code, so the caller can route the operator to a browser
    instead of telling them to check a password that was in fact correct.

    Written from a recorded sign-in against a live Q2 tenant. The password path
    is what that recording shows; the access-code BRANCH is detection only — it
    reports the challenge and stops, it does not try to answer it.
    """
    login_url = preferred_login_url(portal_url)
    progress.step("portal_open", "Open the portal")
    page.goto(login_url, timeout=timeout_ms, wait_until="domcontentloaded")
    progress.done("portal_open", details={"url": login_url})

    api_base = api_base or api_root(portal_url)
    if session_is_live(page, api_base):
        # The SERVER says the restored session still works. Nothing on this side
        # could have told us that: a dead q2token looks identical to a live one.
        progress.step("sign_in", "Reuse the saved session (already signed in)", status="success")
        return

    # A token that did NOT survive the check is a leftover from a previous run,
    # and saying so matters: the operator watching this needs to know the sign-in
    # is happening because the old session expired, not because their password
    # is being rejected.
    if session_token(page) is not None:
        progress.step("sign_in", "Saved session expired — signing in again")
    else:
        progress.step("sign_in", "Sign in")

    login_id = _first_visible(page, _LOGIN_ID_SELECTORS, timeout_ms)
    if login_id is None:
        if looks_authenticated(page):
            progress.done("sign_in")
            return
        # Say WHERE the browser actually was: "form not found" alone can't be
        # told apart from already-signed-in, still-rendering, or redirected.
        try:
            where = f"{page.url} (title: {page.title()!r})"
        except Exception:
            where = "unknown — the page could not be inspected"
        progress.failed("sign_in", error=f"sign-in form not found; browser was at {where}")
        raise Q2LoginError(
            f"Q2 sign-in form for '{service_key}' was not detected. The browser was at: "
            f"{where}. If that is not the sign-in page, the portal redirected; if it is, "
            "the form may still have been rendering or the tenant has changed it."
        )

    # Read credentials only now that a sign-in is genuinely needed — a still-good
    # saved session shouldn't depend on them being in the vault.
    #
    # `try_get` returns None for "this source has no credential" and still raises
    # if the STORE itself won't open. Those are separate problems: one is a
    # password the operator hasn't entered, the other is a vault that can't be
    # read (wrong master key, unreadable file), and telling the operator to go
    # add a password they already added is the worst of both.
    try:
        creds = CredentialStore().try_get(service_key)
    except CredentialStoreError as exc:
        progress.failed("sign_in", error="the credential store could not be read")
        raise Q2LoginError(
            f"The credential store could not be read, so '{service_key}' could not sign in. "
            f"This is NOT a missing password — the vault itself did not open: {exc}"
        ) from exc

    if not creds or not creds.get("username") or not creds.get("password"):
        progress.failed("sign_in", error="no stored username/password")
        raise Q2LoginError(
            f"No username/password stored for '{service_key}'. Add them under "
            "Settings → Sign-ins before an unattended run can sign in."
        )

    password = _first_visible(page, _PASSWORD_SELECTORS, 5_000)
    if password is None:
        progress.failed("sign_in", error="password field not found")
        raise Q2LoginError(
            f"Found the login ID field for '{service_key}' but no password field. "
            "The tenant's sign-in form has changed."
        )

    login_id.fill(creds["username"])
    password.fill(creds["password"])

    # Ask to be remembered. Q2 uses a device cookie to decide whether to
    # challenge for an access code, so this is the difference between a run that
    # can sign in unattended and one that always needs the operator.
    remember = _first_visible(page, _REMEMBER_SELECTORS, 1_000)
    if remember is not None:
        try:
            remember.check(timeout=2_000)
        except Exception:
            pass  # a checkbox we couldn't tick is not a reason to abandon the login

    submit = _first_visible(page, _SUBMIT_SELECTORS, 5_000)
    if submit is None:
        password.press("Enter")
    else:
        submit.click()

    # Settled = the SERVER accepts the session, not merely that the form went
    # away. Q2 is a SPA and the access-code challenge REPLACES the form without
    # navigating, so "the form is gone" is true of both outcomes — and a stale
    # token still sitting in the jar makes the DOM check agree with a login that
    # never happened. That combination reported success and then 403'd on the
    # very next call, which is indistinguishable from a permissions problem.
    #
    # The cheap DOM check gates the expensive one: only bother asking the server
    # once the page looks like it might have finished.
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if looks_authenticated(page, probe_ms=500) and session_is_live(page, api_base):
            progress.done("sign_in")
            return
        if _blocked_by_automation_defence(page):
            progress.failed("sign_in", error="the bank refused the automated browser")
            raise Q2AutomationBlocked(
                f"'{service_key}' refused the automated sign-in: the bank answered "
                "\"Access Denied — you are unauthorized to access this resource\". The "
                "stored username and password are NOT the problem and re-entering them "
                "will not help — the institution is declining the automated browser "
                "itself. Sign in yourself in the recovery browser; the session that "
                "creates is then reused until the bank expires it."
            )
        if _challenged_for_access_code(page):
            progress.failed("sign_in", error="Q2 asked for a one-time access code")
            raise Q2AccessCodeRequired(
                f"'{service_key}' accepted the password but asked for a one-time access "
                "code, which no stored credential can answer. Sign in once in the recovery "
                "browser and tick 'remember this device' — Q2 then stops challenging this "
                "profile and later runs can sign in on their own."
            )
        time.sleep(0.5)

    progress.failed("sign_in", error="sign-in did not complete")
    raise Q2LoginError(
        f"Sign-in for '{service_key}' did not complete within "
        f"{int(timeout_ms / 1000)}s — check the stored credentials, or sign in "
        "manually via the recovery browser."
    )
