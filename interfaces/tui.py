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

import json
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
from core.fetch_ingest import fetch_and_ingest
from core.models import Transaction
from core.observability import get_logger
from core.parsers import ParseError
from core.tools import email_oauth, llm_provider, portal_scrapers
from core.tools.credential_store import ensure_master_key
from core.tools.service_manifest import FetchConfig, ServiceManifest

MANAGE_SECRETS = Path(__file__).resolve().parent.parent / "scripts" / "manage_secrets.py"

log = get_logger("interfaces.tui")


def _report(operation: str, code: str, message: str, remediation: str, exc: Exception) -> None:
    """Log a TUI-boundary failure via the project standard and show the operator
    the actionable message. Used for the catch-all handlers that wrap agent,
    browser, and network operations."""
    print(f"\n{log.failure(operation=operation, code=code, message=message, remediation=remediation, exc=exc)}")


def ensure_llm_ready() -> bool:
    """Make sure an LLM provider is set up — loaded from the encrypted store, or
    configured on first use. The provider choice is just another secret; this is
    the only place the operator goes. Returns True if an LLM is ready."""
    ensure_master_key()
    if llm_provider.load_into_env():
        return True
    if os.environ.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("LLM_PROVIDER", "anthropic")
        return True  # provided via environment (advanced / CI path)

    print("No LLM provider is set up yet — let's pick one.")
    return _configure_llm_provider()


def _configure_llm_provider() -> bool:
    """Choose + store the LLM provider (Claude API or a local/OpenAI-compatible
    server). Stored encrypted in .secrets/ and loaded automatically next run."""
    choice = questionary.select(
        "Which LLM should the harness use?",
        choices=[
            questionary.Choice("Claude API (Anthropic)", "anthropic"),
            questionary.Choice("Local / OpenAI-compatible server (OMLX, Ollama, LM Studio, vLLM)", "openai_compatible"),
            questionary.Choice("Cancel", "cancel"),
        ],
    ).ask()
    if choice in (None, "cancel"):
        return False

    if choice == "anthropic":
        print("The key is stored encrypted in .secrets/ alongside your other secrets.")
        key = questionary.password("Anthropic API key:").ask()
        if not key or not key.strip():
            print("No key entered — nothing changed.")
            return False
        model = questionary.text("Model:", default="claude-opus-4-8").ask()
        llm_provider.store_llm_credential("anthropic", api_key=key.strip(), model=(model or "").strip() or None)
    else:
        print("Point the harness at any OpenAI-compatible server. A key is optional for local ones.")
        base_url = questionary.text("Base URL:", default="http://klabss-MacBook-Pro.local:9090/v1").ask()
        if not base_url or not base_url.strip():
            print("No base URL entered — nothing changed.")
            return False
        model = questionary.text("Model:", default="qwen2.5-coder:7b").ask()
        key = questionary.password("API key (leave blank if the server needs none):").ask()
        llm_provider.store_llm_credential(
            "openai_compatible",
            api_key=(key or "").strip() or None,
            base_url=base_url.strip(),
            model=(model or "").strip() or None,
        )

    llm_provider.load_into_env()
    print("Saved. The harness will use this LLM now (and on future runs).\n")
    return True


def action_llm_provider() -> None:
    """Menu entry: view + change which LLM the harness uses."""
    config = llm_provider.current_config()
    if config:
        print(f"\nCurrent LLM provider: {config.get('provider', '?')}")
        for k in ("model", "base_url", "api_key"):
            if config.get(k):
                print(f"  {k}: {config[k]}")
    else:
        print("\nNo LLM provider configured yet.")
    _configure_llm_provider()


def _print_transactions(transactions: list[Transaction]) -> None:
    """Show the source's OWN columns (from each transaction's `fields`) — never
    a fixed set. The only normalized number is the money-in/out total, which
    comes from the universal signed `amount`."""
    if not transactions:
        print("(no transactions)")
        return
    columns = list(transactions[0].fields.keys()) or ["Date", "Description", "Amount"]

    def cell(t: Transaction, col: str) -> str:
        return str(t.fields.get(col, ""))

    widths = {
        col: min(max([len(col)] + [len(cell(t, col)) for t in transactions]), 26)
        for col in columns
    }
    header = "  ".join(f"{col[:widths[col]]:<{widths[col]}}" for col in columns)
    print("\n" + header)
    print("-" * len(header))
    for t in transactions:
        print("  ".join(f"{cell(t, col)[:widths[col]]:<{widths[col]}}" for col in columns))
    print("-" * len(header))
    money_in = sum(t.amount for t in transactions if t.amount > 0)
    money_out = sum(t.amount for t in transactions if t.amount < 0)
    print(f"{len(transactions)} transactions | money in {money_in:,.2f} | money out {money_out:,.2f}")
    print("(columns above are this source's own, verbatim — only the in/out totals are normalized)")


