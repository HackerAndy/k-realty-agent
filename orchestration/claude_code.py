"""Hand the job to the Claude Code CLI instead of driving the loop ourselves.

**Why this is not another adapter.** `orchestration/agent.py` talks to a model
endpoint: it sends messages, gets tool calls back, dispatches them, and manages
the context. Claude Code is a whole agent — its own loop, its own tools, its own
context management — so there is nothing for `LLMAdapter` to adapt. What the
harness wants from it is the same thing it wants from its own loop: *the files on
disk changed*. Everything downstream of that — `verify.py`, the test gate,
reconciliation, the lint classification, the bench — reads the working tree and
does not care who wrote it.

So this module is an alternative **executor** with the same signature and the
same `AgentResult`, and `codegen` picks between them on the resolved provider.

**The one piece of real integration is vocabulary.** Every gate decides what the
agent did by reading its tool calls back: `files_written`, `untested_code_files`,
`fold_uncovered`, the GUI's "what it did" list. Claude Code calls its tools
`Write`/`Edit`/`Bash` and reports absolute paths; the harness speaks
`write_file`/`str_replace`/`run_command` and repo-relative paths. Translating at
this boundary is what lets every gate keep working unchanged — and getting it
wrong would not fail loudly, it would silently report that a build touched
nothing, which is the same class of bug that nearly shipped when `str_replace`
was added.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from core.observability import get_logger
from core.tools import llm_provider
from orchestration import agent_tools
from orchestration.agent import AgentResult
from orchestration.context_budget import Ledger

log = get_logger("orchestration.claude_code")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Claude Code's tool names, in the harness's vocabulary. Only the ones the gates
# reason about need translating; anything else is passed through under its own
# name and simply counts as a read.
#
# The write set is what matters: a tool that changes a file and is NOT mapped to
# something in `agent_tools.WRITE_TOOLS` makes the whole build look like a no-op
# — no test required, no coverage checked, and "files written: none" on screen.
TOOL_NAMES = {
    "Write": "write_file",
    "Edit": "str_replace",
    "MultiEdit": "str_replace",
    "NotebookEdit": "write_file",
    "Read": "read_file",
    "Bash": "run_command",
    "Grep": "search_files",
    "Glob": "search_files",
}

# Where each tool keeps the thing the harness wants to record.
_PATH_KEYS = ("file_path", "path", "notebook_path")
_COMMAND_KEYS = ("command",)

# What the CLI may do without a human at the keyboard — the SAME set we can
# translate above, derived from it rather than listed twice so the two cannot
# drift. A tool the harness can't put into its own vocabulary is a tool whose
# work no gate would see, and it does not get pre-approved.
#
# This is not a widening: `agent_tools.run_command` gives the harness's own loop
# unrestricted shell in the repo already. It is `--permission-mode acceptEdits`
# alone that was NARROWER than the native path — it approves file edits and
# nothing else, so the agent could write a parser and its test and then be
# refused permission to RUN the test. That breaks the harness's central promise
# ("it tests the code it writes") from the outside, and quietly: the CLI reports
# `is_error: false` on a run it spent asking for a permission it never got.
ALLOWED_TOOLS = sorted(TOOL_NAMES)

# Denials of these mean the agent could not do the job at all, as opposed to
# being turned down for something incidental.
_ESSENTIAL = agent_tools.WRITE_TOOLS | {"run_command"}


def available() -> bool:
    """Is the CLI actually installed? Checked before offering it as a choice."""
    return shutil.which(llm_provider.CLAUDE_CODE_BINARY) is not None


def _relative(path: str) -> str:
    """Repo-relative, because that is what every gate matches against.

    Claude Code reports absolute paths. A gate comparing them to
    `core/parsers/x.py` matches nothing, and silence from a gate reads exactly
    like a clean build.
    """
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return path


def _translate(name: str, payload: dict) -> tuple[str, dict]:
    """One Claude Code tool call in the harness's own terms."""
    mapped = TOOL_NAMES.get(name, name)
    arguments: dict = {}
    for key in _PATH_KEYS:
        if isinstance(payload.get(key), str):
            arguments["path"] = _relative(payload[key])
            break
    for key in _COMMAND_KEYS:
        if isinstance(payload.get(key), str):
            arguments["command"] = payload[key]
            break
    return mapped, arguments


def _refused(result_event: dict, on_event: Callable[[str], None]) -> str:
    """Say so when the CLI was refused a permission, instead of letting the run
    look like it simply finished.

    The CLI answers `is_error: false` on a session it spent asking for a
    permission it never got — so a misconfigured permission mode reads on screen
    as a model that gave up, and the test gate then reports "no test was run"
    about a test the agent wrote and was forbidden to execute. Every denial is
    printed; only one that blocked writing or testing stops the run, because
    those are the two things the harness must have.
    """
    denials = result_event.get("permission_denials") or []
    blocked: list[str] = []
    for denial in denials:
        name = str(denial.get("tool_name") or "")
        mapped = TOOL_NAMES.get(name, name)
        on_event(f"[claude-code] refused permission to use {name}")
        if mapped in _ESSENTIAL:
            blocked.append(name)
    if not blocked:
        return ""
    return ("The Claude Code CLI was refused permission to use "
            + ", ".join(sorted(set(blocked)))
            + ", so it could not edit or test the code. This is the harness's "
              "configuration, not the model's doing — see ALLOWED_TOOLS in "
              "orchestration/claude_code.py.")


