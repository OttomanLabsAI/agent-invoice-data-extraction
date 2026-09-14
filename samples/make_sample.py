"""Build samples/sample-invoice.pdf - a fictional UK MEP subcontractor invoice.

Dev-only helper (needs reportlab). Run once: python samples/make_sample.py
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).with_name("sample-invoice.pdf")


def draw() -> None:
    c = canvas.Canvas(str(OUT), pagesize=A4)
    w, h = A4
    x0 = 20 * mm

    c.setFont("Helvetica-Bold", 18)
    c.drawString(x0, h - 25 * mm, "Northbank Mechanical Services Ltd")
    c.setFont("Helvetica", 9)
    c.drawString(x0, h - 31 * mm, "Unit 7, Riverside Industrial Estate, Dartford, Kent DA1 5QP")
    c.drawString(x0, h - 35 * mm, "accounts@northbankmech.co.uk  ·  01322 555 0192")
    c.drawString(x0, h - 39 * mm, "VAT Reg No: GB 452 8891 07  ·  Company No: 09876543")
    c.drawString(x0, h - 43 * mm, "UTR: 1234567890")

    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - x0, h - 25 * mm, "INVOICE")
    c.setFont("Helvetica", 10)
    rows = [
        ("Invoice No", "NMS-2026-0417"),
        ("Invoice Date", "08/09/2026"),
        ("Due Date", "08/10/2026"),
        ("Your PO No", "GG-HEL18-00231"),
        ("Contract", "HEL18 Mechanical Package"),
    ]
    y = h - 33 * mm
    for label, value in rows:
        c.drawRightString(w - x0 - 48 * mm, y, label + ":")
        c.drawRightString(w - x0, y, value)
        y -= 5 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, h - 62 * mm, "Invoice To:")
    c.setFont("Helvetica", 10)
    c.drawString(x0, h - 67 * mm, "Glent Group Ltd")
    c.drawString(x0, h - 72 * mm, "Accounts Payable")
    c.drawString(x0, h - 77 * mm, "London")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(105 * mm, h - 62 * mm, "Site:")
    c.setFont("Helvetica", 10)
    c.drawString(105 * mm, h - 67 * mm, "HEL18 Data Centre, Hall 2")
    c.drawString(105 * mm, h - 72 * mm, "Helsinki Region, Finland")
    c.drawString(105 * mm, h - 77 * mm, "Application ref: AFP-07 · Period ending 31/08/2026")

    # Table
    top = h - 92 * mm
    c.setFillColor(colors.HexColor("#e8ece8"))
    c.rect(x0, top - 6 * mm, w - 2 * x0, 7 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    cols = [x0 + 2 * mm, 105 * mm, 125 * mm, 145 * mm, 168 * mm]
    for cx, t in zip(cols, ["Description", "Qty", "Unit", "Rate (£)", "Net (£)"]):
        if t in ("Qty", "Unit", "Rate (£)", "Net (£)"):
            c.drawRightString(cx + 15 * mm, top - 4.5 * mm, t)
        else:
            c.drawString(cx, top - 4.5 * mm, t)

    lines = [
        ("Labour - chilled water pipework install, Hall 2 corr. C", "84", "hrs", "48.50", "4,074.00"),
        ("Labour - supervision, site manager", "2", "days", "420.00", "840.00"),
        ("Materials - 150mm carbon steel pipe & fittings (per DN-2251)", "1", "lot", "6,180.00", "6,180.00"),
        ("Materials - pipe supports and brackets", "1", "lot", "935.00", "935.00"),
        ("Hire - 3.5t telehandler, week 34-35", "2", "wk", "310.00", "620.00"),
    ]
    c.setFont("Helvetica", 9)
    y = top - 12 * mm
    for desc, qty, unit, rate, net in lines:
        c.drawString(cols[0], y, desc)
        c.drawRightString(cols[1] + 15 * mm, y, qty)
        c.drawRightString(cols[2] + 15 * mm, y, unit)
        c.drawRightString(cols[3] + 15 * mm, y, rate)
        c.drawRightString(cols[4] + 15 * mm, y, net)
        y -= 6 * mm
    c.setStrokeColor(colors.HexColor("#a7b2a9"))
    c.line(x0, y + 2 * mm, w - x0, y + 2 * mm)

    y -= 4 * mm
    totals = [
        ("Total labour", "4,914.00"),
        ("Total materials & plant", "7,735.00"),
        ("Net total", "12,649.00"),
        ("VAT @ 20% - DOMESTIC REVERSE CHARGE APPLIES", "0.00"),
        ("Gross total", "12,649.00"),
        ("Retention withheld @ 3%", "-379.47"),
        ("CIS deduction @ 20% on labour (4,914.00)", "-982.80"),
    ]
    c.setFont("Helvetica", 9)
    for label, value in totals:
        if label.startswith(("Net", "Gross")):
            c.setFont("Helvetica-Bold", 9)
        c.drawRightString(cols[4] - 4 * mm, y, label)
        c.drawRightString(cols[4] + 15 * mm, y, value)
        c.setFont("Helvetica", 9)
        y -= 5.5 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(cols[4] - 22 * mm, y - 2 * mm, "AMOUNT PAYABLE")
    c.drawRightString(cols[4] + 15 * mm, y - 2 * mm, "GBP 11,286.73")

    y -= 18 * mm
    c.setFont("Helvetica", 8.5)
    text = c.beginText(x0, y)
    for line in [
        "Reverse charge: customer to pay the VAT to HMRC. VAT Act 1994 Section 55A applies. Supply of construction services.",
        "CIS: we are registered for gross payment status verification - please verify UTR 1234567890 before deduction.",
        "Payment terms: 30 days from invoice date. Please quote invoice number on remittance.",
        "",
        "Bank: Barclays Bank plc   Account name: Northbank Mechanical Services Ltd   Sort code: 20-45-77   Account no: 30918824",
        "IBAN: GB29 BARC 2045 7730 9188 24   BIC: BARCGB22",
    ]:
        text.textLine(line)
    c.drawText(text)

    c.setFont("Helvetica-Oblique", 7.5)
    c.setFillColor(colors.HexColor("#777"))
    c.drawString(x0, 15 * mm, "Fictional document generated for testing invoice extraction. Not a real company or invoice.")
    c.showPage()
    c.save()


if __name__ == "__main__":
    draw()
    print("wrote", OUT)