def _parser_built(source_key: str) -> bool:
    """True if a parser exists for this source (file written and/or registered)
    even though the source isn't marked implemented — i.e. built but awaiting
    the operator's approval."""
    from core.parsers import REGISTRY

    return (Path("core/parsers") / f"{source_key}.py").exists() or source_key in REGISTRY


def pending_approvals(services=None) -> list:
    """Sources whose parser is built but not yet activated — the harness's
    outstanding actions that need the operator's explicit yes. Surfaced loudly
    everywhere so nothing waits in the dark."""
    services = services if services is not None else ServiceManifest().load()
    return [
        s for s in services
        if not _is_trigger(s) and s.status != "implemented" and _parser_built(s.key)
    ]


def _is_trigger(s) -> bool:
    """A trigger source (e.g. an inbox) signals that a document has arrived —
    it's a delivery channel, not itself a document you parse into transactions.
    It never has a parser; ingesting it means ingesting what it delivers."""
    return s.input_type == "email_trigger"


def _source_marker(s) -> str:
    if _is_trigger(s):
        if s.fetch and email_oauth.is_configured(s.key):
            return f"✓ auto-fetches inbox → {s.fetch.delivers_to}"
        return "↳ inbox — set up auto-fetch (pulls its document for you)"
    if s.status == "implemented":
        return "✓ ready"
    if _parser_built(s.key):
        return "⚠ ACTION NEEDED — parser built, approve to activate"
    return "· no parser yet"


def _pick_source() -> str | None:
    """Every source, all selectable, each with an explicit status marker.
    Approval-needed sources are flagged and sorted to the top — nothing hides."""
    def rank(s) -> int:
        if s.status != "implemented" and _parser_built(s.key):
            return 0  # approval needed → top
        return 1 if s.status == "implemented" else 2

    services = sorted(ServiceManifest().load(), key=rank)
    choices = [
        questionary.Choice(title=f"{s.label:26}  {_source_marker(s)}", value=s.key)
        for s in services
    ]
    return questionary.select("Which source do you want to ingest?", choices=choices).ask()


def action_ingest() -> None:
    source_key = _pick_source()
    if not source_key:
        return
    manifest = ServiceManifest()
    source = manifest.get(source_key)

    # A fetched source (an inbox) pulls its own document — set it up if needed,
    # then fetch + route to the delivered source's parser. No file prompt.
    if _is_trigger(source):
        _fetch_source(source_key, source, manifest)
        return

    # A source with a login-portal scraper has a second way in. The actual daily
    # scrape (retrieve()) isn't built yet — for now the portal option is recon
    # (explore): log in, watch, and see the live page structure.
    scraper = portal_scrapers.get_scraper(source_key)
    if scraper is not None:
        import core.scrapers as scrapers

        choices = [
            questionary.Choice("Build/rebuild the scraper — demonstrate once, the harness writes it", "build"),
        ]
        if scrapers.has_scraper(source_key):
            choices.append(questionary.Choice("Run the harness-built scraper", "run_built"))
        choices += [
            questionary.Choice("Scrape now, I'll log in — interactive (preview before saving)", "scrape"),
            questionary.Choice("Auto-login and scrape — the harness signs in with stored credentials", "scrape_auto"),
            questionary.Choice("Explore the portal — log in, watch, and see the live pages (recon)", "explore"),
            questionary.Choice("Provide a document file I already have (PDF/CSV)", "file"),
            questionary.Choice("Cancel", "cancel"),
        ]
        method = questionary.select(f"How do you want to get {source.label}'s data?", choices=choices).ask()
        if method in (None, "cancel"):
            return
        if method == "build":
            _build_scraper(source, scraper)
            return
        if method == "run_built":
            _run_built_scraper(source)
            return
        if method == "explore":
            _explore_portal(source, scraper)
            return
        if method == "scrape":
            _scrape_portal(source, scraper)
            return
        if method == "scrape_auto":
            _scrape_portal_auto(source, scraper)
            return
        # "file" → fall through to the normal document flow below.

    # Built-but-not-activated → straight to the review/approval flow (no
    # document ingest yet; the outstanding action is approving the parser).
    if source.status != "implemented" and _parser_built(source_key):
        _review_and_activate(source_key, source, manifest)
        return

    doc = _ask_document()
    if doc is None:
        return
    if source.status == "implemented" and source.parser:
        _ingest_with_parser(source_key, doc)
    else:
        _handle_new_source(source_key, source, doc, manifest)


