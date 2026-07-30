"""Noticing an agent that is busy but getting nowhere.

The watchdog catches a run that goes silent. This is the opposite failure, and the
one that actually happened: a build talked for 82,000 characters while re-treading
the same ground, because the gate kept objecting to a fix it had itself demanded.
Nothing looked wrong — events were streaming the whole time — until the turn
budget ran out.

The rule these pin: a repeat gets ONE warning, then the run ends. A nudge before a
kill, because a model told plainly that it is repeating will often change tack,
and because ending a run that might have recovered is its own kind of damage.
"""

import pytest

from orchestration.repetition import (
    DEFAULT_STRIKES,
    RepetitionDetector,
    fingerprint,
)


def _write(path="core/scrapers/epic.py", content="x = 1"):
    return "write_file", {"path": path, "content": content}


def test_doing_something_once_is_not_a_loop():
    det = RepetitionDetector()
    assert det.observe(*_write()) is None


def test_the_third_identical_call_earns_a_nudge():
    det = RepetitionDetector()

    assert det.observe("run_command", {"command": "poetry run pytest -q"}) is None
    assert det.observe("run_command", {"command": "poetry run pytest -q"}) is None
    repeat = det.observe("run_command", {"command": "poetry run pytest -q"})

    assert repeat is not None and repeat.verdict == "nudge"
    assert repeat.count == DEFAULT_STRIKES
    assert "pytest" in repeat.detail
    assert "three times" in det.nudge_text(repeat) or "3 times" in det.nudge_text(repeat)


def test_doing_it_again_after_the_nudge_stops_the_run():
    """The whole point of the two steps: the second chance is real, and it is one."""
    det = RepetitionDetector()
    for _ in range(DEFAULT_STRIKES):
        det.observe(*_write())

    repeat = det.observe(*_write())

    assert repeat is not None and repeat.verdict == "stop"
    assert "Went in circles" in repeat.describe()


def test_rewriting_a_file_with_DIFFERENT_content_is_progress():
    """Same path, new bytes — that's the agent iterating, which is the job."""
    det = RepetitionDetector()

    assert det.observe(*_write(content="version 1")) is None
    assert det.observe(*_write(content="version 2")) is None
    assert det.observe(*_write(content="version 3")) is None, "three real edits, no complaint"


def test_rewriting_a_file_with_IDENTICAL_content_is_not():
    """The clearest no-progress signal there is: the same bytes, again."""
    det = RepetitionDetector()
    same = "def retrieve():\n    return []\n" * 40      # long enough to be hashed

    det.observe("write_file", {"path": "core/scrapers/epic.py", "content": same})
    det.observe("write_file", {"path": "core/scrapers/epic.py", "content": same})
    repeat = det.observe("write_file", {"path": "core/scrapers/epic.py", "content": same})

    assert repeat is not None and repeat.verdict == "nudge"


@pytest.mark.parametrize("tool", ["read_file", "list_directory", "read_logs"])
def test_looking_at_things_repeatedly_is_never_punished(tool):
    """Re-reading a file is how anything checks its own work, and it changes
    nothing. Blocking that would make the agent worse, not better."""
    det = RepetitionDetector()
    for _ in range(20):
        assert det.observe(tool, {"path": "core/settings.py"}) is None


def test_a_repeat_long_ago_does_not_count():
    """Three identical calls spread across a long, productive run is coincidence.
    The window is short on purpose."""
    det = RepetitionDetector(window=4)

    det.observe("run_command", {"command": "pytest"})
    for i in range(4):
        det.observe("write_file", {"path": f"f{i}.py", "content": str(i)})
    det.observe("run_command", {"command": "pytest"})
    repeat = det.observe("run_command", {"command": "pytest"})

    assert repeat is None, "the first one has fallen out of the window"


def test_different_calls_are_kept_apart():
    det = RepetitionDetector()
    for i in range(10):
        assert det.observe("run_command", {"command": f"pytest tests/test_{i}.py"}) is None


