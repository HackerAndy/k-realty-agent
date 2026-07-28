"""Reusable 'the embedded agent writes TESTED code' primitive.

Every workflow where the agent writes code goes through here, so the standards
apply UNIVERSALLY — not re-stated (or forgotten) per workflow. Today: build_parser
and build_scraper. Any future builder (a fetcher, a report generator, …) calls
run_codegen and inherits the same rules + the same test gate for free.

The two halves:
  - CODE_STANDARDS: prepended to every code-gen system prompt (behavioural rule).
  - untested_code_files(): a post-run safety net that flags code written without a
    test, for workflows that don't have a single fixed test path to run.

Lives in orchestration/ (the agent layer), free to use the agent + anthropic SDK.
"""

from __future__ import annotations

from collections.abc import Callable

from orchestration.agent import AgentResult, run_agent

# Prepended to EVERY code-gen system prompt — the rules that apply to ALL code the
# embedded agent writes, regardless of the specific task.
CODE_STANDARDS = """\
## Standards for ANY code you write (these apply to every task, not just this one)

- WRITE A SELF-CONTAINED TEST for the code you write, and it MUST pass. Embed a
  small representative sample INLINE in the test — never load a gitignored data/
  file. The harness re-runs your test independently; code without a passing test
  is NOT approved. This is non-negotiable and applies to ALL code you produce.
- On any failure you hit, call `read_logs` FIRST and fix the actual cause. If it is
  an external limit (API/billing), a missing credential, or a CAPTCHA, say so and
  STOP rather than thrash.
- Keep core/ framework-free (no langgraph/langchain) — run
  `poetry run python scripts/check_portability.py` before finishing.
- Faithful data: preserve each source's real columns verbatim in
  Transaction.fields; normalize only date/amount/description; invent nothing.
"""


def run_codegen(
    task: str,
    system: str,
    on_event: Callable[[str], None] = print,
    **kwargs,
) -> AgentResult:
    """Run the embedded agent to write code, with CODE_STANDARDS prepended to the
    task-specific system prompt. ALL code generation should go through here so the
    standards are inescapable."""
    return run_agent(task, CODE_STANDARDS + "\n\n" + system, on_event=on_event, **kwargs)


def _is_code_file(path: str) -> bool:
    return (
        (path.startswith("core/") or path.startswith("orchestration/"))
        and path.endswith(".py")
        and not path.endswith("__init__.py")
    )


def untested_code_files(tool_calls: list[tuple[str, dict]]) -> list[str]:
    """Safety net for the general case: code files the agent wrote WITHOUT writing
    any test. A workflow with a fixed test path relies on verify.run_test_file
    instead; this catches any future builder. Returns [] if a test was written (or
    no code was)."""
    written = [
        inp.get("path", "")
        for name, inp in tool_calls
        if name == "write_file" and isinstance(inp, dict)
    ]
    code = [p for p in written if _is_code_file(p)]
    wrote_test = any(p.startswith("tests/") for p in written)
    return code if (code and not wrote_test) else []


def files_written(tool_calls: list[tuple[str, dict]]) -> list[str]:
    return [
        inp.get("path", "")
        for name, inp in tool_calls
        if name == "write_file" and isinstance(inp, dict) and inp.get("path")
    ]


def fold_untested(verification: dict, tool_calls: list[tuple[str, dict]]) -> dict:
    """If the agent wrote code without a test, mark the verification not-ok and
    record which files. Reusable across builders."""
    untested = untested_code_files(tool_calls)
    if untested:
        verification["untested_code"] = untested
        verification["ok"] = False
    return verification


def fold_noop(verification: dict, tool_calls: list[tuple[str, dict]]) -> dict:
    """A fix that changed NOTHING is not a success, however green the tests look.

    Seen in the field: asked to add missing test coverage, the agent wrote no
    files, re-ran the existing suite, and the harness reported ok — because
    untested_code_files() only fires when code IS written. A no-op scored a
    perfect green, which is worse than a failure: it ends the conversation.
    """
    if not files_written(tool_calls):
        verification["no_changes"] = True
        verification["ok"] = False
    return verification


def run_codegen_gated(
    task: str,
    system: str,
    verify: Callable[[], dict],
    on_event: Callable[[str], None] = print,
    *,
    require_changes: bool = False,
    max_retries: int = 1,
    **kwargs,
) -> tuple[AgentResult, dict]:
    """Run code-gen, verify, and give the agent ONE chance to fix its own process
    failure before handing the operator a bad result.

    `verify` re-runs the independent check and returns a verification dict.

    The retry exists because the two failures below are the harness's rules being
    ignored, not hard problems — and the operator was previously the one who had
    to notice and say "you didn't test that", which is the harness's job:

      - code written with no test at all (untested_code)
      - a fix that changed nothing (no_changes, when require_changes)

    A genuinely FAILING test is not retried here: that's a real engineering
    problem for the operator to see and direct, not a rule the agent forgot.
    """
    result = run_agent(task, CODE_STANDARDS + "\n\n" + system, on_event=on_event, **kwargs)
    verification = fold_untested(verify(), result.tool_calls)
    if require_changes:
        verification = fold_noop(verification, result.tool_calls)

    for _ in range(max_retries):
        reasons = []
        if verification.get("untested_code"):
            reasons.append(
                "You changed " + ", ".join(verification["untested_code"]) +
                " but wrote no test for it. Add a self-contained test that actually EXERCISES "
                "what you changed, and run it. If the change genuinely cannot be unit-tested "
                "(pure wiring such as a header or URL), say so explicitly and say what a live "
                "run would have to show instead — do not write a test that only restates the code."
            )
        if verification.get("no_changes"):
            reasons.append(
                "You reported success without writing any file, so nothing was fixed. "
                "Make the actual change, or state plainly that no change is needed and why."
            )
        if not reasons:
            break

        on_event("\nThe harness rejected that run: " + " ".join(reasons) + "\nRetrying once.\n")
        result = run_agent(
            "\n\n".join(reasons) + f"\n\nOriginal task, for context:\n{task}",
            CODE_STANDARDS + "\n\n" + system,
            on_event=on_event,
            **kwargs,
        )
        verification = fold_untested(verify(), result.tool_calls)
        if require_changes:
            verification = fold_noop(verification, result.tool_calls)

    return result, verification
