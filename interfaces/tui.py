# Template candidate: generic (tier 1) — the umbrella-menu pattern (one entry
# point wiring to this client's core/ functions) is client-agnostic.
# See agent-harness-template/docs/promotion-log.md.
"""The single operator interface for the K-Realty Property Finance Tracker.

One entry point:

    poetry run agent

Ingest a source document into transactions. If the source already has a
committed parser (core/parsers/, registered in core/policies/services.yaml),
it runs that. If it DOESN'T, the harness handles it itself: extract now with
the LLM, and/or have the embedded agent (orchestration/agent.py) write a
reusable deterministic parser, verify it, and — with your approval in this
menu — activate it. That's the point: after onboarding you work with the
harness, not a code editor.

Everything runs locally: documents are read from wherever you point at,
parsed transactions are written to data/ inside this repo, and credentials
stay in the encrypted local store. The exceptions to local-only are the LLM
extraction and the agent — both send document/code text to the Anthropic API,
and only ever after you consent. Categorization and P&L were deliberately
left out of the starting flow.

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
    ingest_source,
    ingest_via_llm,
    load_latest_parsed,
    transactions_from_run,
)
from core.models import Transaction
from core.parsers import ParseError
from core.tools import llm_provider
from core.tools.credential_store import ensure_master_key
from core.tools.service_manifest import ServiceManifest

MANAGE_SECRETS = Path(__file__).resolve().parent.parent / "scripts" / "manage_secrets.py"


def ensure_llm_ready() -> bool:
    """Make sure the harness has an LLM key to work with — loaded from the
    encrypted store, or prompted for and stored on first run. The LLM key is
    just another secret; the harness sets itself up so this is the only place
    the operator ever needs to go. Returns True if an LLM is ready."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True  # provided via environment (advanced / CI path)
    ensure_master_key()
    if llm_provider.load_into_env():
        return True

    # First run (or key never stored): prompt and store it.
    print("This harness runs on the Claude API, which isn't set up yet.")
    print("Paste your Anthropic API key to enable the agent and AI extraction.")
    print("It's stored encrypted in .secrets/ alongside your other secrets and")
    print("loaded automatically on future runs — you'll only be asked once.")
    key = questionary.password("Anthropic API key:").ask()
    if not key or not key.strip():
        print("No key entered — agent/AI features stay unavailable this session.\n")
        return False
    llm_provider.store_llm_credential(key.strip())
    llm_provider.load_into_env()
    print("Saved. You won't be asked again.\n")
    return True


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
    """Show every source from the manifest, all selectable. Implemented ones
    note their format; the rest note their build status so you can see, at
    the moment of picking, which still need a parser."""
    services = ServiceManifest().load()
    choices = []
    for s in services:
        if s.status == "implemented":
            title = f"{s.label}  ({s.input_type}, parser ready)"
        else:
            title = f"{s.label}  ({s.status} — no parser yet)"
        choices.append(questionary.Choice(title=title, value=s.key))
    return questionary.select("Which source do you want to ingest?", choices=choices).ask()


def action_ingest() -> None:
    source_key = _pick_source()
    if not source_key:
        return
    manifest = ServiceManifest()
    source = manifest.get(source_key)
    doc = _ask_document()
    if doc is None:
        return
    if source.status == "implemented" and source.parser:
        _ingest_with_parser(source_key, doc)
    else:
        _handle_new_source(source_key, source, doc, manifest)


def _ask_document() -> Path | None:
    path_str = questionary.path("Path to the document (PDF/CSV):").ask()
    if not path_str:
        return None
    doc = Path(path_str).expanduser()
    if not doc.exists():
        print(f"No file at {doc}")
        return None
    return doc


def _ingest_with_parser(source_key: str, doc: Path) -> None:
    print(f"Ingesting {source_key} from {doc}...")
    try:
        run = ingest_source(source_key, doc)
    except ParseError as exc:
        print(f"The built-in parser could not read this layout:\n{exc}\n")
        if not ensure_llm_ready():
            return
        if not questionary.confirm(
            "Try the AI extraction fallback? This sends the document's TEXT to the "
            "Anthropic API — the only step where data leaves this machine.",
            default=False,
        ).ask():
            print("Skipped. The extracted text was saved under data/debug/ for inspection.")
            return
        try:
            run = ingest_source(source_key, doc, allow_llm_fallback=True)
        except Exception as exc2:
            print(f"AI fallback failed: {exc2}")
            return
    labels = {"llm_fallback": "AI fallback", "deterministic_parser": "built-in parser"}
    _print_transactions(transactions_from_run(run))
    print(f"\nParsed via {labels.get(run['extraction_method'], run['extraction_method'])}. "
          f"Saved to {run['run_path']} (stays on this machine).")


