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

import json

from orchestration.context_budget import (
    Ledger,
    TRIM_MARKER,
    collapse_stale_results,
    looks_like_context_overflow,
    observed_ceiling_chars,
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

    collapse_stale_results(messages, {"a": "run_command"}, ledger, trim_above=0, keep_last=0)

    assert ledger.live_total < 500
    assert ledger.trimmed_count == 1


# --- collapsing: both message shapes ----------------------------------------

def test_an_old_result_is_replaced_in_the_anthropic_shape():
    messages = [_anthropic([("a", "out" * 500), ("b", "recent" * 200)])]

    collapse_stale_results(messages, {"a": "run_command", "b": "read_file"},
                           Ledger(), trim_above=0, keep_last=1)

    blocks = messages[0]["content"]
    assert TRIM_MARKER in blocks[0]["content"]
    assert "run_command" in blocks[0]["content"], "the stub says what it stood in for"
    assert blocks[1]["content"].startswith("recent"), "the most recent is untouched"


def test_an_old_result_is_replaced_in_the_openai_shape():
    messages = _openai([("a", "out" * 500), ("b", "recent" * 200)])

    collapse_stale_results(messages, {"a": "run_command", "b": "read_file"},
                           Ledger(), trim_above=0, keep_last=1)

    assert TRIM_MARKER in messages[0]["content"]
    assert messages[1]["content"].startswith("recent")


def test_no_message_is_ever_removed():
    """The pairing rule: orphan a tool_use from its tool_result and the provider
    rejects the whole conversation, several turns later."""
    messages = _openai([(str(i), "out" * 500) for i in range(5)])
    before = len(messages)

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=0)

    assert len(messages) == before
    assert all(m["role"] == "tool" and m["tool_call_id"] for m in messages)


def test_the_assistant_payload_is_not_touched():
    """Only results are trimmed — an assistant turn carries the tool_use blocks."""
    assistant = {"role": "assistant", "content": "I will run the tests" * 100}
    messages = [assistant, *_openai([("a", "out" * 500)])]

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=0)

    assert assistant["content"].startswith("I will run the tests")


def test_objects_that_are_not_dicts_are_stepped_over():
    """The Anthropic adapter parks SDK block objects in the conversation."""
    class _SdkBlock:
        type = "tool_use"

    messages = [{"role": "assistant", "content": [_SdkBlock()]},
                *_openai([("a", "out" * 500)])]

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=0)  # must not raise

    assert TRIM_MARKER in messages[1]["content"]


# --- collapsing: what is left alone -----------------------------------------

def test_recent_results_are_kept_verbatim():
    messages = _openai([(str(i), f"result-{i}" + "x" * 500) for i in range(8)])

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=3)

    assert all(TRIM_MARKER in m["content"] for m in messages[:5])
    assert all(TRIM_MARKER not in m["content"] for m in messages[5:])


def test_zero_means_keep_none_not_keep_everything():
    """The reading that caught out the first draft of the bench flag."""
    messages = _openai([(str(i), "x" * 5000) for i in range(3)])

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=0)

    assert all(TRIM_MARKER in m["content"] for m in messages)


def test_a_small_result_is_not_worth_collapsing():
    messages = _openai([("a", "ok"), ("b", "x" * 5000)])

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=0)

    assert messages[0]["content"] == "ok"


def test_keeping_everything_is_available_for_comparison():
    """KEEP_ALL is how the bench gets its before-number."""
    from orchestration.context_budget import KEEP_ALL
    messages = _openai([(str(i), "x" * 5000) for i in range(4)])

    saved = collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=KEEP_ALL)

    assert saved == 0
    assert all(m["content"] == "x" * 5000 for m in messages)


def test_collapsing_twice_does_not_collapse_the_collapse():
    messages = _openai([("a", "x" * 5000), ("b", "y" * 5000)])
    ledger = Ledger()

    first = collapse_stale_results(messages, {}, ledger, trim_above=0, keep_last=0)
    second = collapse_stale_results(messages, {}, ledger, trim_above=0, keep_last=0)

    assert first > 0 and second == 0
    assert ledger.trimmed_count == 2


def test_an_unknown_id_is_still_collapsed():
    """Being unable to name the tool is not a reason to keep 8,000 characters."""
    messages = _openai([("mystery", "x" * 5000)])

    collapse_stale_results(messages, {}, Ledger(), trim_above=0, keep_last=0)

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


