"""Context accounting, and dropping the output nobody will read again.

The measurement half exists because this was guessed at once and wrongly: whole
-file reads looked like the obvious thing filling a local model's context and
turned out to be about a tenth of it, while `run_command` output nobody had
counted was most of it.

The trimming half has one hard rule, and it is what the tests below are mostly
about: **no message is ever removed.** Only the text inside a tool result
changes. Drop a message and you can orphan an assistant `tool_use` from its
`tool_result`, which providers reject outright — several turns later, with an
error that points nowhere near here.
"""

from __future__ import annotations

from orchestration.context_budget import (
    Ledger,
    TRIM_MARKER,
    collapse_stale_results,
    stub_for,
)


def _anthropic(pairs):
    """One user message carrying a tool_result block per pair, as the SDK shape."""
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": False}
        for i, text in pairs]}


def _openai(pairs):
    return [{"role": "tool", "tool_call_id": i, "content": text} for i, text in pairs]


# --- the ledger -------------------------------------------------------------

def test_the_ledger_attributes_characters_to_their_source():
    ledger = Ledger(system=100, schemas=50)
    ledger.note_prose("x" * 20)
    ledger.note_call("run_command", {"command": "y" * 30})
    ledger.note_result("run_command", "z" * 400)

    assert ledger.live_total == 100 + 50 + 20 + 30 + 400
    assert ledger.results["run_command"] == 400


def test_the_breakdown_leads_with_the_biggest_consumer():
    ledger = Ledger(system=10, schemas=10)
    ledger.note_result("read_file", "a" * 500)
    ledger.note_result("run_command", "b" * 9000)

    first = ledger.breakdown().splitlines()[0]

    assert "run_command results" in first, "the thing to fix should be at the top"


def test_trimming_is_subtracted_from_the_live_total():
    """Otherwise the number reported is what was produced, not what is being sent."""
    ledger = Ledger()
    ledger.note_result("run_command", "z" * 5000)
    messages = [_anthropic([("a", "z" * 5000)])]

    collapse_stale_results(messages, {"a": "run_command"}, ledger, keep_last=0)

    assert ledger.live_total < 500
    assert ledger.trimmed_count == 1


# --- collapsing: both message shapes ----------------------------------------

def test_an_old_result_is_replaced_in_the_anthropic_shape():
    messages = [_anthropic([("a", "out" * 500), ("b", "recent" * 200)])]

    collapse_stale_results(messages, {"a": "run_command", "b": "read_file"},
                           Ledger(), keep_last=1)

    blocks = messages[0]["content"]
    assert TRIM_MARKER in blocks[0]["content"]
    assert "run_command" in blocks[0]["content"], "the stub says what it stood in for"
    assert blocks[1]["content"].startswith("recent"), "the most recent is untouched"


def test_an_old_result_is_replaced_in_the_openai_shape():
    messages = _openai([("a", "out" * 500), ("b", "recent" * 200)])

    collapse_stale_results(messages, {"a": "run_command", "b": "read_file"},
                           Ledger(), keep_last=1)

    assert TRIM_MARKER in messages[0]["content"]
    assert messages[1]["content"].startswith("recent")


def test_no_message_is_ever_removed():
    """The pairing rule: orphan a tool_use from its tool_result and the provider
    rejects the whole conversation, several turns later."""
    messages = _openai([(str(i), "out" * 500) for i in range(5)])
    before = len(messages)

    collapse_stale_results(messages, {}, Ledger(), keep_last=0)

    assert len(messages) == before
    assert all(m["role"] == "tool" and m["tool_call_id"] for m in messages)


def test_the_assistant_payload_is_not_touched():
    """Only results are trimmed — an assistant turn carries the tool_use blocks."""
    assistant = {"role": "assistant", "content": "I will run the tests" * 100}
    messages = [assistant, *_openai([("a", "out" * 500)])]

    collapse_stale_results(messages, {}, Ledger(), keep_last=0)

    assert assistant["content"].startswith("I will run the tests")


def test_objects_that_are_not_dicts_are_stepped_over():
    """The Anthropic adapter parks SDK block objects in the conversation."""
    class _SdkBlock:
        type = "tool_use"

    messages = [{"role": "assistant", "content": [_SdkBlock()]},
                *_openai([("a", "out" * 500)])]

    collapse_stale_results(messages, {}, Ledger(), keep_last=0)  # must not raise

    assert TRIM_MARKER in messages[1]["content"]


# --- collapsing: what is left alone -----------------------------------------

def test_recent_results_are_kept_verbatim():
    messages = _openai([(str(i), f"result-{i}" + "x" * 500) for i in range(8)])

    collapse_stale_results(messages, {}, Ledger(), keep_last=3)

    assert all(TRIM_MARKER in m["content"] for m in messages[:5])
    assert all(TRIM_MARKER not in m["content"] for m in messages[5:])


def test_zero_means_keep_none_not_keep_everything():
    """The reading that caught out the first draft of the bench flag."""
    messages = _openai([(str(i), "x" * 5000) for i in range(3)])

    collapse_stale_results(messages, {}, Ledger(), keep_last=0)

    assert all(TRIM_MARKER in m["content"] for m in messages)


def test_a_small_result_is_not_worth_collapsing():
    messages = _openai([("a", "ok"), ("b", "x" * 5000)])

    collapse_stale_results(messages, {}, Ledger(), keep_last=0)

    assert messages[0]["content"] == "ok"


