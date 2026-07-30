# Template candidate: generic (tier 1) — "abort a run that stopped making
# progress" has no client specifics. See agent-harness-template/docs/promotion-log.md.
"""A progress watchdog for long agent runs, calibrated to the run's own pace.

A flat timeout cannot tell a wedged run from a slow one, and picking a constant
gets it wrong in both directions. Measured on this project's real builds: the
median gap between events is ~2s, the 90th percentile is 38-65s, and a single
healthy model turn went quiet for **178s**. The socket timeout in place at the
time was 120s — below the normal working pace, so it would have killed good
turns, and it did not save the operator from a build that hung for 21 minutes.

So this watches PROGRESS instead of duration:

* every event resets the clock (`beat()`), whatever the event is;
* the allowance is not a constant. It is `max(floor, multiplier x the largest
  gap this run has already shown)`, so a slower model, a bigger prompt or a
  heavier test grows its own budget, while a run that simply stops is still cut
  off in minutes;
* an absolute wall-clock ceiling remains as a backstop, reported as such rather
  than as the primary mechanism.

On a stall the caller gets a description of what happened, and is expected to
dump the process's stacks before exiting — where a hang actually is (a socket
read, a socket write, a subprocess) is the one thing you cannot recover after
the fact, and it is what makes the next one diagnosable.

Framework-free.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# Floor for the silence allowance. 300s is ~1.7x the worst healthy gap observed
# (178s), so ordinary slowness never trips it.
DEFAULT_FLOOR_S = 300.0
# How much slack to give, relative to the slowest thing this run has done so far.
DEFAULT_MULTIPLIER = 4.0
# Backstop for a run that keeps emitting events but never finishes.
DEFAULT_CEILING_S = 30 * 60.0


@dataclass(frozen=True)
class Stall:
    """Why the watchdog fired, in terms the operator can act on."""

    reason: str          # "silence" | "ceiling"
    idle_s: float        # how long since the last sign of life
    budget_s: float      # what it was allowed
    elapsed_s: float     # how long the whole run has been going
    last_event: str      # the last thing it said, which is usually the clue

    def describe(self) -> str:
        if self.reason == "ceiling":
            return (f"Gave up after {self.elapsed_s / 60:.0f} minutes — the run kept going but "
                    f"never finished. Last thing it said: {self.last_event or '(nothing)'}")
        return (f"No sign of life for {self.idle_s / 60:.1f} minutes (allowed "
                f"{self.budget_s / 60:.1f}). Last thing it said: {self.last_event or '(nothing)'}")


class ProgressWatchdog:
    """Call `beat()` on every sign of life; `on_stall` fires when they stop.

    `on_stall` runs on the watchdog's own thread, precisely because the thread
    doing the work is the one that's stuck — it cannot be relied on to notice.
    """

    def __init__(
        self,
        on_stall: Callable[[Stall], None],
        floor_s: float | None = None,
        multiplier: float | None = None,
        ceiling_s: float | None = None,
        tick_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_stall = on_stall
        # Read the module defaults HERE, not in the signature: a default argument
        # is bound at import, so `watchdog.DEFAULT_FLOOR_S = 3` had no effect and a
        # wedge test sat there for the full five minutes proving nothing. Which
        # also means these are now tunable at runtime, as budgets should be.
        self.floor_s = DEFAULT_FLOOR_S if floor_s is None else floor_s
        self.multiplier = DEFAULT_MULTIPLIER if multiplier is None else multiplier
        self.ceiling_s = DEFAULT_CEILING_S if ceiling_s is None else ceiling_s
        self._tick_s = tick_s
        self._clock = clock

        self._lock = threading.Lock()
        self._started_at = clock()
        self._last_beat = self._started_at
        self._largest_gap = 0.0
        self._last_event = ""
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- the signal ---------------------------------------------------------

    def beat(self, event: str = "") -> None:
        """A sign of life. The gap it closes also teaches the budget."""
        now = self._clock()
        with self._lock:
            gap = now - self._last_beat
            if gap > self._largest_gap:
                self._largest_gap = gap
            self._last_beat = now
            if event:
                self._last_event = event

    def budget_s(self) -> float:
        """How much silence is allowed right now, given how this run has behaved."""
        with self._lock:
            return max(self.floor_s, self.multiplier * self._largest_gap)

    def idle_s(self) -> float:
        with self._lock:
            return self._clock() - self._last_beat

    def elapsed_s(self) -> float:
        with self._lock:
            return self._clock() - self._started_at

    # --- running ------------------------------------------------------------

    def check(self) -> Stall | None:
        """One evaluation. Returns the stall if this is the moment it fired."""
        with self._lock:
            if self._fired:
                return None
            now = self._clock()
            idle = now - self._last_beat
            elapsed = now - self._started_at
            budget = max(self.floor_s, self.multiplier * self._largest_gap)
            reason = ""
            if idle > budget:
                reason = "silence"
            elif elapsed > self.ceiling_s:
                reason = "ceiling"
            if not reason:
                return None
            self._fired = True
            stall = Stall(reason=reason, idle_s=idle, budget_s=budget,
                          elapsed_s=elapsed, last_event=self._last_event)
        return stall

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._watch, name="progress-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(self._tick_s):
            stall = self.check()
            if stall is not None:
                self._on_stall(stall)
                return
