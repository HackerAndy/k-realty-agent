"""Login recovery: why a human is needed, and never claiming progress forever.

Two failures this guards against, both seen in the field:
  1. A browser opening with no stated reason — the black box the project forbids.
  2. A wedged worker (alive, no browser) reported as "running" indefinitely, so
     the UI spun on "Log in, then CLOSE the browser window" with no way out.
"""

import subprocess

import pytest

from interfaces import mcp_tools
from core.tools.service_manifest import Service


# --- why a human is needed ---------------------------------------------------

@pytest.mark.parametrize(
    "message, expected",
    [
        ("Login did not complete — a 2FA prompt appeared", "two_factor"),
        ("Enter the verification code we sent", "two_factor"),
        ("Please complete the reCAPTCHA to continue", "captcha"),
        ("Incorrect password for this account", "bad_credentials"),
        ("Login form for 'epic' was not detected", "login_form_not_found"),
        ("Timeout 45000ms exceeded waiting for locator", "page_timeout"),
        ("401 Unauthorized", "session_expired"),
        ("your session has expired", "session_expired"),
        ("the flux capacitor fell off", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_auth_failure(message, expected):
    result = mcp_tools.classify_auth_failure(message)
    assert result["reason"] == expected
    assert result["explanation"], "every reason must say what to do about it"


def test_specific_reasons_beat_the_generic_session_pattern():
    """'session'/'sign-in' appear in almost every auth error, so the broad pattern
    must not swallow a 2FA or CAPTCHA message that also mentions signing in."""
    assert mcp_tools.classify_auth_failure(
        "Sign-in blocked: enter your one-time code"
    )["reason"] == "two_factor"
    assert mcp_tools.classify_auth_failure(
        "Could not sign in — captcha required"
    )["reason"] == "captcha"


def test_unknown_reason_does_not_invent_a_cause():
    text = mcp_tools.classify_auth_failure("weird error")["explanation"].lower()
    assert "couldn't tell why" in text


def test_missing_form_does_not_assert_the_portal_was_redesigned():
    """Reported from the field: the operator was told the login form had changed
    when it hadn't. "Form not found" has likelier causes; don't over-claim."""
    text = mcp_tools.classify_auth_failure(
        "Login form for 'epic' was not detected"
    )["explanation"].lower()
    assert "hadn't finished rendering" in text or "already signed in" in text
    assert "less likely" in text, "a redesign must be hedged, not asserted"


def test_the_real_portal_errors_classify_sensibly():
    """Pin the two messages buildium_owner_portal actually raises."""
    from core.tools import buildium_owner_portal  # noqa: F401  (documents the source)

    did_not_complete = (
        "Login for 'epic' did not complete — check stored credentials, or complete a "
        "2FA/verification prompt manually via browser_session.bootstrap_login()."
    )
    assert mcp_tools.classify_auth_failure(did_not_complete)["reason"] == "two_factor"

    not_detected = (
        "Login form for 'epic' was not detected. The browser was at: "
        "https://example.com/dashboard (title: 'Dashboard')."
    )
    assert mcp_tools.classify_auth_failure(not_detected)["reason"] == "login_form_not_found"


@pytest.fixture
def portal_source(monkeypatch):
    monkeypatch.setattr(
        mcp_tools, "_load_services",
        lambda: [Service(key="epic", label="Epic", login_url="https://example.com")],
    )
    mcp_tools._LOGIN_RECOVERY_PROCS.clear()
    mcp_tools._LOGIN_RECOVERY_META.clear()


class _Proc:
    pid = 999

    def __init__(self, code=None):
        self._code = code
        self.terminated = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._code = 0

    def wait(self, timeout=None):
        return self._code

    def kill(self):
        self._code = -9


def test_start_login_recovery_reports_the_reason(portal_source, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_tools, "reset_profile", lambda *a, **k: None)
    monkeypatch.setattr(mcp_tools.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.chdir(tmp_path)

    started = mcp_tools.start_login_recovery("epic", trigger_error="a 2FA prompt appeared")

    assert started["reason"] == "two_factor"
    assert "2FA" in started["explanation"] or "code" in started["explanation"]


def test_start_login_recovery_without_a_trigger_says_unknown(portal_source, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_tools, "reset_profile", lambda *a, **k: None)
    monkeypatch.setattr(mcp_tools.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.chdir(tmp_path)

    assert mcp_tools.start_login_recovery("epic")["reason"] == "unknown"


# --- wedged-worker detection -------------------------------------------------

def _track(monkeypatch, proc, started_ago, reason="two_factor"):
    monkeypatch.setitem(mcp_tools._LOGIN_RECOVERY_PROCS, "epic", proc)
    monkeypatch.setitem(mcp_tools._LOGIN_RECOVERY_META, "epic", {
        "login_url": "https://example.com",
        "log_path": "/tmp/does-not-exist.log",
        "started_at": mcp_tools.time.monotonic() - started_ago,
        "reason": reason,
        "explanation": "because",
    })


def test_status_running_while_the_browser_is_alive(monkeypatch):
    _track(monkeypatch, _Proc(None), started_ago=600)
    monkeypatch.setattr(mcp_tools, "_recovery_browser_alive", lambda key: True)

    st = mcp_tools.login_recovery_status("epic")
    assert st["status"] == "running"
    assert st["browser_running"] is True
    assert st["reason"] == "two_factor"      # the reason persists across polls


def test_status_is_stuck_when_the_worker_lives_but_no_browser_does(monkeypatch, tmp_path):
    """The exact field failure: worker alive, zero Chromium, UI spinning forever."""
    monkeypatch.chdir(tmp_path)
    _track(monkeypatch, _Proc(None), started_ago=600)
    monkeypatch.setattr(mcp_tools, "_recovery_browser_alive", lambda key: False)

    st = mcp_tools.login_recovery_status("epic")
    assert st["status"] == "stuck"
    assert "isn't running" in st["message"]
    assert next(s for s in st["steps"] if s["key"] == "launch_browser")["status"] == "failed"


def test_no_browser_yet_is_still_running_inside_the_grace_period(monkeypatch):
    """Chromium takes a moment to appear; don't call a healthy launch stuck."""
    _track(monkeypatch, _Proc(None), started_ago=5)
    monkeypatch.setattr(mcp_tools, "_recovery_browser_alive", lambda key: False)

    assert mcp_tools.login_recovery_status("epic")["status"] == "running"


def test_status_completed_when_the_worker_exits_cleanly(monkeypatch):
    _track(monkeypatch, _Proc(0), started_ago=60)
    st = mcp_tools.login_recovery_status("epic")
    assert st["status"] == "completed"
    assert all(s["status"] == "success" for s in st["steps"])


def test_status_idle_when_nothing_is_tracked():
    mcp_tools._LOGIN_RECOVERY_PROCS.pop("epic", None)
    assert mcp_tools.login_recovery_status("epic")["status"] == "idle"


# --- cancel ------------------------------------------------------------------

def test_cancel_kills_the_worker_and_releases_the_profile(monkeypatch):
    proc = _Proc(None)
    _track(monkeypatch, proc, started_ago=600)
    released = {}
    monkeypatch.setattr(mcp_tools, "reset_profile", lambda key: released.setdefault("key", key))

    result = mcp_tools.cancel_login_recovery("epic")

    assert result["cancelled"] is True and proc.terminated
    assert released["key"] == "epic"      # else a retry can't launch: profile locked
    assert "epic" not in mcp_tools._LOGIN_RECOVERY_PROCS


def test_cancel_force_kills_a_worker_that_ignores_terminate(monkeypatch):
    class Stubborn(_Proc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)

    proc = Stubborn(None)
    _track(monkeypatch, proc, started_ago=600)
    monkeypatch.setattr(mcp_tools, "reset_profile", lambda key: None)

    mcp_tools.cancel_login_recovery("epic")
    assert proc.poll() == -9


def test_cancel_when_nothing_is_running_is_not_an_error():
    mcp_tools._LOGIN_RECOVERY_PROCS.pop("epic", None)
    assert mcp_tools.cancel_login_recovery("epic")["cancelled"] is False