def test_keeping_everything_is_available_for_comparison():
    """KEEP_ALL is how the bench gets its before-number."""
    from orchestration.context_budget import KEEP_ALL
    messages = _openai([(str(i), "x" * 5000) for i in range(4)])

    saved = collapse_stale_results(messages, {}, Ledger(), keep_last=KEEP_ALL)

    assert saved == 0
    assert all(m["content"] == "x" * 5000 for m in messages)


def test_collapsing_twice_does_not_collapse_the_collapse():
    messages = _openai([("a", "x" * 5000), ("b", "y" * 5000)])
    ledger = Ledger()

    first = collapse_stale_results(messages, {}, ledger, keep_last=0)
    second = collapse_stale_results(messages, {}, ledger, keep_last=0)

    assert first > 0 and second == 0
    assert ledger.trimmed_count == 2


def test_an_unknown_id_is_still_collapsed():
    """Being unable to name the tool is not a reason to keep 8,000 characters."""
    messages = _openai([("mystery", "x" * 5000)])

    collapse_stale_results(messages, {}, Ledger(), keep_last=0)

    assert TRIM_MARKER in messages[0]["content"]


def test_the_stub_says_how_to_get_it_back():
    text = stub_for("run_command", 8000)

    assert "8,000" in text
    assert "run it again" in text.lower(), "a bare [trimmed] makes the model re-run everything"


# --- wired into the loop ----------------------------------------------------
#
# The unit tests above prove the mechanism. These prove it is actually reached:
# the ledger is only useful if run_agent feeds it, and the trimming is only
# useful if it happens to the messages the adapter is about to send.

class _RecordingAdapter:
    """A stand-in that keeps the conversation the real adapters would build."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.messages_seen = []

    def init_messages(self, task):
        return []

    def next_turn(self, messages, system):
        from orchestration.agent import ProviderTurn
        # A copy, because the point is what the request looked like at the time.
        self.messages_seen.append([dict(m) for m in messages])
        return self._turns.pop(0) if self._turns else ProviderTurn([], ["done"], [])

    def append_assistant(self, messages, payload):
        pass

    def append_tool_results(self, messages, results):
        messages.extend({"role": "tool", "tool_call_id": r.id, "content": r.content}
                        for r in results)


def _cmd_turn(call_id):
    from orchestration.agent import ProviderTurn, ToolCall
    return ProviderTurn(assistant_payload=[], text_blocks=["thinking"],
                        tool_calls=[ToolCall(id=call_id, name="run_command",
                                             input={"command": "poetry run pytest -q"})])


def _run(monkeypatch, turns, **kwargs):
    from orchestration import agent
    adapter = _RecordingAdapter(turns)
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("OUTPUT " + "x" * 5000, False))
    events = []
    result = agent.run_agent("task", "system", on_event=events.append, max_turns=10, **kwargs)
    return adapter, result, events


def test_the_loop_fills_the_ledger(monkeypatch):
    _, result, _ = _run(monkeypatch, [_cmd_turn(str(i)) for i in range(3)])

    assert result.context.system == len("system")
    assert result.context.schemas > 0
    assert result.context.prose >= len("thinking") * 3
    assert result.context.results["run_command"] > 10_000


def test_the_loop_trims_before_the_request_that_has_to_fit(monkeypatch):
    adapter, result, _ = _run(monkeypatch, [_cmd_turn(str(i)) for i in range(6)],
                              keep_last_results=2)

    final = adapter.messages_seen[-1]
    trimmed = [m for m in final if TRIM_MARKER in str(m.get("content", ""))]
    verbatim = [m for m in final if str(m.get("content", "")).startswith("OUTPUT")]

    assert trimmed, "old command output should not still be in the request"
    assert len(verbatim) <= 2, "only the most recent results stay whole"
    assert result.context.trimmed_count == len(trimmed)


def test_keeping_everything_is_what_the_old_behaviour_was(monkeypatch):
    from orchestration.context_budget import KEEP_ALL
    adapter, result, _ = _run(monkeypatch, [_cmd_turn(str(i)) for i in range(6)],
                              keep_last_results=KEEP_ALL)

    final = adapter.messages_seen[-1]

    assert not any(TRIM_MARKER in str(m.get("content", "")) for m in final)
    assert result.context.trimmed_chars == 0


def test_every_exit_reports_what_filled_the_conversation(monkeypatch):
    _, _, events = _run(monkeypatch, [_cmd_turn("a")])

    assert any(e.startswith("[context]") for e in events)


def test_a_request_that_raises_still_reports_the_breakdown(monkeypatch):
    """The prompt that doesn't fit is the one worth explaining, and it leaves by
    raising — so the accounting must not be lost on exactly that run."""
    from orchestration import agent

    class _Boom(_RecordingAdapter):
        def next_turn(self, messages, system):
            if self.messages_seen:
                raise RuntimeError("prefill memory guard rejected this prompt")
            return super().next_turn(messages, system)

    adapter = _Boom([_cmd_turn("a")])
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("OUTPUT " + "x" * 5000, False))
    events = []

    try:
        agent.run_agent("task", "system", on_event=events.append, max_turns=10)
    except RuntimeError as exc:
        assert "prefill memory guard" in str(exc), "the original error must survive"
    else:
        raise AssertionError("the adapter's error should have propagated")

    assert any("where it went" in e for e in events)
    assert any("run_command results" in e for e in events)
