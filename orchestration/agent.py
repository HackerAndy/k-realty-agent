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

from core.tools import llm_provider
from orchestration import agent_tools

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
    def __init__(self, final_text: str, turns: int, tool_calls: list[tuple[str, dict]]):
        self.final_text = final_text
        self.turns = turns
        self.tool_calls = tool_calls  # (name, input) in order, for audit


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


class LLMAdapter:
    def init_messages(self, task: str) -> list[dict]:
        raise NotImplementedError

    def next_turn(self, messages: list[dict], system: str) -> ProviderTurn:
        raise NotImplementedError

    def append_assistant(self, messages: list[dict], assistant_payload: object) -> None:
        raise NotImplementedError

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        raise NotImplementedError


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

        text_blocks = [block.text.strip() for block in message.content if block.type == "text" and block.text.strip()]
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in message.content
            if block.type == "tool_use"
        ]
        return ProviderTurn(assistant_payload=message.content, text_blocks=text_blocks, tool_calls=tool_calls)

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
        choice = raw["choices"][0]
        message = choice.get("message", {})

        text = (message.get("content") or "").strip()
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
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
        }
        return ProviderTurn(
            assistant_payload=assistant_payload,
            text_blocks=text_blocks,
            tool_calls=[c for c in tool_calls if c.id and c.name],
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
) -> AgentResult:
    """Run the tool-use loop until the model stops calling tools (or the turn
    cap is hit). `on_event` receives human-readable progress lines."""
    choice = llm_provider.resolve(provider=provider, model=model, base_url=api_url)
    # Say which model is about to do the work. The operator picks one in
    # Settings; a run that silently used a different one would be unauditable.
    on_event(f"[model] {choice.describe()}")
    adapter = _build_adapter(choice)
    messages = adapter.init_messages(task)
    tool_calls: list[tuple[str, dict]] = []
    final_text = ""

    for turn in range(1, max_turns + 1):
        turn_result = adapter.next_turn(messages=messages, system=system)

        for text in turn_result.text_blocks:
            on_event(text)

        adapter.append_assistant(messages, turn_result.assistant_payload)

        if not turn_result.tool_calls:
            final_text = turn_result.text_blocks[0] if turn_result.text_blocks else ""
            return AgentResult(final_text, turn, tool_calls)

        results: list[ToolResult] = []
        for tool_call in turn_result.tool_calls:
            tool_calls.append((tool_call.name, tool_call.input))
            preview = _preview(tool_call.name, tool_call.input)
            on_event(f"  → {tool_call.name}({preview})")
            result_text, is_error = agent_tools.dispatch(tool_call.name, tool_call.input)
            results.append(ToolResult(id=tool_call.id, content=result_text, is_error=is_error))

        adapter.append_tool_results(messages, results)

    on_event(f"[stopped after {max_turns} turns without finishing]")
    return AgentResult(final_text, max_turns, tool_calls)


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
    if name in ("read_file", "list_directory") and "path" in arguments:
        return arguments["path"]
    if name == "write_file":
        return arguments.get("path", "")
    if name == "run_command":
        cmd = arguments.get("command", "")
        return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    return ""
