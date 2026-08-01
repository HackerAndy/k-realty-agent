#!/usr/bin/env python3
"""Generate the synthetic source documents the codegen bench builds parsers for.

Fake data, real shapes. Each document is a format the harness would actually be
handed, chosen so none of them matches a parser that already exists in
`core/parsers/` — otherwise the bench would be measuring how well the agent can
copy `dfcu_financial_bank.py` rather than whether it can read a document.

Committed rather than generated on demand: `tests/fixtures/*.csv` and
`tests/fixtures/*.pdf` are the two exceptions to the never-commit-financial-data
rule, and a bench whose inputs move is not a baseline. Regenerate with

    poetry run python tests/fixtures/generate_bench_documents.py

and re-check the expectations in `orchestration/bench/cases.py` if you change a
number here — they are asserted against each other by `tests/test_bench.py`.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).parent


# --------------------------------------------------------------------------
# Case 1 — a credit-union export. Accounting-style negatives in parentheses, a
# preamble above the header, and a Totals row that is not a transaction.
# --------------------------------------------------------------------------

RIVERBEND = """\
Riverbend Credit Union - Transaction Export
Account 8841 - Generated 3/31/2026

Account,Posted,Description,Type,Amount,Running Balance
8841,03/02/2026,Payroll ACH - RIVERBEND MFG,Deposit,"2,450.00","4,450.00"
8841,03/05/2026,Hardware store #221,Debit Card,(128.45),"4,321.55"
8841,03/09/2026,Mortgage payment,Transfer,"(1,340.00)","2,981.55"
8841,03/12/2026,Refund - Oak Supply,Credit,62.10,"3,043.65"
8841,03/15/2026,Utility - City Water,ACH,(86.20),"2,957.45"
8841,03/19/2026,Insurance premium,ACH,(212.00),"2,745.45"
8841,03/24/2026,Rent received - 12 Elm,Deposit,"1,150.00","3,895.45"
8841,03/28/2026,Maintenance - Ace Plumbing,Check,(475.00),"3,420.45"
,,Totals,,"1,420.45","3,420.45"
"""


# --------------------------------------------------------------------------
# Case 2 — a credit-card export. The sign convention is INVERTED against ours:
# a charge (money out) is positive in the file. An agent that copies the column
# through unchanged gets every sign backwards, and its own test will agree with
# it, so only the bench's expectations catch this.
# --------------------------------------------------------------------------

SUMMIT = """\
Transaction Date,Post Date,Description,Category,Amount
04/02/2026,04/03/2026,HOME DEPOT #4471,Home Improvement,218.94
04/05/2026,04/06/2026,SHELL OIL 5567,Gas,61.40
04/08/2026,04/09/2026,PAYMENT THANK YOU,Payment,-500.00
04/11/2026,04/12/2026,LOWES #1123,Home Improvement,342.17
04/14/2026,04/15/2026,STATEWIDE INSURANCE,Insurance,189.00
04/18/2026,04/19/2026,RETURN - LOWES #1123,Home Improvement,-87.25
04/22/2026,04/23/2026,CITY UTILITIES AUTOPAY,Utilities,143.66
,,New Balance,,367.92
"""


# --------------------------------------------------------------------------
# Case 3 — a property-management statement PDF. Per-property sections with
# split Charges/Credits columns and a subtotal under each, so the sign comes
# from which column a number sits in and the subtotals are rows to skip.
# Deliberately a different layout from the Buildium fixture next door.
# --------------------------------------------------------------------------

X = {"date": 54, "unit": 130, "cat": 185, "who": 300, "charges_r": 470, "credits_r": 550}

HARBOR_SECTIONS = [
    ("88 Harbor Way", [
        ("05/04/2026", "2A", "Rent", "K. Alvarez", "", "1,375.00"),
        ("05/09/2026", "2A", "Repairs", "Bright Electric", "245.50", ""),
        ("05/16/2026", "2B", "Rent", "T. Nguyen", "", "1,290.00"),
    ], "245.50", "2,665.00"),
    ("410 Lakeview Ct", [
        ("05/06/2026", "-", "Rent", "M. Ostrowski", "", "1,610.00"),
        ("05/20/2026", "-", "Management Fee", "Harbor Property Group", "161.00", ""),
        ("05/28/2026", "-", "Landscaping", "GreenEdge LLC", "320.75", ""),
    ], "481.75", "1,610.00"),
]


def _row(pdf, y, cells, bold=False):
    pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 8)
    date, unit, cat, who, charges, credits = cells
    for key, value in (("date", date), ("unit", unit), ("cat", cat), ("who", who)):
        if value:
            pdf.drawString(X[key], y, value)
    if charges:
        pdf.drawRightString(X["charges_r"], y, charges)
    if credits:
        pdf.drawRightString(X["credits_r"], y, credits)


def write_harbor(out_path: Path) -> None:
    pdf = canvas.Canvas(str(out_path), pagesize=letter)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(54, 740, "Harbor Property Group")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(54, 726, "Owner Activity Statement")
    pdf.drawString(54, 713, "Period 5/1/2026 - 5/31/2026")

    y = 680
    for property_name, rows, charges_total, credits_total in HARBOR_SECTIONS:
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(54, y, property_name)
        y -= 16
        _row(pdf, y, ("Date", "Unit", "Category", "Payee / Payer", "Charges", "Credits"), bold=True)
        y -= 13
        for row in rows:
            _row(pdf, y, row)
            y -= 13
        pdf.setFont("Helvetica", 8)
        pdf.drawString(X["cat"], y, f"Subtotal - {property_name}")
        pdf.drawRightString(X["charges_r"], y, charges_total)
        pdf.drawRightString(X["credits_r"], y, credits_total)
        y -= 26

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(X["cat"], y, "Net owner activity")
    pdf.drawRightString(X["credits_r"], y, "3,547.75")

    pdf.setFont("Helvetica", 7)
    pdf.drawString(54, 40, "Harbor Property Group - generated 6/1/2026 - Page 1 of 1")
    pdf.save()


def main() -> None:
    written = []
    for name, text in (("bench_riverbend_credit_union.csv", RIVERBEND),
                       ("bench_summit_card_services.csv", SUMMIT)):
        path = FIXTURES / name
        path.write_text(text, encoding="utf-8")
        written.append(path)

    harbor = FIXTURES / "bench_harbor_property_group.pdf"
    write_harbor(harbor)
    written.append(harbor)

    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
