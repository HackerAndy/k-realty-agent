"""Workflow: have the embedded agent build a deterministic parser for a source.

Composes the parser-builder system prompt + a task, runs the agent loop (which
studies the existing parser, writes core/parsers/<key>.py, registers it, and
self-verifies), then does an INDEPENDENT verification in a clean subprocess
(don't just trust the agent's word) and returns the result for the harness to
show the human. Activation (flipping the source to `implemented` in
services.yaml) is the human's call in the TUI — this workflow never touches it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from orchestration.codegen import run_codegen_gated
from orchestration.verify import run_test_file, test_path_for

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS = REPO_ROOT / "core" / "prompts"
# The invariants both jobs share, plus the half that applies to the job at hand —
# see the same split in build_scraper.py. Building and fixing want different
# instructions, and the one the agent isn't doing is context it has to read past.
CONTRACT_PATH = PROMPTS / "parser_contract.v1.md"
SYSTEM_PROMPT_PATH = PROMPTS / "parser_builder.v1.md"
REVISE_PROMPT_PATH = PROMPTS / "parser_reviser.v1.md"


def _system(job: Path) -> str:
    return CONTRACT_PATH.read_text() + "\n\n" + job.read_text()


def build_parser_for_source(
    source_key: str,
    sample_path: Path,
    source_label: str = "",
    on_event: Callable[[str], None] = print,
) -> dict:
    """Run the agent to build a parser for `source_key`, then verify it
    independently. Returns a dict with the agent's summary, the tool calls it
    made (audit), and the verification result (transactions or error)."""
    system = _system(SYSTEM_PROMPT_PATH)
    task = (
        f"Build a deterministic parser for the source '{source_key}'"
        + (f" ({source_label})" if source_label else "")
        + f".\n\nA real sample document is at: {sample_path}\n"
        f"Write the parser to: core/parsers/{source_key}.py\n"
        f"Register it in core/parsers/__init__.py under the key '{source_key}'.\n"
        f"Write a SELF-CONTAINED test (inline sample, not a data/ file) to "
        f"{test_path_for('parser', source_key)} and run it — it MUST pass. The harness "
        "re-runs it independently and will not approve an untested or failing parser.\n"
        "Also sanity-run the parser against the real sample before you finish."
    )

    result, verification = run_codegen_gated(
        task, system, lambda: verify_parser(source_key, sample_path), on_event=on_event, test_path=test_path_for("parser", source_key)
    )
    return {
        "source_key": source_key,
        "parser_path": f"core/parsers/{source_key}.py",
        "agent_summary": result.final_text,
        "tool_calls": result.tool_calls,
        "verification": verification,
    }


def revise_parser_for_source(
    source_key: str,
    sample_path: Path,
    feedback: str,
    source_label: str = "",
    on_event: Callable[[str], None] = print,
) -> dict:
    """Have the agent REVISE an existing parser per the operator's feedback,
    then re-verify. This is the debug/maintain half — the operator says what's
    wrong in plain English, the harness fixes its own code."""
    system = _system(REVISE_PROMPT_PATH)
    task = (
        f"The parser at core/parsers/{source_key}.py already exists, but the operator "
        f"reviewed its output and wants changes.\n\n"
        f"OPERATOR FEEDBACK (address this exactly):\n{feedback}\n\n"
        f"Sample document: {sample_path}\n"
        f"Read the current parser first, then revise it to address the feedback. Keep it "
        f"registered under '{source_key}'. Preserve the source's real columns verbatim in "
        f"Transaction.fields — don't invent columns. UPDATE the test at "
        f"{test_path_for('parser', source_key)} to cover the change and run it — it MUST pass "
        "(the harness re-runs it). Say what you changed.\n"
        "If you find the code is ALREADY correct — the reported problem was fixed on an "
        "earlier run — call no_change_needed with what you checked, and stop. Do not invent "
        "an edit to satisfy the gate."
    )
    result, verification = run_codegen_gated(
        task, system, lambda: verify_parser(source_key, sample_path),
        on_event=on_event, require_changes=True,
        test_path=test_path_for("parser", source_key),
    )
    return {
        "source_key": source_key,
        "parser_path": f"core/parsers/{source_key}.py",
        "agent_summary": result.final_text,
        "tool_calls": result.tool_calls,
        "verification": verification,
    }


def verify_parser(source_key: str, sample_path: Path) -> dict:
    """Independently verify the agent's parser. The GATE is the agent's own test
    (run in a fresh subprocess) — untested or failing code is not `ok`. Also runs
    the parser on the real sample so the human can preview its output.

    Returns {ok, test, transactions|error}. `ok` requires the test to pass AND the
    sample to parse."""
    test = run_test_file(test_path_for("parser", source_key))

    code = (
        "import json; from pathlib import Path; from core.parsers import get_parser; "
        f"ts = get_parser({source_key!r})(Path({str(sample_path)!r})); "
        "print(json.dumps([t.model_dump(mode='json') for t in ts]))"
    )
    proc = subprocess.run(
        ["poetry", "run", "python", "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return {"ok": False, "test": test, "error": (proc.stderr or proc.stdout).strip()[-4000:]}
    try:
        transactions = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        return {"ok": False, "test": test, "error": f"Could not read parser output: {exc}\n{proc.stdout[-2000:]}"}
    return {"ok": test["ok"], "test": test, "transactions": transactions}