def _distinct_turn(i):
    """A turn whose tool call differs from the others, so the repetition detector
    stays out of the way of whatever is actually under test."""
    from orchestration.agent import ProviderTurn, ToolCall
    return ProviderTurn(assistant_payload=[], text_blocks=[f"step {i}"],
                        tool_calls=[ToolCall(id=str(i), name="run_command",
                                             input={"command": f"pytest tests/test_{i}.py"})])


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
                              keep_last_results=2, trim_above_chars=0)

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


# --- when trimming happens --------------------------------------------------

def test_a_short_conversation_is_left_completely_alone():
    """Trimming rewrites old messages, which is the prefix a server caches. Doing
    it before there is a reason costs a re-prefill and buys nothing."""
    ledger = Ledger()
    ledger.note_result("run_command", "x" * 5000)
    messages = _openai([("a", "x" * 5000), ("b", "y" * 5000)])

    saved = collapse_stale_results(messages, {}, ledger, keep_last=0, trim_above=48_000)

    assert saved == 0
    assert all(m["content"].startswith(("x", "y")) for m in messages)


def test_crossing_the_threshold_trims_hard():
    """One big collapse buys many quiet turns; drip-feeding buys another re-prefill."""
    ledger = Ledger()
    ledger.note_result("run_command", "x" * 60_000)
    messages = _openai([(str(i), "x" * 6000) for i in range(10)])

    saved = collapse_stale_results(messages, {}, ledger, keep_last=2, trim_above=48_000)

    assert saved > 40_000, "should reclaim most of it in one go"
    assert sum(TRIM_MARKER in m["content"] for m in messages) == 8


def test_the_threshold_is_measured_on_what_is_still_live():
    """Already-trimmed characters must not keep the conversation over the line,
    or every subsequent turn trims again for nothing."""
    ledger = Ledger()
    ledger.note_result("run_command", "x" * 60_000)
    ledger.trimmed_chars = 55_000

    saved = collapse_stale_results(_openai([("a", "x" * 6000)]), {}, ledger,
                                   keep_last=0, trim_above=48_000)

    assert saved == 0


# --- trimming the CALL side -------------------------------------------------
#
# Collapsing results alone was not enough. Instrumenting a run that died put
# `str_replace` ARGUMENTS at the top of the list — 9,324 tokens, more than
# command output and more than every file read together — because each edit
# carries old_str AND new_str, and both are pure history the moment it lands.

from orchestration.context_budget import collapse_stale_call_args  # noqa: E402


