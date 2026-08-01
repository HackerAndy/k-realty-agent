"""What is filling the conversation, and dropping the parts nobody will read again.

Two jobs, kept together because the second is only trustworthy with the first.

**Accounting.** A run that dies of context used to say only that it died. The
ledger records where every character came from — the system prompt, the model's
own prose, each tool's arguments, each tool's results — so a run reports what
filled it rather than leaving it to be guessed at. It was guessed at once, and
wrongly: whole-file reads looked like the obvious culprit and turned out to be
about a tenth, while `run_command` output nobody had counted was most of it.

**Collapsing.** Every tool result stays in the conversation for the rest of the
run, and each one is capped at 8,000 characters. Seventeen `pytest -v` and
`python -c` results is the whole context budget of a local model, spent on
output the agent read once, acted on, and will never look at again.

So old results are replaced by a stub saying what was there and how to get it
back. Note what this deliberately does NOT do: it never removes a message. The
tool_use/tool_result pairing that providers reject a conversation for breaking
is untouched, because only the *text inside* a result changes. That is the whole
reason to start here rather than with the summarising condenser, which has to
solve the pairing problem to forget anything at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from collections import Counter

# How many of the most recent tool results stay verbatim. The agent is usually
# acting on the last one or two; beyond that it has already extracted what it
# needed. Six is roughly the last two or three turns of work.
KEEP_LAST_RESULTS = 6

# Below this a result is left alone: collapsing it saves nothing worth the loss.
MIN_COLLAPSE_CHARS = 300

# Don't trim at all until the conversation is this big, then trim hard.
#
# The first version trimmed every turn it could, and that is the expensive way
# to do it: trimming rewrites OLD messages, which is exactly the prefix a server
# keeps in its KV cache, so each one plausibly forces a full re-prefill. It fired
# on 30 of one run's 47 turns and that run took 2.4x as long as the same case
# before trimming existed.
#
# Waiting until there is a reason keeps the prefix byte-identical across most
# turns, and collapsing everything at once when the moment comes buys enough
# room that the next trim is many turns away. 48,000 chars is ~12k tokens: well
# clear of the ~33k where a local model started refusing prompts, and high enough
# that a short run never trims at all.
TRIM_ABOVE_CHARS = 48_000

# Turns trimming off. Spelled as a keep-count larger than any run rather than a
# flag, so there is exactly one knob and no second code path to get wrong.
# Note 0 is NOT this: 0 means keep none, i.e. trim everything the agent is not
# currently acting on. That reading caught out the first draft of the bench flag.
KEEP_ALL = 1_000_000

# Rough and honestly labelled. The point is comparing runs, not billing.
CHARS_PER_TOKEN = 4

# How much of the ceiling to aim for once a server has told us where it is.
# Well under, because the ceiling MOVES: the same server refused a prompt at
# kv_len 26,015 on one run and accepted 35,072 on another, the difference being
# how much memory something else on the host was holding at the time. A margin
# that only just fits the last refusal will be refused again.
CEILING_HEADROOM = 0.6

# A prompt the server would not even start on. The wording varies by server, so
# this matches the shapes rather than one product's sentence: what they have in
# common is saying the context/prompt is too large to prefill.
_OVERFLOW_SIGNS = ("prefill", "context too large", "context length", "kv_len",
                   "too many tokens", "maximum context", "reduce the length")


def looks_like_context_overflow(error: object) -> bool:
    """Is this the server refusing the prompt for size, rather than a real fault?

    Worth telling apart because it is the one failure the harness can actually do
    something about: everything else is the operator's or the model's problem,
    but a prompt too big is a prompt we chose to send.
    """
    text = str(error).lower()
    return any(sign in text for sign in _OVERFLOW_SIGNS)


def observed_ceiling_chars(error: object) -> int | None:
    """The prompt size the server just refused, in characters, if it said.

    Servers that guard on prefill tend to report the length they rejected —
    oMLX says `kv_len=26015`. That number is worth more than any constant we
    could pick, because it is measured on the machine actually running the
    model, at the moment it actually ran out.
    """
    match = re.search(r"kv_len\s*=\s*(\d+)", str(error))
    if not match:
        return None
    return int(match.group(1)) * CHARS_PER_TOKEN


# The sentence that identifies a stub as ours, so a second pass doesn't collapse
# a collapse. Kept as a constant because the check and the text must not drift.
TRIM_MARKER = "Trimmed to leave room"


def stub_for(tool: str, size: int) -> str:
    """What replaces a result the agent has finished with.

    It says which tool, how much, and how to get it back — a bare "[trimmed]"
    invites the model to assume the worst and re-run everything.
    """
    return (f"[{tool} produced {size:,} characters here. {TRIM_MARKER}; "
            f"you already acted on it. Run it again if you truly need it back.]")


@dataclass
class Ledger:
    """Where the conversation's characters came from, by source."""

    system: int = 0
    schemas: int = 0
    prose: int = 0
    call_args: Counter = field(default_factory=Counter)
    results: Counter = field(default_factory=Counter)
    trimmed_chars: int = 0
    trimmed_count: int = 0

    def note_prose(self, text: str) -> None:
        self.prose += len(text or "")

    def note_call(self, tool: str, arguments: dict) -> None:
        # The arguments are conversation too, and str_replace carries two copies
        # of a code block in them — worth seeing separately from the results.
        self.call_args[tool] += sum(len(str(v)) for v in (arguments or {}).values())

    def note_result(self, tool: str, text: str) -> None:
        self.results[tool] += len(text or "")

    @property
    def live_total(self) -> int:
        """Everything currently in the conversation, after any trimming."""
        return (self.system + self.schemas + self.prose
                + sum(self.call_args.values()) + sum(self.results.values())
                - self.trimmed_chars)

    def summary(self) -> str:
        """One line for the event stream, so a run says what filled it."""
        live = self.live_total
        parts = [f"~{live // CHARS_PER_TOKEN:,} tok in the conversation"]
        if self.trimmed_count:
            parts.append(f"{self.trimmed_count} stale result(s) trimmed, "
                         f"saving ~{self.trimmed_chars // CHARS_PER_TOKEN:,} tok")
        return "[context] " + "; ".join(parts)

    def breakdown(self) -> str:
        """The detail, for a run that died and has to explain itself."""
        rows = [("system prompt", self.system), ("tool schemas", self.schemas),
                ("model prose", self.prose)]
        rows += [(f"{tool} args", n) for tool, n in self.call_args.most_common()]
        rows += [(f"{tool} results", n) for tool, n in self.results.most_common()]
        rows = [(label, n) for label, n in rows if n]
        width = max((len(label) for label, _ in rows), default=0)
        return "\n".join(
            f"  {label:<{width}}  {n:>8,} chars  ~{n // CHARS_PER_TOKEN:>6,} tok"
            for label, n in sorted(rows, key=lambda r: -r[1]))


