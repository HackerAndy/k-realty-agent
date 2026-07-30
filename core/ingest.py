"""Ingest a source document into transactions.

Source-driven: given a source key (from core/policies/services.yaml) and an
input file, look up the parser that source declares, run it, and persist
the transactions. The pipeline knows nothing about any source's format —
that lives in the per-source parser under core/parsers/.

Two ways in:
- ingest_source(): run a source's committed deterministic parser (preferred —
  free, reproducible, verified). Optional LLM fallback if it fails.
- ingest_via_llm(): extract with the LLM directly, for a source that has no
  parser yet — so the harness is never blocked. (The embedded agent then
  builds a deterministic parser so future runs don't need this.)

Deliberately minimal downstream: parse + persist only. No categorization,
no P&L. Everything written to disk goes under data/ (gitignored).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.models import Transaction
from core.observability import get_logger
from core.parsers import ParseError, get_parser
from core.tools.service_manifest import ServiceManifest, ServiceManifestError

DATA_DIR = Path("data/parsed")
DEBUG_DIR = Path("data/debug")

log = get_logger("core.ingest")


class IngestError(RuntimeError):
    pass


def _dump_debug_text(source_key: str, extracted_text: str) -> Path:
    """Save the full extracted text so the layout can be inspected/shared
    without re-running the source, and so a parser can be iterated on."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = DEBUG_DIR / f"{source_key}-extracted.txt"
    dump_path.write_text(extracted_text)
    return dump_path


def _transport_of(run: dict) -> str:
    """Which route delivered a run.

    Recorded on the run itself since routes became first-class. Runs written
    before that are inferred from how they were read, which is enough: the portal
    scrape had its own extraction method, and everything else arrived as a file.
    """
    return run.get("transport") or (
        "scrape" if run.get("extraction_method") == "portal_scrape" else "upload")


