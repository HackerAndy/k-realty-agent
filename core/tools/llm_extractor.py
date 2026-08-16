# Template candidate: generic (tier 1) — "LLM-extract transactions from any
# source document" has no client specifics; the prompt carries the framing.
# See agent-harness-template/docs/promotion-log.md.
"""Generic LLM transaction extraction for any source, any format.

Two jobs:
- "extract now": handle a source that has no deterministic parser yet, so the
  harness is never blocked waiting on code.
- fallback: rescue a deterministic parser that failed on an unexpected layout.

Everything it returns should be treated as lower-confidence than a verified
deterministic parser (that's why the harness prefers building a real parser).

It runs on WHATEVER PROVIDER AND MODEL the operator chose in Settings — resolved
through llm_provider.resolve(), like every other LLM call in the harness. It used
to hardcode a Claude model, which meant an operator pointing the harness at their
own local server still got "check ANTHROPIC_API_KEY" (or, with a key present, a
run on a model they never picked). Reading a document's text stays local; only
that text goes to the provider.

Framework-free (the anthropic SDK is plain Python, allowed in core/).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pdfplumber
import yaml
from pydantic import BaseModel, Field

from core.models import Transaction
from core.observability import get_logger
from core.tools import llm_provider

PROMPT_PATH = Path("core/prompts/transaction_extraction.v1.yaml")

# Servers without structured-output support are told the shape in words instead.
JSON_INSTRUCTION = (
    'Reply with ONLY a JSON object, no prose and no markdown fence:\n'
    '{"transactions": [{"date": "<as printed>", "description": "<verbatim>", '
    '"amount": <signed number>, "property_name": <string or null>, '
    '"unit": <string or null>}]}'
)

log = get_logger("core.tools.llm_extractor")


class ExtractionError(RuntimeError):
    pass


class _Row(BaseModel):
    date: str = Field(description="Transaction date as printed, e.g. 5/16/2026")
    property_name: str | None = Field(default=None, description="Property, if identified")
    unit: str | None = Field(default=None, description="Unit, if identified")
    description: str = Field(description="Description, verbatim")
    amount: float = Field(description="Positive for money in, negative for money out")


class _Extracted(BaseModel):
    transactions: list[_Row]


def read_document_text(path: Path) -> str:
    """Best-effort text from a source document: dedup'd text for PDFs, raw text
    for CSV/TSV/TXT."""
    if path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.dedupe_chars().extract_text() or "" for page in pdf.pages)
    return path.read_text(errors="replace")


def _parse_date(date_str: str) -> datetime:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    raise ExtractionError(log.failure(
        operation="parse_extracted_date",
        code="UNPARSEABLE_DATE",
        message=f"Could not parse extracted date {date_str!r}.",
        remediation="The LLM returned a date in an unrecognized format; add its format to "
                    "_parse_date, or fix the row manually.",
        context={"date_str": date_str},
    ))


def extract_with_model(
    document_text: str, source_key: str, source_label: str
) -> tuple[list[Transaction], llm_provider.LLMChoice]:
    """Extract, and say which model did it — the caller records that on the run
    so a set of rows can always be traced to what produced them."""
    prompt = yaml.safe_load(PROMPT_PATH.read_text())
    choice = llm_provider.resolve()
    system = prompt["system"].format(source_label=source_label, source_key=source_key)
    user = prompt["user_template"].format(
        source_label=source_label, document_text=document_text
    )

    try:
        rows = llm_provider.complete_json(
            choice, system, user, _Extracted,
            json_instruction=JSON_INSTRUCTION,
            max_tokens=16000,
            timeout_s=600,   # a long document on a local box is minutes, not seconds
        ).transactions
    except Exception as exc:
        raise ExtractionError(log.failure(
            operation="llm_extract",
            code="LLM_API_ERROR",
            message=f"The LLM extraction call failed for '{source_key}' on {choice.describe()}.",
            remediation=(
                "Check the provider under Settings — the key for the Claude API, or that the "
                "server and model are reachable for a local one — then retry, or build a "
                "deterministic parser."
            ),
            context={"source_key": source_key, "provider": choice.provider,
                     "model": choice.model, "model_source": choice.model_source,
                     "base_url": choice.base_url, "text_chars": len(document_text)},
            exc=exc,
        )) from exc

    return _to_transactions(rows, source_key, len(document_text)), choice


def extract_transactions(
    document_text: str, source_key: str, source_label: str
) -> list[Transaction]:
    """Transactions only, for callers that don't record which model ran."""
    return extract_with_model(document_text, source_key, source_label)[0]


def _to_transactions(rows: list[_Row], source_key: str, text_chars: int) -> list[Transaction]:
    if not rows:
        raise ExtractionError(log.failure(
            operation="llm_extract",
            code="LLM_ZERO_ROWS",
            message="LLM extraction returned zero transactions.",
            remediation="The document may not contain a transaction table, or the text is unusable "
                        "— inspect the source document.",
            context={"source_key": source_key, "text_chars": text_chars},
        ))

    transactions = []
    for row in rows:
        # Preserve whatever columns the LLM found; invent nothing.
        fields = {"Date": row.date, "Description": row.description, "Amount": f"{row.amount:.2f}"}
        if row.property_name:
            fields["Property"] = row.property_name
        if row.unit:
            fields["Unit"] = row.unit
        transactions.append(
            Transaction(
                source_key=source_key,
                date=_parse_date(row.date),
                amount=row.amount,
                description=row.description,
                fields=fields,
            )
        )
    return transactions
