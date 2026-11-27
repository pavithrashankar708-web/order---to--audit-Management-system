"""Report builders. Each returns dict(title, headers, types, rows, note). Money cells are integer paise."""
import csv, io
from datetime import date
from decimal import Decimal
from . import services as S
from .core import fmt_money, fmt_qty, fmt_bp, plain_money, fmt_gst, bp_diff, div_round

ONE = S.one
ALL = S.allrows
PAID = "(SELECT COALESCE(SUM(amount_paise),0) FROM payments p WHERE p.invoice_id=inv.id)"


def _range(a, col="inv.date"):
    sql, args = "", []
    if a.get("date_from"):
        sql += f" AND {col}>=?"; args.append(a["date_from"])
    if a.get("date_to"):
        sql += f" AND {col}<=?"; args.append(a["date_to"])
    return sql, args


def sales_register(conn, a):
    w, args = _range(a)
    rows = [[r["date"], r["number"], r["name"], r["gstin"] or "-", r["state"], r["taxable_paise"], r["cgst_paise"], r["sgst_paise"],
             r["igst_paise"], r["total_paise"], r["paid"], r["total_paise"] - r["paid"]] for r in ALL(conn,
        f"SELECT inv.*, c.name, c.gstin, c.state, {PAID} AS paid FROM invoices inv JOIN customers c ON c.id=inv.customer_id"
        f" WHERE 1=1{w} ORDER BY inv.date, inv.id", *args)]
    return dict(title="Sales register", headers=["Date", "Invoice", "Customer", "GSTIN (dummy)", "State", "Taxable", "CGST", "SGST", "IGST", "Total", "Received", "Outstanding"],
                types=["text"] * 5 + ["money"] * 7, rows=rows)


def customer_sales(conn, a):
    w, args = _range(a)
    rows = [[r["name"], r["ctype"], r["n"], r["taxable"], r["total"], r["paid"], r["total"] - r["paid"]] for r in ALL(conn,
        f"SELECT c.name, c.ctype, COUNT(*) n, SUM(inv.taxable_paise) taxable, SUM(inv.total_paise) total, SUM({PAID}) paid"
        f" FROM invoices inv JOIN customers c ON c.id=inv.customer_id WHERE 1=1{w} GROUP BY c.id ORDER BY total DESC", *args)]
    return dict(title="Customer-wise sales", headers=["Customer", "Type", "Invoices", "Taxable", "Invoice total", "Received", "Outstanding"],
                types=["text", "text", "text", "money", "money", "money", "money"], rows=rows)


def product_sales(conn, a):
    w, args = _range(a)
    rows = [[r["description"], r["hsn"], r["qty"], r["taxable"], r["total"]] for r in ALL(conn,
        f"SELECT il.description, il.hsn, SUM(il.qty) qty, SUM(il.taxable_paise) taxable, SUM(il.total_paise) total FROM invoice_lines il"
        f" JOIN invoices inv ON inv.id=il.invoice_id WHERE 1=1{w} GROUP BY il.product_id ORDER BY total DESC", *args)]
    return dict(title="Product-wise sales", headers=["Product", "HSN", "Packets", "Taxable", "Invoice total"],
                types=["text", "text", "text", "money", "money"], rows=rows)


def gst_outward(conn, a):
    w, args = _range(a)
    rows = [[("Inter-state (IGST)" if r["interstate"] else "Intra-state (CGST+SGST)"), r["hsn"], fmt_gst(r["gst_bp"]), r["taxable"], r["cgst"], r["sgst"], r["igst"], r["total"]]
            for r in ALL(conn, f"SELECT inv.interstate, il.hsn, il.gst_bp, SUM(il.taxable_paise) taxable, SUM(il.cgst_paise) cgst,"
                               f" SUM(il.sgst_paise) sgst, SUM(il.igst_paise) igst, SUM(il.total_paise) total FROM invoice_lines il"
                               f" JOIN invoices inv ON inv.id=il.invoice_id WHERE 1=1{w} GROUP BY inv.interstate, il.hsn, il.gst_bp", *args)]
    return dict(title="GST outward supply summary", headers=["Supply type", "HSN", "Rate", "Taxable", "CGST", "SGST", "IGST", "Invoice value"],
                types=["text"] * 3 + ["money"] * 5, rows=rows)


