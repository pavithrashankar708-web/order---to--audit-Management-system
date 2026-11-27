import io
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from . import services as S
from .core import fmt_money, fmt_gst

R = "Rs. "  # built-in PDF fonts have no rupee glyph


def invoice_pdf(conn, invoice_id):
    inv = S.one(conn, "SELECT inv.*, o.number order_no FROM invoices inv JOIN orders o ON o.id=inv.order_id WHERE inv.id=?", invoice_id)
    c = S.one(conn, "SELECT * FROM customers WHERE id=?", inv["customer_id"])
    lines = S.allrows(conn, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", invoice_id)
    paid, out = S.invoice_balance(conn, invoice_id)
    st = getSampleStyleSheet()
    m = lambda v: fmt_money(v, R)
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=A4, leftMargin=30, rightMargin=30, topMargin=28, bottomMargin=28)
    co = S.COMPANY
    els = [Paragraph(f"<b>TAX INVOICE</b> &nbsp; {inv['number']}", st["Title"]),
           Paragraph(f"Date: {inv['date']} &nbsp;|&nbsp; Order: {inv['order_no']} &nbsp;|&nbsp; Place of supply: {inv['place_of_supply']}", st["Normal"]), Spacer(1, 8)]
    parties = Table([[Paragraph(f"<b>Supplier</b><br/>{co['name']}<br/>{co['address']}<br/>GSTIN: {co['gstin']} (DEMO)", st["Normal"]),
                      Paragraph(f"<b>Bill to</b><br/>{c['name']}<br/>{c['address'] or ''}<br/>State: {c['state']}<br/>GSTIN: {c['gstin'] or 'Unregistered'} (DEMO)", st["Normal"])]],
                    colWidths=[265, 265])
    parties.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    hdr = ["#", "Description", "HSN", "Qty", "Rate", "Taxable", "GST %", "CGST", "SGST", "IGST", "Total"]
    data = [hdr] + [[i, l["description"], l["hsn"], l["qty"], m(l["rate_paise"]), m(l["taxable_paise"]), fmt_gst(l["gst_bp"]),
                     m(l["cgst_paise"]), m(l["sgst_paise"]), m(l["igst_paise"]), m(l["total_paise"])] for i, l in enumerate(lines, 1)]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6D5BD0")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey)]))
    kind = "IGST (inter-state supply)" if inv["interstate"] else "CGST + SGST (intra-state, Tamil Nadu)"
    tot = Table([["Taxable value", m(inv["taxable_paise"])], ["CGST", m(inv["cgst_paise"])], ["SGST", m(inv["sgst_paise"])], ["IGST", m(inv["igst_paise"])],
                 ["Invoice total", m(inv["total_paise"])], ["Payment received", m(paid)], ["Outstanding balance", m(out)]], colWidths=[130, 110], hAlign="RIGHT")
    tot.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8), ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, 4), (-1, 4), "Helvetica-Bold"),
                             ("FONTNAME", (0, 6), (-1, 6), "Helvetica-Bold"), ("LINEABOVE", (0, 4), (-1, 4), 0.5, colors.black)]))
    els += [parties, Spacer(1, 10), t, Spacer(1, 10), Paragraph(f"Tax applied: {kind}", st["Normal"]), Spacer(1, 6), tot, Spacer(1, 14),
            Paragraph(f"<i>{S.GSTIN_NOTE}. Prototype invoice generated for demonstration only.</i>", st["Italic"])]
    doc.build(els)
    return bio.getvalue()
