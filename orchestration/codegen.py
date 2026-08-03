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

from core.tools import llm_provider
from orchestration import agent_tools
from orchestration.agent import AgentResult, run_agent
from orchestration.verify import (
    REPO_ROOT,
    covers_changes,
    hardcoded_options,
    lint,
    reconciles,
    removed_tests,
    snapshot_files,
    wholesale_rewrites,
)

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
        if name in agent_tools.WRITE_TOOLS and isinstance(inp, dict)
    ]
    code = _unique(p for p in written if _is_code_file(p))
    wrote_test = any(p.startswith("tests/") for p in written)
    return code if (code and not wrote_test) else []


def _unique(paths) -> list[str]:
    """Each path once, in the order it was first touched. Four edits to one file
    are one changed file — every caller here means the set, and the operator-
    facing message named the same file three times before this."""
    return list(dict.fromkeys(paths))


def files_written(tool_calls: list[tuple[str, dict]]) -> list[str]:
    """Every path the agent changed, by whichever tool it used.

    Keyed off `agent_tools.WRITE_TOOLS` rather than the name of one tool: this
    list is what every gate downstream means by "the change", so a write tool it
    doesn't recognise doesn't get a test required, coverage checked or lint run,
    and the run scores as a no-op that touched nothing.
    """
    return _unique(
        inp.get("path", "")
        for name, inp in tool_calls
        if name in agent_tools.WRITE_TOOLS and isinstance(inp, dict) and inp.get("path")
    )


def fold_untested(verification: dict, tool_calls: list[tuple[str, dict]]) -> dict:
    """If the agent wrote code without a test, mark the verification not-ok and
    record which files. Reusable across builders."""
    untested = untested_code_files(tool_calls)
    if untested:
        verification["untested_code"] = untested
        verification["ok"] = False
    return verification


def fold_uncovered(verification: dict, tool_calls: list[tuple[str, dict]], test_path: str) -> dict:
    """Require the test to EXERCISE what changed, not merely to exist.

    "Was a test written?" and "does a test cover this?" look identical when the
    agent leaves a stale test in place. Measured on this project's own history:
    the XSRF fix changed 22 executable lines and its passing test executed 4 of
    them — the other 18, including the whole token path, never ran.
    """
    code = [p for p in files_written(tool_calls) if _is_code_file(p)]
    if not code:
        return verification
    result = covers_changes(test_path, code)
    verification["coverage"] = result
    if result.get("checked") and not result["ok"]:
        verification["uncovered_changes"] = result.get("uncovered", {})
        verification["ok"] = False
    return verification


def fold_hardcoded(verification: dict, tool_calls: list[tuple[str, dict]]) -> dict:
    """Infrastructure rule, enforced rather than merely requested: the choices a
    portal asks for belong in settings, not baked into the code.

    The prompt already tells the agent to declare them. A prompt is advice — the
    Epic scraper was written under that instruction and still froze a 30-day
    window, an accounting basis, a property selection and two more. This turns it
    into something the build fails on.
    """
    offenders: dict[str, list[dict]] = {}
    for path in files_written(tool_calls):
        if not _is_code_file(path):
            continue
        found = hardcoded_options(path)
        if found:
            offenders[path] = found
    if offenders:
        verification["hardcoded_options"] = offenders
        verification["ok"] = False
    return verification


def fold_lint(verification: dict, tool_calls: list[tuple[str, dict]]) -> dict:
    """Code that reads as if it works and doesn't.

    Epic's scraper computed a property filter from the operator's selection and
    then never used it, so choosing a property silently did nothing. Every other
    gate was green: the settings were declared, read at run time, and the test
    passed — because the tests checked the declaration's SHAPE and nothing checked
    that a chosen value reached the request. A dead store is that bug, and ruff
    names it for free.
    """
    findings = lint(files_written(tool_calls))
    blocking = [f for f in findings if f.get("blocking", True)]
    advisory = [f for f in findings if not f.get("blocking", True)]
    if blocking:
        verification["lint"] = blocking
        verification["ok"] = False
    # Recorded even though it changes nothing, because the alternative — dropping
    # the rule so the finding never appears — is how F401 stopped being visible
    # to anyone at all. Advisory means "not worth a rebuild", not "not worth
    # knowing".
    if advisory:
        verification["lint_advisory"] = advisory
    return verification


def fold_rewrite(verification: dict, before: dict[str, str]) -> dict:
    """A fix that replaced the file instead of editing it is not a fix.

    The opposite failure to fold_noop, and it cost more: three revises in a row
    rewrote a working test file wholesale, the last one leaving it unparseable.
    Every other gate looks only at what the new file contains, so none of them can
    notice what the old one contained and the new one doesn't.
    """
    rewritten = wholesale_rewrites(before)
    if rewritten:
        verification["wholesale_rewrite"] = rewritten
        verification["ok"] = False

    # Deleting tests is the same loss in a shape the similarity ratio can't see:
    # a revise that cut a 778-line test file to 455 kept enough lines to score
    # well above the threshold, and six tests went with it.
    dropped = removed_tests(before)
    if dropped:
        verification["removed_tests"] = dropped
        verification["ok"] = False
    return verification


