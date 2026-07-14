"""The embedded agent loop — this is what makes the harness self-maintaining.

A manual Anthropic tool-use loop: given a task and a system prompt, Claude
drives the repo tools (read/write/run_command/list) until it reports done.
It's how the harness builds and repairs its own parsers without a human
opening a code editor.

Requires ANTHROPIC_API_KEY. Progress is streamed to an `on_event` callback so
the harness UI can show what the agent is doing (transparency matters — you
should see every file it writes and command it runs).

Lives in orchestration/ (the agent layer), free to use the anthropic SDK.
"""

from __future__ import annotations

from collections.abc import Callable

import anthropic

from orchestration import agent_tools

MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
DEFAULT_MAX_TURNS = 40


class AgentResult:
    def __init__(self, final_text: str, turns: int, tool_calls: list[tuple[str, dict]]):
        self.final_text = final_text
        self.turns = turns
        self.tool_calls = tool_calls  # (name, input) in order, for audit


def run_agent(
    task: str,
    system: str,
    on_event: Callable[[str], None] = print,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> AgentResult:
    """Run the tool-use loop until the model stops calling tools (or the turn
    cap is hit). `on_event` receives human-readable progress lines."""
    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": task}]
    tool_calls: list[tuple[str, dict]] = []
    final_text = ""

    for turn in range(1, max_turns + 1):
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=agent_tools.TOOL_SCHEMAS,
            thinking={"type": "adaptive"},
            messages=messages,
        ) as stream:
            message = stream.get_final_message()

        # Echo the model's own narration between tool calls.
        for block in message.content:
            if block.type == "text" and block.text.strip():
                on_event(block.text.strip())
        # Append the full assistant turn (incl. thinking) so context is preserved.
        messages.append({"role": "assistant", "content": message.content})

        if message.stop_reason != "tool_use":
            final_text = next(
                (b.text for b in message.content if b.type == "text"), ""
            )
            return AgentResult(final_text, turn, tool_calls)

        tool_results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            tool_calls.append((block.name, block.input))
            preview = _preview(block.name, block.input)
            on_event(f"  → {block.name}({preview})")
            result_text, is_error = agent_tools.dispatch(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    on_event(f"[stopped after {max_turns} turns without finishing]")
    return AgentResult(final_text, max_turns, tool_calls)


def _preview(name: str, arguments: dict) -> str:
    if name in ("read_file", "list_directory") and "path" in arguments:
        return arguments["path"]
    if name == "write_file":
        return arguments.get("path", "")
    if name == "run_command":
        cmd = arguments.get("command", "")
        return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    return ""
