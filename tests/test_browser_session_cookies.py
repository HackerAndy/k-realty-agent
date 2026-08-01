"""Why a saved login stops surviving, and what keeps it alive.

Field failure this guards: the operator signed in to the bank, the run finished,
and the very next run asked them to sign in again — every time, forever.

The persistent profile was working exactly as designed. Chromium writes cookies
that carry an expiry to its on-disk store and keeps SESSION cookies in memory
only, dropping them when the process exits. That is correct browser behaviour
and precisely the wrong half to lose: the cookie a portal issues at login
usually has no expiry. The profile ended up holding all seventeen of the bank's
tracking and device cookies and none of the one that meant "signed in".

So the harness keeps that half itself. These tests drive fake contexts — no
browser needed.
"""

import json
import os
import time

import core.tools.browser_session as bs

SESSION = {"name": "q2token", "value": "abc123", "domain": "online.example.com",
           "path": "/", "expires": -1, "httpOnly": True, "secure": True,
           "sameSite": "Lax"}
PERSISTENT = {"name": "_ga", "value": "GA1.2", "domain": ".example.com",
              "path": "/", "expires": 1_800_000_000.0, "httpOnly": False,
              "secure": False, "sameSite": "Lax"}


class _FakeContext:
    def __init__(self, cookies=(), readable=True):
        self._cookies = list(cookies)
        self._readable = readable
        self.added = []

    def cookies(self):
        if not self._readable:
            raise RuntimeError("Target page, context or browser has been closed")
        return list(self._cookies)

    def add_cookies(self, cookies):
        self.added.extend(cookies)


def test_the_login_survives_into_the_next_run(tmp_path):
    """The whole point: sign in once, and the next run is already signed in."""
    saved = bs.save_session_cookies(_FakeContext([SESSION]), tmp_path)
    assert saved == 1

    context = _FakeContext()
    assert bs.restore_session_cookies(context, tmp_path) == 1
    assert context.added[0]["name"] == "q2token"
    assert context.added[0]["value"] == "abc123"


def test_only_the_half_chromium_drops_is_kept(tmp_path):
    """Cookies with an expiry are already in the profile; duplicating them here
    would mean two stores disagreeing about the same cookie."""
    bs.save_session_cookies(_FakeContext([SESSION, PERSISTENT]), tmp_path)

    stored = json.loads((tmp_path / "session_cookies.json").read_text())

    assert [c["name"] for c in stored] == ["q2token"]


def test_a_browser_already_gone_does_not_lose_the_run(tmp_path):
    """Called from a finally and from a poll tick — after an abrupt Cmd-Q the
    context can no longer be read, and raising there would take down a run that
    had otherwise succeeded."""
    assert bs.save_session_cookies(_FakeContext(readable=False), tmp_path) == 0


def test_the_last_good_save_is_what_gets_reported(tmp_path):
    """After a Cmd-Q the final save reads nothing, but the poll tick already
    wrote a real file. Reporting 0 would tell the operator their login was lost
    when it is sitting on disk."""
    bs.save_session_cookies(_FakeContext([SESSION]), tmp_path)

    assert bs.saved_session_cookie_count(tmp_path) == 1


def test_nothing_saved_yet_is_not_an_error(tmp_path):
    assert bs.restore_session_cookies(_FakeContext(), tmp_path) == 0
    assert bs.saved_session_cookie_count(tmp_path) == 0


def test_a_corrupt_file_is_not_fatal(tmp_path):
    """A half-written file means one more login, not a run that can't start."""
    (tmp_path / "session_cookies.json").write_text("{not json")

    assert bs.restore_session_cookies(_FakeContext(), tmp_path) == 0
    assert bs.saved_session_cookie_count(tmp_path) == 0


def test_a_cookie_without_a_legal_samesite_is_still_restored(tmp_path):
    """add_cookies rejects anything outside Strict/Lax/None, and a cookie that
    never declared one comes back as "" — dropping the whole cookie over that
    would silently cost the session it was carrying."""
    (tmp_path / "session_cookies.json").write_text(
        json.dumps([{**SESSION, "sameSite": ""}])
    )

    context = _FakeContext()
    assert bs.restore_session_cookies(context, tmp_path) == 1
    assert "sameSite" not in context.added[0]


def test_the_saved_session_is_not_world_readable(tmp_path):
    """It is a live credential for as long as the portal honours it."""
    bs.save_session_cookies(_FakeContext([SESSION]), tmp_path)

    mode = (tmp_path / "session_cookies.json").stat().st_mode

    assert mode & 0o077 == 0


# --- a cookie too old to be worth restoring ---------------------------------
#
# Session cookies have no expiry of their own, so restoring one makes it immortal
# LOCALLY while the server expired it long ago. That is how a dead `q2token` came
# back from disk, passed a "cookie present means signed in" check, and produced a
# 403 that read like a permissions problem. Verifying the session is the real fix
# (q2_online_banking.looks_authenticated); this is the backstop for cookies that
# cannot possibly still be good.

def _age_file(path, seconds_old):
    when = time.time() - seconds_old
    os.utime(path, (when, when))


def test_a_stale_cookie_file_is_not_restored(tmp_path):
    bs.save_session_cookies(_FakeContext([SESSION]), tmp_path)
    _age_file(tmp_path / "session_cookies.json", bs.MAX_RESTORE_AGE_S + 60)

    context = _FakeContext()
    assert bs.restore_session_cookies(context, tmp_path) == 0
    assert context.added == []


def test_a_recent_cookie_file_is_still_restored(tmp_path):
    bs.save_session_cookies(_FakeContext([SESSION]), tmp_path)
    _age_file(tmp_path / "session_cookies.json", bs.MAX_RESTORE_AGE_S - 600)

    context = _FakeContext()
    assert bs.restore_session_cookies(context, tmp_path) == 1
    assert context.added[0]["name"] == "q2token"


def test_the_age_limit_spans_an_overnight_run():
    """A scheduled run hours later must still get the session back. This limit is
    a backstop against certainly-dead cookies, not a session policy."""
    assert bs.MAX_RESTORE_AGE_S >= 8 * 60 * 60