def fold_reconciliation(verification: dict, path: str) -> dict:
    """A scraper must check its own arithmetic, or say why it can't.

    The last unenforced rule in the scraper prompt, and the gap showed up in the
    field within one build: Epic reconciles, DFCU (same instructions, same model)
    does not, though the bank hands back a running balance on every row. An
    instruction nothing checks is a suggestion.
    """
    result = reconciles(path)
    if not result["ok"]:
        verification["unreconciled"] = result["detail"]
        verification["ok"] = False
    else:
        verification["reconciliation"] = result["detail"]
    return verification


def declared_no_change(tool_calls: list[tuple[str, dict]]) -> str:
    """The reason the agent gave for changing nothing, or "" if it never said."""
    for name, inp in tool_calls:
        if name == "no_change_needed" and isinstance(inp, dict):
            reason = (inp.get("reason") or "").strip()
            if reason:
                return reason
    return ""


def fold_noop(verification: dict, tool_calls: list[tuple[str, dict]]) -> dict:
    """A fix that changed NOTHING is not a success, however green the tests look.

    Seen in the field: asked to add missing test coverage, the agent wrote no
    files, re-ran the existing suite, and the harness reported ok — because
    untested_code_files() only fires when code IS written. A no-op scored a
    perfect green, which is worse than a failure: it ends the conversation.

    Unless the agent SAYS so. "Already fixed on the previous run" is a true
    answer, and a gate that counts files can't tell it from laziness — so it
    refused both, every time, and a source whose code was already correct could
    never be approved at all (a real deadlock: the operator's only two moves,
    revise again and give up, both leave it stuck). `no_change_needed` makes the
    claim explicit and attributable; the operator reads the reason and decides.
    Silence still fails, which is the case worth catching.
    """
    if files_written(tool_calls):
        return verification
    reason = declared_no_change(tool_calls)
    if reason:
        verification["no_change_reason"] = reason
        return verification
    verification["no_changes"] = True
    verification["ok"] = False
    return verification


def _executor(provider: str | None = None, model: str | None = None,
              api_url: str | None = None, **_ignored):
    """Which thing does the work: the harness's own loop, or the Claude Code CLI.

    Resolved through llm_provider like every other model decision, so Settings
    remains the single answer to "what is this harness running" — a second
    opinion here is exactly what the one-model-choice rule forbids.

    Both sides return the same AgentResult and leave their changes on disk, so
    everything downstream — the test gate, coverage, reconciliation, lint, the
    bench — is identical either way. Only who edits the files differs.
    """
    choice = llm_provider.resolve(provider=provider, model=model, base_url=api_url)
    if choice.is_agent:
        from orchestration.claude_code import run_claude_code
        return run_claude_code
    return run_agent


