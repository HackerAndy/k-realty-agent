"""Workflow: have the embedded agent build a portal scraper from a demonstration.

The scraper analog of build_parser. Steps:
  1. Record the operator's DEMONSTRATION (they log in, set filters, click Generate)
     — core/tools/demo_recorder.py captures the network requests + clicks + page.
  2. Run the agent (scraper_builder prompt + a task pointing at the demonstration).
     The agent finds the data endpoint / navigation, writes core/scrapers/<key>.py,
     registers it, and self-verifies against the CAPTURED data.
  3. Independently confirm the scraper imported + registered (fresh subprocess).
Activation (flipping the source on in services.yaml) stays the human's call in the
TUI — this workflow never touches it.

Domain-agnostic: the agent, not this file, produces the client's scraper.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from core.tools import demo_recorder
from orchestration.agent import run_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = REPO_ROOT / "core" / "prompts" / "scraper_builder.v1.md"


def build_scraper_for_source(
    source_key: str,
    portal_url: str,
    source_label: str = "",
    on_event: Callable[[str], None] = print,
    demo_path: Path | str | None = None,
) -> dict:
    """Record a demonstration (or reuse one), then run the agent to author a
    scraper. Pass `demo_path` to reuse a prior capture — so a retry after an LLM
    fix doesn't force you to demonstrate again."""
    if demo_path is None:
        on_event(f"Recording your demonstration for {source_label or source_key} — a browser "
                 "will open; log in, set filters, click Generate, then press Enter.")
        demo_path = demo_recorder.record(source_key, portal_url)
        on_event(f"Demonstration captured → {demo_path}. The agent will now write the scraper.\n")
    else:
        on_event(f"Reusing your captured demonstration → {demo_path}. The agent will now write the scraper.\n")

    system = SYSTEM_PROMPT_PATH.read_text()
    task = (
        f"Build a portal scraper for the source '{source_key}'"
        + (f" ({source_label})" if source_label else "")
        + ".\n\nThe operator's demonstration (network requests, clicks, and the final "
        f"rendered page) is at: {demo_path}\n"
        f"Read it first. Write the scraper to: core/scrapers/{source_key}.py\n"
        f"Register it in core/scrapers/__init__.py under the key '{source_key}'.\n"
        "Prefer calling the data endpoint directly; fall back to replaying the clicks. "
        "Verify your extraction against the captured data before you finish."
    )

    result = run_agent(task, system, on_event=on_event)
    verification = verify_scraper(source_key)
    return {
        "source_key": source_key,
        "scraper_path": f"core/scrapers/{source_key}.py",
        "demonstration": str(demo_path),
        "agent_summary": result.final_text,
        "tool_calls": result.tool_calls,
        "verification": verification,
    }


def revise_scraper_for_source(
    source_key: str,
    feedback: str = "",
    on_event: Callable[[str], None] = print,
) -> dict:
    """Have the agent REVISE an existing scraper — the recovery half. The agent
    reads its own failure logs (read_logs) plus any operator feedback, fixes
    core/scrapers/<source_key>.py, and re-verifies. Used both when the operator
    requests changes and when a build fails verification."""
    system = SYSTEM_PROMPT_PATH.read_text()
    task = (
        f"The scraper at core/scrapers/{source_key}.py needs fixing.\n\n"
        "First call read_logs to see the harness's own recent failure records — the actual "
        "cause is there (with a remediation hint). If it's an external limit (API/billing), a "
        "missing credential, or a CAPTCHA, say so and stop; otherwise fix the code.\n"
        + (f"\nOPERATOR FEEDBACK (address this too):\n{feedback}\n" if feedback else "")
        + f"\nRead the current scraper first, keep it registered under '{source_key}', preserve "
        "the source's real columns in Transaction.fields, re-verify against the captured "
        "demonstration data, and say what you changed."
    )
    result = run_agent(task, system, on_event=on_event)
    verification = verify_scraper(source_key)
    return {
        "source_key": source_key,
        "scraper_path": f"core/scrapers/{source_key}.py",
        "agent_summary": result.final_text,
        "tool_calls": result.tool_calls,
        "verification": verification,
    }


def verify_scraper(source_key: str) -> dict:
    """Independently confirm the agent's scraper imports and is registered (fresh
    subprocess, so it also proves the registration took). Full data verification
    is the agent's job (against captured data) + the human's first live run."""
    code = (
        "from core.scrapers import REGISTRY; "
        f"assert {source_key!r} in REGISTRY, 'not registered'; "
        f"print('registered:', {source_key!r} in REGISTRY)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-3000:]}
    return {"ok": True, "detail": proc.stdout.strip()}
