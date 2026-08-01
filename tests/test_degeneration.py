"""A model that gets stuck repeating itself inside ONE message.

Measured, not guessed. Two turns of a real build produced 66,000 characters each
that were the same few paragraphs over and over; together they were ~33,000
tokens — more than the local model's whole KV budget — and the run died several
turns later on prefill memory. The numbers in these tests are that run's.
"""

import pytest

from orchestration import degeneration


def _loop(block: str, times: int) -> str:
    return ("\n".join(block for _ in range(times))) + "\n"


LOOPING_PARAGRAPH = """\
OK, I think I've analyzed this enough. Let me write the fix now.

The URL is `/mobilews/accountHistory/{accountId}`. This looks like a REST API.
- Session cookies (from the main portal)
- Or a specific auth header

The error is a 403, which typically means "forbidden". This could mean:
1. No session/cookies
2. Invalid/expired session
3. Missing required headers
"""


# --- what it catches --------------------------------------------------------

def test_a_repeated_paragraph_is_caught_and_trimmed():
    text = _loop(LOOPING_PARAGRAPH, 60)
    kept, found = degeneration.collapse(text)
    assert found is not None
    assert len(kept) < len(text) / 10
    assert found.dropped > 0


def test_the_distinct_content_survives():
    """Trimming must never lose something the model said only once."""
    text = "The endpoint is /mobilews/accountHistory.\n" + _loop(LOOPING_PARAGRAPH, 60)
    kept, _ = degeneration.collapse(text)
    assert "The endpoint is /mobilews/accountHistory." in kept
    assert "Missing required headers" in kept


def test_the_trimmed_text_says_it_was_trimmed():
    kept, _ = degeneration.collapse(_loop(LOOPING_PARAGRAPH, 60))
    assert "repeated itself" in kept


def test_it_survives_a_loop_cut_off_mid_paragraph():
    """max_tokens truncates the last copy, so the tail is a partial block. A
    fixed-period cycle detector missed the real messages for exactly this."""
    text = _loop(LOOPING_PARAGRAPH, 60) + "The error is a 403, which typ"
    _, found = degeneration.collapse(text)
    assert found is not None


def test_an_irregular_loop_is_still_caught():
    """The real model varied what it repeated — periods of 69, 32, 17, 10 lines."""
    text = "".join(
        LOOPING_PARAGRAPH + ("\nLet me check the headers again...\n" if i % 3 else "")
        for i in range(60)
    )
    _, found = degeneration.collapse(text)
    assert found is not None


# --- what it must NOT touch -------------------------------------------------

def test_ordinary_output_is_untouched():
    text = "\n".join(f"Step {i}: something specific and different happened here." for i in range(200))
    kept, found = degeneration.collapse(text)
    assert found is None and kept == text


def test_short_messages_are_never_inspected():
    kept, found = degeneration.collapse(_loop(LOOPING_PARAGRAPH, 3))
    assert found is None and kept == _loop(LOOPING_PARAGRAPH, 3)


def test_quoted_code_is_not_mistaken_for_a_loop():
    """A message quoting a file repeats `)` and `try:` many times. Measured, that
    drags whole-message novelty to 0.46 — into the loop band. Judging prose only
    is what keeps honest work out of it."""
    code = "\n".join([
        "    try:", "        value = fetch(row)", "    except KeyError:",
        "        raise ScrapeError(", "            log.failure(", "        )", "    )",
    ] * 40)
    text = "Here is the scraper I wrote:\n\n```python\n" + code + "\n```\n\nIt passes."
    kept, found = degeneration.collapse(text)
    assert found is None and kept == text


def test_a_fenced_block_is_never_edited_even_when_the_prose_loops():
    code = "```python\nx = 1\nx = 1\nx = 1\n```"
    text = _loop(LOOPING_PARAGRAPH, 60) + "\n" + code
    kept, found = degeneration.collapse(text)
    assert found is not None
    assert code in kept


def test_empty_text_is_safe():
    assert degeneration.collapse("") == ("", None)


# --- the measurement it is built on -----------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("a\nb\nc", 1.0),
    ("a\na\na\na", 0.25),
    ("```\na\na\na\na\n```\nb", 1.0),   # code excluded
])
def test_novelty_counts_distinct_prose_lines(text, expected):
    assert degeneration.novelty(text)[0] == pytest.approx(expected)


def test_novelty_of_the_real_loops_is_below_the_threshold():
    assert degeneration.novelty(_loop(LOOPING_PARAGRAPH, 60))[0] < degeneration.MAX_NOVELTY


# --- the same loop, spread across turns -------------------------------------
#
# `collapse` sees one message at a time. This is the failure it cannot see:
# three consecutive turns each opening with the identical paragraph ("No log
# records for dfcu_financial_bank. The operator's question is the key…"),
# re-deriving the analysis instead of acting on it. Every restatement is paid for
# again on every later turn, and that run died of context.

ANALYSIS = (
    "No log records for `dfcu_financial_bank`. The operator's question is the key: "
    "**\"is this reconciling automatically?\"** — **No.** The scraper has zero "
    "reconciliation calls, so nothing checks the extraction against the bank's own "
    "numbers. Let me look at how epic does it before deciding what to write here."
)


def test_three_turns_of_the_same_analysis_is_caught():
    r = degeneration.Restatement()
    assert r.observe(ANALYSIS) is False
    assert r.observe(ANALYSIS) is False      # twice is a habit
    assert r.observe(ANALYSIS) is True       # three times is a loop


def test_it_keeps_firing_while_the_model_keeps_restating():
    r = degeneration.Restatement()
    for _ in range(3):
        r.observe(ANALYSIS)
    assert r.observe(ANALYSIS) is True


def test_a_different_turn_resets_it():
    r = degeneration.Restatement()
    r.observe(ANALYSIS)
    r.observe(ANALYSIS)
    r.observe("Right — writing the reconcile.record() call now.")
    assert r.observe(ANALYSIS) is False


def test_only_the_opening_is_compared():
    """Models restate the conclusion up front and then continue differently;
    comparing whole messages would miss exactly the case this exists for."""
    r = degeneration.Restatement()
    for i in range(3):
        fired = r.observe(ANALYSIS + f"\n\nNow let me try approach {i}.")
    assert fired is True


def test_a_genuinely_different_opening_is_not_a_restatement():
    r = degeneration.Restatement()
    for i in range(5):
        assert r.observe(f"Turn {i}: something specific and new happened here, at length.") is False


def test_whitespace_differences_do_not_disguise_a_restatement():
    r = degeneration.Restatement()
    r.observe(ANALYSIS)
    r.observe("  " + ANALYSIS.replace(". ", ".  "))
    assert r.observe(ANALYSIS + "   ") is True


def test_empty_turns_are_ignored():
    """A tool-only turn says nothing and must not count as agreeing with itself."""
    r = degeneration.Restatement()
    for _ in range(5):
        assert r.observe("") is False
