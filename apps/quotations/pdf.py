"""Quotation PDF (ReportLab). Uses only data stored on the approved quotation and the company profile."""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.core.models import CompanyProfile


def _money(value, currency=""):
    if value is None:
        return "—"
    return f"{value:,.2f} {currency}".strip()


def render_quotation_pdf(q, draft=False) -> bytes:
    p = CompanyProfile.get_solo()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Quotation {q.number}", author=p.legal_name)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=11)
    normal = ParagraphStyle("n", parent=styles["Normal"], fontSize=9.5, leading=12.5)
    h = ParagraphStyle("h", parent=styles["Heading1"], fontSize=16, spaceAfter=2)
    story = []
    header_left = [Paragraph(f"<b>{p.legal_name}</b> ({p.brand_name})", normal),
                   Paragraph("<br/>".join(x for x in [p.address.replace("\n", "<br/>"), ", ".join(x for x in [p.city, p.state, p.country] if x)] if x), small)]
    contact_bits = [x for x in [p.phone and f"Tel: {p.phone}", p.email, p.website, p.gstin and f"GSTIN: {p.gstin}", p.iec_code and f"IEC: {p.iec_code}"] if x]
    header_left.append(Paragraph(" · ".join(contact_bits), small))
    logo = ""
    if p.logo:
        try:
            with p.logo.open("rb") as fh:  # works for disk and database storage
                logo = Image(BytesIO(fh.read()), width=35 * mm, height=18 * mm, kind="proportional")
        except Exception:  # noqa: BLE001 - a broken logo must not prevent the quotation
            logo = ""
    t = Table([[header_left, logo]], colWidths=[140 * mm, 40 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story += [t, Spacer(1, 6 * mm)]
    story.append(Paragraph(("DRAFT – NOT VALID " if draft else "") + "QUOTATION", h))
    company = q.company
    buyer = [f"<b>{company.name}</b>", company.address.replace("\n", "<br/>") if company.address else "",
             ", ".join(x for x in [company.city, company.country.name if company.country_id else ""] if x),
             f"Attn: {q.contact.full_name or q.contact.email}" if q.contact else ""]
    meta = [["Quotation no.", q.number], ["Date", (q.approved_at or q.created_at).strftime("%d %b %Y")],
            ["Valid until", q.valid_until.strftime("%d %b %Y") if q.valid_until else "—"], ["Currency", q.currency]]
    mt = Table([[Paragraph("<br/>".join(b for b in buyer if b), normal), Table(meta, colWidths=[28 * mm, 42 * mm])]],
               colWidths=[110 * mm, 70 * mm])
    mt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [mt, Spacer(1, 5 * mm)]
    rows = [["#", "Product", "Size", "Material", "Pressure", "Connection", "Qty", "Unit price", "Amount"]]
    for line in q.lines.all():
        desc = line.description + (f"<br/><font size=7 color='#555555'>Packing: {line.packaging}</font>" if line.packaging else "")
        rows.append([str(line.position), Paragraph(desc, small), line.size, line.material, line.pressure_rating,
                     Paragraph(line.connection_type, small), f"{line.quantity:,} {line.unit}",
                     _money(line.unit_price), _money(line.line_total)])
    rows.append(["", "", "", "", "", "", "", "Subtotal", _money(q.subtotal, q.currency)])
    if q.discount_percent:
        rows.append(["", "", "", "", "", "", "", f"Discount {q.discount_percent}%", f"- {_money(q.discount_amount, q.currency)}"])
    if q.freight_amount is not None:
        rows.append(["", "", "", "", "", "", "", "Freight", _money(q.freight_amount, q.currency)])
    rows.append(["", "", "", "", "", "", "", "TOTAL", _money(q.total, q.currency)])
    n_lines = q.lines.count()
    table = Table(rows, colWidths=[7 * mm, 48 * mm, 15 * mm, 15 * mm, 17 * mm, 25 * mm, 18 * mm, 18 * mm, 21 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b5394")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, n_lines), 0.3, colors.HexColor("#bbbbbb")),
        ("ALIGN", (6, 1), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (7, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story += [table, Spacer(1, 5 * mm)]
    terms = [["Incoterm", f"{q.incoterm} {q.port_of_loading if q.incoterm in ('FOB', 'FCA', 'EXW') else q.destination}".strip()],
             ["Port of loading", q.port_of_loading or "—"], ["Destination", q.destination or "—"],
             ["Payment terms", q.payment_terms or "—"], ["Lead time", q.lead_time or "—"],
             ["Freight", q.freight_note or (_money(q.freight_amount, q.currency) if q.freight_amount is not None else "—")],
             ["Packaging", q.packaging or "As per item packing"]]
    tt = Table([[Paragraph(f"<b>{k}</b>", small), Paragraph(v, small)] for k, v in terms], colWidths=[35 * mm, 145 * mm])
    tt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story.append(tt)
    if any(line.moq for line in q.lines.all()):
        story.append(Paragraph("MOQ per item: " + "; ".join(f"#{line.position}: {line.moq:,}" for line in q.lines.all() if line.moq), small))
    if q.notes:
        story += [Spacer(1, 3 * mm), Paragraph(q.notes.replace("\n", "<br/>"), small)]
    if p.quotation_terms:
        story += [Spacer(1, 3 * mm), Paragraph("<b>Terms &amp; conditions</b>", small), Paragraph(p.quotation_terms.replace("\n", "<br/>"), small)]
    story += [Spacer(1, 8 * mm), Paragraph(f"For {p.legal_name}", normal)]
    if q.approved_by:
        story.append(Paragraph(f"Approved by {q.approved_by.get_full_name() or q.approved_by.username}", small))
    doc.build(story)
    return buf.getvalue()