def purchase_register(conn, a):
    w, args = _range(a, "p.date")
    rows = []
    for r in ALL(conn, f"SELECT p.date, p.number, p.supplier_inv_no, s.name sup, m.name mat, m.unit, pl.qty_milli, pl.rate_paise, pl.gst_bp, pl.amount_paise"
                       f" FROM purchase_lines pl JOIN purchases p ON p.id=pl.purchase_id JOIN suppliers s ON s.id=p.supplier_id"
                       f" JOIN materials m ON m.id=pl.material_id WHERE 1=1{w} ORDER BY p.date, p.id, pl.id", *args):
        gst = div_round(r["amount_paise"] * r["gst_bp"], 10000)
        rows.append([r["date"], r["number"], r["supplier_inv_no"] or "-", r["sup"], r["mat"], f"{fmt_qty(r['qty_milli'])} {r['unit']}",
                     r["rate_paise"], r["amount_paise"], gst, r["amount_paise"] + gst])
    return dict(title="Purchase register", headers=["Date", "Purchase", "Supplier inv.", "Supplier", "Material", "Qty", "Rate/unit", "Taxable", "GST", "Total"],
                types=["text"] * 6 + ["money"] * 4, rows=rows)


def stock_summary(conn, a):
    rows = []
    for m in ALL(conn, "SELECT * FROM materials WHERE active=1 ORDER BY category DESC, name"):
        bal = S.stock_balance(conn, material_id=m["id"])
        rows.append([("Raw material" if m["category"] == "RM" else "Packaging"), m["name"], fmt_qty(bal), m["unit"], fmt_qty(m["reorder_milli"]),
                     S.div_round(bal * m["last_cost_paise"], 1000), "LOW" if bal <= m["reorder_milli"] and m["reorder_milli"] else "OK"])
    for p in ALL(conn, "SELECT * FROM products ORDER BY name"):
        rows.append(["Finished goods", f"{p['name']} {p['pack_size']}", str(S.stock_balance(conn, product_id=p["id"])), "pkt", "-", None, "OK"])
    return dict(title="Stock summary", headers=["Type", "Item", "Balance", "Unit", "Reorder level", "Value @ latest cost", "Status"],
                types=["text"] * 5 + ["money", "text"], rows=rows,
                note="Raw/packaging valued at latest purchase price. Finished-goods value is not computed in this prototype.")


def _item_filter(a):
    it = a.get("item") or ""
    if it.startswith("m"):
        return " AND l.material_id=?", [int(it[1:])]
    if it.startswith("p"):
        return " AND l.product_id=?", [int(it[1:])]
    return "", []


def stock_ledger(conn, a):
    w, args = _range(a, "l.date")
    w2, a2 = _item_filter(a)
    rows = []
    for r in ALL(conn, f"SELECT l.*, COALESCE(m.name, p.name) item FROM stock_ledger l LEFT JOIN materials m ON m.id=l.material_id"
                       f" LEFT JOIN products p ON p.id=l.product_id WHERE 1=1{w}{w2} ORDER BY l.date, l.id", *(args + a2)):
        q = fmt_qty(r["qty"]) if r["item_kind"] != "FG" else str(r["qty"])
        rows.append([r["date"], r["item"], r["movement"], r["batch_ref"], r["source_no"], q, r["unit"], r["note"] or ""])
    return dict(title="Stock ledger (source-linked)", headers=["Date", "Item", "Movement", "Batch / lot", "Source document", "Qty (+in / -out)", "Unit", "Note"],
                types=["text"] * 8, rows=rows)