def _handle_new_source(source_key: str, source, doc: Path, manifest: ServiceManifest) -> None:
    """No parser for this source yet — the harness handles it itself: extract
    now with the LLM and/or have the embedded agent write a reusable parser."""
    print(f"\n'{source.label}' has no parser yet — the harness can handle it itself.")
    if not ensure_llm_ready():
        return
    choice = questionary.select(
        "What should the harness do with this source?",
        choices=[
            "Extract transactions now, and build a reusable parser",
            "Extract transactions now (LLM only)",
            "Build a reusable parser now (no extraction yet)",
            "Cancel",
        ],
    ).ask()
    if choice in (None, "Cancel"):
        return
    if not questionary.confirm(
        "This sends the document's TEXT to the Anthropic API — the step where data "
        "leaves this machine. Proceed?",
        default=False,
    ).ask():
        return

    if choice.startswith("Extract"):
        _extract_now(source_key, doc)
    if "build a reusable parser" in choice.lower():
        _build_parser_via_agent(source_key, source, doc, manifest)


def _extract_now(source_key: str, doc: Path) -> None:
    print("\nExtracting via the LLM (claude-opus-4-8)...")
    try:
        run = ingest_via_llm(source_key, doc)
    except Exception as exc:
        print(f"LLM extraction failed: {exc}")
        return
    _print_transactions(transactions_from_run(run))
    print(f"\nExtracted via LLM — treat as lower-confidence than a verified parser. "
          f"Saved to {run['run_path']}.")


def _build_parser_via_agent(source_key: str, source, doc: Path, manifest: ServiceManifest) -> None:
    from orchestration.build_parser import build_parser_for_source

    print(f"\nThe agent will now write a parser for {source.label}. Its actions:\n")
    try:
        result = build_parser_for_source(source_key, doc, source_label=source.label, on_event=print)
    except Exception as exc:
        print(f"\nThe agent run failed: {exc}")
        return

    verification = result["verification"]
    print()
    if not verification["ok"]:
        print("The agent's parser did not verify:")
        print(verification.get("error", "(no detail)"))
        print(f"The code it wrote is at {result['parser_path']} — review it or try again.")
        return

    txns = [Transaction.model_validate(t) for t in verification["transactions"]]
    print(f"The agent wrote {result['parser_path']}; it produced {len(txns)} transactions:")
    _print_transactions(txns)
    if result["agent_summary"]:
        print(f"\nAgent's summary:\n{result['agent_summary']}")
    if questionary.confirm(
        f"\nActivate this parser for '{source_key}'? (marks the source implemented so future "
        "runs use it automatically)",
        default=False,
    ).ask():
        manifest.update(source_key, parser=source_key, status="implemented")
        print(f"Activated. '{source.label}' now has a parser; future ingests use it.")
    else:
        print("Left inactive. The parser code stays in core/parsers/ for review; the source is "
              "still marked as needing a parser.")


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
    from core.tools.credential_store import MASTER_KEY_PATH

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("\nLLM provider: anthropic (from ANTHROPIC_API_KEY in the environment)")
    elif llm_provider.is_configured():
        print(f"\nLLM provider: {llm_provider.configured_provider()} "
              f"(stored encrypted in {MASTER_KEY_PATH.parent}/)")
    else:
        print("\nLLM provider: not configured — you'll be prompted on the next agent/AI action")

    services = ServiceManifest().load()
    implemented = [s for s in services if s.status == "implemented"]
    print(f"Sources (core/policies/services.yaml): {len(implemented)} of {len(services)} have a parser.")
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
    print("Sources without a parser: pick one under 'Ingest a source' and the harness "
          "will extract it now and/or write a parser for it (with your approval).")
    print("Deliberately not built yet: categorization, P&L, automated fetching of the "
          "source documents, orchestration graph.")


def main() -> int:
    print("K-Realty Property Finance Tracker")
    print("Ingests a source document into transactions. Data stays on this machine")
    print("(parsed output in data/, credentials encrypted in .secrets/); the agent")
    print("and AI extraction send document/code text to the LLM after you consent.\n")
    # First-run setup: make sure the harness has an LLM key (prompt + store if not).
    ensure_llm_ready()
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