def test_the_fingerprint_hashes_long_content_rather_than_carrying_it():
    """Keeping whole files in the window would grow without bound over a long run."""
    long_text = "y" * 5000
    print_ = fingerprint("write_file", {"path": "a.py", "content": long_text})

    assert long_text not in print_
    assert "sha1:" in print_
    assert print_ == fingerprint("write_file", {"path": "a.py", "content": long_text})
    assert print_ != fingerprint("write_file", {"path": "a.py", "content": long_text + "!"})


def test_argument_order_does_not_change_the_fingerprint():
    assert fingerprint("t", {"a": 1, "b": 2}) == fingerprint("t", {"b": 2, "a": 1})


def test_a_stop_explains_itself_to_the_operator():
    det = RepetitionDetector()
    for _ in range(DEFAULT_STRIKES + 1):
        last = det.observe("run_command", {"command": "poetry run pytest -q"})

    text = last.describe()
    assert "poetry run pytest" in text
    assert "Ending the run" in text


# --- wired into the loop ----------------------------------------------------

class _FakeAdapter:
    """Replays a scripted set of turns, so no model is needed."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.tool_results = []

    def init_messages(self, task):
        return []

    def next_turn(self, messages, system):
        from orchestration.agent import ProviderTurn
        return self._turns.pop(0) if self._turns else ProviderTurn([], ["done"], [])

    def append_assistant(self, messages, payload):
        pass

    def append_tool_results(self, messages, results):
        self.tool_results.extend(results)


def _turn(call_id, name, args):
    from orchestration.agent import ProviderTurn, ToolCall
    return ProviderTurn(assistant_payload=[], text_blocks=[],
                        tool_calls=[ToolCall(id=call_id, name=name, input=args)])


def test_the_loop_nudges_through_the_tool_result(monkeypatch):
    """The nudge has to ride ON the result: a separate message interleaved with
    tool_use blocks is rejected by the providers."""
    from orchestration import agent

    cmd = {"command": "poetry run pytest -q"}
    adapter = _FakeAdapter([_turn(str(i), "run_command", cmd) for i in range(3)])
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("exit=1\n1 failed", False))

    agent.run_agent("task", "system", on_event=lambda t: None, max_turns=5)

    nudged = [r for r in adapter.tool_results if "STOP AND RE-THINK" in r.content]
    assert len(nudged) == 1, "told once, not every turn"
    assert "1 failed" in nudged[0].content, "the real result is still there"


def test_the_loop_stops_when_it_keeps_going(monkeypatch):
    """And says so, instead of spending the remaining turns to arrive nowhere."""
    from orchestration import agent

    cmd = {"command": "poetry run pytest -q"}
    adapter = _FakeAdapter([_turn(str(i), "run_command", cmd) for i in range(8)])
    events = []
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("exit=1", False))

    result = agent.run_agent("task", "system", on_event=events.append, max_turns=20)

    assert "Went in circles" in result.stopped_reason
    assert result.turns == 4, "three strikes, then the one after the nudge"
    assert any("Went in circles" in e for e in events), "the operator sees it too"
    assert len(result.tool_calls) == 4, "and it did not keep calling"


def test_an_ordinary_run_is_left_alone(monkeypatch):
    from orchestration import agent

    adapter = _FakeAdapter([_turn("1", "write_file", {"path": "a.py", "content": "1"}),
                            _turn("2", "write_file", {"path": "b.py", "content": "2"})])
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("ok", False))

    result = agent.run_agent("task", "system", on_event=lambda t: None, max_turns=5)

    assert result.stopped_reason == ""
    assert not any("STOP AND RE-THINK" in r.content for r in adapter.tool_results)


def test_a_run_that_ended_itself_reaches_the_operator():
    """Through the same channel as every other refusal, so the screen needs no
    special case: whatever stopped it is named."""
    from orchestration import verify

    reasons = verify.blockers({
        "ok": False,
        "test": {"ok": True},
        "agent_stopped": "Went in circles: run_command(poetry run pytest -q) again after "
                         "being told it was repeating.",
    })

    assert any("Went in circles" in r for r in reasons)
    assert not any("test failed" in r for r in reasons), "the test passed; say what did stop it"
