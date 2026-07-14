"""Ingest a source document into transactions.

Source-driven: given a source key (from core/policies/services.yaml) and an
input file, look up the parser that source declares, run it, and persist
the transactions. The pipeline knows nothing about any source's format —
that lives in the per-source parser under core/parsers/.

Deliberately minimal downstream: parse + persist only. No categorization,
no P&L, no thresholds — the starting flow is just getting clean
transactions out of each source. Analysis can be layered back on later.

If the parser fails and allow_llm_fallback=True (operator consent —
document text leaves the machine), the LLM extractor takes over. On failure
without consent, the full extracted text is saved to data/debug/ for
inspection.

Everything written to disk goes under data/ (gitignored).
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
    without re-running the source, and so the parser can be iterated on."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = DEBUG_DIR / f"{source_key}-extracted.txt"
    dump_path.write_text(extracted_text)
    return dump_path


def ingest_source(
    source_key: str,
    input_path: Path,
    allow_llm_fallback: bool = False,
    manifest: ServiceManifest | None = None,
) -> dict:
    """Parse one source document into transactions and persist them.

    Looks up `source_key` in the service manifest, runs the parser that
    source declares, and writes the result to
    data/parsed/<source_key>-<month>.json.

    Raises IngestError if the source is unknown or has no parser built yet.
    allow_llm_fallback: only pass True with explicit operator consent.
    """
    manifest = manifest or ServiceManifest()
    try:
        source = manifest.get(source_key)
    except ServiceManifestError as exc:
        raise IngestError(f"Unknown source '{source_key}'.") from exc
    if not source.parser or source.status != "implemented":
        raise IngestError(
            f"No parser built for '{source_key}' yet (status: {source.status}). "
            "Add one in core/parsers/ and set the source's parser + status in "
            "core/policies/services.yaml."
        )

    parser = get_parser(source.parser)

    used_llm = False
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
        from core.tools.llm_statement_extractor import extract_transactions_via_llm

        transactions = extract_transactions_via_llm(exc.extracted_text, source_uri=str(input_path))
        used_llm = True

    month_key = f"{transactions[0].transaction_date:%Y-%m}" if transactions else "unknown"

    run = {
        "parsed_at": datetime.now(UTC).isoformat(),
        "source_key": source_key,
        "source_input": str(input_path),
        "parser": source.parser,
        "extraction_method": "llm_fallback" if used_llm else "deterministic_parser",
        "month": month_key,
        "transaction_count": len(transactions),
        "transactions": [t.model_dump(mode="json") for t in transactions],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_path = DATA_DIR / f"{source_key}-{month_key}.json"
    run_path.write_text(json.dumps(run, indent=2))

    run["run_path"] = str(run_path)
    return run


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
