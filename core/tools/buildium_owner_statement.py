# Template candidate: platform-reusable (tier 2) — parses the Buildium
# (managebuilding.com) Owner Statement PDF layout; reusable for any future
# client whose property manager runs on Buildium.
# See agent-harness-template/docs/promotion-log.md.
"""Parse a Buildium-style Owner Statement PDF into Transactions.

Layout assumptions (verified against the checked-in fixture at
tests/fixtures/sample_owner_statement.pdf, which mimics the standard
managebuilding.com Owner Statement):

- "Property: <name>" lines start a per-property section
- transaction rows are "MM/DD/YYYY  <description>  <amount>"
- "Beginning/Ending cash balance" rows are running balances, not
  transactions, and are skipped
- negative amounts are expenses, positive are income (rent)

IMPORTANT: built against the fixture, not yet against one of Andy's real
statements. The row/heading regexes are the first thing to revisit once a
real PDF is available — parse_statement() raises StatementParseError with
the offending page text rather than silently returning partial data.

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
ROW_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+\(?(-?)\$?([\d,]+\.\d{2})\)?$")
BALANCE_RE = re.compile(r"(beginning|ending)\s+cash\s+balance", re.IGNORECASE)
UNIT_RE = re.compile(r"\bunit\s+([A-Za-z0-9]+)\b", re.IGNORECASE)


class StatementParseError(RuntimeError):
    pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def parse_statement(pdf_path: Path) -> list[Transaction]:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not text.strip():
        raise StatementParseError(f"No extractable text in {pdf_path} — scanned image PDF?")

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
                f"Transaction row found before any 'Property:' heading: {line!r}. "
                "Statement layout differs from expectations — revisit the parser."
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
                source_uri=str(pdf_path),
                property_id=_slug(current_property),
                unit_id=unit_match.group(1).upper() if unit_match else None,
                transaction_date=datetime.strptime(date_str, "%m/%d/%Y"),
                amount=amount,
                description=description,
            )
        )

    if not transactions:
        raise StatementParseError(
            f"Parsed zero transactions from {pdf_path}. Extracted text began:\n"
            f"{text[:500]}\n— statement layout differs from expectations; revisit the parser."
        )
    return transactions