def _review_and_activate(source_key: str, source, manifest: ServiceManifest) -> None:
    """Entry for a parser built but never activated. Get a document to review
    against, then hand off to the review loop."""
    print(f"\nReview: {source.label}")
    print(f"A parser was built for this source (core/parsers/{source_key}.py) but is NOT")
    print("active yet, so the harness isn't using it. Review it, then you decide.\n")
    doc = _ask_document()
    if doc is None:
        if questionary.confirm(
            f"No document given. Activate '{source.label}' without previewing?", default=False
        ).ask():
            manifest.update(source_key, parser=source_key, status="implemented")
            print(f"✓ Activated. '{source.label}' now uses this parser automatically.")
        else:
            print("Left inactive.")
        return
    _review_parser(source_key, source, doc, manifest)


def _review_parser(source_key: str, source, doc: Path, manifest: ServiceManifest) -> None:
    """Preview the parser's output, then loop: Activate / Request changes (the
    agent revises it) / Leave inactive. This is the debug+approve loop — if the
    columns are wrong or rows are missing, you say so in plain English and the
    harness fixes its own parser, re-verifies, and shows you again."""
    from orchestration.build_parser import revise_parser_for_source, verify_parser

    # Always verify in a fresh subprocess so a just-revised parser is what you see
    # (not a stale in-process import).
    verification = verify_parser(source_key, doc)
    while True:
        if verification["ok"]:
            print(f"\n{source.label} — parser output on {doc.name}:")
            _print_transactions([Transaction.model_validate(t) for t in verification["transactions"]])
        else:
            print(f"\nThe parser errored on this document:\n  {verification.get('error', '')}")

        choice = questionary.select(
            "Review — what would you like to do?",
            choices=[
                "Activate this parser (use it from now on)",
                "Request changes — tell the agent what to fix",
                "Leave inactive for now",
            ],
        ).ask()

        if choice is None or choice.startswith("Leave"):
            print("Left inactive — it stays flagged ⚠ ACTION NEEDED until you activate it.")
            return
        if choice.startswith("Activate"):
            manifest.update(source_key, parser=source_key, status="implemented")
            print(f"\n✓ Activated. '{source.label}' now uses this parser automatically.")
            return

        # Request changes → the harness revises its own parser.
        feedback = questionary.text(
            "What's wrong or what should change? Plain English, e.g. 'the columns should "
            "match the CSV headers exactly', 'it's dropping the fee rows', 'dates are off by "
            "a day':"
        ).ask()
        if not feedback or not feedback.strip():
            print("No feedback entered — nothing changed.")
            continue
        if not ensure_llm_ready():
            continue
        print("\nThe agent will revise the parser. Its actions:\n")
        try:
            result = revise_parser_for_source(
                source_key, doc, feedback.strip(), source_label=source.label, on_event=print
            )
        except Exception as exc:
            _report("revise_parser", "TUI_REVISE_FAILED",
                    "The parser revision run failed.",
                    "See data/logs/agent.jsonl for detail; try again or adjust your feedback.", exc)
            continue
        verification = result["verification"]
        if result["agent_summary"]:
            print(f"\nWhat the agent changed:\n{result['agent_summary']}")
        # loop back: re-preview the revised parser and offer the choice again


