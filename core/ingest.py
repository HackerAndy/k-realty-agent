"""Ingest a Buildium owner-statement PDF into transactions.

Deliberately minimal: parse the PDF into Transaction records and persist
them. No categorization, no P&L, no thresholds — the starting flow is
just getting clean transactions out of the statement. Downstream analysis
can be layered back on later.

The deterministic parser handles the real statement layout. If it fails
and allow_llm_fallback=True (operator consent — statement text leaves the
machine), the LLM extractor takes over. On failure without consent, the
full extracted text is saved to data/debug/ for inspection.

Everything written to disk goes under data/ (gitignored).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.models import Transaction
from core.tools.buildium_owner_statement import StatementParseError, parse_statement

DATA_DIR = Path("data/parsed")
DEBUG_DIR = Path("data/debug")


def _dump_debug_text(pdf_path: Path, extracted_text: str) -> Path:
    """Save the full extracted text so the layout can be inspected/shared
    without re-running the PDF, and so the parser can be iterated against it."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = DEBUG_DIR / f"{pdf_path.stem}-extracted.txt"
    dump_path.write_text(extracted_text)
    return dump_path


def ingest_statement(pdf_path: Path, allow_llm_fallback: bool = False) -> dict:
    """Parse one owner-statement PDF into transactions and persist them.

    Returns a summary dict (also written to data/parsed/<month>.json) with
    the parsed transactions under "transactions".

    allow_llm_fallback: only pass True with explicit operator consent — it
    sends the statement text to the Anthropic API when the deterministic
    parser fails.
    """
    used_llm = False
    try:
        transactions = parse_statement(pdf_path)
    except StatementParseError as exc:
        dump_path = _dump_debug_text(pdf_path, exc.extracted_text) if exc.extracted_text else None
        if not allow_llm_fallback:
            if dump_path:
                raise StatementParseError(
                    f"{exc} \nFull extracted text saved to {dump_path} for inspection.",
                    extracted_text=exc.extracted_text,
                ) from exc
            raise
        from core.tools.llm_statement_extractor import extract_transactions_via_llm

        transactions = extract_transactions_via_llm(exc.extracted_text, source_uri=str(pdf_path))
        used_llm = True

    month_key = f"{transactions[0].transaction_date:%Y-%m}" if transactions else "unknown"

    run = {
        "parsed_at": datetime.now(UTC).isoformat(),
        "source_pdf": str(pdf_path),
        "extraction_method": "llm_fallback" if used_llm else "deterministic_parser",
        "month": month_key,
        "transaction_count": len(transactions),
        "transactions": [t.model_dump(mode="json") for t in transactions],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_path = DATA_DIR / f"{month_key}.json"
    run_path.write_text(json.dumps(run, indent=2))

    run["run_path"] = str(run_path)
    return run


def load_latest_parsed() -> dict | None:
    """Most recent persisted parse, or None if nothing has been parsed yet."""
    if not DATA_DIR.exists():
        return None
    runs = sorted(DATA_DIR.glob("*.json"))
    if not runs:
        return None
    data = json.loads(runs[-1].read_text())
    data["run_path"] = str(runs[-1])
    return data


def transactions_from_run(run: dict) -> list[Transaction]:
    return [Transaction.model_validate(t) for t in run["transactions"]]