def _openai_call(cid, name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


class _SdkToolUse:
    """What the Anthropic adapter actually parks in the conversation."""
    type = "tool_use"

    def __init__(self, cid, name, payload):
        self.id, self.name, self.input = cid, name, payload


def _big(n=5000):
    return "x" * n


def _loaded(ledger, chars=200_000):
    ledger.note_call("str_replace", {"old_str": "x" * chars})
    return ledger


def test_edit_payloads_are_trimmed_in_the_openai_shape():
    messages = [_openai_call("a", "str_replace",
                             {"path": "core/parsers/x.py", "old_str": _big(), "new_str": _big()})]
    ledger = _loaded(Ledger())

    saved = collapse_stale_call_args(messages, ledger, keep_last=0, trim_above=0)

    args = json.loads(messages[0]["tool_calls"][0]["function"]["arguments"])
    assert saved > 9000
    assert TRIM_MARKER in args["old_str"] and TRIM_MARKER in args["new_str"]
    assert args["path"] == "core/parsers/x.py", "the useful part stays readable"


def test_edit_payloads_are_trimmed_in_the_anthropic_shape():
    block = {"type": "tool_use", "id": "a", "name": "str_replace",
             "input": {"path": "core/parsers/x.py", "old_str": _big(), "new_str": _big()}}
    messages = [{"role": "assistant", "content": [block]}]

    collapse_stale_call_args(messages, _loaded(Ledger()), keep_last=0, trim_above=0)

    kept = messages[0]["content"][0]
    assert TRIM_MARKER in kept["input"]["old_str"]
    assert kept["input"]["path"] == "core/parsers/x.py"


def test_an_sdk_block_is_replaced_by_the_dict_the_api_also_accepts():
    """The adapter parks SDK objects, not dicts — mutating those is not the move."""
    messages = [{"role": "assistant",
                 "content": [_SdkToolUse("a", "write_file",
                                         {"path": "core/parsers/x.py", "content": _big()})]}]

    collapse_stale_call_args(messages, _loaded(Ledger()), keep_last=0, trim_above=0)

    kept = messages[0]["content"][0]
    assert isinstance(kept, dict) and kept["type"] == "tool_use"
    assert kept["id"] == "a" and kept["name"] == "write_file", "identity must survive"
    assert TRIM_MARKER in kept["input"]["content"]


def test_every_argument_key_survives_the_trim():
    """A tool_use whose input lost a required field is a malformed conversation."""
    args = {"path": "x.py", "old_str": _big(), "new_str": _big()}
    messages = [_openai_call("a", "str_replace", args)]

    collapse_stale_call_args(messages, _loaded(Ledger()), keep_last=0, trim_above=0)

    after = json.loads(messages[0]["tool_calls"][0]["function"]["arguments"])
    assert set(after) == set(args)
    assert all(isinstance(v, str) for v in after.values())


def test_recent_calls_keep_their_payloads():
    messages = [_openai_call(str(i), "str_replace",
                             {"path": "x.py", "old_str": _big()}) for i in range(5)]

    collapse_stale_call_args(messages, _loaded(Ledger()), keep_last=2, trim_above=0)

    trimmed = [m for m in messages
               if TRIM_MARKER in m["tool_calls"][0]["function"]["arguments"]]
    assert len(trimmed) == 3


def test_trimming_call_args_twice_is_a_no_op():
    messages = [_openai_call("a", "str_replace", {"path": "x.py", "old_str": _big()})]
    ledger = _loaded(Ledger())

    first = collapse_stale_call_args(messages, ledger, keep_last=0, trim_above=0)
    second = collapse_stale_call_args(messages, ledger, keep_last=0, trim_above=0)

    assert first > 0 and second == 0


def test_call_args_respect_the_same_threshold_as_results():
    messages = [_openai_call("a", "str_replace", {"path": "x.py", "old_str": _big()})]

    assert collapse_stale_call_args(messages, Ledger(), keep_last=0,
                                    trim_above=48_000) == 0


# --- learning the ceiling from the server -----------------------------------

def test_a_prefill_refusal_is_recognised_and_its_size_read():
    error = ('OpenAI-compatible API error 400: oMLX prefill memory guard rejected this '
             'prompt: Prefill context too large for available memory (preflight safety '
             'guard, kv_len=26015, min_chunk=32)')

    assert looks_like_context_overflow(error)
    assert observed_ceiling_chars(error) == 26015 * 4


def test_an_ordinary_failure_is_not_mistaken_for_one():
    """Shrinking the context would not help, and would hide the real fault."""
    assert not looks_like_context_overflow("Connection refused")
    assert not looks_like_context_overflow("401 Unauthorized")
    assert observed_ceiling_chars("Connection refused") is None


def test_a_refusal_without_a_size_still_recovers():
    """Not every server reports the length it rejected; the run should still try."""
    assert looks_like_context_overflow("This model's maximum context length is 8192 tokens")
    assert observed_ceiling_chars("maximum context length is 8192 tokens") is None


def test_a_refused_prompt_is_retried_smaller_instead_of_killing_the_run(monkeypatch):
    """The failure that killed two bench cases four times over.

    A prompt the server refuses for SIZE is the one failure here the harness can
    act on — it chose what to send. The refusal also carries the real ceiling,
    measured on the machine running the model, which beats any constant because
    the ceiling moves with whatever else is resident on that host.
    """
    from orchestration import agent

    class _RefusesOnce(_RecordingAdapter):
        def __init__(self, turns):
            super().__init__(turns)
            self.refused = False

        def next_turn(self, messages, system):
            if not self.refused and len(self.messages_seen) == 2:
                self.refused = True
                raise RuntimeError(
                    "OpenAI-compatible API error 400: oMLX prefill memory guard "
                    "rejected this prompt (preflight safety guard, kv_len=26015)")
            return super().next_turn(messages, system)

    adapter = _RefusesOnce([_cmd_turn(str(i)) for i in range(4)])
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("OUTPUT " + "x" * 6000, False))
    events = []

    result = agent.run_agent("task", "system", on_event=events.append, max_turns=8)

    assert adapter.refused, "the test must actually have exercised the refusal"
    assert result.turns > 1, "the run continued rather than dying"
    assert any("refused that prompt as too large" in e for e in events)
    # 26,015 is the server's own number, echoed back; 15,609 is 60% of it, the
    # margin we then aim at. Both matter: the first says we listened, the second
    # says we left room, because the ceiling moves between runs.
    assert any("~26,015 tokens" in e for e in events), "the ceiling came from the server"
    assert any("~15,609 tokens" in e for e in events), "and we aimed well under it"


