"""Reconciliation against a source's own control totals.

The signal that answers the question a passing test and a successful run both
miss: *did we pull everything?* Both of those went green while the Epic scraper
was 403-broken, and both would go green again if a date window silently clipped
ten rows.

The property that matters most here is that "we didn't look" must never render
the same as "we looked and it balanced".
"""

import threading

import pytest

from core import reconcile
from interfaces import mcp_tools


@pytest.fixture(autouse=True)
def _clean():
    yield
    for name in ("s", "epic", "live", "a", "b"):
        reconcile.clear(name)


def test_recording_outside_a_channel_is_harmless():
    """Extraction code also runs from tests, the agent, and plain scripts."""
    assert reconcile.record("Rent", 100.0, 100.0) is True
    assert reconcile.read("nobody") == []


def test_a_balanced_check_passes():
    with reconcile.channel("s"):
        assert reconcile.record("Rent Income", 1450.00, 1450.00) is True
    assert reconcile.summary("s")["ok"] is True


def test_a_shortfall_is_caught_with_the_arithmetic_shown():
    """The operator needs the numbers, not just a red light."""
    with reconcile.channel("s"):
        reconcile.record("Rent Income", 1450.00, 1200.00)
    result = reconcile.summary("s")

    assert result["ok"] is False and result["checked"] == 1
    d = result["discrepancies"][0]
    assert d["expected"] == 1450.00 and d["actual"] == 1200.00 and d["difference"] == -250.00


def test_nothing_checked_is_not_a_pass():
    """THE point of the module. 'The source published no totals' and 'the totals
    balanced' must be distinguishable, or we've rebuilt the false green."""
    with reconcile.channel("s"):
        pass
    result = reconcile.summary("s")
    assert result["ok"] is None, "must be None, never True"
    assert result["checked"] == 0


def test_float_drift_is_tolerated_but_a_missing_cent_is_not():
    with reconcile.channel("s"):
        # accumulating hundreds of floats never lands exactly
        assert reconcile.record("drift", 1000.00, 1000.004) is True
        # a genuinely missing amount does not hide inside the tolerance
        assert reconcile.record("missing", 1000.00, 999.99) is False
    assert reconcile.summary("s")["ok"] is False


def test_one_bad_check_fails_the_whole_run():
    with reconcile.channel("s"):
        reconcile.record("a", 10.0, 10.0)
        reconcile.record("b", 20.0, 15.0)
        reconcile.record("c", 30.0, 30.0)
    result = reconcile.summary("s")
    assert result["checked"] == 3 and result["ok"] is False
    assert [d["label"] for d in result["discrepancies"]] == ["b"]


def test_unusable_numbers_are_treated_as_a_discrepancy_not_a_pass():
    with reconcile.channel("s"):
        assert reconcile.record("bad", None, 10.0) is False
    assert reconcile.summary("s")["ok"] is False


def test_reopening_a_channel_clears_the_previous_run():
    with reconcile.channel("s"):
        reconcile.record("old", 1.0, 99.0)
    with reconcile.channel("s"):
        reconcile.record("new", 1.0, 1.0)
    assert reconcile.summary("s")["ok"] is True


def test_channels_do_not_leak_across_threads():
    """Two sources scraping at once must not pollute each other's verdict."""
    done = threading.Event()

    def other():
        with reconcile.channel("b"):
            reconcile.record("b_only", 5.0, 5.0)
        done.set()

    with reconcile.channel("a"):
        threading.Thread(target=other).start()
        done.wait(timeout=2)
        reconcile.record("a_only", 1.0, 99.0)

    assert reconcile.summary("a")["ok"] is False
    assert reconcile.summary("b")["ok"] is True


# --- what run_scraper reports ------------------------------------------------

def _fake_scrape(monkeypatch, record_totals):
    from core.models import Transaction
    from datetime import datetime

    txn = Transaction(source_key="epic", date=datetime(2026, 7, 1), amount=10.0,
                      description="x", fields={"Amount": "10.00"})

    def scraper():
        record_totals()
        return [txn]

    monkeypatch.setattr(mcp_tools, "has_scraper", lambda k: True)
    monkeypatch.setattr(mcp_tools, "get_scraper", lambda k: scraper)
    monkeypatch.setattr(mcp_tools, "persist_scraped", lambda t, s: {"run_path": "data/parsed/x.json"})


def test_run_scraper_reports_a_shortfall_as_a_failed_step(monkeypatch):
    """A silent shortfall must not hide behind a green 'Run scraper'."""
    _fake_scrape(monkeypatch, lambda: reconcile.record("Rent Income", 1450.0, 1200.0))

    result = mcp_tools.run_scraper("epic")

    assert result["reconciliation"]["ok"] is False
    step = next(s for s in result["steps"] if s["key"] == "reconcile")
    assert step["status"] == "failed"
    assert "1450" in step["error"] and "1200" in step["error"]


def test_run_scraper_reports_a_clean_reconciliation(monkeypatch):
    _fake_scrape(monkeypatch, lambda: reconcile.record("Rent Income", 10.0, 10.0))

    result = mcp_tools.run_scraper("epic")

    assert result["reconciliation"]["ok"] is True
    assert next(s for s in result["steps"] if s["key"] == "reconcile")["status"] == "success"


def test_run_scraper_blames_the_scraper_not_the_source_when_nothing_was_checked(monkeypatch):
    """A scraper that records no totals must not look reconciled — and the label
    must not claim the SOURCE publishes none, which the harness cannot know.
    Saying so misreads as "nothing to check here", i.e. a pass."""
    _fake_scrape(monkeypatch, lambda: None)

    result = mcp_tools.run_scraper("epic")

    assert result["reconciliation"]["ok"] is None
    step = next(s for s in result["steps"] if s["key"] == "reconcile")
    assert step["status"] == "pending"
    assert "this scraper records no control totals" in step["label"]
    assert "published" not in step["label"], "don't assert anything about the source"