def _fetch_source(source_key: str, source, manifest: ServiceManifest) -> None:
    """A fetched source (inbox): set it up if it isn't yet, then fetch + route.
    Everything happens here in the TUI — the harness asks, stores, and fetches."""
    if source.fetch is None or not email_oauth.is_configured(source_key):
        if not _setup_email_fetch(source_key, source, manifest):
            return
        source = manifest.get(source_key)  # reload with the fetch config just saved
        if not questionary.confirm(f"Fetch from {source.label} now?", default=True).ask():
            return
    else:
        acct = email_oauth.account_email(source_key) or "the connected inbox"
        choice = questionary.select(
            f"'{source.label}' is set up — fetches {acct} → {source.fetch.delivers_to}.",
            choices=["Fetch now", "Reconfigure", "Cancel"],
        ).ask()
        if choice in (None, "Cancel"):
            return
        if choice == "Reconfigure":
            if not _setup_email_fetch(source_key, source, manifest):
                return
            source = manifest.get(source_key)
    _run_fetch(source_key, source, manifest)


def _setup_email_fetch(source_key: str, source, manifest: ServiceManifest) -> bool:
    """Walk the operator through connecting an inbox and configuring what to fetch.
    Returns True if setup completed. The harness is self-documenting here — it
    tells you exactly what to create in Google Cloud, then does the rest."""
    print(f"\nSet up automatic fetch for '{source.label}'")
    print("Instead of handing the harness a file each month, it pulls the document")
    print("straight from your inbox.\n")
    print("Google Workspace/Gmail requires OAuth (app passwords were disabled in 2025).")
    print("One-time Google Cloud setup, if you haven't done it:")
    print("  1. console.cloud.google.com → create a project (e.g. 'k-realty-agent')")
    print("  2. APIs & Services → Library → enable 'Gmail API'")
    print("  3. OAuth consent screen → User type: Internal → add scope gmail.readonly")
    print("  4. Credentials → Create credentials → OAuth client ID → Desktop app")
    print("  5. Download the client JSON")
    print("\nWhat gets stored: the access token goes ENCRYPTED into .secrets/; the search")
    print("settings go in core/policies/services.yaml (plain text, yours to edit). The")
    print("scope is read-only — the harness can read/download mail, never send or delete.\n")

    if not questionary.confirm(
        "Ready to connect now? (A browser opens; you click 'Allow' once.)", default=True
    ).ask():
        print("No problem — pick this source again under 'Ingest a source' when ready.")
        return False

    json_path = questionary.path("Path to the downloaded OAuth client JSON:").ask()
    if not json_path:
        return False
    client_json = Path(json_path).expanduser()
    if not client_json.exists():
        print(f"No file at {client_json}")
        return False

    print("\nOpening a browser for Google consent — approve read-only access...")
    try:
        email = email_oauth.run_consent(source_key, client_json)
    except Exception as exc:
        _report("email_consent", "TUI_CONSENT_FAILED", "Google consent failed.",
                "Confirm the client JSON is a Desktop OAuth client and try again.", exc)
        return False
    print(f"✓ Connected to {email}. Token stored encrypted in .secrets/.\n")

    # Where does the fetched document go? Only sources with an active parser.
    doc_sources = [
        s for s in manifest.load()
        if not _is_trigger(s) and s.status == "implemented" and s.parser
    ]
    if not doc_sources:
        print("There's no source with an active parser to deliver to yet. Build/activate a")
        print("parser for the document's source first (e.g. the Epic statement), then set")
        print("up fetch. Your inbox connection is saved, so you won't repeat that step.")
        return False
    delivers_to = questionary.select(
        "Which source's parser should handle the fetched document?",
        choices=[questionary.Choice(title=f"{s.label}  ({s.key})", value=s.key) for s in doc_sources],
    ).ask()
    if not delivers_to:
        return False

    print("\nNow narrow the search to the one message that carries the document.")
    from_address = questionary.text("Sender to match (e.g. donotreply@example.com), or blank:").ask()
    subject = questionary.text("Subject contains (optional):").ask()
    suffix = questionary.text("Attachment file type:", default=".pdf").ask()
    newer = questionary.text("Only search the last N days (optional, e.g. 45):").ask()

    cfg = FetchConfig(
        provider="gmail",
        delivers_to=delivers_to,
        from_address=(from_address.strip() or None) if from_address else None,
        subject_contains=(subject.strip() or None) if subject else None,
        attachment_suffix=(suffix.strip() or None) if suffix else None,
        newer_than_days=(int(newer) if newer and newer.strip().isdigit() else None),
    )
    manifest.set_fetch(source_key, cfg)
    print(f"\n✓ Saved. '{source.label}' now fetches from {email} and routes the attachment")
    print(f"  to {delivers_to}'s parser. Search settings are in services.yaml under 'fetch:'.")
    return True


