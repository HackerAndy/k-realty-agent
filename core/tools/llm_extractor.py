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

PROMPT_PATH = Path("core/prompts/transaction_extraction.v1.yaml")
MODEL = "claude-opus-4-8"


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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _parse_date(date_str: str) -> datetime:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    raise ExtractionError(f"Could not parse extracted date {date_str!r}")


def extract_transactions(
    document_text: str, source_key: str, source_label: str
) -> list[Transaction]:
    """Send document text to the API and return transactions (marked
    extraction_method=llm in metadata)."""
    prompt = yaml.safe_load(PROMPT_PATH.read_text())
    client = anthropic.Anthropic()

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
    rows = response.parsed_output.transactions
    if not rows:
        raise ExtractionError(
            "LLM extraction returned zero transactions — the document may not contain "
            "a transaction table, or the text is unusable."
        )

    transactions = []
    for seq, row in enumerate(rows, start=1):
        prop = _slug(row.property_name) if row.property_name else source_key
        transactions.append(
            Transaction(
                entity_id=f"llm-{source_key}-{seq:03d}",
                source_system=source_key,
                property_id=prop,
                unit_id=row.unit.strip() if row.unit else None,
                transaction_date=_parse_date(row.date),
                amount=row.amount,
                description=row.description,
                metadata={"extraction_method": "llm", "model": MODEL},
            )
        )
    return transactions
