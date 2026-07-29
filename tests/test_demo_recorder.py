"""Capturing a portal demonstration without a terminal.

The recorder used to end with input() — "press Enter when your data is on
screen". The GUI is the only front-end now, and it has no Enter to press, so the
finish signal is closing the browser window, exactly like login recovery.

That change creates two hazards worth pinning:

* A page can't be read once it's closed. Everything the agent needs (the clicks,
  the rendered table, the URL the operator actually reached) has to be
  snapshotted WHILE the window is open, or the demonstration comes back empty
  and the operator is asked to do it twice.
* The demonstration is not one tab. The operator opens a new tab to reach the
  portal, or the portal opens its report in one; tying the session's life to the
  tab the recorder happened to hold ended demonstrations seconds after they
  began ("finished: page closed" while the browser was still on screen).
"""

import json

import pytest

import core.tools.browser_session as bs
import core.tools.demo_recorder as dr


class _FakePage:
    """A tab that becomes real at `opens_at` and closes at `closes_after`.

    Poll number is counted by is_closed(), which the wait loop calls once per
    page per poll. A tab still showing about:blank is not a page the operator
    is working in — the recorder must ignore it, not finish on it.
    """

    def __init__(self, url="https://portal.example.com/reports",
                 opens_at=2, closes_after=None, visible=True):
        self._target = url
        self._opens_at = opens_at
        self._closes_after = closes_after
        self.visible = visible
        self.url = "about:blank"
        self.checks = 0
        self.evaluated = 0

    def is_closed(self):
        self.checks += 1
        if self.checks >= self._opens_at:
            self.url = self._target
        return self._closes_after is not None and self.checks >= self._closes_after

    def goto(self, *a, **k):
        pass

    def evaluate(self, script):
        if "visibilityState" in script:
            return "visible" if self.visible else "hidden"
        self.evaluated += 1
        return [{"kind": "click", "text": f"Generate {self.evaluated}"}] * self.evaluated


class _FakeContext:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def add_init_script(self, *a, **k):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """Drive record() against a fake browser, in a temp working directory."""
    monkeypatch.setattr(bs, "_find_pids", lambda pattern: [4242])
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(dr, "_extract_requests", lambda har: [{"url": "/api/transactions"}])
    monkeypatch.setattr(dr, "_page_structure",
                        lambda page: {"title": f"Reports {page.url}",
                                      "tables": [{"headers": ["Date", "Amount"]}]})
    monkeypatch.setattr(dr, "PROFILE_ROOT", tmp_path / "profiles")

    def run(*pages, url="", out_dir=None, max_wait_s=60.0):
        context = _FakeContext(*pages)

        class _PW:
            class chromium:
                @staticmethod
                def launch_persistent_context(*a, **k):
                    return context

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(dr, "sync_playwright", lambda: _PW())
        path = dr.record("portal", url, out_dir or (tmp_path / "demos"), max_wait_s=max_wait_s)
        return json.loads(path.read_text()), context

    return run


def test_closing_the_window_finishes_the_recording(recorder):
    """It must not wait on stdin — under pytest, input() would raise outright,
    which is exactly the regression this guards."""
    demo, context = recorder(_FakePage(closes_after=3))

    assert context.closed, "the context is closed so the HAR is flushed"
    assert demo["candidate_requests"] == [{"url": "/api/transactions"}]


def test_the_page_is_captured_while_it_is_still_open(recorder):
    """Read it after the close and there's nothing left to read."""
    demo, _ = recorder(_FakePage(closes_after=4))

    assert demo["recorded_actions"], "the operator's clicks survived the close"
    assert demo["final_page"]["title"].startswith("Reports")


def test_the_longest_run_of_clicks_wins(recorder):
    """A hard navigation resets the in-page action array; taking the newest
    snapshot blindly would throw away everything they did before it."""
    page = _FakePage(closes_after=5)
    demo, _ = recorder(page)

    assert len(demo["recorded_actions"]) == max(1, page.evaluated)


def test_it_learns_the_url_instead_of_asking_for_one(recorder):
    """The wizard has no URL field: whatever they navigated to is the answer."""
    demo, _ = recorder(_FakePage(closes_after=4), url="")

    assert demo["final_url"] == "https://portal.example.com/reports"
    assert demo["start_url"] == "https://portal.example.com/reports"
    assert "about:blank" not in demo["visited_urls"]


def test_a_given_url_is_still_honoured(recorder):
    demo, _ = recorder(_FakePage(closes_after=3), url="https://portal.example.com/login")
    assert demo["start_url"] == "https://portal.example.com/login"


def test_a_snapshot_that_blows_up_does_not_end_the_session(recorder, monkeypatch):
    """A transient evaluate() failure mid-navigation is normal; ending the
    operator's demonstration over one would not be."""
    monkeypatch.setattr(dr, "_page_structure",
                        lambda page: (_ for _ in ()).throw(RuntimeError("navigating")))
    page = _FakePage(closes_after=4)

    demo, _ = recorder(page)

    assert page.checks >= 4, "it kept polling"
    assert demo["final_page"] == {}


def test_closing_the_first_tab_does_not_end_the_demonstration(recorder):
    """THE reported bug: 'finished: page closed' arrived while the browser was
    still open, because the operator had opened a second tab (to reach the
    portal, or the portal opened its report in one) and closed the first."""
    first = _FakePage(url="https://portal.example.com/login", closes_after=4)
    second = _FakePage(url="https://portal.example.com/gl-report",
                       opens_at=3, closes_after=8)

    demo, _ = recorder(first, second)

    assert second.checks >= 8, "the session lived as long as the tab in use"
    assert demo["final_url"] == "https://portal.example.com/gl-report"
    assert demo["final_page"]["title"].endswith("gl-report"), "it read the tab they were in"


def test_the_tab_in_front_is_the_one_that_counts(recorder):
    """With two tabs open, the data is on the one they're looking at."""
    background = _FakePage(url="https://portal.example.com/login",
                           closes_after=6, visible=False)
    foreground = _FakePage(url="https://portal.example.com/gl-report",
                           closes_after=6, visible=True)

    demo, _ = recorder(background, foreground)

    assert demo["final_url"] == "https://portal.example.com/gl-report"


def test_clicks_from_every_tab_reach_the_agent(recorder):
    """The demonstration is what they did, wherever they did it."""
    first = _FakePage(url="https://portal.example.com/login", closes_after=5)
    second = _FakePage(url="https://portal.example.com/gl-report",
                       opens_at=2, closes_after=5)

    demo, _ = recorder(first, second)

    assert len(demo["recorded_actions"]) == first.evaluated + second.evaluated


def test_it_gives_up_rather_than_waiting_forever(recorder, monkeypatch):
    ticks = iter(range(0, 100_000, 100))
    monkeypatch.setattr(bs.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="Gave up waiting"):
        recorder(_FakePage(closes_after=None), max_wait_s=30.0)
