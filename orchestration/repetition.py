# Template candidate: generic (tier 1) — "notice an agent going in circles" has
# no client specifics. See agent-harness-template/docs/promotion-log.md.
"""Notice when an agent stops getting anywhere, and say so before the turns run out.

The watchdog catches a run that goes SILENT. This catches the opposite: a run
that is busy and productive-looking while doing the same thing over and over —
writing a file it has already written byte for byte, or re-running a command that
has already answered. That is what burned a real 82,000-character build: the
agent was talking the whole time, so nothing looked wrong until the turn budget
ran out.

Two steps, deliberately, because a repeat is not always a mistake:

1. **A nudge.** The third identical call inside a short window gets a note
   appended to its own result — the agent is about to read that anyway — saying
   it has done this three times and should change approach or state what is
   blocking it. Models often can, when told.
2. **A stop.** If the same call comes back AFTER the nudge, the run ends with
   "went in circles". Continuing would only spend turns to reach the same place.

What counts as "the same call" is the tool name plus its arguments, with file
CONTENT hashed rather than compared by path: rewriting a file with different text
is progress, rewriting it with identical text is not. Reads are exempt — re-reading
a file is how anyone re-checks their work, and it changes nothing.

Framework-free.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass

# How far back a repeat still counts. Short on purpose: three identical calls
# spread over a long, otherwise productive run is coincidence, not a loop.
DEFAULT_WINDOW = 8
# Occurrences inside that window before the agent is told.
DEFAULT_STRIKES = 3

# Looking at things is not doing things — an agent re-reading a file is checking
# itself, and blocking that would make it worse, not better.
READ_ONLY_TOOLS = frozenset({"read_file", "search_files", "list_directory", "read_logs"})

NUDGE = (
    "STOP AND RE-THINK. You have now made this exact call {count} times in a row "
    "with the same result, so it is not going to tell you anything new. Either "
    "change your approach, or stop and say plainly what is blocking you and what "
    "you would need to get past it. Repeating it again will end this run."
)


@dataclass(frozen=True)
class Repetition:
    """A call that has come round again."""

    verdict: str        # "nudge" | "stop"
    tool: str           # which tool
    count: int          # how many times, inside the window
    detail: str         # the argument that identifies it, for the operator's log

    def describe(self) -> str:
        if self.verdict == "stop":
            return (f"Went in circles: {self.tool}({self.detail}) again after being told it was "
                    f"repeating. Ending the run rather than spending the remaining turns.")
        return f"Repeating itself: {self.tool}({self.detail}) {self.count} times — nudged."


def fingerprint(name: str, args: dict) -> str:
    """What makes two calls "the same".

    File content is hashed, not carried: the question is whether this write says
    anything new, and a path alone cannot answer that — the agent rewriting the
    same bytes is the clearest no-progress signal there is.
    """
    payload: dict = {}
    for key, value in sorted((args or {}).items()):
        if isinstance(value, str) and len(value) > 200:
            payload[key] = "sha1:" + hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()
        else:
            payload[key] = value
    return name + ":" + json.dumps(payload, sort_keys=True, default=str)


def _detail(name: str, args: dict) -> str:
    """The bit of the call an operator would recognise it by."""
    args = args or {}
    for key in ("path", "command"):
        value = args.get(key)
        if value:
            text = str(value)
            return text if len(text) <= 70 else text[:67] + "…"
    return ""


class RepetitionDetector:
    """Feed it every tool call; it says when the agent has stopped progressing."""

    def __init__(self, window: int = DEFAULT_WINDOW, strikes: int = DEFAULT_STRIKES) -> None:
        self.window = window
        self.strikes = strikes
        self._recent: deque[str] = deque(maxlen=window)
        self._nudged: set[str] = set()

    def observe(self, name: str, args: dict) -> Repetition | None:
        """Record a call. Returns a nudge the first time it repeats too often, a
        stop if it happens again afterwards, and None the rest of the time."""
        if name in READ_ONLY_TOOLS:
            return None

        print_ = fingerprint(name, args)
        already_nudged = print_ in self._nudged
        self._recent.append(print_)
        count = sum(1 for item in self._recent if item == print_)

        if already_nudged and count >= 2:
            # It heard the nudge and did it anyway.
            return Repetition("stop", name, count, _detail(name, args))
        if not already_nudged and count >= self.strikes:
            self._nudged.add(print_)
            return Repetition("nudge", name, count, _detail(name, args))
        return None

    def nudge_text(self, repetition: Repetition) -> str:
        return NUDGE.format(count=repetition.count)