def stock_rollforward(conn, a):
    """Opening + receipts - issues/sales ± adjustments = closing, per item, for the chosen period."""
    d_from, d_to = a.get("date_from") or "0000-01-01", a.get("date_to") or "9999-12-31"
    rows = []
    items = [("m", m["id"], m["name"], m["category"] != "FG", m["unit"]) for m in ALL(conn, "SELECT * FROM materials WHERE active=1 ORDER BY category DESC, name")]
    items += [("p", p["id"], f"{p['name']} {p['pack_size']}", False, "pkt") for p in ALL(conn, "SELECT * FROM products ORDER BY name")]
    for pre, iid, name, milli, unit in items:
        col = "material_id" if pre == "m" else "product_id"
        def q(extra, *args):
            return ONE(conn, f"SELECT COALESCE(SUM(qty),0) FROM stock_ledger WHERE {col}=? AND {extra}", iid, *args)[0]
        opening = q("(date<? OR movement='OPENING')", d_from)
        rec = q("movement IN('PURCHASE','PRODUCTION_RECEIPT') AND date>=? AND date<=?", d_from, d_to)
        iss = -q("movement IN('ISSUE_TO_PRODUCTION','PACKAGING_USE','SALE') AND date>=? AND date<=?", d_from, d_to)
        adj = q("movement='ADJUSTMENT' AND date>=? AND date<=?", d_from, d_to)
        closing = q("date<=?", d_to)
        f = fmt_qty if milli or pre == "m" else str
        rows.append([name, unit, f(opening), f(rec), f(iss), f(adj), f(closing), "OK" if opening + rec - iss + adj == closing else "CHECK"])
    return dict(title="Stock roll-forward", headers=["Item", "Unit", "Opening", "+ Receipts", "- Issues / sales", "± Adjustments", "Closing", "Reconciles"],
                types=["text"] * 8, rows=rows, note="Closing = Opening + Receipts - Issues/Sales ± Adjustments.")


def production_variance(conn, a):
    rows = []
    for r in ALL(conn, "SELECT id FROM production_runs WHERE kind='PRODUCTION' AND status='COMPLETED' ORDER BY date, id"):
        s = S.run_summary(conn, r["id"])
        run = s["run"]
        rows.append([run["batch_id"], "Yield (packets)", str(run["planned_output"]), str(run["actual_good"]), fmt_bp(s["yield_bp"]), "FLAG" if s["yield_flag"] else "ok"])
        for l in s["lines"]:
            tag = "FLAG - excess" if l["adverse"] else "FLAG - under-use" if l["flag"] else "ok"
            rows.append([run["batch_id"], f"{l['name']} ({l['unit']})", fmt_qty(l["planned_milli"]), fmt_qty(l["actual_milli"]), fmt_bp(l["var_bp"]), tag])
    return dict(title="Production variance report", headers=["Batch", "Item", "Planned", "Actual", "Variance", "Flag (>3%)"],
                types=["text"] * 6, rows=rows, note="Variance = (actual - planned) / planned. Materials are compared with the batch plan; yield with planned output.")


def cost_per_packet(conn, a):
    rows = []
    for r in ALL(conn, "SELECT id FROM production_runs WHERE kind='PRODUCTION' AND status='COMPLETED' ORDER BY date, id"):
        s = S.run_summary(conn, r["id"])
        run = s["run"]
        price = ONE(conn, "SELECT price_paise FROM products WHERE id=?", run["product_id"])[0]
        rows.append([run["batch_id"], run["actual_good"], s["material_cost"], s["labour"], s["power"], s["total_cost"], s["cost_per_packet"], price - s["cost_per_packet"]])
    return dict(title="Cost per packet", headers=["Batch", "Good packets", "Material", "Labour", "Gas/power", "Total cost", "Cost / packet", "Margin / packet (pre-GST)"],
                types=["text", "text"] + ["money"] * 6, rows=rows, note="Materials costed at latest purchase price at issue time; overheads as entered.")


def collections(conn, a):
    w, args = _range(a, "pay.date")
    rows = [[r["date"], r["receipt_no"], r["number"], r["name"], r["mode"], r["reference"] or "", r["amount_paise"]] for r in ALL(conn,
        f"SELECT pay.*, inv.number, c.name FROM payments pay JOIN invoices inv ON inv.id=pay.invoice_id JOIN customers c ON c.id=pay.customer_id"
        f" WHERE 1=1{w} ORDER BY pay.date, pay.id", *args)]
    return dict(title="Collections (receipts)", headers=["Date", "Receipt", "Invoice", "Customer", "Mode", "Reference", "Amount"],
                types=["text"] * 6 + ["money"], rows=rows)


