"""The embedded agent loop — this is what makes the harness self-maintaining.

A provider-aware tool-use loop: given a task and a system prompt, the selected
LLM drives the repo tools (read/write/run_command/list) until it reports done.
It's how the harness builds and repairs its own parsers without a human opening
a code editor.

Which provider and model it runs on is NOT decided here: it comes from
core.tools.llm_provider.resolve(), i.e. from what the operator chose in Settings,
and every run announces the answer. Progress is streamed to an `on_event` callback so the
harness UI can show what the agent is doing (transparency matters — you should
see every file it writes and command it runs).

Lives in orchestration/ (the agent layer), free to use the anthropic SDK.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from collections.abc import Callable

import anthropic

from core.observability import get_logger
from core.tools import llm_provider
from orchestration import agent_tools, context_budget, degeneration
from orchestration.repetition import RepetitionDetector

log = get_logger("orchestration.agent")

# Which provider/model to use is decided in ONE place — core/tools/llm_provider —
# from what the operator chose in Settings. These re-exports keep the old import
# sites working without giving this module a second opinion.
DEFAULT_PROVIDER = llm_provider.DEFAULT_PROVIDER
DEFAULT_MODEL = llm_provider.DEFAULT_MODEL
DEFAULT_OMLX_MODEL = llm_provider.DEFAULT_OMLX_MODEL
DEFAULT_OMLX_BASE_URL = llm_provider.DEFAULT_OMLX_BASE_URL
DEFAULT_OMLX_API_KEY = llm_provider.DEFAULT_OMLX_API_KEY
MAX_TOKENS = 16000
DEFAULT_MAX_TURNS = 40


class AgentResult:
    def __init__(self, final_text: str, turns: int, tool_calls: list[tuple[str, dict]],
                 stopped_reason: str = "", context: context_budget.Ledger | None = None):
        self.final_text = final_text
        self.turns = turns
        self.tool_calls = tool_calls  # (name, input) in order, for audit
        # Where the conversation's characters went. A run that dies of context
        # has to be able to say what filled it, rather than leaving it to be
        # guessed at — which was guessed at once, and wrongly.
        self.context = context or context_budget.Ledger()
        # Set when the loop ended itself rather than the model finishing — today
        # only "went in circles". Verification still decides whether what it wrote
        # before that is acceptable; this only says why it stopped when it did.
        self.stopped_reason = stopped_reason


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ToolResult:
    id: str
    content: str
    is_error: bool


@dataclass
class ProviderTurn:
    assistant_payload: object
    text_blocks: list[str]
    tool_calls: list[ToolCall]
    # Set when this turn's prose was a loop and got trimmed before it entered the
    # conversation. Trimming has to happen HERE, in the adapter, because the
    # assistant payload is provider-shaped and this is the last place its text is
    # still separable from its tool calls.
    looped: degeneration.Degeneration | None = None


class LLMAdapter:
    def init_messages(self, task: str) -> list[dict]:
        raise NotImplementedError

    def next_turn(self, messages: list[dict], system: str) -> ProviderTurn:
        raise NotImplementedError

    def append_assistant(self, messages: list[dict], assistant_payload: object) -> None:
        raise NotImplementedError

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        raise NotImplementedError


def _collapse_anthropic_content(content: list) -> tuple[list, degeneration.Degeneration | None]:
    """Trim a looping text block in place, leaving tool_use blocks untouched.

    The SDK's blocks are objects, not dicts, so a trimmed text block is rebuilt as
    the plain dict the API also accepts — the assistant payload is only ever sent
    back, never re-read as an SDK object.
    """
    found = None
    out = []
    for block in content:
        if getattr(block, "type", None) != "text":
            out.append(block)
            continue
        kept, looped = degeneration.collapse(block.text)
        if looped is None:
            out.append(block)
            continue
        found = found or looped
        out.append({"type": "text", "text": kept})
    return out, found


class AnthropicAdapter(LLMAdapter):
    def __init__(self, model: str, max_tokens: int, api_key: str | None = None):
        # The key comes from the resolved choice (the vault), not only from the
        # environment — a worker process that never called load_into_env() would
        # otherwise fail with "no API key" while Settings plainly shows one.
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens

    def init_messages(self, task: str) -> list[dict]:
        return [{"role": "user", "content": task}]

    def next_turn(self, messages: list[dict], system: str) -> ProviderTurn:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=agent_tools.TOOL_SCHEMAS,
            thinking={"type": "adaptive"},
            messages=messages,
        ) as stream:
            message = stream.get_final_message()

        content, looped = _collapse_anthropic_content(message.content)
        text_blocks = [block.text.strip() for block in content if block.type == "text" and block.text.strip()]
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in content
            if block.type == "tool_use"
        ]
        return ProviderTurn(assistant_payload=content, text_blocks=text_blocks,
                            tool_calls=tool_calls, looped=looped)

    def append_assistant(self, messages: list[dict], assistant_payload: object) -> None:
        messages.append({"role": "assistant", "content": assistant_payload})

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": item.id,
                        "content": item.content,
                        "is_error": item.is_error,
                    }
                    for item in results
                ],
            }
        )


def _first_choice(raw: object, model: str, base_url: str) -> dict:
    """The first choice from an OpenAI-compatible response, or a usable error.

    `raw["choices"][0]` assumed a shape and got another: a build died on
    `KeyError: 'choices'`, which reached the operator as the complete sentence
    "Build failed: 'choices'". The server had answered HTTP 200 — so the error
    handling one layer down, which only covers non-200 — and put whatever it
    wanted to say in the body. Servers do this: a 200 carrying `{"error": ...}`,
    a rate-limit notice, an empty object when a request is refused.

    Whatever came back, say so. The operator cannot fix an unmet expectation
    they can't see, and neither can the agent reading the log.
    """
    if isinstance(raw, dict):
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            return choices[0]
        # Servers commonly put the real reason here while still answering 200.
        err = raw.get("error")
        if err:
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(log.failure(
                operation="llm_turn",
                code="LLM_ERROR_IN_BODY",
                message=f"{model} at {base_url} returned an error instead of a reply: {detail}",
                remediation="This came from the model server, not the harness. Check the "
                            "server's own logs, and whether the request exceeded a limit "
                            "it enforces (context length, tokens, rate).",
                context={"model": model, "base_url": base_url,
                         "error": str(detail)[:500],
                         "response_keys": sorted(raw)},
            ))
        # The keys it DID send go in the message, not only the structured
        # context: the message is what reaches the screen, and those keys are the
        # only clue to what the server actually replied with.
        keys = ", ".join(sorted(raw)) or "nothing at all"
        raise RuntimeError(log.failure(
            operation="llm_turn",
            code="LLM_NO_CHOICES",
            message=f"{model} at {base_url} answered without any 'choices' — nothing to "
                    f"read as a reply. What it did send: {keys}.",
            remediation="The server answered successfully but not in OpenAI-compatible "
                        "shape. Check that the base URL points at an OpenAI-compatible "
                        "/chat/completions endpoint, and look at the server's own logs.",
            context={"model": model, "base_url": base_url,
                     "response_keys": sorted(raw),
                     "response_sample": json.dumps(raw)[:500]},
        ))
    raise RuntimeError(log.failure(
        operation="llm_turn",
        code="LLM_BAD_RESPONSE",
        message=f"{model} at {base_url} answered with {type(raw).__name__}, not a JSON "
                f"object: {str(raw)[:120]}",
        remediation="Check that the base URL points at an OpenAI-compatible "
                    "/chat/completions endpoint.",
        context={"model": model, "base_url": base_url, "response_sample": str(raw)[:500]},
    ))


class OpenAICompatibleAdapter(LLMAdapter):
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        max_tokens: int,
        temperature: float,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["input_schema"],
                },
            }
            for schema in agent_tools.TOOL_SCHEMAS
        ]

    def init_messages(self, task: str) -> list[dict]:
        return [{"role": "user", "content": task}]

    def next_turn(self, messages: list[dict], system: str) -> ProviderTurn:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": self.tools,
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        raw = _openai_chat_completion(self.base_url, self.api_key, payload)
        choice = _first_choice(raw, self.model, self.base_url)
        message = choice.get("message", {})

        text, looped = degeneration.collapse((message.get("content") or "").strip())
        text_blocks = [text] if text else []

        tool_calls: list[ToolCall] = []
        for call in message.get("tool_calls", []) or []:
            function = call.get("function") or {}
            args = function.get("arguments") or "{}"
            try:
                parsed_args = json.loads(args)
            except json.JSONDecodeError:
                parsed_args = {}
            tool_calls.append(
                ToolCall(
                    id=call.get("id", ""),
                    name=function.get("name", ""),
                    input=parsed_args,
                )
            )

        assistant_payload = {
            "role": "assistant",
            # The TRIMMED text, not the original: this is the copy that goes back
            # in the conversation on every subsequent turn, so a loop left here is
            # paid for again at each one.
            "content": text or message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
        }
        return ProviderTurn(
            assistant_payload=assistant_payload,
            text_blocks=text_blocks,
            tool_calls=[c for c in tool_calls if c.id and c.name],
            looped=looped,
        )

    def append_assistant(self, messages: list[dict], assistant_payload: object) -> None:
        messages.append(assistant_payload)

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        for item in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.id,
                    "content": item.content,
                }
            )


def run_agent(
    task: str,
    system: str,
    on_event: Callable[[str], None] = print,
    max_turns: int = DEFAULT_MAX_TURNS,
    provider: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    keep_last_results: int = -1,
    trim_above_chars: int = -1,
) -> AgentResult:
    """Run the tool-use loop until the model stops calling tools (or the turn
    cap is hit). `on_event` receives human-readable progress lines.

    `keep_last_results` is how many recent tool results stay verbatim; older
    large ones are replaced by a stub, but only once the conversation exceeds
    `trim_above_chars`. Both take -1 for the default.
    `context_budget.KEEP_ALL` turns trimming off, which is how the bench
    measures what it is worth. Note 0 means keep NONE, not keep everything.
    """
    if keep_last_results < 0:
        keep_last_results = int(os.getenv("AGENT_KEEP_LAST_RESULTS",
                                          str(context_budget.KEEP_LAST_RESULTS)))
    if trim_above_chars < 0:
        trim_above_chars = int(os.getenv("AGENT_TRIM_ABOVE_CHARS",
                                         str(context_budget.TRIM_ABOVE_CHARS)))
    choice = llm_provider.resolve(provider=provider, model=model, base_url=api_url)
    # Say which model is about to do the work. The operator picks one in
    # Settings; a run that silently used a different one would be unauditable.
    on_event(f"[model] {choice.describe()}")
    adapter = _build_adapter(choice)
    messages = adapter.init_messages(task)
    tool_calls: list[tuple[str, dict]] = []
    final_text = ""
    # Busy is not the same as getting somewhere: this notices the agent doing the
    # identical thing over and over, which the turn cap alone would only reveal
    # after every turn had been spent.
    repetition = RepetitionDetector()
    # The same failure as `looped`, spread across turns instead of inside one
    # message — three turns that each re-derive the same analysis rather than act.
    restating = degeneration.Restatement()
    loops = 0

    ledger = context_budget.Ledger(
        system=len(system), schemas=len(json.dumps(agent_tools.TOOL_SCHEMAS)))
    # Which tool produced which result, so a collapsed one can still say what it
    # was standing in for.
    tool_by_id: dict[str, str] = {}

    for turn in range(1, max_turns + 1):
        # Before the request, not after: this is the turn whose prompt has to
        # fit. Results the agent has already acted on become a one-line stub —
        # nothing is removed, so tool_use/tool_result pairing is untouched.
        freed = context_budget.collapse_stale_results(messages, tool_by_id, ledger,
                                                      keep_last=keep_last_results,
                                                      trim_above=trim_above_chars)
        if freed:
            on_event(f"  [trimmed ~{freed // context_budget.CHARS_PER_TOKEN:,} tokens of "
                     f"tool output the agent had finished with]")
        # Announced BEFORE the call, not after: this is the moment the run goes
        # quiet, sometimes for minutes on a local model, and both the operator and
        # the watchdog need to know what the silence is for.
        on_event(f"Asking the model (turn {turn} of {max_turns})…")
        try:
            turn_result = adapter.next_turn(messages=messages, system=system)
        except Exception:
            # The request that doesn't fit is the one worth explaining, and it
            # leaves by raising — so without this the accounting is lost on
            # precisely the runs it exists for. Say what filled the conversation,
            # then let the original error through untouched.
            on_event(ledger.summary())
            on_event("[context] where it went:\n" + ledger.breakdown())
            raise

        for text in turn_result.text_blocks:
            on_event(text)
            ledger.note_prose(text)

        adapter.append_assistant(messages, turn_result.assistant_payload)

        # A model that spends a whole turn repeating one paragraph has stopped
        # reasoning. The first time is trimmed and called out; a second time ends
        # the run, because the remaining turns would only buy more of the same —
        # and each one still costs the context the next turn has to fit into.
        if turn_result.looped is not None:
            loops += 1
            on_event(f"  [{turn_result.looped.describe()}]")
            if loops >= 2:
                return _finish(
                    AgentResult(
                        final_text, turn, tool_calls, context=ledger,
                        stopped_reason="The model got stuck repeating itself and was not "
                                       "making progress, so the run was ended."),
                    on_event)

        repeating_itself = restating.observe(
            turn_result.text_blocks[0] if turn_result.text_blocks else "")
        if repeating_itself:
            on_event("  [Three turns in a row have opened with the same analysis — "
                     "saying so, since re-deriving it is not progress.]")

        if not turn_result.tool_calls:
            final_text = turn_result.text_blocks[0] if turn_result.text_blocks else ""
            return _finish(AgentResult(final_text, turn, tool_calls, context=ledger), on_event)

        results: list[ToolResult] = []
        circling = None
        for tool_call in turn_result.tool_calls:
            tool_calls.append((tool_call.name, tool_call.input))
            tool_by_id[tool_call.id] = tool_call.name
            ledger.note_call(tool_call.name, tool_call.input)
            preview = _preview(tool_call.name, tool_call.input)
            on_event(f"  → {tool_call.name}({preview})")
            result_text, is_error = agent_tools.dispatch(tool_call.name, tool_call.input)

            repeat = repetition.observe(tool_call.name, tool_call.input)
            if repeat is not None:
                on_event(f"  [{repeat.describe()}]")
                if repeat.verdict == "stop":
                    circling = repeat
                else:
                    # The nudge rides along with the result the agent is about to
                    # read anyway. A separate message would have to be interleaved
                    # with tool_use blocks, which providers reject.
                    result_text = f"{result_text}\n\n{repetition.nudge_text(repeat)}"

            ledger.note_result(tool_call.name, result_text)
            results.append(ToolResult(id=tool_call.id, content=result_text, is_error=is_error))

        if circling is not None:
            return _finish(
                AgentResult(final_text, turn, tool_calls, context=ledger,
                            stopped_reason=circling.describe()), on_event)

        # Same delivery as the repetition nudge: ride along with a result the agent
        # is about to read. A standalone message can't be interleaved with tool_use
        # blocks — providers reject it.
        nudges = [
            degeneration.LOOP_WARNING if turn_result.looped is not None else "",
            degeneration.RESTATEMENT_WARNING if repeating_itself else "",
        ]
        nudge = "\n\n".join(n for n in nudges if n)
        if nudge and results:
            results[0] = ToolResult(
                id=results[0].id,
                content=f"{results[0].content}\n\n{nudge}",
                is_error=results[0].is_error,
            )

        adapter.append_tool_results(messages, results)

    on_event(f"[stopped after {max_turns} turns without finishing]")
    return _finish(
        AgentResult(final_text, max_turns, tool_calls, context=ledger,
                    stopped_reason=f"Hit the {max_turns}-turn cap without finishing."),
        on_event)


def _finish(result: AgentResult, on_event: Callable[[str], None]) -> AgentResult:
    """Say what filled the conversation, on the way out of every exit.

    Emitted rather than merely returned because the operator watches the stream,
    and because a run killed by the model server's context limit never returns
    at all — the last thing in the log is then the only account of what filled
    it. Routed through one helper so a new early return cannot silently skip it.
    """
    on_event(result.context.summary())
    return result


def _build_adapter(choice: llm_provider.LLMChoice) -> LLMAdapter:
    """Turn the resolved choice into a client. No provider/model decisions are
    made here — that is llm_provider.resolve()'s job, and only its job."""
    if choice.is_anthropic:
        return AnthropicAdapter(
            model=choice.model,
            max_tokens=int(os.getenv("AGENT_MAX_TOKENS", str(MAX_TOKENS))),
            api_key=choice.api_key,
        )

    if choice.provider == "openai_compatible":
        return OpenAICompatibleAdapter(
            model=choice.model,
            base_url=choice.base_url,
            api_key=choice.api_key,
            max_tokens=int(os.getenv("OMLX_MAX_TOKENS", str(MAX_TOKENS))),
            temperature=float(os.getenv("OMLX_TEMPERATURE", "0.2")),
        )

    raise ValueError(f"Unsupported LLM provider '{choice.provider}'.")


# The HTTP call itself lives in core/tools/llm_provider so core/ code can reach a
# local server without importing orchestration. Kept under the old name here.
_openai_chat_completion = llm_provider.chat_completion


def _preview(name: str, arguments: dict) -> str:
    if name == "read_file" and "path" in arguments:
        span = ""
        if arguments.get("start_line") or arguments.get("end_line"):
            span = f":{arguments.get('start_line') or 1}-{arguments.get('end_line') or ''}"
        return f"{arguments['path']}{span}"
    if name == "search_files":
        where = arguments.get("path") or "."
        return f"{arguments.get('pattern', '')!r} in {where}"
    if name == "list_directory" and "path" in arguments:
        return arguments["path"]
    if name == "write_file":
        return arguments.get("path", "")
    # The operator is watching this scroll past; an edit should say WHERE, since
    # "str_replace(core/parsers/x.py)" three times running looks like a loop and
    # three different line numbers do not.
    if name == "str_replace":
        return arguments.get("path", "")
    if name == "insert":
        return f"{arguments.get('path', '')} after line {arguments.get('insert_line', '?')}"
    if name == "run_command":
        cmd = arguments.get("command", "")
        return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    return ""
