"""The watchdog that ends a wedged build — without ending a slow one.

The field failure: a build sat for 21 minutes holding an open socket to the
operator's model, emitting nothing, with no way to stop it from the app. The
obvious fix — a timeout — is wrong here, and measurably so. On this project's
real runs the median gap between events is ~2s, the 90th percentile is 38-65s,
and one healthy model turn went quiet for 178s; the socket timeout in place was
120s, i.e. already below the normal working pace.

So these tests pin the distinction the watchdog exists to make: silence that
means "wedged" versus silence that means "thinking".
"""

import pytest

from orchestration.watchdog import DEFAULT_FLOOR_S, ProgressWatchdog, Stall


class _Clock:
    """A hand-cranked clock, so these tests take no real time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _dog(clock, **kw):
    fired: list[Stall] = []
    dog = ProgressWatchdog(on_stall=fired.append, clock=clock, **kw)
    return dog, fired


def test_a_slow_turn_is_not_a_stall():
    """178s of silence was a real, healthy model turn on the operator's setup.
    Anything that kills that is worse than the bug it's fixing."""
    clock = _Clock()
    dog, _ = _dog(clock)

    clock.advance(178.0)

    assert dog.check() is None
    assert dog.idle_s() == 178.0


def test_silence_past_the_budget_is_a_stall():
    clock = _Clock()
    dog, _ = _dog(clock)

    clock.advance(DEFAULT_FLOOR_S + 1)
    stall = dog.check()

    assert stall is not None and stall.reason == "silence"
    assert "No sign of life" in stall.describe()


def test_the_budget_learns_from_the_run_s_own_slowest_moment():
    """The point of not using a constant: a run that has already shown a 200s
    turn is allowed a longer next one, without anyone tuning a number."""
    clock = _Clock()
    dog, _ = _dog(clock, floor_s=300.0, multiplier=4.0)

    assert dog.budget_s() == 300.0          # nothing observed yet: the floor
    clock.advance(200.0)
    dog.beat("a slow turn came back")       # a 200s gap, healthy

    assert dog.budget_s() == 800.0, "4x the slowest thing it has actually done"

    clock.advance(700.0)
    assert dog.check() is None, "still inside the earned budget"
    clock.advance(200.0)
    assert dog.check() is not None


def test_every_beat_resets_the_clock():
    clock = _Clock()
    dog, _ = _dog(clock, floor_s=100.0, multiplier=1.0)

    for _ in range(10):
        clock.advance(90.0)
        dog.beat("still going")
        assert dog.check() is None, "a run making progress is never cut off"


def test_a_run_that_never_finishes_hits_the_ceiling():
    """Events keep coming, so silence never trips — but a build cannot run all
    day either. Reported as the backstop it is, not as a stall."""
    clock = _Clock()
    dog, _ = _dog(clock, ceiling_s=600.0, floor_s=300.0)

    for _ in range(30):
        clock.advance(30.0)
        dog.beat("busy")

    stall = dog.check()
    assert stall is not None and stall.reason == "ceiling"
    assert "never finished" in stall.describe()


def test_it_fires_once_and_only_once():
    """The handler kills the process; being called twice would race with that."""
    clock = _Clock()
    dog, _ = _dog(clock)

    clock.advance(DEFAULT_FLOOR_S * 2)

    assert dog.check() is not None
    assert dog.check() is None


def test_the_stall_carries_the_last_thing_it_said():
    """Which is the actual clue: "asking the model (turn 12)" versus "running
    pytest" are different bugs with different fixes."""
    clock = _Clock()
    dog, _ = _dog(clock)
    dog.beat("Asking the model (turn 12)")

    clock.advance(DEFAULT_FLOOR_S + 1)
    stall = dog.check()

    assert stall.last_event == "Asking the model (turn 12)"
    assert "turn 12" in stall.describe()


def test_the_thread_calls_the_handler_when_the_run_goes_quiet():
    """on_stall runs on the watchdog's thread ON PURPOSE — the thread doing the
    work is the one that's stuck, so it cannot be asked to notice."""
    clock = _Clock()
    dog, fired = _dog(clock, floor_s=1.0, tick_s=0.01)
    dog.start()
    try:
        clock.advance(5.0)
        for _ in range(200):                      # ~2s of ticks, no real sleep loop
            if fired:
                break
            import time as _t
            _t.sleep(0.01)
    finally:
        dog.stop()

    assert fired and fired[0].reason == "silence"


def test_beats_from_several_threads_do_not_corrupt_the_budget():
    """Events arrive from the agent loop while the watchdog reads the same state."""
    import threading

    clock = _Clock()
    dog, _ = _dog(clock)

    def hammer():
        for _ in range(500):
            dog.beat("tick")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert dog.idle_s() == 0.0
    assert dog.budget_s() == DEFAULT_FLOOR_S


@pytest.mark.parametrize("reason", ["silence", "ceiling"])
def test_every_stall_can_explain_itself_to_an_operator(reason):
    stall = Stall(reason=reason, idle_s=400.0, budget_s=300.0, elapsed_s=1900.0,
                  last_event="Asking the model (turn 3)")
    text = stall.describe()
    assert text and not text.endswith(":")
    assert "turn 3" in text


def test_the_budgets_can_be_changed_at_runtime(monkeypatch):
    """They were baked in at import, because they were default ARGUMENTS — so
    setting the module value did nothing, and a wedge test silently waited the
    full five minutes proving nothing. Operators and tests both need them tunable."""
    from orchestration import watchdog

    monkeypatch.setattr(watchdog, "DEFAULT_FLOOR_S", 7.0)
    monkeypatch.setattr(watchdog, "DEFAULT_CEILING_S", 99.0)

    dog = watchdog.ProgressWatchdog(on_stall=lambda s: None)

    assert dog.floor_s == 7.0 and dog.ceiling_s == 99.0