def _run_fetch(source_key: str, source, manifest: ServiceManifest) -> None:
    print(f"\nFetching {source.label} → {source.fetch.delivers_to}...")
    try:
        runs = fetch_and_ingest(source_key, manifest=manifest, on_event=print)
    except Exception as exc:
        _report("fetch_source", "TUI_FETCH_FAILED", f"Fetch failed for {source.label}.",
                "See data/logs/agent.jsonl; if it hit the login/OAuth, re-run email setup.", exc)
        return
    if not runs:
        return
    for run in runs:
        _print_transactions(transactions_from_run(run))
        print(f"Ingested via {run['parser']} — saved to {run['run_path']} (stays on this machine).")


def _explore_portal(source, scraper) -> None:
    """Watch-it-happen recon: open the real portal in a visible browser, you log
    in and navigate to the page you want scraped, then the harness reads that
    exact screen back to you. Read-only — it looks, it never clicks or changes
    anything. This is how we see the live pages before building the scraper."""
    print(f"\nExplore {source.label}'s portal — {scraper.PORTAL_URL}")
    print("A real browser window will open so you can watch. You log in yourself")
    print("(handles any 2FA), navigate to the financial page you'd want scraped daily,")
    print("then press Enter and the harness reads that page's structure — the tables and")
    print("their columns, any download links. It CLICKS NOTHING and changes nothing; it")
    print("only looks, so we can build the scraper against the real page, not a guess.\n")
    if not questionary.confirm("Open the portal in a browser now?", default=True).ask():
        return
    try:
        path = scraper.explore_interactive()
    except Exception as exc:
        _report("portal_explore", "TUI_EXPLORE_FAILED", "Portal recon couldn't complete.",
                "If a browser didn't open, install Chromium: 'poetry run playwright install chromium'.", exc)
        return
    _show_portal_structure(path)


def _scrape_portal(source, scraper) -> None:
    """Scrape the live portal into transactions, PREVIEW them, and only save on
    your approval. The first run is the verification — if signs/rows/columns are
    off, you say so and I fix the scraper (same spirit as the parser review loop).

    Interactive (headed) for now: you log in and open the report so it's rendered
    and authenticated, then it scrapes that page. Unattended/headless scraping is
    a separate, later step — Buildium bounces headless sessions to login."""
    target = scraper.read_captured_url() or scraper.PORTAL_URL
    print(f"\nScrape {source.label}'s portal — {target}")
    print("A browser window opens so you can watch. Log in if needed and open the")
    print("owner-statement report so its table is visible, then press Enter and it scrapes")
    print("that page and shows you the result BEFORE saving anything.\n")
    if not questionary.confirm("Open the portal and scrape?", default=True).ask():
        return
    try:
        transactions = scraper.retrieve_interactive()
    except Exception as exc:
        _report("portal_scrape", "TUI_SCRAPE_FAILED", "Portal scrape couldn't complete.",
                "See the message above and data/logs/agent.jsonl for what the page was.", exc)
        return

    print(f"\n{source.label} — scraped from the portal:")
    _print_transactions(transactions)
    if not questionary.confirm("Do these look right — save them?", default=False).ask():
        print("Not saved. Tell me what's off (signs, missing/extra rows, wrong columns) "
              "and I'll fix the scraper.")
        return
    from core.fetch_ingest import persist_scraped

    run = persist_scraped(transactions, target)
    print(f"Saved {run['transaction_count']} transactions to {run['run_path']} (stays on this machine).")