def outstanding(conn, a):
    today = date.today()
    rows = [[r["name"], r["number"], r["date"], (today - date.fromisoformat(r["date"])).days, r["total_paise"], r["paid"], r["total_paise"] - r["paid"]] for r in ALL(conn,
        f"SELECT inv.*, c.name, {PAID} AS paid FROM invoices inv JOIN customers c ON c.id=inv.customer_id ORDER BY inv.date, inv.id") if r["total_paise"] > r["paid"]]
    return dict(title="Outstanding receivables", headers=["Customer", "Invoice", "Invoice date", "Days", "Invoice total", "Received", "Outstanding"],
                types=["text"] * 4 + ["money"] * 3, rows=rows)


def ageing(conn, a):
    today, buckets = date.today(), {}
    for r in ALL(conn, f"SELECT inv.date, inv.total_paise, c.name, {PAID} AS paid FROM invoices inv JOIN customers c ON c.id=inv.customer_id"):
        out = r["total_paise"] - r["paid"]
        if out <= 0:
            continue
        d = (today - date.fromisoformat(r["date"])).days
        b = buckets.setdefault(r["name"], [0, 0, 0, 0])
        b[0 if d <= 30 else 1 if d <= 60 else 2 if d <= 90 else 3] += out
    rows = [[n, *b, sum(b)] for n, b in sorted(buckets.items())]
    return dict(title="Customer ageing (by invoice date)", headers=["Customer", "0-30 days", "31-60 days", "61-90 days", "90+ days", "Total"],
                types=["text"] + ["money"] * 5, rows=rows, note="Ageing is measured from invoice date; no credit terms are modelled.")


REPORTS = {"sales_register": sales_register, "customer_sales": customer_sales, "product_sales": product_sales,
           "gst_outward": gst_outward, "purchase_register": purchase_register, "stock_summary": stock_summary,
           "stock_ledger": stock_ledger, "stock_rollforward": stock_rollforward, "production_variance": production_variance,
           "cost_per_packet": cost_per_packet, "collections": collections, "outstanding": outstanding, "ageing": ageing}


def cell_text(v, t, symbol="₹"):
    if v is None:
        return "-"
    return fmt_money(v, symbol) if t == "money" else str(v)


def to_csv(rep):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rep["headers"])
    for r in rep["rows"]:
        w.writerow([("" if v is None else plain_money(v)) if t == "money" else v for v, t in zip(r, rep["types"])])
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(rep):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = rep["title"][:30]
    ws.append(rep["headers"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="6D5BD0")
    for r in rep["rows"]:
        ws.append([(None if v is None else Decimal(plain_money(v))) if t == "money" else v for v, t in zip(r, rep["types"])])
    for j, t in enumerate(rep["types"], 1):
        ws.column_dimensions[ws.cell(1, j).column_letter].width = 18
        if t == "money":
            for row in ws.iter_rows(min_row=2, min_col=j, max_col=j):
                row[0].number_format = "#,##0.00"
    bio = io.BytesIO(); wb.save(bio)
    return bio.getvalue()


def to_pdf(rep):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    st = getSampleStyleSheet()
    data = [rep["headers"]] + [[cell_text(v, t, "Rs. ") for v, t in zip(r, rep["types"])] for r in rep["rows"]]
    tbl = Table(data, repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6D5BD0")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                             ("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C9C3EA")),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F3FD")])]))
    els = [Paragraph(f"{S.COMPANY['name']} - {rep['title']}", st["Title"]), Paragraph(f"Generated {date.today():%d %b %Y}", st["Normal"]), Spacer(1, 8), tbl]
    if rep.get("note"):
        els += [Spacer(1, 6), Paragraph(rep["note"], st["Italic"])]
    doc.build(els)
    return bio.getvalue()
