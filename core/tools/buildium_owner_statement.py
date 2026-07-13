# Template candidate: platform-reusable (tier 2) — parses the Buildium
# (managebuilding.com) Rental Owner Statement PDF layout; reusable for any
# future client whose property manager runs on Buildium.
# See agent-harness-template/docs/promotion-log.md.
"""Parse a Buildium-style Rental Owner Statement PDF into Transactions.

Known quirks of the real statements (learned from Andy's actual PDF,
2026-07-13):

- Bold text is fake-bolded by double-printing glyphs, so naive extraction
  yields doubled characters ("BBeeggiinnnniinngg ccaasshh bbaallaannccee").
  Fixed by pdfplumber's dedupe_chars() before extract_text().
- Dates are NOT zero-padded: "5/16/2026".
- The statement opens with a columnar "Summary by property" section; the
  row-level transaction detail comes later in the document.

The row/heading patterns below still need finishing against the full text
of a real statement (we've only seen its first page so far). On failure,
parse_statement() raises StatementParseError carrying the complete
extracted text so the caller can save it for inspection (see
core/monthly_cycle.py, which writes it to data/debug/) — and so the LLM
extraction fallback can take over without re-reading the PDF.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

from core.models import Transaction

SOURCE_SYSTEM = "epic_property_management_statement"

PROPERTY_RE = re.compile(r"^Property:\s*(.+?)\s*$")
ROW_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\s+(.+?)\s+\(?(-?)\$?([\d,]+\.\d{2})\)?$")
BALANCE_RE = re.compile(r"(beginning|ending)\s+cash\s+balance", re.IGNORECASE)
UNIT_RE = re.compile(r"\bunit\s+([A-Za-z0-9]+)\b", re.IGNORECASE)


class StatementParseError(RuntimeError):
    def __init__(self, message: str, extracted_text: str = ""):
        super().__init__(message)
        self.extracted_text = extracted_text


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def extract_text(pdf_path: Path) -> str:
    """Extract text with doubled-glyph (fake-bold) deduplication."""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(page.dedupe_chars().extract_text() or "" for page in pdf.pages)


def parse_statement_text(text: str, source_uri: str) -> list[Transaction]:
    """Parse already-extracted statement text. Split out from the PDF layer
    so layouts can be tested/iterated on as plain text."""
    transactions: list[Transaction] = []
    current_property: str | None = None
    seq = 0

    for line in text.splitlines():
        line = line.strip()
        prop_match = PROPERTY_RE.match(line)
        if prop_match:
            current_property = prop_match.group(1)
            continue

        row_match = ROW_RE.match(line)
        if not row_match:
            continue
        date_str, description, sign, amount_str = row_match.groups()
        if BALANCE_RE.search(description):
            continue
        if current_property is None:
            raise StatementParseError(
                f"Transaction row found before any property heading: {line!r}. "
                "Statement layout differs from expectations — revisit the parser.",
                extracted_text=text,
            )

        amount = float(amount_str.replace(",", ""))
        if sign == "-" or line.rstrip().endswith(")"):
            amount = -amount
        unit_match = UNIT_RE.search(description)
        seq += 1
        transactions.append(
            Transaction(
                entity_id=f"stmt-{_slug(current_property)}-{seq:03d}",
                source_system=SOURCE_SYSTEM,
                source_uri=source_uri,
                property_id=_slug(current_property),
                unit_id=unit_match.group(1).upper() if unit_match else None,
                transaction_date=datetime.strptime(date_str, "%m/%d/%Y"),
                amount=amount,
                description=description,
            )
        )

    if not transactions:
        raise StatementParseError(
            f"Parsed zero transactions from {source_uri}. Extracted text began:\n"
            f"{text[:500]}\n— statement layout differs from expectations; revisit the parser.",
            extracted_text=text,
        )
    return transactions


def parse_statement(pdf_path: Path) -> list[Transaction]:
    text = extract_text(pdf_path)
    if not text.strip():
        raise StatementParseError(f"No extractable text in {pdf_path} — scanned image PDF?")
    return parse_statement_text(text, source_uri=str(pdf_path))
