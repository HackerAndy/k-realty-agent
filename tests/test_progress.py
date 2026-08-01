"""The live progress channel — what's happening RIGHT NOW, not after the fact.

Field complaint this answers: "Run scraper" sat for 30-60s as a single step while
it was really launching a browser, signing in, and calling two API endpoints —
indistinguishable from a hang. The channel makes those phases readable WHILE the
operation runs, which means the reader is on a different thread from the writer
(FastAPI serves sync endpoints from a threadpool), so thread isolation is the
property that actually matters here.
"""

import threading
import time

from core import progress
from interfaces import mcp_tools


def test_writers_are_noops_with_no_channel_open():
    """Scrapers also run from tests, the agent, and plain scripts."""
    progress.step("x", "X")          # must not raise
    progress.done("x")
    progress.failed("y", error="e")
    assert progress.read("nobody") == []


def test_steps_are_recorded_in_order_with_durations():
    with progress.channel("s1"):
        progress.step("a", "Phase A")
        progress.done("a")
        progress.step("b", "Phase B")

    steps = progress.read("s1")
    assert [s["key"] for s in steps] == ["a", "b"]
    assert steps[0]["status"] == "success" and steps[0]["duration_ms"] >= 0
    assert steps[1]["status"] == "in-progress" and "duration_ms" not in steps[1]
    progress.clear("s1")


def test_reopening_a_channel_clears_the_previous_run():
    """Otherwise a poller sees last run's steps interleaved with this one's."""
    with progress.channel("s2"):
        progress.step("old", "Old")
    with progress.channel("s2"):
        progress.step("new", "New")
    assert [s["key"] for s in progress.read("s2")] == ["new"]
    progress.clear("s2")


def test_repeating_a_key_updates_rather_than_duplicates():
    with progress.channel("s3"):
        progress.step("a", "Fetching")
        progress.step("a", "Fetching page 2")
    steps = progress.read("s3")
    assert len(steps) == 1 and steps[0]["label"] == "Fetching page 2"
    progress.clear("s3")


def test_failed_records_the_error_and_duration():
    with progress.channel("s4"):
        progress.step("a", "Sign in")
        progress.failed("a", error="2FA required")
    step = progress.read("s4")[0]
    assert step["status"] == "failed" and step["error"] == "2FA required"
    assert "duration_ms" in step
    progress.clear("s4")


def test_read_returns_copies_so_callers_cannot_corrupt_live_state():
    with progress.channel("s5"):
        progress.step("a", "A")
        snapshot = progress.read("s5")
        snapshot[0]["label"] = "tampered"
        assert progress.read("s5")[0]["label"] == "A"
    progress.clear("s5")


def test_another_thread_can_read_while_the_operation_runs(monkeypatch):
    """THE point of the channel: the GUI polls from a different thread than the
    one running the scrape."""
    seen = []
    release = threading.Event()

    def worker():
        with progress.channel("live"):
            progress.step("launch", "Start browser session")
            release.set()
            time.sleep(0.15)          # simulate the slow phase
            progress.done("launch")
            progress.step("signin", "Sign in")
            time.sleep(0.05)
            progress.done("signin")

    t = threading.Thread(target=worker)
    t.start()
    release.wait(timeout=2)
    seen.append(progress.read("live"))     # mid-flight read from THIS thread
    t.join(timeout=3)
    seen.append(progress.read("live"))

    mid, final = seen
    assert [s["key"] for s in mid] == ["launch"]
    assert mid[0]["status"] == "in-progress", "the slow phase must be visible while running"
    assert [s["key"] for s in final] == ["launch", "signin"]
    assert all(s["status"] == "success" for s in final)
    progress.clear("live")


def test_the_active_channel_does_not_leak_across_threads():
    """Two sources scraping at once must not write into each other's channel."""
    done = threading.Event()

    def other():
        with progress.channel("thread_b"):
            progress.step("b_only", "B")
        done.set()

    with progress.channel("thread_a"):
        threading.Thread(target=other).start()
        done.wait(timeout=2)
        progress.step("a_only", "A")

    assert [s["key"] for s in progress.read("thread_a")] == ["a_only"]
    assert [s["key"] for s in progress.read("thread_b")] == ["b_only"]
    progress.clear("thread_a")
    progress.clear("thread_b")


def test_step_count_is_bounded():
    """A runaway loop must not grow the channel without limit."""
    with progress.channel("flood"):
        for i in range(progress.MAX_STEPS + 50):
            progress.step(f"k{i}", "step")
    assert len(progress.read("flood")) == progress.MAX_STEPS
    progress.clear("flood")


# --- the tool the GUI polls --------------------------------------------------

def test_action_progress_names_the_current_phase_and_its_elapsed_time():
    with progress.channel("epic"):
        progress.step("launch", "Start browser session")
        progress.done("launch")
        progress.step("signin", "Sign in")

        p = mcp_tools.action_progress("epic")

    assert p["current"] == "Sign in"
    assert p["current_elapsed_s"] is not None and p["current_elapsed_s"] >= 0
    assert [s["key"] for s in p["steps"]] == ["launch", "signin"]
    progress.clear("epic")


def test_action_progress_is_empty_and_harmless_when_nothing_runs():
    p = mcp_tools.action_progress("idle_source")
    assert p["steps"] == [] and p["current"] is None


# --- reporting progress must never be the thing that breaks the run ---------
#
# From the field: a scraper wrote `progress.done("sign_in", status="success")` —
# redundant, harmless-looking, and the natural way to say it. It raised
# `_finish() got multiple values for argument 'status'` mid-scrape with the
# browser already open, and the traceback pointed at core/progress.py, sending
# both the operator and the agent hunting in the wrong file.

def test_a_redundant_status_detail_does_not_crash_done():
    with progress.channel("collide"):
        progress.step("sign_in", "Sign in")
        progress.done("sign_in", status="success")
        steps = progress.read("collide")
    assert steps[0]["status"] == "success"
    progress.clear("collide")


def test_a_status_detail_never_outranks_the_reported_outcome():
    """`failed(..., status="success")` must still read as failed."""
    with progress.channel("collide"):
        progress.step("sign_in", "Sign in")
        progress.failed("sign_in", error="bad password", status="success")
        steps = progress.read("collide")
    assert steps[0]["status"] == "failed" and steps[0]["error"] == "bad password"
    progress.clear("collide")


def test_step_survives_details_named_like_its_own_fields():
    with progress.channel("collide"):
        progress.step("fetch", "Fetch rows", label="nope", key="nope", started_at=0)
        steps = progress.read("collide")
    assert steps[0]["label"] == "Fetch rows" and steps[0]["key"] == "fetch"
    assert steps[0]["started_at"] > 0
    progress.clear("collide")


def test_real_details_still_get_through():
    with progress.channel("collide"):
        progress.step("fetch", "Fetch rows")
        progress.done("fetch", details={"rows": 19}, rows=19)
        steps = progress.read("collide")
    assert steps[0]["rows"] == 19 and steps[0]["details"] == {"rows": 19}
    progress.clear("collide")


def test_duration_is_still_recorded_when_details_collide():
    with progress.channel("collide"):
        progress.step("fetch", "Fetch rows")
        progress.done("fetch", duration_ms=999999)
        steps = progress.read("collide")
    assert steps[0]["duration_ms"] < 999999
    progress.clear("collide")