def run_codegen_gated(
    task: str,
    system: str,
    verify: Callable[[], dict],
    on_event: Callable[[str], None] = print,
    *,
    require_changes: bool = False,
    test_path: str | None = None,
    reconcile_path: str | None = None,
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
    # Every call this BUILD made, across rounds — not just the latest one.
    #
    # Each fold below asks "what did this build change?", and after a retry the
    # honest answer is cumulative. Judging a round in isolation punished the
    # agent for obeying the retry instruction precisely: told to fix two unused
    # locals, it edited only the parser, wrote no `tests/` file that round, and
    # `fold_untested` refused a build whose test existed and passed. The better
    # it followed the correction, the more certainly it failed the next gate.
    performed: list[tuple[str, dict]] = []

    def _assess(res):
        performed.extend(res.tool_calls)
        v = fold_untested(verify(), performed)
        # A loop that ended ITSELF (went in circles, hit the turn cap) has to say
        # so: the files it left behind may still be fine, but "it stopped early"
        # changes how much weight to put on them.
        if getattr(res, "stopped_reason", ""):
            v["agent_stopped"] = res.stopped_reason
        if test_path and not v.get("untested_code"):
            v = fold_uncovered(v, performed, test_path)
        v = fold_hardcoded(v, performed)
        v = fold_lint(v, performed)
        if reconcile_path and (REPO_ROOT / reconcile_path).is_file():
            v = fold_reconciliation(v, reconcile_path)
        if require_changes:
            v = fold_noop(v, res.tool_calls)   # "did THIS round write anything?"
            # The snapshot wins on conflict: it was taken before the run, whereas
            # a recorded original could be from a second write of the same file.
            v = fold_rewrite(v, {**agent_tools.originals(), **before})
        return v

    # What these files looked like BEFORE the agent touched them. Only meaningful
    # on a revise (require_changes): a build's first version has nothing to be
    # compared against. Taken here rather than inside the fold because by then the
    # agent has already overwritten them.
    #
    # Two sources, because neither alone is enough. This snapshot covers the two
    # files a revise is SUPPOSED to touch even if the agent never writes them;
    # agent_tools records anything else it actually overwrote, which is the only
    # way to catch damage to a file nobody predicted.
    agent_tools.forget_originals()
    before = snapshot_files(
        [p for p in (reconcile_path, test_path) if p] if require_changes else []
    )

    execute = _executor(**kwargs)
    result = execute(task, CODE_STANDARDS + "\n\n" + system, on_event=on_event, **kwargs)
    verification = _assess(result)

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
        if verification.get("uncovered_changes"):
            detail = "; ".join(f"{p} lines {ls}" for p, ls in verification["uncovered_changes"].items())
            reasons.append(
                "Your test passes but never RUNS the code you changed — " + detail + ". A test that "
                "leaves the change unexecuted proves nothing. Extend it so those lines actually run. "
                "If they genuinely cannot be exercised in a unit test (pure wiring such as a header "
                "or URL), say so explicitly and say what a live run would have to show instead."
            )
        if verification.get("hardcoded_options"):
            detail = "; ".join(
                f"{p}: " + ", ".join(f"line {o['line']} {o['detail']}" for o in found)
                for p, found in verification["hardcoded_options"].items())
            reasons.append(
                "You hardcoded choices that the operator must be able to change without a code "
                "edit — " + detail + ". Declare them in a module-level SETTINGS list and read them "
                "at run time with settings.values_for(SERVICE_KEY), using the values you saw in the "
                "demonstration as the DEFAULTS so behaviour is unchanged. If a value genuinely is "
                "fixed protocol rather than a preference, leave it and add a `# fixed: <reason>` "
                "comment on that line saying why."
            )
        if verification.get("lint"):
            detail = "; ".join(
                f"{f['path']} line {f['line']} {f['code']} {f['detail']}"
                for f in verification["lint"])
            reasons.append(
                "Your code contains something that does nothing — " + detail + ". A value you "
                "compute and never use is usually a wire you forgot to connect: if it came from "
                "the operator's settings, the setting is silently being ignored. Either use it, "
                "or delete it and say why it isn't needed. If it is a caught exception you never "
                "read (`except X as e`), put it in the log.failure record or in the error you "
                "raise — an error caught and recorded nowhere is the failure this harness exists "
                "to prevent — or drop the binding and write `except X:`."
            )
        if verification.get("wholesale_rewrite"):
            detail = "; ".join(
                f"{path} (only {int(ratio * 100)}% of its lines survived)"
                for path, ratio in verification["wholesale_rewrite"].items())
            reasons.append(
                "You REPLACED a file instead of editing it — " + detail + ". You were "
                "asked to fix something specific; rewriting from scratch silently drops "
                "work that was already there and already passing, and nothing else the "
                "harness checks can see what went missing. Start again from the file as "
                "it is on disk NOW: read it, change only the lines the problem is in, and "
                "leave everything else byte for byte."
            )
        if verification.get("removed_tests"):
            detail = "; ".join(
                f"{path}: {', '.join(names)}"
                for path, names in verification["removed_tests"].items())
            reasons.append(
                "You DELETED tests that were passing — " + detail + ". Every one of "
                "them proved something about this source, and with them gone the code "
                "they covered can break without anything noticing. Put them back exactly "
                "as they were, then make your change alongside them. If a test was "
                "genuinely wrong, fix its assertion — do not remove it. If you renamed "
                "one, say so explicitly in your report."
            )
        if verification.get("unreconciled"):
            reasons.append(
                "Your scraper doesn't check what it extracted against the source's own "
                "numbers — " + verification["unreconciled"] + " Look at the payload you "
                "already have: a per-account total, an ending or running balance, a row "
                "count. Call reconcile.record(label, expected=<the source's number>, "
                "actual=<what you extracted>) so a run can answer 'did we get "
                "everything'. A passing test cannot: it proves the parsing is unchanged, "
                "not that the portal gave you every row. If this source genuinely "
                "publishes no totals, set a module-level "
                "NO_CONTROL_TOTALS = \"<why>\" saying what you looked at and what wasn't "
                "there."
            )
        if verification.get("extracted_nothing"):
            reasons.append(
                "Your parser ran on the real sample document and returned NO transactions. "
                "The document has some — read it again and find where the rows actually "
                "are, rather than trusting the shape you assumed. A test that passes on "
                "an empty result is agreeing with the bug, so fix the test too: assert the "
                "real transaction count from the document you were given."
            )
        if verification.get("no_changes"):
            reasons.append(
                "You reported success without writing any file, so nothing was fixed. "
                "Make the actual change — or, if the code is genuinely already correct, "
                "call no_change_needed with what you checked and what proves it."
            )
        if not reasons:
            break

        on_event("\nThe harness rejected that run: " + " ".join(reasons) + "\nRetrying once.\n")
        result = execute(
            "\n\n".join(reasons) + f"\n\nOriginal task, for context:\n{task}",
            CODE_STANDARDS + "\n\n" + system,
            on_event=on_event,
            **kwargs,
        )
        verification = _assess(result)

    return result, verification
