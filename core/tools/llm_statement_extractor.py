# Template candidate: generic (tier 1) — "LLM-extract transactions from
# document text when the deterministic parser fails" has no client or
# platform specifics; the prompt file carries the domain framing.
# See agent-harness-template/docs/promotion-log.md.
"""LLM extraction fallback (rung 2 of the parsing ladder).

When the deterministic statement parser can't handle a layout, this module
sends the extracted text to the Anthropic API for structured extraction.
Everything it returns is marked extraction_method=llm in Transaction
metadata, and the pipeline routes ALL of it to NEEDS_REVIEW — LLM-extracted
amounts are never auto-approved, consistent with the never-guess rule.

PRIVACY: this is the only place in the agent where data leaves the machine
(statement text is sent to the Anthropic API). Callers must obtain operator
consent before invoking it — the TUI does this explicitly per run.

Requires ANTHROPIC_API_KEY in the environment.

This module must stay framework-free (no langgraph/langchain imports —
the anthropic SDK is plain Python and is allowed in core/).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import anthropic
import yaml
from pydantic import BaseModel, Field

from core.models import Transaction

PROMPT_PATH = Path("core/prompts/statement_extraction.v1.yaml")
MODEL = "claude-opus-4-8"
SOURCE_SYSTEM = "epic_property_management_statement"


class ExtractedTransaction(BaseModel):
    date: str = Field(description="Transaction date exactly as printed, e.g. 5/16/2026")
    property_name: str = Field(description="Property this transaction belongs to, as named in the statement")
    unit: str | None = Field(default=None, description="Unit within the property, if identified")
    description: str = Field(description="Transaction description, verbatim")
    amount: float = Field(description="Positive for income, negative for expenses")


class ExtractedStatement(BaseModel):
    transactions: list[ExtractedTransaction]


class LLMExtractionError(RuntimeError):
    pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _parse_date(date_str: str) -> datetime:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    raise LLMExtractionError(f"Could not parse extracted date {date_str!r}")


def extract_transactions_via_llm(statement_text: str, source_uri: str) -> list[Transaction]:
    """Send statement text to the Anthropic API for structured extraction.

    Caller is responsible for having obtained operator consent first —
    statement text leaves the machine here.
    """
    prompt = yaml.safe_load(PROMPT_PATH.read_text())
    client = anthropic.Anthropic()

    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=prompt["system"],
        messages=[
            {
                "role": "user",
                "content": prompt["user_template"].format(statement_text=statement_text),
            }
        ],
        output_format=ExtractedStatement,
    )
    extracted = response.parsed_output
    if not extracted.transactions:
        raise LLMExtractionError(
            "LLM extraction returned zero transactions — the document may not "
            "contain a transaction section, or the text extraction is unusable."
        )

    transactions = []
    for seq, row in enumerate(extracted.transactions, start=1):
        transactions.append(
            Transaction(
                entity_id=f"llm-{_slug(row.property_name)}-{seq:03d}",
                source_system=SOURCE_SYSTEM,
                source_uri=source_uri,
                property_id=_slug(row.property_name),
                unit_id=row.unit.upper() if row.unit else None,
                transaction_date=_parse_date(row.date),
                amount=row.amount,
                description=row.description,
                metadata={"extraction_method": "llm", "model": MODEL},
            )
        )
    return transactions