# Argument keys worth keeping whole however old the call is. They are short, and
# they are what makes a trimmed call still readable as history — "str_replace on
# core/parsers/x.py" says something; "str_replace on [trimmed]" says nothing.
KEEP_ARG_KEYS = frozenset({"path", "command", "pattern", "insert_line", "start_line",
                           "end_line", "level", "limit", "since_minutes", "source_key"})


def stub_for_arg(key: str, size: int) -> str:
    return f"[{key}: {size:,} characters, {TRIM_MARKER} — this call already ran]"


def _trim_arguments(arguments: dict, min_chars: int) -> tuple[dict, int]:
    """Shorten the bulky string values of a call that has already happened.

    Every key survives with a string value, because the assistant message is
    replayed to the provider and a tool_use whose input lost a required field is
    a malformed conversation. Only the *content* of the big ones goes.
    """
    trimmed, saved = dict(arguments), 0
    for key, value in arguments.items():
        if key in KEEP_ARG_KEYS or not isinstance(value, str) or len(value) < min_chars:
            continue
        if TRIM_MARKER in value:
            continue
        replacement = stub_for_arg(key, len(value))
        saved += len(value) - len(replacement)
        trimmed[key] = replacement
    return trimmed, saved


def _call_slots(messages: list) -> list[tuple[str, object, object]]:
    """Every tool call's arguments, oldest first, as something we can write back.

    Two shapes, and the Anthropic one needs care: its tool_use entries are SDK
    block objects, not dicts, so they are REPLACED with the plain dict the API
    also accepts rather than mutated in place — the same move
    `_collapse_anthropic_content` already makes for a trimmed text block.
    """
    slots: list[tuple[str, object, object]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for index, block in enumerate(content):
                kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if kind == "tool_use":
                    slots.append(("anthropic", content, index))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                slots.append(("openai", call, None))
    return slots


def collapse_stale_call_args(
    messages: list,
    ledger: Ledger,
    keep_last: int = KEEP_LAST_RESULTS,
    min_chars: int = MIN_COLLAPSE_CHARS,
    trim_above: int = TRIM_ABOVE_CHARS,
) -> int:
    """Shorten the arguments of tool calls the agent has finished with.

    Measured, after collapsing results alone was not enough: `str_replace`
    arguments were the single largest consumer of a run that died — 9,324 tokens,
    more than command output, more than every file read together. Each edit
    carries `old_str` AND `new_str`, and both are pure history the moment the
    edit lands. Results were collapsed first because they were the obvious half;
    this is the half the instrumentation found.
    """
    if ledger.live_total < trim_above:
        return 0

    slots = _call_slots(messages)
    stale = slots[:-keep_last] if keep_last else slots

    saved = 0
    for shape, holder, index in stale:
        if shape == "anthropic":
            block = holder[index]
            if isinstance(block, dict):
                arguments = block.get("input") or {}
                identity = {"id": block.get("id"), "name": block.get("name")}
            else:
                arguments = getattr(block, "input", None) or {}
                identity = {"id": getattr(block, "id", ""), "name": getattr(block, "name", "")}
            if not isinstance(arguments, dict):
                continue
            trimmed, freed = _trim_arguments(arguments, min_chars)
            if freed <= 0:
                continue
            holder[index] = {"type": "tool_use", **identity, "input": trimmed}
            saved += freed
        else:
            function = holder["function"]
            raw = function.get("arguments")
            if not isinstance(raw, str) or len(raw) < min_chars:
                continue
            try:
                arguments = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(arguments, dict):
                continue
            trimmed, freed = _trim_arguments(arguments, min_chars)
            if freed <= 0:
                continue
            function["arguments"] = json.dumps(trimmed)
            saved += freed

    ledger.trimmed_chars += saved
    return saved


def _result_slots(messages: list) -> Iterator[tuple[dict, str]]:
    """Every tool result in the conversation, oldest first, as mutable handles.

    Both adapters are covered because both build their results out of plain
    dicts: the OpenAI-compatible one appends `{"role": "tool", "content": ...}`
    per result, the Anthropic one appends one user message whose content is a
    list of `tool_result` blocks. Anything else — assistant payloads, SDK block
    objects — is skipped rather than guessed at.
    """
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            yield message, "content"
            continue
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield block, "content"


def collapse_stale_results(
    messages: list,
    tool_by_id: dict[str, str],
    ledger: Ledger,
    keep_last: int = KEEP_LAST_RESULTS,
    min_chars: int = MIN_COLLAPSE_CHARS,
    trim_above: int = TRIM_ABOVE_CHARS,
) -> int:
    """Replace the text of old, large tool results with a stub. Returns chars saved.

    Does nothing until the conversation exceeds `trim_above` characters — see
    that constant for why waiting is worth more than trimming early.

    `tool_by_id` maps a tool_use/tool_call id to the tool's name, so the stub can
    say which tool it is standing in for. A result whose id is unknown is still
    collapsed — under a generic name rather than being left alone, since being
    unable to name it is not a reason to keep 8,000 characters of it.
    """
    # `<`, not `<=`, so trim_above=0 means "always" rather than "never on an
    # empty conversation" — the degenerate case the tests lean on.
    if ledger.live_total < trim_above:
        return 0

    slots = list(_result_slots(messages))
    stale = slots[:-keep_last] if keep_last else slots

    saved = 0
    collapsed = 0
    for holder, key in stale:
        text = holder.get(key)
        if not isinstance(text, str) or len(text) < min_chars:
            continue
        if text.startswith("[") and TRIM_MARKER in text:
            continue  # already collapsed on an earlier turn
        identifier = holder.get("tool_use_id") or holder.get("tool_call_id") or ""
        replacement = stub_for(tool_by_id.get(identifier, "a tool"), len(text))
        saved += len(text) - len(replacement)
        collapsed += 1
        holder[key] = replacement

    ledger.trimmed_chars += saved
    ledger.trimmed_count += collapsed
    return saved