def _persist(source_key: str, transactions: list[Transaction], input_path: Path, method: str,
             parser: str | None, transport: str | None = None, model: str | None = None) -> dict:
    month_key = f"{transactions[0].date:%Y-%m}" if transactions else "unknown"
    route = transport or _transport_of({"extraction_method": method})
    run = {
        "parsed_at": datetime.now(UTC).isoformat(),
        "source_key": source_key,
        "source_input": str(input_path),
        "parser": parser,
        "extraction_method": method,
        # WHICH ROUTE delivered this data (upload | scrape | email). The funnel
        # draws the route the current data actually arrived by as a solid line,
        # so this has to be recorded rather than guessed from the parser used.
        "transport": route,
        # WHICH MODEL read it, when a model did. Rows a model produced are only
        # as trustworthy as the model that produced them, so the run says which
        # one rather than leaving the operator to infer it from Settings today.
        "model": model,
        "month": month_key,
        "transaction_count": len(transactions),
        "transactions": [t.model_dump(mode="json") for t in transactions],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Keyed by ROUTE as well as month: one file per source-month meant a scrape
    # silently destroyed the rows an upload of the same month had produced, so
    # only ever one route's results existed and the others could say nothing.
    run_path = DATA_DIR / f"{source_key}-{route}-{month_key}.json"
    run_path.write_text(json.dumps(run, indent=2))
    run["run_path"] = str(run_path)
    return run


def _source(source_key: str, manifest: ServiceManifest | None):
    manifest = manifest or ServiceManifest()
    try:
        return manifest.get(source_key)
    except ServiceManifestError as exc:
        raise IngestError(log.failure(
            operation="resolve_source",
            code="UNKNOWN_SOURCE",
            message=f"Unknown source '{source_key}'.",
            remediation="Check the key against core/policies/services.yaml.",
            context={"source_key": source_key},
            exc=exc,
        )) from exc


def ingest_source(
    source_key: str,
    input_path: Path,
    allow_llm_fallback: bool = False,
    manifest: ServiceManifest | None = None,
    transport: str = "upload",
) -> dict:
    """Run a source's committed parser and persist the transactions.

    Raises IngestError if the source has no parser built yet.
    allow_llm_fallback: only pass True with explicit operator consent.
    """
    source = _source(source_key, manifest)
    if not source.parser or source.status != "implemented":
        raise IngestError(log.failure(
            operation="ingest_source",
            code="NO_PARSER",
            message=f"No parser built for '{source_key}' yet (status: {source.status}).",
            remediation="Build/activate a parser for this source in the TUI first.",
            context={"source_key": source_key, "status": source.status, "parser": source.parser},
        ))

    parser = get_parser(source.parser)
    try:
        transactions = parser(input_path)
    except ParseError as exc:
        dump_path = _dump_debug_text(source_key, exc.extracted_text) if exc.extracted_text else None
        human = log.failure(
            operation="ingest_source",
            code="PARSER_FAILED",
            message=f"Parser '{source.parser}' could not read {input_path.name}.",
            remediation=(f"Inspect the extracted text at {dump_path}." if dump_path
                         else "Review the document layout against the parser.")
                        + " Or retry with the AI extraction fallback.",
            context={"source_key": source_key, "parser": source.parser,
                     "input": str(input_path), "debug_dump": str(dump_path) if dump_path else None},
            exc=exc,
        )
        if not allow_llm_fallback:
            if dump_path:
                raise ParseError(human, extracted_text=exc.extracted_text) from exc
            raise
        from core.tools.llm_extractor import extract_with_model

        transactions, choice = extract_with_model(exc.extracted_text, source_key, source.label)
        return _persist(source_key, transactions, input_path, "llm_fallback", source.parser,
                        transport, model=choice.describe())

    return _persist(source_key, transactions, input_path, "deterministic_parser", source.parser, transport)


def ingest_via_llm(
    source_key: str, input_path: Path, manifest: ServiceManifest | None = None,
    transport: str = "upload",
) -> dict:
    """Extract a source's document with the LLM directly (no deterministic
    parser required). This is the "handle it now" path for a source the harness
    doesn't yet have a parser for. Runs on the provider/model chosen in Settings;
    document text is sent to that provider."""
    source = _source(source_key, manifest)
    from core.tools.llm_extractor import extract_with_model, read_document_text

    text = read_document_text(input_path)
    transactions, choice = extract_with_model(text, source_key, source.label)
    return _persist(source_key, transactions, input_path, "llm_extract", None, transport,
                    model=choice.describe())


def _runs_newest_first():
    """Every persisted run, newest first, skipping anything unreadable.

    Ordered by the run's OWN parsed_at, not the file's mtime — two scrapes half a
    minute apart land in different month files, and which of them is the later run
    is a fact about the run, not about when the file was last touched. Reads the
    run's own source_key for the same reason: key prefixes can't collide, and
    files written under the older naming still load.
    """
    if not DATA_DIR.exists():
        return
    runs = []
    for path in DATA_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["run_path"] = str(path)
        runs.append((data.get("parsed_at") or "", path.stat().st_mtime, data))
    for _, _, data in sorted(runs, key=lambda r: (r[0], r[1]), reverse=True):
        yield data


def load_latest_parsed() -> dict | None:
    """Most recent persisted parse across all sources, or None."""
    return next(_runs_newest_first(), None)


def load_latest_parsed_for(source_key: str, transport: str | None = None) -> dict | None:
    """Most recent persisted parse for ONE source, or None.

    With a transport, the most recent one THAT ROUTE produced — which is how a
    route can show its own rows instead of whichever route happened to run last.
    """
    for run in _runs_newest_first():
        if run.get("source_key") != source_key:
            continue
        if transport is not None and _transport_of(run) != transport:
            continue
        return run
    return None


def runs_by_transport(source_key: str) -> dict[str, dict]:
    """The latest run each route produced for this source, keyed by route.

    Transactions are left out — this answers "what has this route ever done",
    which the graph asks for every route at once. A route missing from the map
    has never run, and says so rather than borrowing another route's count.
    """
    latest: dict[str, dict] = {}
    for run in _runs_newest_first():
        if run.get("source_key") != source_key:
            continue
        route = _transport_of(run)
        if route in latest:
            continue                    # newest first, so the first one wins
        latest[route] = {
            "transport": route,
            "count": run.get("transaction_count", 0),
            "month": run.get("month"),
            "parsed_at": run.get("parsed_at"),
            "extraction_method": run.get("extraction_method"),
            "parser": run.get("parser"),
            "model": run.get("model"),
            "run_path": run.get("run_path"),
        }
    return latest


def transactions_from_run(run: dict) -> list[Transaction]:
    return [Transaction.model_validate(t) for t in run["transactions"]]
