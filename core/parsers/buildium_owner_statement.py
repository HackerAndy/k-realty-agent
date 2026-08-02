# Template candidate: platform-reusable (tier 2) — parses the Buildium
# (managebuilding.com) Rental Owner Statement PDF layout; reusable for any
# future client whose property manager runs on Buildium.
# See agent-harness-template/docs/promotion-log.md.
"""Parse a Buildium Rental Owner Statement PDF into Transactions.

Built against Andy's real statement (2026-07-13). The document layout:

- Pages 1-2 are summaries (columnar "Summary by property" / "Income
  statement") — skipped; only the "Detail transactions" table is parsed.
- The detail table has 8 columns (Date, Property, Unit, Account, Name,
  Memo, Amount, Balance) whose header row repeats on each page it spans.
- Table cells WRAP across visual lines (e.g. "Manageme" / "nt Fees",
  "8095 Prospect Av" / "e. *EPM*"), so text-line regexes are hopeless.
  Instead, words are bucketed into columns by x-coordinate against the
  header row's positions, and continuation lines (no date) merge into the
  current row — joined without a space when the fragment continues a word
  (starts lowercase or previous fragment ends with "-").
- Sign comes from the section: "Additions to cash" rows are income (+),
  "Subtractions from cash" rows are expenses (-). The section carries
  across page breaks. "Balance" is a running total and is ignored.
- Fake-bold doubled glyphs are removed via pdfplumber's dedupe_chars().

On failure, parse_statement() raises ParseError (the shared parser
exception from core.parsers.base) carrying the full extracted text so
core/ingest.py can dump it to data/debug/ and offer the LLM extraction
fallback.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

from core import reconcile
from core.models import Transaction
from core.parsers.base import ParseError

SOURCE_SYSTEM = "epic_property_management_statement"

COLUMNS = ["Date", "Property", "Unit", "Account", "Name", "Memo", "Amount", "Balance"]
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
AMOUNT_RE = re.compile(r"^\(?-?\$?[\d,]+\.\d{2}\)?$")
SKIP_PREFIXES = (
    "Total from",
    "Beginning cash balance",
    "Ending cash balance",
    "Generated",
    "Date Property Unit",  # repeated header caught positionally, belt+braces
)
X_TOLERANCE = 3.0  # right-aligned amounts can start slightly left of their header


def extract_text(pdf_path: Path) -> str:
    """Full text with doubled-glyph (fake-bold) deduplication — used for
    debug dumps and as input to the LLM fallback."""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(page.dedupe_chars().extract_text() or "" for page in pdf.pages)


def _lines_from_words(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines by their top coordinate."""
    rows: dict[int, list[dict]] = {}
    for word in words:
        rows.setdefault(round(word["top"]), []).append(word)
    return [sorted(rows[top], key=lambda w: w["x0"]) for top in sorted(rows)]


def _find_header(lines: list[list[dict]]) -> list[float] | None:
    """Locate the 8-column header row; return each column's x0."""
    for line in lines:
        texts = [w["text"] for w in line]
        if texts == COLUMNS:
            return [w["x0"] for w in line]
    return None


def _bucket(line: list[dict], col_x: list[float]) -> list[str]:
    """Assign each word to a column by its center x; join per column."""
    cells = [[] for _ in COLUMNS]
    for word in line:
        center = (word["x0"] + word["x1"]) / 2
        idx = 0
        for j, x in enumerate(col_x):
            if center >= x - X_TOLERANCE:
                idx = j
        cells[idx].append(word["text"])
    return [" ".join(c) for c in cells]


def _merge_fragment(existing: str, fragment: str, word_continuation: bool) -> str:
    """Merge a wrapped-cell continuation.

    Narrow columns (Property, Unit, Account) truncate mid-word, so a
    fragment starting lowercase (or following a trailing '-') continues the
    word and joins without a space ("Manageme"+"nt Fees", "Av"+"e. *EPM*").
    Wide columns (Name, Memo) wrap at word boundaries, so they always join
    with a space ("applied to"+"balances")."""
    if not existing:
        return fragment
    if not fragment:
        return existing
    if word_continuation and (fragment[0].islower() or existing.endswith("-")):
        return existing + fragment
    return existing + " " + fragment


