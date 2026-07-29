"""How the recovery browser's wait loop decides the operator is done.

Field failure this guards: the operator logged in and QUIT the browser (Cmd-Q),
which never delivered a CDP close event, so page.is_closed() stayed False and the
worker span forever — the UI sat on "Log in, then CLOSE the browser window" with
no way out. The loop must therefore also believe OS truth (no Chromium process
left holding the profile) and must never wait without a deadline.

The loop is exercised through a fake page/context, so no real browser is needed.
"""

from pathlib import Path
import threading

import pytest

import core.tools.browser_session as bs


class _FakePage:
    """A page whose CDP close event may never arrive — the abrupt-quit case."""

    def __init__(self, closes_after=None):
        self._closes_after = closes_after
        self.checks = 0
        self.closed_pages = []

    def is_closed(self):
        self.checks += 1
        if self._closes_after is None:
            return False                       # event never delivered
        return self.checks >= self._closes_after

    def bring_to_front(self):
        pass

    def goto(self, *a, **k):
        pass


class _DeadPage(_FakePage):
    def is_closed(self):
        raise RuntimeError("Target closed")


class _FakeContext:
    def __init__(self, page):
        self._page = page
        self.pages = [page]
        self.closed = False

    def new_page(self):
        return self._page

    def add_init_script(self, *a, **k):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Drive bootstrap_login_until_window_closed against a fake browser.

    Everything is patched via monkeypatch so nothing leaks into other tests —
    `time` is a real shared module and must be restored.
    """
    state = {"pids": [4242], "killed": []}

    monkeypatch.setattr(bs, "reset_profile", lambda *a, **k: None)
    monkeypatch.setattr(bs, "_find_pids", lambda pattern: list(state["pids"]))
    monkeypatch.setattr(bs, "_kill_pids", lambda pids, exclude=None: state["killed"].extend(pids))
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)   # no real waiting

    def set_pids(fn):
        monkeypatch.setattr(bs, "_find_pids", fn)

    def set_clock(fn):
        monkeypatch.setattr(bs.time, "monotonic", fn)

    state["set_pids"] = set_pids
    state["set_clock"] = set_clock

    def run(page, max_wait_s=60.0):
        context = _FakeContext(page)

        class _PW:
            class chromium:
                @staticmethod
                def launch_persistent_context(*a, **k):
                    return context

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(bs, "sync_playwright", lambda: _PW())
        monkeypatch.setattr(bs, "_open_login_page", lambda p, u: None)
        bs.bootstrap_login_until_window_closed(
            "epic", "https://example.com", profile_root=tmp_path, max_wait_s=max_wait_s
        )
        return context

    return run, state


def test_returns_when_the_page_reports_closed(harness):
    """The happy path: the close event did arrive."""
    run, _ = harness
    page = _FakePage(closes_after=3)
    context = run(page)
    assert context.closed


def test_returns_when_the_browser_was_quit_without_a_close_event(harness, capsys):
    """THE reported bug. is_closed() never becomes True, but the Chromium holding
    this profile is gone — the operator did log in and quit, so save and exit."""
    run, state = harness
    page = _FakePage(closes_after=None)

    checks = {"n": 0}

    def pids_then_gone(pattern):
        checks["n"] += 1
        return [] if checks["n"] > 2 else [4242]

    state["set_pids"](pids_then_gone)
    context = run(page, max_wait_s=60.0)

    assert context.closed
    assert "no browser process left" in capsys.readouterr().out


def test_a_single_pgrep_miss_does_not_end_the_session_early(harness):
    """Requires TWO consecutive empty checks — one flaky pgrep must not abort a
    login the operator is still in the middle of."""
    run, state = harness
    page = _FakePage(closes_after=6)
    seq = [[4242], [], [4242], [4242], [4242], [4242], [4242], [4242]]
    state["set_pids"](lambda pattern: seq.pop(0) if seq else [4242])

    run(page, max_wait_s=60.0)

    # It kept going past the single miss and ended via the page close instead.
    assert page.checks >= 6


def test_a_lost_connection_is_treated_as_closed(harness, capsys):
    run, _ = harness
    context = run(_DeadPage())
    assert context.closed
    assert "connection lost" in capsys.readouterr().out


def test_the_process_pattern_is_absolute(tmp_path, monkeypatch):
    """THE bug: Chromium is launched with an absolute --user-data-dir (Playwright
    resolves whatever it is given), so a pattern built from the relative path
    matched nothing, the loop read that as "the browser is gone", and the window
    the operator was about to use closed a second after it opened."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".browser_profiles" / "epic").mkdir(parents=True)

    pattern = bs._profile_pattern(Path(".browser_profiles/epic"))

    assert pattern.startswith("user-data-dir=/")
    assert str(tmp_path.resolve()) in pattern


