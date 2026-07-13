"""The monthly cycle, as one framework-free pipeline function.

statement PDF -> Transactions -> categorize (rules, never guesses) ->
threshold checks -> persist run to data/runs/ -> P&L text.

Parsing uses a two-rung ladder: the deterministic parser first (free,
reproducible, auditable); if that fails and the caller passed
allow_llm_fallback=True (operator consent — statement text leaves the
machine), the LLM extractor takes over and every extracted row is forced
to NEEDS_REVIEW regardless of categorizer output.

This is deliberately a plain function in core/: it IS the business logic,
so it lives where the portability contract keeps it reusable. When the
LangGraph layer gets built out, orchestration/graph.py wraps these same
steps as nodes (adding checkpointing/HITL interrupts) — it must not
reimplement them.

Everything written to disk goes under data/ (gitignored — financial data
never belongs in the repo).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.models import AuditRecord, Decision, DecisionStatus, Transaction
from core.tools.buildium_owner_statement import StatementParseError, parse_statement
from core.tools.categorizer import Categorizer
from core.tools.pnl_report import render_pnl
from core.validators.thresholds import EXPENSE_FLAG_THRESHOLD_PER_UNIT, exceeds_expense_threshold

DATA_DIR = Path("data/runs")
DEBUG_DIR = Path("data/debug")


def _dump_debug_text(pdf_path: Path, extracted_text: str) -> Path:
    """Save the full extracted text so the layout can be inspected/shared
    without re-running the PDF, and so the parser can be iterated against it."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = DEBUG_DIR / f"{pdf_path.stem}-extracted.txt"
    dump_path.write_text(extracted_text)
    return dump_path


def run_monthly_cycle(pdf_path: Path, allow_llm_fallback: bool = False) -> dict:
    """Run the full cycle on one Owner Statement PDF. Returns a summary dict
    (also persisted to data/runs/<month>.json) with the rendered P&L text
    under "report".

    allow_llm_fallback: only pass True with explicit operator consent —
    it sends the statement text to the Anthropic API when the
    deterministic parser fails.
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

    categorizer = Categorizer()

    pairs: list[tuple[Transaction, Decision]] = []
    audit_records: list[AuditRecord] = []
    for transaction in transactions:
        decision = categorizer.categorize(transaction)
        if exceeds_expense_threshold(transaction) and decision.status == DecisionStatus.AUTO_APPROVED:
            decision = decision.model_copy(
                update={
                    "status": DecisionStatus.NEEDS_REVIEW,
                    "reasoning": decision.reasoning
                    + f" Flagged: single expense exceeds ${EXPENSE_FLAG_THRESHOLD_PER_UNIT:,.2f}/unit threshold.",
                }
            )
        if transaction.metadata.get("extraction_method") == "llm" and decision.status == DecisionStatus.AUTO_APPROVED:
            decision = decision.model_copy(
                update={
                    "status": DecisionStatus.NEEDS_REVIEW,
                    "reasoning": decision.reasoning
                    + " Flagged: extracted by LLM fallback (not the deterministic parser) — "
                    "verify the amount/date against the statement before approving.",
                }
            )
        pairs.append((transaction, decision))
        audit_records.append(
            AuditRecord(
                entity_id=transaction.entity_id,
                decision=decision,
                actor="agent:rules_categorizer",
            )
        )

    month_label = f"{pairs[0][0].transaction_date:%B %Y}" if pairs else "unknown month"
    month_key = f"{pairs[0][0].transaction_date:%Y-%m}" if pairs else "unknown"
    report = render_pnl(pairs, month_label)

    run = {
        "run_at": datetime.now(UTC).isoformat(),
        "source_pdf": str(pdf_path),
        "extraction_method": "llm_fallback" if used_llm else "deterministic_parser",
        "month": month_key,
        "transactions": [t.model_dump(mode="json") for t, _ in pairs],
        "decisions": [d.model_dump(mode="json") for _, d in pairs],
        "audit_records": [a.model_dump(mode="json") for a in audit_records],
        "report": report,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_path = DATA_DIR / f"{month_key}.json"
    run_path.write_text(json.dumps(run, indent=2))

    run["run_path"] = str(run_path)
    run["needs_review_count"] = sum(
        1 for _, d in pairs if d.status == DecisionStatus.NEEDS_REVIEW
    )
    return run


def load_latest_run() -> dict | None:
    """Most recent persisted run, or None if the cycle has never been run."""
    if not DATA_DIR.exists():
        return None
    runs = sorted(DATA_DIR.glob("*.json"))
    if not runs:
        return None
    data = json.loads(runs[-1].read_text())
    data["run_path"] = str(runs[-1])
    return data