def _clean_property(raw: str) -> str:
    """Normalize a property cell: drop asterisk-wrapped manager markers
    (e.g. "*EPM*") and collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"\*[^*]*\*", "", raw)).strip()


def parse_statement(pdf_path: Path) -> list[Transaction]:
    transactions: list[Transaction] = []
    sign: int | None = None  # +1 additions, -1 subtractions; carries across pages
    current: dict | None = None
    section_totals: list[tuple[str, int, float]] = []

    def finalize(row: dict) -> None:
        property_name = _clean_property(row["cells"][1])
        unit = re.sub(r"\s+", " ", row["cells"][2]).strip()
        account = re.sub(r"\s+", " ", row["cells"][3]).strip()
        name = re.sub(r"\s+", " ", row["cells"][4]).strip()
        memo = re.sub(r"\s+", " ", row["cells"][5]).strip()
        description = " | ".join(part for part in (account, name, memo) if part)
        # The statement's actual detail-table columns, preserved verbatim. This
        # source DOES have Property/Unit, so they belong here — for a source that
        # didn't, they simply wouldn't appear.
        fields = {
            "Date": f"{row['date']:%m/%d/%Y}",
            "Property": property_name,
            "Unit": unit,
            "Account": account,
            "Name": name,
            "Memo": memo,
            "Amount": f"{row['amount']:.2f}",
        }
        transactions.append(
            Transaction(
                source_key=SOURCE_SYSTEM,
                source_uri=str(pdf_path),
                date=row["date"],
                amount=row["amount"],
                description=description,
                fields=fields,
            )
        )

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page = page.dedupe_chars()
            lines = _lines_from_words(page.extract_words())
            col_x = _find_header(lines)
            if col_x is None:
                continue  # summary/letterhead page — no detail table here

            header_seen = False
            for line in lines:
                texts = [w["text"] for w in line]
                if texts == COLUMNS:
                    header_seen = True
                    continue
                if not header_seen:
                    continue  # letterhead above the table

                joined = " ".join(texts)
                if joined == "Additions to cash":
                    sign = +1
                    continue
                if joined == "Subtractions from cash":
                    sign = -1
                    continue
                if joined.startswith(SKIP_PREFIXES):
                    if current:
                        finalize(current)
                        current = None
                    # The statement's own arithmetic, which this parser used to
                    # walk straight past. "Total from additions to cash $1,250.00"
                    # is the only thing in the document that can tell a complete
                    # read from a partial one — the test cannot, because it is
                    # written from this parser's own output.
                    if joined.startswith("Total from") and sign is not None:
                        stated = _stated_total(joined)
                        if stated is not None:
                            section_totals.append((joined, sign, stated))
                    continue

                cells = _bucket(line, col_x)
                if DATE_RE.match(cells[0].strip()):
                    if current:
                        finalize(current)
                    if sign is None:
                        raise ParseError(
                            f"Transaction row before any cash section: {joined!r} — "
                            "layout differs from expectations; revisit the parser.",
                            extracted_text=extract_text(pdf_path),
                        )
                    amount_str = cells[6].strip()
                    if not AMOUNT_RE.match(amount_str):
                        raise ParseError(
                            f"Row has a date but no parseable amount: {joined!r}",
                            extracted_text=extract_text(pdf_path),
                        )
                    current = {
                        "date": datetime.strptime(cells[0].strip(), "%m/%d/%Y"),
                        "amount": sign * float(amount_str.replace(",", "").replace("$", "")),
                        "cells": cells,
                    }
                elif current is not None:
                    for j in (1, 2, 3, 4, 5):  # merge wrapped cells (not date/amounts)
                        current["cells"][j] = _merge_fragment(
                            current["cells"][j], cells[j], word_continuation=j in (1, 2, 3)
                        )
            if current:
                finalize(current)
                current = None

    if not transactions:
        raise ParseError(
            f"Parsed zero transactions from {pdf_path} — no 'Detail transactions' "
            "table found, or the layout differs from expectations.",
            extracted_text=extract_text(pdf_path),
        )

    _reconcile_sections(transactions, section_totals, pdf_path)
    return transactions


def _stated_total(line: str) -> float | None:
    """The money figure a "Total from ..." line states, if it has one."""
    matches = re.findall(r"\(?-?\$?[\d,]+\.\d{2}\)?", line)
    if not matches:
        return None
    text = matches[-1]
    negative = text.startswith("(") and text.endswith(")")
    value = float(text.strip("()").replace(",", "").replace("$", ""))
    return -value if negative else value


def _reconcile_sections(transactions: list[Transaction],
                        section_totals: list[tuple[str, int, float]],
                        pdf_path: Path) -> None:
    """Check each cash section against the total the statement states for it.

    A section total is stated unsigned — "Total from subtractions from cash
    $850.00" — so it is compared against the magnitude of what was extracted
    under that section's sign. Missing a row, or dropping one to a page break,
    breaks this; nothing else in the parser or its test would notice.

    Raises rather than only recording, because handing back a number known to
    disagree with the document is worse than failing loudly. record() returns
    the real comparison with or without an active run, so this fires in a unit
    test too.
    """
    if not section_totals:
        return
    for label, sign, stated in section_totals:
        extracted = sum(t.amount for t in transactions if (t.amount >= 0) == (sign > 0))
        if not reconcile.record(label, expected=stated, actual=abs(extracted)):
            raise ParseError(
                f"{label}: the statement says {stated:,.2f} but {abs(extracted):,.2f} was "
                f"extracted. Rows are missing or misread — refusing to return a partial "
                f"read of a financial document.",
                extracted_text=extract_text(pdf_path),
            )
