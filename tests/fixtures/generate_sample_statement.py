#!/usr/bin/env python3
"""Generate a sample Buildium-style Owner Statement PDF fixture.

This mimics the typical managebuilding.com Owner Statement layout (per-
property sections, date/description/amount rows, beginning/ending balance
lines) so the parser has something real-shaped to run against until Andy
provides an actual statement. Regenerate with:

    poetry run python tests/fixtures/generate_sample_statement.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_PATH = Path(__file__).parent / "sample_owner_statement.pdf"

LINES = [
    ("h1", "Epic Property Management"),
    ("h2", "Owner Statement"),
    ("meta", "Statement Period: 06/01/2026 - 06/30/2026"),
    ("meta", "Owner: K-Realty LLC"),
    ("gap", ""),
    ("prop", "Property: 123 Maple St (Duplex)"),
    ("colhead", ("Date", "Description", "Amount")),
    ("row", ("06/01/2026", "Beginning cash balance", "1,500.00")),
    ("row", ("06/03/2026", "Rent payment - Unit A", "1,200.00")),
    ("row", ("06/03/2026", "Rent payment - Unit B", "1,150.00")),
    ("row", ("06/10/2026", "Management fee - Epic Property Management", "-235.00")),
    ("row", ("06/15/2026", "Plumbing repair - kitchen sink Unit A", "-285.00")),
    ("row", ("06/30/2026", "Ending cash balance", "3,330.00")),
    ("gap", ""),
    ("prop", "Property: 456 Oak Ave"),
    ("colhead", ("Date", "Description", "Amount")),
    ("row", ("06/01/2026", "Beginning cash balance", "800.00")),
    ("row", ("06/03/2026", "Rent payment", "1,400.00")),
    ("row", ("06/10/2026", "Management fee - Epic Property Management", "-140.00")),
    ("row", ("06/18/2026", "Lawn care service", "-60.00")),
    ("row", ("06/22/2026", "Mystery vendor XYZ special assessment", "-120.00")),
    ("row", ("06/30/2026", "Ending cash balance", "1,880.00")),
]

X_DATE, X_DESC, X_AMOUNT_RIGHT = 72, 150, 540


def main() -> None:
    pdf = canvas.Canvas(str(OUT_PATH), pagesize=letter)
    y = 750
    for kind, content in LINES:
        if kind == "h1":
            pdf.setFont("Helvetica-Bold", 16)
            pdf.drawString(X_DATE, y, content)
        elif kind == "h2":
            pdf.setFont("Helvetica-Bold", 13)
            pdf.drawString(X_DATE, y, content)
        elif kind == "meta":
            pdf.setFont("Helvetica", 10)
            pdf.drawString(X_DATE, y, content)
        elif kind == "prop":
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(X_DATE, y, content)
        elif kind == "colhead":
            pdf.setFont("Helvetica-Bold", 9)
            date, desc, amount = content
            pdf.drawString(X_DATE, y, date)
            pdf.drawString(X_DESC, y, desc)
            pdf.drawRightString(X_AMOUNT_RIGHT, y, amount)
        elif kind == "row":
            pdf.setFont("Helvetica", 9)
            date, desc, amount = content
            pdf.drawString(X_DATE, y, date)
            pdf.drawString(X_DESC, y, desc)
            pdf.drawRightString(X_AMOUNT_RIGHT, y, amount)
        y -= 18
    pdf.save()
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