def _build_scraper(source, scraper) -> None:
    """The directive path: the harness's OWN agent writes the scraper from your
    demonstration. A browser opens, you show it how to reach your data once, and
    the embedded agent authors + self-verifies core/scrapers/<key>.py."""
    if not ensure_llm_ready():
        return
    portal_url = (source.login_url or getattr(scraper, "GENERAL_LEDGER_URL", None)
                  or getattr(scraper, "PORTAL_URL", ""))
    if not portal_url:
        print("No portal URL known for this source (set login_url in services.yaml).")
        return
    print(f"\nThe harness will build a scraper for {source.label} from your demonstration.")
    print("A browser opens: log in, set your filters/dropdowns, and click Generate/Search so")
    print("YOUR data is on screen, then press Enter. The agent captures what happened (the")
    print("data request it fired + your clicks) and writes the scraper itself.\n")
    print("This sends the captured page/request text to the LLM (the agent). Proceed?")
    if not questionary.confirm("Start the demonstration + build?", default=True).ask():
        return
    from orchestration.build_scraper import build_scraper_for_source

    print("\nThe agent's actions:\n")
    try:
        result = build_scraper_for_source(source.key, portal_url, source_label=source.label, on_event=print)
    except Exception as exc:
        _report("build_scraper", "TUI_BUILD_SCRAPER_FAILED", "The scraper-building run failed.",
                "See data/logs/agent.jsonl; you can retry the demonstration.", exc)
        return
    if result["agent_summary"]:
        print(f"\nAgent's summary:\n{result['agent_summary']}")
    if result["verification"]["ok"]:
        print(f"\n✓ Scraper written + registered: {result['scraper_path']}")
        print("Review it, then choose 'Run the harness-built scraper' to try it. Activating it as "
              "this source's method stays your call.")
    else:
        print(f"\nThe scraper didn't verify: {result['verification'].get('error', '(no detail)')}")
        print(f"The code it wrote is at {result['scraper_path']} — you can ask it to revise.")


def _run_built_scraper(source) -> None:
    """Run the agent-authored scraper (core/scrapers/<key>.py) and preview before saving."""
    import core.scrapers as scrapers

    print(f"\nRunning the harness-built scraper for {source.label}...")
    try:
        transactions = scrapers.get_scraper(source.key)()
    except Exception as exc:
        _report("run_built_scraper", "TUI_RUN_BUILT_SCRAPER_FAILED", "The built scraper failed.",
                "See data/logs/agent.jsonl; you can rebuild it from a fresh demonstration.", exc)
        return
    print(f"\n{source.label} — from the harness-built scraper:")
    _print_transactions(transactions)
    if not questionary.confirm("Do these look right — save them?", default=False).ask():
        print("Not saved. Rebuild the scraper if the extraction is off.")
        return
    from core.fetch_ingest import persist_scraped

    run = persist_scraped(transactions, source.login_url or source.key)
    print(f"Saved {run['transaction_count']} transactions to {run['run_path']}.")


def _scrape_portal_auto(source, scraper) -> None:
    """Test the unattended path: the harness signs in with your STORED credentials
    (no manual login) and scrapes. This first run is headed so you can WATCH what
    Buildium does after the password — log straight in, ask for email verification,
    or throw a CAPTCHA. That's the fact that decides whether daily-unattended is
    possible."""
    print(f"\nAuto-login scrape for {source.label} — {scraper.read_captured_url() or scraper.PORTAL_URL}")
    print("The harness signs in itself using your stored Epic credentials (you don't type")
    print("anything). This run opens a VISIBLE window so you can see exactly what happens")
    print("after the password — a clean login, a 'check your email' step, or a CAPTCHA.")
    print("Requires your Epic username/password stored (Manage services & credentials).\n")
    if not questionary.confirm("Run the auto-login scrape now?", default=True).ask():
        return
    try:
        transactions = scraper.retrieve(headless=False)  # headed so you can watch this test
    except Exception as exc:
        _report("portal_scrape_auto", "TUI_SCRAPE_AUTO_FAILED",
                "Auto-login scrape didn't complete.",
                "Read the message above — it says where the login landed (verification/CAPTCHA/"
                "bad-credentials). Full detail in data/logs/agent.jsonl.", exc)
        return

    print(f"\n{source.label} — scraped after automated login:")
    _print_transactions(transactions)
    if not questionary.confirm("Do these look right — save them?", default=False).ask():
        print("Not saved.")
        return
    from core.fetch_ingest import persist_scraped

    run = persist_scraped(transactions, scraper.read_captured_url() or scraper.PORTAL_URL)
    print(f"Saved {run['transaction_count']} transactions to {run['run_path']}.")
    print("\nThat worked headed. Next step toward truly hands-off: try it headless "
          "(retrieve(headless=True)) — if that also works, it can run scheduled/unattended.")


