#!/usr/bin/env python3
"""Generate a sample Buildium Rental Owner Statement PDF fixture.

Mimics the REAL managebuilding.com layout (verified against an actual
statement 2026-07-13) with fake data: an 8-column detail table (Date,
Property, Unit, Account, Name, Memo, Amount, Balance) whose header repeats
per page, cells that wrap across lines mid-word, sign determined by
"Additions to cash" / "Subtractions from cash" sections that carry across
page breaks, and balance/total/footer lines to be skipped. Regenerate:

    poetry run python tests/fixtures/generate_sample_statement.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_PATH = Path(__file__).parent / "sample_owner_statement.pdf"

# Column x positions taken from a real statement
X = {"date": 61.5, "prop": 89.2, "unit": 172.5, "acct": 216.8,
     "name": 272.2, "memo": 355.5, "amount_r": 513.0, "balance_r": 579.0}
HEADERS = [("date", "Date"), ("prop", "Property"), ("unit", "Unit"), ("acct", "Account"),
           ("name", "Name"), ("memo", "Memo")]


def draw_header_row(pdf, y):
    pdf.setFont("Helvetica-Bold", 8)
    for key, label in HEADERS:
        pdf.drawString(X[key], y, label)
    pdf.drawRightString(X["amount_r"], y, "Amount")
    pdf.drawRightString(X["balance_r"], y, "Balance")


def draw_cells(pdf, y, date="", prop="", unit="", acct="", name="", memo="", amount="", balance=""):
    pdf.setFont("Helvetica", 8)
    if date:
        pdf.drawString(37.5, y, date)
    for key, value in (("prop", prop), ("unit", unit), ("acct", acct), ("name", name), ("memo", memo)):
        if value:
            pdf.drawString(X[key], y, value)
    if amount:
        pdf.drawRightString(X["amount_r"], y, amount)
    if balance:
        pdf.drawRightString(X["balance_r"], y, balance)


def draw_letterhead(pdf, page_num):
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(61.5, 760, "Rental Owner Statement")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(61.5, 746, "Sample Property Management, LLC")
    pdf.drawString(61.5, 734, f"Page {page_num}")
    pdf.drawString(61.5, 722, "Statement period 5/16/2026 - 6/15/2026")


def main() -> None:
    pdf = canvas.Canvas(str(OUT_PATH), pagesize=letter)

    # --- Page 1: summary page (no detail header -> parser must skip it) ---
    draw_letterhead(pdf, 1)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(61.5, 690, "Summary by property")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(61.5, 674, "123 Maple St. 456 Oak Ave. All properties")
    pdf.drawString(61.5, 660, "Beginning cash balance $500.00 $200.00 $700.00")
    pdf.showPage()

    # --- Page 2: detail transactions, additions + start of subtractions ---
    draw_letterhead(pdf, 2)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(61.5, 700, "Detail transactions")
    y = 680
    draw_header_row(pdf, y); y -= 14
    pdf.setFont("Helvetica", 8)
    pdf.drawString(61.5, y, "Beginning cash balance as of 5/16/2026"); pdf.drawRightString(X["balance_r"], y, "$700.00"); y -= 14
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(61.5, y, "Additions to cash"); y -= 14

    # wrapped property cell ("456 Oak Av" + "e." continuation) and wrapped account
    draw_cells(pdf, y, date="5/24/2026", prop="456 Oak Av", unit="1", acct="Rent",
               name="Unit 1 - Sam Tenant", memo="by Sam Tenant", amount="900.00", balance="1,600.00"); y -= 10
    draw_cells(pdf, y, prop="e. *SPM*", acct="Income"); y -= 14

    draw_cells(pdf, y, date="5/27/2026", prop="123 Maple St.", unit="Lower", acct="Non-",
               name="Unit Lower - Pat", memo="Payment", amount="250.00", balance="1,850.00"); y -= 10
    draw_cells(pdf, y, acct="Refundable", name="Renter"); y -= 10
    draw_cells(pdf, y, acct="Pet"); y -= 10
    draw_cells(pdf, y, acct="Deposit"); y -= 14

    draw_cells(pdf, y, date="6/15/2026", prop="123 Maple St.", unit="Property", acct="Owner",
               name="Alex Owner", memo="Owner Contribution", amount="100.00", balance="1,950.00"); y -= 10
    draw_cells(pdf, y, unit="level", acct="Contributio"); y -= 10
    draw_cells(pdf, y, acct="n"); y -= 14

    pdf.setFont("Helvetica", 8)
    pdf.drawString(61.5, y, "Total from additions to cash"); pdf.drawRightString(X["balance_r"], y, "$1,250.00"); y -= 14
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(61.5, y, "Subtractions from cash"); y -= 14

    draw_cells(pdf, y, date="5/29/2026", prop="123 Maple St.", unit="Upper", acct="Repairs",
               name="Unit Upper - Fixit Co", memo="Faucet replacement", amount="300.00", balance="1,650.00"); y -= 14

    pdf.setFont("Helvetica", 7)
    pdf.drawString(61.5, 40, "Generated 06/15/2026 Page 2")
    pdf.showPage()

    # --- Page 3: subtractions continue (repeated header, NO section line —
    #     tests that the sign carries across the page break) ---
    draw_letterhead(pdf, 3)
    y = 690
    draw_header_row(pdf, y); y -= 14

    draw_cells(pdf, y, date="6/15/2026", prop="123 Maple St.", unit="Lower", acct="Manageme",
               name="Unit Lower - Sample", memo="5/16/2026 - 6/15/2026", amount="99.00", balance="1,551.00"); y -= 10
    draw_cells(pdf, y, acct="nt Fees", name="Prop Mgmt LLC", memo="(Flat fee: $99.00)"); y -= 14

    draw_cells(pdf, y, date="6/15/2026", prop="456 Oak Av", unit="Property", acct="Owner",
               name="Alex Owner", memo="Owner Draw", amount="451.00", balance="1,100.00"); y -= 10
    draw_cells(pdf, y, prop="e. *SPM*", unit="level", acct="Draw"); y -= 14

    pdf.setFont("Helvetica", 8)
    pdf.drawString(61.5, y, "Total from subtractions from cash"); pdf.drawRightString(X["balance_r"], y, "$850.00"); y -= 14
    pdf.drawString(61.5, y, "Ending cash balance as of 6/15/2026"); pdf.drawRightString(X["balance_r"], y, "$1,100.00")

    pdf.setFont("Helvetica", 7)
    pdf.drawString(61.5, 40, "Generated 06/15/2026 Page 3")
    pdf.save()
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
