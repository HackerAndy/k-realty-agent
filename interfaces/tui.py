# Template candidate: generic (tier 1) — the umbrella-menu pattern (one entry
# point wiring to this client's core/ functions) is client-agnostic.
# See agent-harness-template/docs/promotion-log.md.
"""The single operator interface for the K-Realty Property Finance Tracker.

One entry point:

    poetry run agent

Right now it does one job: parse an Owner Statement PDF into transactions
and let you review them. Categorization, P&L, and thresholds were
deliberately left out of the starting flow — the focus is clean parsing.

Everything runs locally: statement PDFs are read from wherever you point
at, parsed transactions are written to data/ inside this repo, and
credentials stay in the encrypted local store. One deliberate exception to
local-only: the optional AI extraction fallback sends statement text to the
Anthropic API, and only ever after you consent.

Architecture note: menu items call the plain functions in core/ directly
for now. Once orchestration/graph.py is built out, this file switches to
calling the orchestration API instead — the menu shouldn't need to change.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import questionary

from core.ingest import DATA_DIR, ingest_statement, load_latest_parsed, transactions_from_run
from core.models import Transaction
from core.tools.buildium_owner_statement import StatementParseError
from core.tools.service_manifest import ServiceManifest

MANAGE_SECRETS = Path(__file__).resolve().parent.parent / "scripts" / "manage_secrets.py"


def _print_transactions(transactions: list[Transaction]) -> None:
    print(f"\n{'Date':8} {'Property':22} {'Unit':8} {'Amount':>12}  Description")
    print("-" * 100)
    for t in transactions:
        unit = t.unit_id or "-"
        print(
            f"{t.transaction_date:%m/%d/%y} {t.property_id:22} {unit:8} "
            f"{t.amount:>12,.2f}  {t.description[:60]}"
        )
    money_in = sum(t.amount for t in transactions if t.amount > 0)
    money_out = sum(t.amount for t in transactions if t.amount < 0)
    print("-" * 100)
    print(f"{len(transactions)} transactions | money in {money_in:,.2f} | money out {money_out:,.2f}")


def action_parse() -> None:
    pdf_path = questionary.path("Path to the Owner Statement PDF:").ask()
    if not pdf_path:
        return
    pdf = Path(pdf_path).expanduser()
    if not pdf.exists():
        print(f"No file at {pdf}")
        return
    print(f"Parsing {pdf}...")
    try:
        run = ingest_statement(pdf)
    except StatementParseError as exc:
        print(f"The built-in parser could not read this statement layout:\n{exc}\n")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("AI fallback unavailable: ANTHROPIC_API_KEY is not set in this shell.")
            print("Set it and re-run, or share the extracted text above so the parser can be fixed.")
            return
        consent = questionary.confirm(
            "Try the AI extraction fallback? This sends the statement's TEXT to the "
            "Anthropic API — the only step where data leaves this machine.",
            default=False,
        ).ask()
        if not consent:
            print("Skipped. The extracted text was saved under data/debug/ for inspection.")
            return
        print("Extracting via Anthropic API (claude-opus-4-8)...")
        try:
            run = ingest_statement(pdf, allow_llm_fallback=True)
        except Exception as llm_exc:
            print(f"AI fallback failed: {llm_exc}")
            return

    _print_transactions(transactions_from_run(run))
    method = "AI fallback" if run["extraction_method"] == "llm_fallback" else "built-in parser"
    print(f"\nParsed via {method}. Saved to {run['run_path']} (stays on this machine).")


def action_view_latest() -> None:
    run = load_latest_parsed()
    if run is None:
        print("Nothing parsed yet — parse a statement first.")
        return
    print(f"Latest parse: {run['month']} (from {run['run_path']})")
    _print_transactions(transactions_from_run(run))


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
    run = load_latest_parsed()
    if run is None:
        print("Statement parsing: never run.")
    else:
        print(f"Latest parse: {run['month']} — {run['transaction_count']} transaction(s).")
    print(f"Parsed data lives in {DATA_DIR}/ (gitignored, local only).")
    print("Not yet built (deliberately kept out of the starting flow): categorization, "
          "P&L reporting, dollar thresholds, the other 7 source scrapers, Telegram "
          "alerts, orchestration graph.")


def main() -> int:
    print("K-Realty Property Finance Tracker")
    print("Parses an Owner Statement PDF into transactions. Data stays on this machine")
    print("(parsed output in data/, credentials encrypted in .secrets/), except the")
    print("optional AI parse fallback, which asks consent before sending text out.\n")
    actions = {
        "Parse a statement (PDF → transactions)": action_parse,
        "View latest parsed transactions": action_view_latest,
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
