"""Capturing a portal demonstration without a terminal.

The recorder used to end with input() — "press Enter when your data is on
screen". The GUI is the only front-end now, and it has no Enter to press, so the
finish signal is closing the browser window, exactly like login recovery.

That change creates one hazard worth pinning: a page can't be read once it's
closed. Everything the agent needs (the clicks, the rendered table, the URL the
operator actually reached) has to be snapshotted WHILE the window is open, or
the demonstration comes back empty and the operator is asked to do it twice.
"""

import json

import pytest

import core.tools.browser_session as bs
import core.tools.demo_recorder as dr


class _FakePage:
    """Closes after N is_closed() checks; serves changing state until then."""

    def __init__(self, closes_after=3, url="https://portal.example.com/reports"):
        self._closes_after = closes_after
        self.checks = 0
        self.url = "about:blank"
        self._target = url
        self.evaluated = 0

    def is_closed(self):
        self.checks += 1
        if self.checks == 2:
            self.url = self._target      # they navigated after the browser opened
        return self._closes_after is not None and self.checks >= self._closes_after

    def goto(self, *a, **k):
        pass

    def evaluate(self, script):
        self.evaluated += 1
        return [{"kind": "click", "text": f"Generate {self.evaluated}"}] * self.evaluated


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
def recorder(monkeypatch, tmp_path):
    """Drive record() against a fake browser, in a temp working directory."""
    monkeypatch.setattr(bs, "_find_pids", lambda pattern: [4242])
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(dr, "_extract_requests", lambda har: [{"url": "/api/transactions"}])
    monkeypatch.setattr(dr, "_page_structure",
                        lambda page: {"title": "Reports", "tables": [{"headers": ["Date", "Amount"]}]})
    monkeypatch.setattr(dr, "PROFILE_ROOT", tmp_path / "profiles")

    def run(page, url="", out_dir=None, max_wait_s=60.0):
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
    assert demo["final_page"]["title"] == "Reports"


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


def test_it_gives_up_rather_than_waiting_forever(recorder, monkeypatch):
    ticks = iter(range(0, 100_000, 100))
    monkeypatch.setattr(bs.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="Gave up waiting"):
        recorder(_FakePage(closes_after=None), max_wait_s=30.0)
