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
Requires ANTHROPIC_API_KEY. Reading a document's text stays local; only that
text is sent to the API.

Framework-free (the anthropic SDK is plain Python, allowed in core/).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import anthropic
import pdfplumber
import yaml
from pydantic import BaseModel, Field

from core.models import Transaction
from core.observability import get_logger

PROMPT_PATH = Path("core/prompts/transaction_extraction.v1.yaml")
MODEL = "claude-opus-4-8"

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


def extract_transactions(
    document_text: str, source_key: str, source_label: str
) -> list[Transaction]:
    """Send document text to the API and return transactions (marked
    extraction_method=llm in metadata)."""
    prompt = yaml.safe_load(PROMPT_PATH.read_text())
    client = anthropic.Anthropic()

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=prompt["system"].format(source_label=source_label, source_key=source_key),
            messages=[
                {
                    "role": "user",
                    "content": prompt["user_template"].format(
                        source_label=source_label, document_text=document_text
                    ),
                }
            ],
            output_format=_Extracted,
        )
    except Exception as exc:
        raise ExtractionError(log.failure(
            operation="llm_extract",
            code="LLM_API_ERROR",
            message=f"The LLM extraction call failed for '{source_key}'.",
            remediation="Check ANTHROPIC_API_KEY and network; retry, or build a deterministic parser.",
            context={"source_key": source_key, "model": MODEL, "text_chars": len(document_text)},
            exc=exc,
        )) from exc

    rows = response.parsed_output.transactions
    if not rows:
        raise ExtractionError(log.failure(
            operation="llm_extract",
            code="LLM_ZERO_ROWS",
            message="LLM extraction returned zero transactions.",
            remediation="The document may not contain a transaction table, or the text is unusable "
                        "— inspect the source document.",
            context={"source_key": source_key, "text_chars": len(document_text)},
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
