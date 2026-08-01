# Template candidate: generic (tier 1) — "a model got stuck repeating itself" has
# no client specifics. See agent-harness-template/docs/promotion-log.md.
"""Catch a model that gets stuck repeating itself INSIDE one message.

`repetition.py` watches tool calls across turns. This watches the prose of a
single turn, which is a different failure with a much larger bill.

What it is for, measured on a real build that died. Two of that run's thirteen
turns produced a 66,000-character message each. Both were the same handful of
paragraphs over and over — 915 prose lines, 149 of them distinct — and both
stopped only because they hit `max_tokens`. Nothing was wrong with the harness
and nothing looked wrong on screen: the agent was producing text the whole time.
But those two messages alone were ~33,000 tokens of the conversation, more than
the local model's entire KV budget, so the run died several turns later with an
opaque HTTP 400 about prefill memory. The repeated text is worthless — one copy
says everything sixty-three say — and it is worse than worthless in context,
because it evicts the demonstration and the code the agent still needs.

## How it decides, and why not the obvious way

The obvious test is a repeating tail block. It does not work: measured on the
real messages, the model varies what it repeats, so the period wobbles (69, 32,
17, 17, 10, 21 lines) and a fixed-period cycle finds nothing. What IS stable is
how little of the message is distinct. Across every build message on disk:

    novelty (distinct prose lines / prose lines)
      0.16  0.20  0.25  0.27  0.31  0.33   <- the six known loops
      0.64  0.85  1.00 x11                 <- everything healthy

Code fences are excluded from that count, and this is what makes the split
clean rather than marginal: a message quoting a file it just wrote repeats `)`
36 times and `try:` 13 times, which drags whole-message novelty down to 0.46 and
puts honest work in the same band as a loop. Judge the prose, leave the code
alone.

Trimming keeps the first two copies of any repeated prose line and every fenced
block verbatim, so nothing the model said only once is ever lost. Reasoning prose
is not a deliverable — files are written with write_file, never quoted into being
— so the cost of trimming it is bounded, and the cost of not trimming it is the
whole run.

A model that has started looping is also not thinking any more, so the caller
ends the run on a second occurrence rather than spending the remaining turns to
reach the same place.

Framework-free.
"""

from __future__ import annotations

from dataclasses import dataclass

# Below this a message is not worth inspecting: the point is the tens of
# thousands of characters a loop produces. The known loops were all >62,000.
MIN_TEXT_CHARS = 8000
# Too few lines and the ratio is noise — three lines, one repeated, reads as 0.67.
MIN_PROSE_LINES = 40
# Distinct prose lines over total. The observed gap is 0.33 to 0.64; sitting in
# the middle of it means neither a slightly repetitive answer nor a slightly
# varied loop lands on the wrong side by accident.
MAX_NOVELTY = 0.45
# Copies of a repeated line to keep. Two, not one: saying a thing twice is a
# writing habit, and keeping both means a trimmed message still reads naturally.
KEEP_COPIES = 2


@dataclass(frozen=True)
class Degeneration:
    """A message that got stuck, and what it cost."""

    novelty: float      # distinct prose lines / prose lines, before trimming
    lines: int          # prose lines before trimming
    dropped: int        # characters removed
    kept: int           # characters left

    def describe(self) -> str:
        return (
            f"The model got stuck repeating itself — {self.lines} lines, only "
            f"{self.novelty:.0%} of them distinct. Kept the distinct ones, dropped "
            f"{self.dropped:,} characters so they don't crowd out the rest of the run."
        )


def _is_fence(line: str) -> bool:
    return line.strip().startswith("```")


def novelty(text: str) -> tuple[float, int]:
    """(distinct prose lines / prose lines, prose line count), ignoring code."""
    seen: list[str] = []
    in_code = False
    for line in text.splitlines():
        if _is_fence(line):
            in_code = not in_code
            continue
        stripped = line.strip()
        if in_code or not stripped:
            continue
        seen.append(stripped)
    if not seen:
        return 1.0, 0
    return len(set(seen)) / len(seen), len(seen)


def collapse(text: str) -> tuple[str, Degeneration | None]:
    """Return the text with a repetition loop trimmed, plus what was found.

    Unchanged text and `None` when there is no loop — the overwhelmingly common
    case, and it must stay cheap and invisible.
    """
    if not text or len(text) < MIN_TEXT_CHARS:
        return text, None

    ratio, count = novelty(text)
    if count < MIN_PROSE_LINES or ratio > MAX_NOVELTY:
        return text, None

    counts: dict[str, int] = {}
    out: list[str] = []
    in_code = False
    for line in text.splitlines():
        if _is_fence(line):
            in_code = not in_code
            out.append(line)
            continue
        stripped = line.strip()
        if in_code or not stripped:
            out.append(line)
            continue
        counts[stripped] = counts.get(stripped, 0) + 1
        if counts[stripped] <= KEEP_COPIES:
            out.append(line)

    kept = "\n".join(out).rstrip()
    kept += (
        f"\n\n[the model repeated itself here — {count} lines, {ratio:.0%} of them "
        f"distinct. The repeats were dropped.]"
    )
    return kept, Degeneration(
        novelty=ratio,
        lines=count,
        dropped=len(text) - len(kept),
        kept=len(kept),
    )


class Restatement:
    """Tracks whether the model keeps saying the same thing turn after turn.

    `collapse` catches a loop INSIDE one message. This catches the same failure
    spread across messages, which the per-message check cannot see and which cost
    a real run: three consecutive turns each opened with the identical paragraph
    ("No log records for dfcu_financial_bank. The operator's question is the
    key…"), re-deriving the analysis instead of acting on it. Every restatement
    is paid for again on every later turn, and the run died of context.

    Deliberately compares only the OPENING of each message. Models restate their
    conclusion up front and then continue differently, so comparing whole
    messages misses it; and a shared opening is itself the signal — new thinking
    does not begin with the same two sentences three times running.
    """

    # How much of a message counts as its opening. Long enough to be distinctive,
    # short enough that a genuinely new message with a similar first line differs.
    OPENING_CHARS = 300
    # Consecutive turns with the same opening before it counts. Two is a habit;
    # three is a loop.
    STRIKES = 3

    def __init__(self) -> None:
        self._last: str | None = None
        self._run = 0

    def observe(self, text: str) -> bool:
        """Record this turn's message. True when the model is restating itself."""
        opening = " ".join((text or "").split())[: self.OPENING_CHARS]
        if not opening:
            return False
        if opening == self._last:
            self._run += 1
        else:
            self._last, self._run = opening, 1
        return self._run >= self.STRIKES


RESTATEMENT_WARNING = (
    "You have now opened three turns in a row with the same analysis. Re-stating "
    "it is not progress and it is crowding out the rest of the run. Take the next "
    "concrete action, or stop and say plainly what is blocking you."
)

LOOP_WARNING = (
    "You just repeated yourself many times over instead of acting. That text was "
    "trimmed. Do not re-state your analysis again — take the next concrete action "
    "(write the file, run the test), or stop and say plainly what is blocking you."
)
