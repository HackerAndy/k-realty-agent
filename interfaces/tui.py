# Template candidate: generic (tier 1) — the umbrella-menu pattern (one entry
# point: run cycle / review queue / report / credentials / status) is
# client-agnostic; the menu items wire to this client's core/ functions.
# See agent-harness-template/docs/promotion-log.md.
"""The single operator interface for the K-Realty Property Finance Tracker.

One entry point controls everything:

    poetry run agent

Everything runs locally: statement PDFs are read from wherever you point
at, results are written to data/ inside this repo, and credentials stay in
the encrypted local store. One deliberate exception to local-only: the AI
extraction fallback sends statement text to the Anthropic API, gated behind
an explicit per-run consent prompt.

Architecture note: menu items call the plain business functions in core/
directly for now. Once orchestration/graph.py is built out (checkpointing,
HITL interrupts), this file switches to calling the orchestration API
instead — the menu itself shouldn't need to change.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import questionary
import yaml

from core.models import Decision, DecisionStatus, Transaction
from core.monthly_cycle import DATA_DIR, load_latest_run, run_monthly_cycle
from core.tools.buildium_owner_statement import StatementParseError
from core.tools.pnl_report import DEFAULT_CATEGORIES_PATH, render_pnl
from core.tools.service_manifest import ServiceManifest

MANAGE_SECRETS = Path(__file__).resolve().parent.parent / "scripts" / "manage_secrets.py"


def _load_pairs(run: dict) -> list[tuple[Transaction, Decision]]:
    return [
        (Transaction.model_validate(t), Decision.model_validate(d))
        for t, d in zip(run["transactions"], run["decisions"])
    ]


def _save_run(run: dict, pairs: list[tuple[Transaction, Decision]]) -> None:
    run["transactions"] = [t.model_dump(mode="json") for t, _ in pairs]
    run["decisions"] = [d.model_dump(mode="json") for _, d in pairs]
    month_label = f"{pairs[0][0].transaction_date:%B %Y}" if pairs else "unknown month"
    run["report"] = render_pnl(pairs, month_label)
    run_path = Path(run["run_path"])
    persisted = {k: v for k, v in run.items() if k not in ("run_path", "needs_review_count")}
    run_path.write_text(json.dumps(persisted, indent=2))
    print(f"Saved updates to {run_path}")


def _category_choices() -> list[questionary.Choice]:
    data = yaml.safe_load(DEFAULT_CATEGORIES_PATH.read_text())
    choices = []
    for section in ("income_categories", "expense_categories"):
        for entry in data.get(section, []):
            choices.append(
                questionary.Choice(title=f"{entry['label']} (line {entry['schedule_e_line']})", value=entry["key"])
            )
    return choices


def action_run_cycle() -> None:
    pdf_path = questionary.path("Path to the Owner Statement PDF:").ask()
    if not pdf_path:
        return
    pdf = Path(pdf_path).expanduser()
    if not pdf.exists():
        print(f"No file at {pdf}")
        return
    print(f"Reading {pdf} and categorizing against core/policies/ rules...")
    try:
        run = run_monthly_cycle(pdf)
    except StatementParseError as exc:
        print(f"The built-in parser could not read this statement layout:\n{exc}\n")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("LLM fallback unavailable: ANTHROPIC_API_KEY is not set in this shell.")
            print("Set it and re-run, or share the extracted text above so the parser can be fixed.")
            return
        consent = questionary.confirm(
            "Try the AI extraction fallback? This sends the statement's TEXT "
            "to the Anthropic API (the only step where data leaves this machine). "
            "Every extracted transaction will be flagged for your review, never auto-approved.",
            default=False,
        ).ask()
        if not consent:
            print("Skipped. The extracted text was saved under data/debug/ for inspection.")
            return
        print("Extracting via Anthropic API (claude-opus-4-8)...")
        try:
            run = run_monthly_cycle(pdf, allow_llm_fallback=True)
        except Exception as llm_exc:
            print(f"LLM fallback failed: {llm_exc}")
            return
        print("Extraction complete — ALL transactions below are flagged for review.")
    print(f"\n{run['report']}\n")
    print(f"Run saved to {run['run_path']} (stays on this machine).")
    if run["needs_review_count"]:
        print(f"{run['needs_review_count']} transaction(s) need review — use 'Review flagged transactions'.")


def action_review() -> None:
    run = load_latest_run()
    if run is None:
        print("No runs yet — run the monthly cycle first.")
        return
    pairs = _load_pairs(run)
    flagged = [i for i, (_, d) in enumerate(pairs) if d.status == DecisionStatus.NEEDS_REVIEW]
    if not flagged:
        print("Nothing flagged in the latest run.")
        return

    print(f"{len(flagged)} flagged transaction(s) in {run['month']}. For each, pick a "
          "category, keep it flagged, or stop.")
    changed = False
    for i in flagged:
        transaction, decision = pairs[i]
        print(f"\n  {transaction.transaction_date:%m/%d} {transaction.amount:>10,.2f}  "
              f"{transaction.description}  [{transaction.property_id}]")
        print(f"  Why flagged: {decision.reasoning}")
        choice = questionary.select(
            "Assign a Schedule E category:",
            choices=[*_category_choices(),
                     questionary.Choice(title="— Keep flagged for now", value="__keep__"),
                     questionary.Choice(title="— Stop reviewing", value="__stop__")],
            default=decision.recommendation,
        ).ask()
        if choice in (None, "__stop__"):
            break
        if choice == "__keep__":
            continue
        pairs[i] = (
            transaction,
            Decision(
                entity_id=transaction.entity_id,
                status=DecisionStatus.AUTO_APPROVED,
                recommendation=choice,
                confidence=1.0,
                reasoning="Manually categorized by the operator via the TUI.",
            ),
        )
        run["audit_records"].append(
            {
                "entity_id": transaction.entity_id,
                "decision": pairs[i][1].model_dump(mode="json"),
                "actor": "human:operator",
                "created_at": datetime.now(UTC).isoformat(),
                "notes": "Resolved from the TUI review queue.",
            }
        )
        changed = True
    if changed:
        _save_run(run, pairs)
        print("\nUpdated P&L:\n")
        print(run["report"])


def action_view_report() -> None:
    run = load_latest_run()
    if run is None:
        print("No runs yet — run the monthly cycle first.")
        return
    print(f"\n{run['report']}\n")
    print(f"(from {run['run_path']})")


def action_services() -> None:
    choice = questionary.select(
        "Services & credentials (runs scripts/manage_secrets.py):",
        choices=["list", "setup", "add", "edit", "remove", "generate-key", "back"],
    ).ask()
    if choice in (None, "back"):
        return
    if choice in ("add", "edit", "remove"):
        args = ["services", choice]
        if choice in ("edit", "remove"):
            key = questionary.text("Service key:").ask()
            if not key:
                return
            args.append(key)
    elif choice == "list":
        args = ["services", "list"]
    else:
        args = [choice]
    subprocess.run([sys.executable, str(MANAGE_SECRETS), *args])


def action_status() -> None:
    services = ServiceManifest().load()
    print(f"\nServices in manifest: {len(services)} (core/policies/services.yaml)")
    run = load_latest_run()
    if run is None:
        print("Monthly cycle: never run.")
    else:
        pairs = _load_pairs(run)
        flagged = sum(1 for _, d in pairs if d.status == DecisionStatus.NEEDS_REVIEW)
        print(f"Latest run: {run['month']} — {len(pairs)} transaction(s), {flagged} flagged.")
    print(f"Run data lives in {DATA_DIR}/ (gitignored, local only).")
    print("Not yet built: portal scrapers for the other 7 sources, Telegram alerts, "
          "LLM assist for unknowns, orchestration graph.")


def main() -> int:
    print("K-Realty Property Finance Tracker")
    print("Data stays on this machine (runs in data/, credentials encrypted in .secrets/),")
    print("with one exception: the optional AI extraction fallback sends statement text")
    print("to the Anthropic API — and only ever asks for your consent first.\n")
    actions = {
        "Run monthly cycle (Owner Statement PDF → P&L)": action_run_cycle,
        "Review flagged transactions": action_review,
        "View latest P&L report": action_view_report,
        "Manage services & credentials": action_services,
        "Status": action_status,
    }
    while True:
        choice = questionary.select("What would you like to do?", choices=[*actions, "Quit"]).ask()
        if choice in (None, "Quit"):
            return 0
        actions[choice]()
        print()


if __name__ == "__main__":
    sys.exit(main())
