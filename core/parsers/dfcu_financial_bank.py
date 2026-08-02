# Template candidate: platform-reusable (tier 2) — parses the DFCU Financial
# online-banking CSV transaction export; the column layout (Account Number,
# Post Date, Check, Description, Debit, Credit, Status, Balance) is a common
# retail-banking export shape, reusable for any client on a similar export.
# See agent-harness-template/docs/promotion-log.md.
"""Parse a DFCU Financial bank CSV transaction export into Transactions.

Built against Andy's real checking export (dfcu_ckecking_20260714csv). The
document is a plain CSV with a single header row and one row per posted
transaction:

    Account Number,Post Date,Check,Description,Debit,Credit,Status,Balance

- Rows are newest-first. Each row carries the running account Balance *after*
  that transaction, so the file is internally reconcilable: walking oldest to
  newest, every balance equals the prior balance plus Credit minus Debit.
  parse() enforces that chain to the penny and raises ParseError on any
  mismatch — a misread/garbled amount cannot slip through silently.
- Debit is money out, Credit is money in; they are mutually exclusive per row.
  The normalized `amount` = Credit - Debit (positive in, negative out).
- All eight source columns are preserved verbatim in Transaction.fields —
  nothing is invented and nothing bank-specific is dropped. The bank has no
  concept of "property" or "unit", so those simply aren't present.

On failure (unexpected header, unparseable date, both/neither Debit & Credit
set, or a broken balance chain) parse() raises ParseError carrying the raw
file text so core/ingest.py can dump it for inspection or the LLM fallback.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

from core import reconcile
from core.models import Transaction
from core.parsers.base import ParseError

SOURCE_SYSTEM = "dfcu_financial_bank"

EXPECTED_HEADER = [
    "Account Number",
    "Post Date",
    "Check",
    "Description",
    "Debit",
    "Credit",
    "Status",
    "Balance",
]


def _cents(value: str) -> int:
    """Parse a money string to integer cents (avoids float drift in the
    balance-chain reconciliation). Empty string -> 0."""
    text = value.strip().replace(",", "").replace("$", "")
    if not text:
        return 0
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    amount = round(float(text) * 100)
    return -amount if negative else amount


def parse(path: Path) -> list[Transaction]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)

    if not rows:
        raise ParseError(f"Empty file: {path}", extracted_text=raw)

    header = [h.strip() for h in rows[0]]
    if header != EXPECTED_HEADER:
        raise ParseError(
            f"Unexpected DFCU CSV header {header!r}; expected {EXPECTED_HEADER!r}. "
            "Layout differs from expectations — revisit the parser.",
            extracted_text=raw,
        )

    parsed: list[dict] = []
    for lineno, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line
        if len(row) != len(EXPECTED_HEADER):
            raise ParseError(
                f"Row {lineno} has {len(row)} fields, expected {len(EXPECTED_HEADER)}: {row!r}",
                extracted_text=raw,
            )
        account, post_date, check, description, debit, credit, status, balance = (
            cell.strip() for cell in row
        )

        try:
            txn_date = datetime.strptime(post_date, "%m/%d/%Y")
        except ValueError as exc:
            raise ParseError(
                f"Row {lineno}: unparseable Post Date {post_date!r}.",
                extracted_text=raw,
            ) from exc

        debit_c = _cents(debit)
        credit_c = _cents(credit)
        if debit_c and credit_c:
            raise ParseError(
                f"Row {lineno} has both a Debit and a Credit ({debit!r}/{credit!r}); "
                "ambiguous sign — refusing to guess.",
                extracted_text=raw,
            )
        if not debit_c and not credit_c:
            raise ParseError(
                f"Row {lineno} has neither a Debit nor a Credit: {row!r}.",
                extracted_text=raw,
            )

        amount_c = credit_c - debit_c  # + money in, - money out
        parsed.append(
            {
                # the source's actual columns, verbatim (nothing invented)
                "raw": dict(zip(EXPECTED_HEADER, (account, post_date, check, description, debit, credit, status, balance))),
                "description": re.sub(r"\s+", " ", description).strip(),
                "balance_c": _cents(balance),
                "amount_c": amount_c,
                "date": txn_date,
                "lineno": lineno,
            }
        )

    if not parsed:
        raise ParseError(
            f"Parsed zero transactions from {path} — file had a valid header but no "
            "transaction rows.",
            extracted_text=raw,
        )

    # Reconcile the running-balance chain to the penny. Rows are newest-first,
    # so each row's balance must equal the NEXT (older) row's balance plus this
    # row's signed amount. Any mismatch means an amount or balance was misread.
    # Routed through reconcile.record so the check is VISIBLE, not just enforced.
    # It was neither reported to the operator nor detectable by the build gate
    # before — the harness could not tell this parser, which verifies every row
    # to the penny, from one that checks nothing. record() returns the real
    # comparison with or without an active run, so the raise below still fires in
    # a plain unit test.
    for i in range(len(parsed) - 1):
        expected = parsed[i + 1]["balance_c"] + parsed[i]["amount_c"]
        if not reconcile.record(f"balance chain at line {parsed[i]['lineno']}",
                                expected=expected / 100,
                                actual=parsed[i]["balance_c"] / 100):
            r = parsed[i]
            nxt = parsed[i + 1]
            raise ParseError(
                f"Balance chain broke at line {r['lineno']}: "
                f"prior balance {nxt['balance_c'] / 100:.2f} + amount "
                f"{r['amount_c'] / 100:.2f} = {expected / 100:.2f}, "
                f"but row shows {r['balance_c'] / 100:.2f}.",
                extracted_text=raw,
            )

    transactions: list[Transaction] = []
    for r in parsed:
        transactions.append(
            Transaction(
                source_key=SOURCE_SYSTEM,
                source_uri=str(path),
                date=r["date"],
                amount=r["amount_c"] / 100,  # + money in, - money out
                description=r["description"],
                fields=r["raw"],  # the bank's actual columns, exactly as exported
            )
        )

    return transactions