def _show_portal_structure(path) -> None:
    """Print what recon found on the captured page — immediate visibility, then
    the full structure is on disk for the build step."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception as exc:
        print(f"Captured, but couldn't read {path}: {exc}")
        return
    print(f"\nCaptured page: {data.get('title', '(no title)')}")
    print(f"URL: {data.get('captured_url', data.get('url', ''))}")

    tables = data.get("tables", [])
    print(f"\nTables on the page: {len(tables)}")
    for i, t in enumerate(tables, 1):
        headers = ", ".join(t.get("headers", [])) or "(no <th> header cells)"
        print(f"  [{i}] {t.get('row_count', '?')} rows — columns: {headers}")

    downloads = data.get("download_candidates", [])
    print(f"\nDownload / report links: {len(downloads)}")
    for d in downloads[:15]:
        print(f"  • {(d.get('text') or '')[:48]:48}  {d.get('href', '')}")

    print(f"\nFull structure saved to {path}")
    print("That's the recon. Share that file (it's page structure only — no dollar")
    print("amounts) and we'll design the scraper against exactly what's there.")


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
            _report("ingest_ai_fallback", "TUI_AI_FALLBACK_FAILED", "AI extraction fallback failed.",
                    "Check ANTHROPIC_API_KEY/network; the extracted text is under data/debug/.", exc2)
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
        _report("extract_now", "TUI_EXTRACT_FAILED", "LLM extraction failed.",
                "Check ANTHROPIC_API_KEY/network, or build a deterministic parser instead.", exc)
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
        _report("build_parser", "TUI_BUILD_PARSER_FAILED", "The parser-building agent run failed.",
                "See data/logs/agent.jsonl for detail; retry or adjust the sample document.", exc)
        return
    if result["agent_summary"]:
        print(f"\nAgent's summary:\n{result['agent_summary']}")
    if not result["verification"]["ok"]:
        print("\nThe agent's parser did not verify:")
        print(result["verification"].get("error", "(no detail)"))
        print(f"The code it wrote is at {result['parser_path']}.")
        # Still hand off to the review loop so you can request changes.
    # Review → activate / request changes / leave (same loop as a pending parser).
    _review_parser(source_key, source, doc, manifest)


def action_view_latest() -> None:
    run = load_latest_parsed()
    if run is None:
        print("Nothing parsed yet — parse a statement first.")
        return
    print(f"Latest parse: {run.get('source_key', '?')} {run.get('month', '')} "
          f"(from {run['run_path']})")
    try:
        txns = transactions_from_run(run)
    except Exception:
        print("  This saved run is from an older transaction format — re-ingest the source "
              "to view it in the current column layout.")
        return
    _print_transactions(txns)


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
    pending = pending_approvals(services)
    if pending:
        print("\n⚠ ACTION NEEDED:")
        for s in pending:
            print(f"  • {s.label}: a parser is built but NOT activated — "
                  f"'Ingest a source' → {s.label} to review + approve it.")

    implemented = [s for s in services if s.status == "implemented"]
    print(f"\nSources (core/policies/services.yaml): {len(implemented)} of {len(services)} have a parser.")
    for s in services:
        if s.status == "implemented":
            marker = "✓"
        elif _parser_built(s.key):
            marker = "⚠"
        else:
            marker = " "
        parser = s.parser or ("(built, awaiting approval)" if _parser_built(s.key) else "-")
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
    base_actions = [
        ("ingest", action_ingest),
        ("view", action_view_latest),
        ("services", action_services),
        ("llm", action_llm_provider),
        ("status", action_status),
    ]
    while True:
        # Recompute each loop so indicators clear the instant you act on them.
        pending = pending_approvals()
        if pending:
            names = ", ".join(s.label for s in pending)
            print(f"⚠ ACTION NEEDED — {len(pending)} parser(s) built and waiting for your "
                  f"approval: {names}")
            print("  → choose 'Ingest a source' and pick the flagged source to review + activate.\n")
        labels = {
            "ingest": "Ingest a source (document → transactions)"
                      + (f"   ⚠ {len(pending)} awaiting approval" if pending else ""),
            "view": "View latest parsed transactions",
            "services": "Manage services & credentials",
            "llm": "LLM provider (choose Claude API or a local server)",
            "status": "Status",
        }
        display_to_func = {labels[key]: func for key, func in base_actions}
        choice = questionary.select(
            "What would you like to do?", choices=[*display_to_func, "Quit"]
        ).ask()
        if choice in (None, "Quit"):
            return 0
        display_to_func[choice]()
        print()


if __name__ == "__main__":
    sys.exit(main())
