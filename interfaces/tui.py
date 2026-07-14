# Template candidate: generic (tier 1) — the umbrella-menu pattern (one entry
# point wiring to this client's core/ functions) is client-agnostic.
# See agent-harness-template/docs/promotion-log.md.
"""The single operator interface for the K-Realty Property Finance Tracker.

One entry point:

    poetry run agent

Right now it does one job: ingest a source document (pick a source, point
at its file) into transactions. Each source's parser lives in core/parsers/
and is registered against that source in core/policies/services.yaml — so
the source picker doubles as the build-out map (implemented sources are
selectable; the rest are greyed with their status). Categorization, P&L,
and thresholds were deliberately left out of the starting flow.

Everything runs locally: documents are read from wherever you point at,
parsed transactions are written to data/ inside this repo, and credentials
stay in the encrypted local store. One deliberate exception to local-only:
the optional AI extraction fallback sends document text to the Anthropic
API, and only ever after you consent.

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

from core.ingest import (
    DATA_DIR,
    IngestError,
    ingest_source,
    load_latest_parsed,
    transactions_from_run,
)
from core.models import Transaction
from core.parsers import ParseError
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


def _pick_source() -> str | None:
    """Show every source from the manifest; only implemented ones are
    selectable, the rest are greyed with their build status."""
    services = ServiceManifest().load()
    choices = []
    for s in services:
        if s.status == "implemented":
            choices.append(questionary.Choice(title=f"{s.label}  ({s.input_type})", value=s.key))
        else:
            choices.append(
                questionary.Choice(
                    title=f"{s.label}  [{s.status}]", value=s.key, disabled=s.status
                )
            )
    return questionary.select("Which source do you want to ingest?", choices=choices).ask()


def action_ingest() -> None:
    source_key = _pick_source()
    if not source_key:
        return
    path_str = questionary.path("Path to the document (PDF/CSV):").ask()
    if not path_str:
        return
    doc = Path(path_str).expanduser()
    if not doc.exists():
        print(f"No file at {doc}")
        return
    print(f"Ingesting {source_key} from {doc}...")
    try:
        run = ingest_source(source_key, doc)
    except IngestError as exc:
        print(exc)
        return
    except ParseError as exc:
        print(f"The built-in parser could not read this layout:\n{exc}\n")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("AI fallback unavailable: ANTHROPIC_API_KEY is not set in this shell.")
            print("Set it and re-run, or share the extracted text above so the parser can be fixed.")
            return
        consent = questionary.confirm(
            "Try the AI extraction fallback? This sends the document's TEXT to the "
            "Anthropic API — the only step where data leaves this machine.",
            default=False,
        ).ask()
        if not consent:
            print("Skipped. The extracted text was saved under data/debug/ for inspection.")
            return
        print("Extracting via Anthropic API (claude-opus-4-8)...")
        try:
            run = ingest_source(source_key, doc, allow_llm_fallback=True)
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
    implemented = [s for s in services if s.status == "implemented"]
    print(f"\nSources (core/policies/services.yaml): {len(implemented)} of {len(services)} have a parser.")
    for s in services:
        marker = "✓" if s.status == "implemented" else " "
        parser = s.parser or "-"
        print(f"  [{marker}] {s.key:28} {s.status:13} parser={parser}")
    run = load_latest_parsed()
    print()
    if run is None:
        print("Nothing ingested yet.")
    else:
        print(f"Latest ingest: {run['source_key']} {run['month']} — "
              f"{run['transaction_count']} transaction(s).")
    print(f"Parsed data lives in {DATA_DIR}/ (gitignored, local only).")
    print("Deliberately not built yet: categorization, P&L, thresholds, the parsers for "
          "the other sources above, automated fetching, orchestration graph.")


def main() -> int:
    print("K-Realty Property Finance Tracker")
    print("Ingests a source document into transactions. Data stays on this machine")
    print("(parsed output in data/, credentials encrypted in .secrets/), except the")
    print("optional AI parse fallback, which asks consent before sending text out.\n")
    actions = {
        "Ingest a source (document → transactions)": action_ingest,
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
