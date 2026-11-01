"""Excel and PDF exports."""
from io import BytesIO

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HEADER_FILL = PatternFill("solid", fgColor="0B5394")


def _sheet(ws, headers, rows):
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEADER_FILL
    for r in rows:
        ws.append(["" if v is None else v for v in r])
    for i, h in enumerate(headers, start=1):
        width = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows[:200]]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 60)
    ws.freeze_panes = "A2"


def _xlsx_response(wb, filename):
    buf = BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def companies_to_xlsx(qs):
    wb = Workbook()
    ws = wb.active
    ws.title = "Companies"
    rows = []
    for c in qs.prefetch_related("contacts", "products_interested"):
        primary = c.primary_contact
        rows.append([
            c.pk, c.name, c.country.name if c.country_id else "", c.city, c.website, c.get_company_type_display(),
            c.get_business_type_display(), c.oem_category, c.oem_score, c.get_stage_display(), c.get_lead_source_display(),
            c.get_verification_status_display(), c.owner.username if c.owner_id else "",
            ", ".join(p.name for p in c.products_interested.all()), c.existing_brands, c.expected_order_quantity,
            float(c.expected_order_value) if c.expected_order_value is not None else None, c.expected_value_currency,
            c.next_action, c.next_action_date.isoformat() if c.next_action_date else "",
            c.last_contacted_at.strftime("%Y-%m-%d") if c.last_contacted_at else "",
            c.last_reply_at.strftime("%Y-%m-%d") if c.last_reply_at else "",
            primary.full_name if primary else "", primary.email if primary else "",
            primary.get_email_status_display() if primary and primary.email else "", "Yes" if c.do_not_contact else "",
        ])
    _sheet(ws, ["ID", "Company", "Country", "City", "Website", "Type", "OEM/Export", "OEM category", "OEM score", "Stage", "Source",
                "Verification", "Owner", "Products interested", "Brands", "Expected qty", "Expected value", "Currency", "Next action",
                "Next action date", "Last contacted", "Last reply", "Primary contact", "E-mail", "E-mail status", "Do not contact"], rows)
    return _xlsx_response(wb, f"companies-{timezone.localdate()}.xlsx")


def report_to_xlsx(snap):
    m = snap.metrics
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    keys = [k for k, v in m.items() if isinstance(v, (int, float, str)) and k != "delivery_note"]
    _sheet(ws, ["Metric", "Value"], [[k.replace("_", " ").capitalize(), m[k]] for k in keys])
    ws.append([])
    ws.append(["Note", m.get("delivery_note", "")])
    for title, key, cols in (("By country", "by_country", ["country", "new_companies", "emails_sent", "replies", "interested", "quotations_sent"]),
                             ("By product", "by_product", ["category", "companies_interested", "quoted_lines"]),
                             ("By employee", "by_employee", ["employee", "emails_approved_sent", "quotations_approved", "tasks_completed", "open_opportunities"])):
        sheet = wb.create_sheet(title)
        _sheet(sheet, [c.replace("_", " ").capitalize() for c in cols], [[row.get(c, 0) for c in cols] for row in m.get(key, [])])
    if snap.narrative:
        ns = wb.create_sheet("Narrative")
        for line in snap.narrative.splitlines():
            ns.append([line])
        ns.column_dimensions["A"].width = 120
    return _xlsx_response(wb, f"report-{snap.period}-{snap.start}.xlsx")


def report_to_pdf(snap) -> bytes:
    m = snap.metrics
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"{snap.get_period_display()} report {snap.start}")
    st = getSampleStyleSheet()
    story = [Paragraph(f"Flotec Export &amp; OEM Sales – {snap.get_period_display()} report", st["Title"]),
             Paragraph(f"{snap.start} to {snap.end}", st["Normal"]), Spacer(1, 5 * mm)]
    kpis = [["New companies", m["new_companies"], "Verified buyers", m["verified_buyers"]],
            ["E-mails sent", m["emails_sent"], "Bounced", m["emails_bounced"]],
            ["Replies", m["replies_received"], "Interested", m["interested"]],
            ["Quotations requested", m["quotations_requested"], "Quotations sent", m["quotations_sent"]],
            ["Follow-ups sent", m["followups_sent"], "Meetings logged", m["meetings_logged"]],
            ["Orders confirmed", m["orders_confirmed"], "Complaints", m["complaints"]],
            ["Pending e-mail approvals", m["pending_email_approvals"], "Pending quotation approvals", m["pending_quotation_approvals"]],
            ["Agent errors", m["agent_errors"], "AI cost (USD)", f"{m['ai_cost_usd']:.2f}"]]
    t = Table(kpis, colWidths=[50 * mm, 30 * mm, 60 * mm, 30 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story += [t, Spacer(1, 3 * mm), Paragraph(f"<font size=7>{m.get('delivery_note', '')}</font>", st["Normal"])]
    pv = ", ".join(f"{v:,.0f} {k}" for k, v in m.get("open_pipeline_value", {}).items()) or "—"
    story += [Spacer(1, 3 * mm), Paragraph(f"Open pipeline: {m['open_pipeline_companies']} companies, expected value {pv}", st["Normal"])]
    if snap.narrative:
        story += [Spacer(1, 4 * mm), Paragraph("<b>Summary</b>", st["Normal"]),
                  Paragraph(snap.narrative.replace("\n", "<br/>"), st["Normal"])]
    if m.get("by_country"):
        rows = [["Country", "New", "Sent", "Replies", "Interested", "Quotes"]] + [
            [r["country"], r.get("new_companies", 0), r.get("emails_sent", 0), r.get("replies", 0), r.get("interested", 0),
             r.get("quotations_sent", 0)] for r in m["by_country"][:25]]
        ct = Table(rows, repeatRows=1)
        ct.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8),
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b5394")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]))
        story += [Spacer(1, 5 * mm), Paragraph("<b>Country-wise performance</b>", st["Normal"]), ct]
    if m.get("by_employee"):
        rows = [["Employee", "E-mails approved", "Quotations approved", "Tasks done", "Open opps"]] + [
            [r["employee"], r.get("emails_approved_sent", 0), r.get("quotations_approved", 0), r.get("tasks_completed", 0),
             r.get("open_opportunities", 0)] for r in m["by_employee"]]
        et = Table(rows, repeatRows=1)
        et.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        story += [Spacer(1, 5 * mm), Paragraph("<b>Employee-wise performance</b>", st["Normal"]), et]
    doc.build(story)
    return buf.getvalue()