def _preview(name: str, arguments: dict) -> str:
    if arguments.get("path"):
        return arguments["path"]
    command = arguments.get("command", "")
    return command if len(command) <= 80 else command[:77] + "..."


def run_claude_code(
    task: str,
    system: str,
    on_event: Callable[[str], None] = print,
    max_turns: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    **_ignored,
) -> AgentResult:
    """Run one job through the CLI. Same contract as `agent.run_agent`.

    `system` is appended to Claude Code's own system prompt rather than
    replacing it: the harness's contract is a set of extra rules about this
    repository, not a substitute for knowing how to edit code, and replacing the
    prompt would throw away the thing we came for.

    `max_turns` is accepted for signature compatibility and CANNOT be honoured:
    the CLI runs its own loop and offers no turn cap (2.1.x has only
    `--max-budget-usd`, which is an API-billing number and means nothing on a
    subscription). It says so rather than accepting the number and ignoring it —
    a bound the caller believes in and nothing enforces is worse than no bound.
    """
    choice = llm_provider.resolve(provider=provider, model=model, base_url=api_url)
    on_event(f"[model] {choice.describe()}")
    if max_turns is not None:
        on_event(f"[claude-code] the {max_turns}-turn cap does not apply here — the CLI "
                 "runs its own loop and exposes no turn limit. It stops when it is done, "
                 "or when whatever launched it times out.")

    if not available():
        raise RuntimeError(log.failure(
            operation="claude_code_run",
            code="CLAUDE_CODE_NOT_INSTALLED",
            message=f"'{llm_provider.CLAUDE_CODE_BINARY}' is not on PATH, so the Claude "
                    "Code coding method cannot run.",
            remediation="Install the Claude Code CLI, or choose a different coding "
                        "method in Settings.",
            context={"binary": llm_provider.CLAUDE_CODE_BINARY},
        ))

    command = [
        llm_provider.CLAUDE_CODE_BINARY,
        "-p", task,
        "--output-format", "stream-json",
        "--verbose",
        "--model", choice.model,
        # It has to be able to edit AND run tests without a human at the
        # keyboard; this runs against a checkout the harness controls.
        "--permission-mode", "acceptEdits",
        "--allowedTools", " ".join(ALLOWED_TOOLS),
        "--append-system-prompt", system,
    ]

    env = dict(os.environ)
    if choice.api_key:
        env["ANTHROPIC_API_KEY"] = choice.api_key
    else:
        # Actively REMOVED, not merely left unset. The subprocess inherits this
        # process's environment, and `llm_provider.load_into_env()` exports
        # ANTHROPIC_API_KEY whenever the anthropic provider has ever been
        # configured — so without this, an operator who chose "use the CLI's own
        # login" would be moved onto per-token API billing by a setting they made
        # for something else, with nothing on screen saying so.
        for leaked in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(leaked, None)

    tool_calls: list[tuple[str, dict]] = []
    final_text = ""
    turns = 0
    stopped_reason = ""
    ledger = Ledger(system=len(system))

    process = subprocess.Popen(
        command, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # Not every line is an event — the CLI writes plain diagnostics too,
            # and losing them is how "it just failed" happens.
            on_event(line[:500])
            continue

        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    on_event(block["text"].strip())
                    ledger.note_prose(block["text"])
                elif block.get("type") == "tool_use":
                    name, arguments = _translate(block.get("name", ""), block.get("input") or {})
                    tool_calls.append((name, arguments))
                    ledger.note_call(name, arguments)
                    on_event(f"  → {name}({_preview(name, arguments)})")
        elif kind == "result":
            turns = int(event.get("num_turns") or 0)
            final_text = str(event.get("result") or "")
            cost = event.get("total_cost_usd")
            if event.get("is_error"):
                stopped_reason = (f"The Claude Code CLI ended with an error "
                                  f"({event.get('subtype') or 'unknown'}).")
            stopped_reason = _refused(event, on_event) or stopped_reason
            on_event(f"[claude-code] {turns} turns"
                     + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""))

    process.wait()
    if process.returncode != 0 and not stopped_reason:
        stopped_reason = f"The Claude Code CLI exited with status {process.returncode}."

    on_event(ledger.summary())
    return AgentResult(final_text, turns or 1, tool_calls,
                       stopped_reason=stopped_reason, context=ledger)
