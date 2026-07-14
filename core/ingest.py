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
from core.parsers import ParseError, get_parser
from core.tools.service_manifest import ServiceManifest, ServiceManifestError

DATA_DIR = Path("data/parsed")
DEBUG_DIR = Path("data/debug")


class IngestError(RuntimeError):
    pass


def _dump_debug_text(source_key: str, extracted_text: str) -> Path:
    """Save the full extracted text so the layout can be inspected/shared
    without re-running the source, and so a parser can be iterated on."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = DEBUG_DIR / f"{source_key}-extracted.txt"
    dump_path.write_text(extracted_text)
    return dump_path


def _persist(source_key: str, transactions: list[Transaction], input_path: Path, method: str, parser: str | None) -> dict:
    month_key = f"{transactions[0].transaction_date:%Y-%m}" if transactions else "unknown"
    run = {
        "parsed_at": datetime.now(UTC).isoformat(),
        "source_key": source_key,
        "source_input": str(input_path),
        "parser": parser,
        "extraction_method": method,
        "month": month_key,
        "transaction_count": len(transactions),
        "transactions": [t.model_dump(mode="json") for t in transactions],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_path = DATA_DIR / f"{source_key}-{month_key}.json"
    run_path.write_text(json.dumps(run, indent=2))
    run["run_path"] = str(run_path)
    return run


def _source(source_key: str, manifest: ServiceManifest | None):
    manifest = manifest or ServiceManifest()
    try:
        return manifest.get(source_key)
    except ServiceManifestError as exc:
        raise IngestError(f"Unknown source '{source_key}'.") from exc


def ingest_source(
    source_key: str,
    input_path: Path,
    allow_llm_fallback: bool = False,
    manifest: ServiceManifest | None = None,
) -> dict:
    """Run a source's committed parser and persist the transactions.

    Raises IngestError if the source has no parser built yet.
    allow_llm_fallback: only pass True with explicit operator consent.
    """
    source = _source(source_key, manifest)
    if not source.parser or source.status != "implemented":
        raise IngestError(
            f"No parser built for '{source_key}' yet (status: {source.status})."
        )

    parser = get_parser(source.parser)
    try:
        transactions = parser(input_path)
    except ParseError as exc:
        dump_path = _dump_debug_text(source_key, exc.extracted_text) if exc.extracted_text else None
        if not allow_llm_fallback:
            if dump_path:
                raise ParseError(
                    f"{exc} \nFull extracted text saved to {dump_path} for inspection.",
                    extracted_text=exc.extracted_text,
                ) from exc
            raise
        from core.tools.llm_extractor import extract_transactions

        transactions = extract_transactions(exc.extracted_text, source_key, source.label)
        return _persist(source_key, transactions, input_path, "llm_fallback", source.parser)

    return _persist(source_key, transactions, input_path, "deterministic_parser", source.parser)


def ingest_via_llm(
    source_key: str, input_path: Path, manifest: ServiceManifest | None = None
) -> dict:
    """Extract a source's document with the LLM directly (no deterministic
    parser required). This is the "handle it now" path for a source the harness
    doesn't yet have a parser for. Requires ANTHROPIC_API_KEY; document text is
    sent to the API."""
    source = _source(source_key, manifest)
    from core.tools.llm_extractor import extract_transactions, read_document_text

    text = read_document_text(input_path)
    transactions = extract_transactions(text, source_key, source.label)
    return _persist(source_key, transactions, input_path, "llm_extract", None)


def load_latest_parsed() -> dict | None:
    """Most recent persisted parse across all sources, or None."""
    if not DATA_DIR.exists():
        return None
    runs = sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        return None
    data = json.loads(runs[-1].read_text())
    data["run_path"] = str(runs[-1])
    return data


def transactions_from_run(run: dict) -> list[Transaction]:
    return [Transaction.model_validate(t) for t in run["transactions"]]