def test_a_pattern_that_never_matches_does_not_end_the_session(harness):
    """The same failure, guarded at the loop instead of the pattern: if presence
    was never observed, absence proves nothing about the browser — it more likely
    means the pattern is wrong, so keep waiting for the close event."""
    run, state = harness
    state["set_pids"](lambda pattern: [])            # nothing ever matches
    page = _FakePage(closes_after=8)

    run(page, max_wait_s=60.0)

    assert page.checks >= 8, "it waited for the operator to close the window"


class _Tab:
    """A tab with a URL of its own, for the free-browsing wait."""

    def __init__(self, url="about:blank", closes_after=None):
        self.url = url
        self._closes_after = closes_after
        self.checks = 0

    def is_closed(self):
        self.checks += 1
        return self._closes_after is not None and self.checks >= self._closes_after


class _Tabs:
    def __init__(self, *tabs):
        self.pages = list(tabs)


def _wait_over(monkeypatch, tmp_path, context, max_wait_s=60.0):
    monkeypatch.setattr(bs, "_find_pids", lambda pattern: [4242])
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    return bs.wait_until_browsing_done(context, tmp_path, max_wait_s=max_wait_s)


def test_free_browsing_ends_when_the_last_real_tab_closes(monkeypatch, tmp_path):
    """Not when the FIRST one does — the operator opens a tab to reach the
    portal and closes the one the browser started them on."""
    first = _Tab("https://portal.example.com/login", closes_after=3)
    second = _Tab("https://portal.example.com/report", closes_after=7)

    reason = _wait_over(monkeypatch, tmp_path, _Tabs(first, second))

    assert reason == "all windows closed"
    assert second.checks >= 7, "it stayed with the tab still open"


def test_a_blank_tab_is_not_a_demonstration_that_already_ended(monkeypatch, tmp_path):
    """Chromium opens a blank tab at launch, and on macOS can spawn another when
    the last window closes. Counting blanks as real pages would end the session
    before the operator typed anything; counting them as pages-still-open would
    mean it never ends. They're neither."""
    blank = _Tab("about:blank")

    reason = _wait_over(monkeypatch, tmp_path, _Tabs(blank), max_wait_s=0.0)

    assert reason == "deadline", "it waited for them rather than declaring it over"


def test_the_context_is_closed_on_the_calling_thread(tmp_path):
    """Sync Playwright objects belong to the greenlet that created them, so
    closing from a helper thread only raises greenlet.error — and swallowing
    that meant the close never happened. It is the close that writes a
    demonstration's HAR, i.e. the best material the scraper builder gets."""
    seen = {}

    class _Ctx:
        def close(self):
            seen["thread"] = threading.get_ident()

    bs.close_context(_Ctx(), tmp_path)

    assert seen["thread"] == threading.get_ident()


def test_a_close_that_hangs_is_broken_by_killing_the_browser(monkeypatch, tmp_path):
    """close() waits for Chromium to exit; on macOS closing the last window
    doesn't quit the app, so it can wait forever. The watchdog kills the
    profile's processes, which is what lets the close return."""
    released = threading.Event()
    monkeypatch.setattr(bs, "_find_pids", lambda pattern: [4242])
    monkeypatch.setattr(bs, "_kill_pids", lambda pids, exclude=None: released.set())

    class _Ctx:
        def close(self):
            assert released.wait(timeout=5.0), "the watchdog never fired"

    bs.close_context(_Ctx(), tmp_path, timeout_s=0.1)

    assert released.is_set()


def test_it_gives_up_instead_of_waiting_forever(harness):
    """No deadline was the reason a wedged worker could hang indefinitely."""
    run, state = harness
    ticks = iter(range(0, 10_000, 10))
    state["set_clock"](lambda: next(ticks))

    with pytest.raises(RuntimeError, match="Gave up waiting"):
        run(_FakePage(closes_after=None), max_wait_s=30.0)