def test_a_second_refusal_gives_up_rather_than_looping(monkeypatch):
    """Shrinking twice would be a losing fight; the operator should see the error."""
    from orchestration import agent

    class _AlwaysRefuses(_RecordingAdapter):
        def next_turn(self, messages, system):
            self.messages_seen.append([])
            raise RuntimeError("prefill context too large, kv_len=26015")

    monkeypatch.setattr(agent, "_build_adapter", lambda choice: _AlwaysRefuses([]))
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("x", False))
    events = []

    try:
        agent.run_agent("task", "system", on_event=events.append, max_turns=8)
    except RuntimeError as exc:
        assert "prefill" in str(exc), "the original error reaches the operator"
    else:
        raise AssertionError("a hopeless run must not be swallowed")

    assert any("where it went" in e for e in events), "and it explains what filled it"


# --- the turn budget the agent could not see --------------------------------

def test_the_agent_is_told_when_its_budget_runs_low(monkeypatch):
    """The turn cap ended four of nine builds, and the model had no idea it was
    approaching one: on_event goes to the operator's screen, not the
    conversation. Something that cannot see a limit cannot ration against it."""
    from orchestration import agent

    # Distinct commands per turn: identical ones trip the repetition detector,
    # which ends the run long before any budget warning is due.
    turns = [_distinct_turn(i) for i in range(12)]
    adapter = _RecordingAdapter(turns)
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("ok", False))

    agent.run_agent("task", "system", on_event=lambda _: None, max_turns=12)

    delivered = "\n".join(str(m.get("content", "")) for m in adapter.messages_seen[-1])
    assert "10 turns left of 12" in delivered, "warned in time to change course"
    assert "3 turns left of 12" in delivered, "and told to land it"
    assert "PASSES" in delivered, "landing means leaving something that works"


def test_a_short_run_is_not_nagged_about_a_budget_it_will_not_reach(monkeypatch):
    from orchestration import agent

    adapter = _RecordingAdapter([_cmd_turn("a")])
    monkeypatch.setattr(agent, "_build_adapter", lambda choice: adapter)
    monkeypatch.setattr(agent.agent_tools, "dispatch", lambda n, a: ("ok", False))

    agent.run_agent("task", "system", on_event=lambda _: None, max_turns=40)

    delivered = "\n".join(str(m.get("content", "")) for m in adapter.messages_seen[-1])
    assert "turns left" not in delivered


def test_the_warning_rides_on_a_tool_result():
    """A standalone message cannot be interleaved with tool_use blocks — providers
    reject it. Same rail the loop and repetition nudges already use."""
    from orchestration.agent import _budget_note

    assert _budget_note(turn=30, max_turns=40).startswith("[10 turns left")
    assert _budget_note(turn=37, max_turns=40).startswith("[3 turns left")
    assert _budget_note(turn=5, max_turns=40) == ""


# --- a partial view must not read as a small conversation --------------------
#
# The Claude Code executor sees the prose and tool-call arguments that stream out
# of the CLI, and nothing else — no tool results, no schemas, none of the CLI's
# own context handling. It reported ~3k tokens where the local model reported
# ~34k, which invites reading a tenth of the VIEW as a tenth of the context.

def test_a_watched_conversation_says_it_is_a_floor():
    ledger = Ledger(system=400, complete=False)
    ledger.note_prose("x" * 800)

    line = ledger.summary()

    assert "floor, not a total" in line
    assert "in the conversation" not in line, "that phrase claims an account it does not have"


def test_a_held_conversation_still_reports_plainly():
    ledger = Ledger(system=400)
    ledger.note_prose("x" * 800)

    assert "in the conversation" in ledger.summary()
    assert "floor" not in ledger.summary()


def test_the_breakdown_says_what_it_could_not_see():
    """A missing row reads as 'that cost nothing' unless it says otherwise."""
    ledger = Ledger(system=400, complete=False)
    ledger.note_call("run_command", {"command": "pytest -q"})

    assert "absent rather than zero" in ledger.breakdown()
