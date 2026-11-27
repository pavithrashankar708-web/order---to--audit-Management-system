import sqlite3
from datetime import date
from flask import Blueprint, g, render_template, request, redirect, url_for, flash, abort, session, Response
from werkzeug.security import check_password_hash
from . import db as DB, services as S, reports as RP, aura, pdf, seed
from .core import BizError, to_paise, to_milli, to_bp, plain_money, fmt_qty, fmt_gst, fmt_money, div_round
import secrets

bp = Blueprint("main", __name__)
OFFICE = ("admin", "supervisor", "owner", "auditor")
ALL, ONE = S.allrows, S.one


def roles(*names):
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def inner(*a, **kw):
            if g.user["role"] not in names:
                abort(403)
            return fn(*a, **kw)
        return inner
    return deco


def do(fn, ok=None):
    """Run a posting inside one transaction; show business errors as flash messages."""
    try:
        with DB.atomic(g.db):
            val = fn()
        if ok:
            flash(ok, "ok")
        return True, val
    except BizError as e:
        flash(str(e), "err")
    except sqlite3.IntegrityError as e:
        flash(f"Could not save: {e}", "err")
    return False, None


def fdate(name="date"):
    v = request.form.get(name) or date.today().isoformat()
    try:
        date.fromisoformat(v)
    except ValueError:
        raise BizError("Invalid date")
    return v


def fint(name, default=0):
    v = (request.form.get(name) or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise BizError(f"{name} must be a whole number")


# ───────────── auth ─────────────
@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = ONE(g.db, "SELECT * FROM users WHERE username=? AND active=1", request.form.get("username", "").strip())
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear(); session["uid"] = u["id"]; session["csrf"] = secrets.token_hex(16)
            return redirect(url_for("main.home"))
        flash("Invalid username or password", "err")
    return render_template("login.html", demo=seed.USERS, pw=seed.DEMO_PASSWORD)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ───────────── dashboard ─────────────
@bp.route("/")
def home():
    if g.user["role"] == "salesman":
        return redirect(url_for("main.orders"))
    c = g.db
    inv = ONE(c, "SELECT COALESCE(SUM(total_paise),0) t, COALESCE(SUM(taxable_paise),0) x, COUNT(*) n FROM invoices")
    paid = ONE(c, "SELECT COALESCE(SUM(amount_paise),0) FROM payments")[0]
    stock = []
    for m in ALL(c, "SELECT * FROM materials WHERE active=1 ORDER BY category DESC, name"):
        bal = S.stock_balance(c, material_id=m["id"])
        pct = 100 if not m["reorder_milli"] else min(100, bal * 50 // m["reorder_milli"])
        stock.append(dict(m=m, bal=bal, pct=pct, low=bool(m["reorder_milli"]) and bal <= m["reorder_milli"]))
    last = ONE(c, "SELECT id FROM production_runs WHERE kind='PRODUCTION' AND status='COMPLETED' ORDER BY id DESC")
    return render_template("dashboard.html", inv=inv, paid=paid, outstanding=inv["t"] - paid, stock=stock,
                           pending=ONE(c, "SELECT COUNT(*) FROM orders WHERE status='SUBMITTED'")[0],
                           fg=sum(S.stock_balance(c, product_id=p["id"]) for p in ALL(c, "SELECT id FROM products")),
                           last=S.run_summary(c, last["id"]) if last else None, insights=aura.insights(c)[:5],
                           recent=ALL(c, "SELECT o.*, cu.name cname FROM orders o JOIN customers cu ON cu.id=o.customer_id ORDER BY o.id DESC LIMIT 5"))


# ───────────── masters (Settings) ─────────────
class F:
    def __init__(s, name, label, typ="text", opts=None, req=True, default=""):
        s.name, s.label, s.type, s.opts, s.req, s.default = name, label, typ, opts, req, default


MASTERS = {
    "products": dict(table="products", title="Products", write=("admin",), order="name", fields=[
        F("sku", "SKU"), F("name", "Product name"), F("pack_size", "Pack size", default="200 g"), F("price_paise", "Price ₹ (before GST)", "rupees"),
        F("distributor_price_paise", "Distributor price ₹ (optional)", "rupees", req=False), F("gst_bp", "GST %", "percent", default="5"),
        F("hsn", "HSN code"), F("std_batch_output", "Standard batch output (packets)", "int", default="0")]),
    "customers": dict(table="customers", title="Customers", write=("admin",), order="name", fields=[
        F("name", "Customer name"), F("ctype", "Type", "select", ["retailer", "distributor"]), F("state", "State", default="Tamil Nadu"),
        F("gstin", "GSTIN (use dummy values)", req=False), F("address", "Address", req=False)]),
    "suppliers": dict(table="suppliers", title="Suppliers", write=("supervisor",), order="name", fields=[
        F("name", "Supplier name"), F("state", "State", req=False), F("gstin", "GSTIN (dummy)", req=False)]),
    "materials": dict(table="materials", title="Raw & packaging materials", write=("supervisor",), order="category DESC, name", fields=[
        F("code", "Code"), F("name", "Name"), F("category", "Category", "select", ["RM", "PM"]), F("unit", "Unit (kg, L, pcs)"),
        F("last_cost_paise", "Cost per unit ₹", "rupees"), F("reorder_milli", "Reorder level", "qty", default="0"),
        F("is_primary_pack", "Primary pack (pouch)?", "bool", req=False), F("gst_bp", "Purchase GST %", "percent", default="0")]),
}


def _parse(f):
    if f.type == "bool":
        return 1 if request.form.get(f.name) else 0
    raw = (request.form.get(f.name) or "").strip()
    if not raw:
        if f.req:
            raise BizError(f"{f.label} is required")
        return None
    return {"rupees": to_paise, "percent": to_bp, "qty": to_milli, "int": int}.get(f.type, str)(raw)


def _show(f, v):
    if v is None or v == "":
        return "—"
    return {"rupees": fmt_money, "percent": fmt_gst, "qty": fmt_qty, "bool": lambda x: "Yes" if x else "No"}.get(f.type, str)(v)


def _edit_val(f, v):
    if v is None:
        return f.default
    return {"rupees": plain_money, "percent": lambda x: fmt_gst(x).rstrip("%"), "qty": fmt_qty}.get(f.type, lambda x: x)(v)


@bp.route("/master/<key>")
@roles(*OFFICE)
def master_list(key):
    cfg = MASTERS.get(key) or abort(404)
    rows = ALL(g.db, f"SELECT * FROM {cfg['table']} ORDER BY active DESC, {cfg['order']}")
    shown = [[_show(f, r[f.name]) for f in cfg["fields"]] for r in rows]
    return render_template("master_list.html", cfg=cfg, key=key, rows=list(zip(rows, shown)), can=g.user["role"] in cfg["write"])


@bp.route("/master/<key>/new", methods=["GET", "POST"])
@bp.route("/master/<key>/<int:rid>/edit", methods=["GET", "POST"])
def master_form(key, rid=None):
    cfg = MASTERS.get(key) or abort(404)
    if g.user["role"] not in cfg["write"]:
        abort(403)
    row = ONE(g.db, f"SELECT * FROM {cfg['table']} WHERE id=?", rid) if rid else None
    if rid and not row:
        abort(404)
    if request.method == "POST":
        def save():
            vals = [_parse(f) for f in cfg["fields"]]
            names = [f.name for f in cfg["fields"]]
            if rid:
                g.db.execute(f"UPDATE {cfg['table']} SET " + ",".join(f"{n}=?" for n in names) + " WHERE id=?", (*vals, rid))
            else:
                g.db.execute(f"INSERT INTO {cfg['table']}({','.join(names)}) VALUES({','.join('?' * len(names))})", vals)
        ok, _ = do(save, "Saved")
        if ok:
            return redirect(url_for("main.master_list", key=key))
    vals = [(f, request.form.get(f.name, _edit_val(f, row[f.name] if row else None))) for f in cfg["fields"]]
    return render_template("master_form.html", cfg=cfg, key=key, vals=vals, row=row)


@bp.route("/master/<key>/<int:rid>/toggle", methods=["POST"])
def master_toggle(key, rid):
    cfg = MASTERS.get(key) or abort(404)
    if g.user["role"] not in cfg["write"]:
        abort(403)
    do(lambda: g.db.execute(f"UPDATE {cfg['table']} SET active=1-active WHERE id=?", (rid,)), "Status changed")
    return redirect(url_for("main.master_list", key=key))


@bp.route("/bom")
@roles(*OFFICE)
def bom_list():
    rows = ALL(g.db, "SELECT p.*, (SELECT COUNT(*) FROM bom_lines b WHERE b.product_id=p.id) n FROM products p ORDER BY name")
    return render_template("bom_list.html", rows=rows)


@bp.route("/bom/<int:pid>", methods=["GET", "POST"])
@roles(*OFFICE)
def bom(pid):
    p = ONE(g.db, "SELECT * FROM products WHERE id=?", pid) or abort(404)
    if request.method == "POST":
        if g.user["role"] != "supervisor":
            abort(403)
        act = request.form.get("action")
        def go():
            if act == "set_output":
                n = fint("std_batch_output")
                if n <= 0:
                    raise BizError("Expected batch output must be positive")
                g.db.execute("UPDATE products SET std_batch_output=? WHERE id=?", (n, pid))
            elif act == "add_line":
                q = to_milli(request.form.get("qty") or "0")
                if q <= 0:
                    raise BizError("Standard quantity must be positive")
                g.db.execute("INSERT INTO bom_lines(product_id,material_id,std_milli) VALUES(?,?,?) ON CONFLICT(product_id,material_id) DO UPDATE SET std_milli=excluded.std_milli",
                             (pid, int(request.form["material_id"]), q))
            elif act == "del_line":
                g.db.execute("DELETE FROM bom_lines WHERE id=? AND product_id=?", (int(request.form["line_id"]), pid))
        do(go, "BOM updated (applies to new production runs only)")
        return redirect(url_for("main.bom", pid=pid))
    lines = ALL(g.db, "SELECT b.*, m.name, m.unit, m.category FROM bom_lines b JOIN materials m ON m.id=b.material_id WHERE product_id=? ORDER BY m.category DESC, m.name", pid)
    return render_template("bom.html", p=p, lines=lines, materials=ALL(g.db, "SELECT * FROM materials WHERE active=1 ORDER BY category DESC, name"))


# ───────────── orders ─────────────
@bp.route("/orders")
@roles("salesman", "admin", "owner", "auditor")
def orders():
    where, args = "1=1", []
    if g.user["role"] == "salesman":
        where, args = "o.salesman_id=?", [g.user["id"]]
    st = request.args.get("status")
    if st:
        where += " AND o.status=?"; args.append(st)
    rows = ALL(g.db, f"SELECT o.*, c.name cname, u.name sname, (SELECT SUM(qty*price_paise) FROM order_lines l WHERE l.order_id=o.id) est"
                     f" FROM orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_id WHERE {where} ORDER BY o.id DESC", *args)
    return render_template("orders.html", rows=rows, status=st)


@bp.route("/orders/new", methods=["GET", "POST"])
@roles("salesman", "admin")
def order_new():
    if request.method == "POST":
        def go():
            lines = [(int(p), int(q or 0)) for p, q in zip(request.form.getlist("product_id"), request.form.getlist("qty")) if p]
            return S.create_order(g.db, int(request.form.get("customer_id") or 0), g.user["id"], lines, fdate(), request.form.get("notes", ""))
        ok, oid = do(go, "Order submitted for admin verification")
        if ok:
            return redirect(url_for("main.order_detail", oid=oid))
    return render_template("order_new.html", customers=ALL(g.db, "SELECT * FROM customers WHERE active=1 ORDER BY name"),
                           products=ALL(g.db, "SELECT * FROM products WHERE active=1 ORDER BY name"))


def _order_ctx(oid):
    o = ONE(g.db, "SELECT o.*, c.name cname, c.state, c.gstin, c.ctype, u.name sname FROM orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_id WHERE o.id=?", oid)
    if not o or (g.user["role"] == "salesman" and o["salesman_id"] != g.user["id"]):
        abort(404)
    return o


@bp.route("/orders/<int:oid>")
@roles("salesman", "admin", "owner", "auditor")
def order_detail(oid):
    o = _order_ctx(oid)
    inv = ONE(g.db, "SELECT * FROM invoices WHERE order_id=?", oid)
    disp = ONE(g.db, "SELECT * FROM dispatches WHERE invoice_id=?", inv["id"]) if inv else None
    return render_template("order_detail.html", o=o, lines=S.order_availability(g.db, oid), inv=inv, disp=disp,
                           bal=S.invoice_balance(g.db, inv["id"]) if inv else None)


@bp.route("/orders/<int:oid>/<action>", methods=["POST"])
@roles("admin")
def order_action(oid, action):
    o = _order_ctx(oid)
    uid = g.user["id"]
    if action == "approve":
        appr = {l["id"]: int(request.form[f"appr_{l['id']}"]) for l in ALL(g.db, "SELECT id FROM order_lines WHERE order_id=?", oid)
                if request.form.get(f"appr_{l['id']}", "").strip().isdigit()}
        do(lambda: S.approve_order(g.db, oid, appr, uid), "Order approved against available stock")
    elif action == "reject":
        do(lambda: S.reject_order(g.db, oid, request.form.get("reason", ""), uid), "Order rejected")
    elif action == "invoice":
        ok, iid = do(lambda: S.create_invoice(g.db, oid, fdate(), uid), "GST invoice generated")
        if ok:
            return redirect(url_for("main.invoice", iid=iid))
    elif action == "dispatch":
        inv = ONE(g.db, "SELECT id FROM invoices WHERE order_id=?", oid) or abort(404)
        do(lambda: S.dispatch_invoice(g.db, inv["id"], fdate(), request.form.get("vehicle", ""), uid), "Dispatched - stock reduced using FIFO batch allocation")
    else:
        abort(404)
    return redirect(url_for("main.order_detail", oid=oid))


# ───────────── invoices & payments ─────────────
@bp.route("/invoices")
@roles("admin", "owner", "auditor")
def invoices():
    rows = ALL(g.db, "SELECT inv.*, c.name cname, (SELECT COALESCE(SUM(amount_paise),0) FROM payments p WHERE p.invoice_id=inv.id) paid,"
                     " (SELECT 1 FROM dispatches d WHERE d.invoice_id=inv.id) dispatched FROM invoices inv JOIN customers c ON c.id=inv.customer_id ORDER BY inv.id DESC")
    return render_template("invoices.html", rows=rows)


@bp.route("/invoices/<int:iid>")
@roles("admin", "owner", "auditor")
def invoice(iid):
    inv = ONE(g.db, "SELECT inv.*, c.name cname, c.gstin, c.address, c.state, o.number onum FROM invoices inv JOIN customers c ON c.id=inv.customer_id JOIN orders o ON o.id=inv.order_id WHERE inv.id=?", iid) or abort(404)
    return render_template("invoice.html", inv=inv, lines=ALL(g.db, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", iid),
                           pays=ALL(g.db, "SELECT * FROM payments WHERE invoice_id=? ORDER BY id", iid), bal=S.invoice_balance(g.db, iid),
                           note=S.GSTIN_NOTE, token=secrets.token_hex(8))


@bp.route("/invoices/<int:iid>/pdf")
@roles("admin", "owner", "auditor")
def invoice_pdf(iid):
    inv = ONE(g.db, "SELECT number FROM invoices WHERE id=?", iid) or abort(404)
    return Response(pdf.invoice_pdf(g.db, iid), mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={inv['number'].replace('/', '_')}.pdf"})


@bp.route("/invoices/<int:iid>/pay", methods=["POST"])
@roles("admin")
def pay(iid):
    do(lambda: S.record_payment(g.db, iid, to_paise(request.form.get("amount") or "0"), fdate(), request.form.get("mode", "Cash"),
                                request.form.get("reference", ""), g.user["id"], request.form.get("idem") or None), "Payment recorded")
    return redirect(url_for("main.invoice", iid=iid))


@bp.route("/customers/<int:cid>")
@roles("admin", "owner", "auditor")
def customer(cid):
    c = ONE(g.db, "SELECT * FROM customers WHERE id=?", cid) or abort(404)
    rows = ALL(g.db, "SELECT inv.*, (SELECT COALESCE(SUM(amount_paise),0) FROM payments p WHERE p.invoice_id=inv.id) paid FROM invoices inv WHERE customer_id=? ORDER BY inv.id DESC", cid)
    orders_ = ALL(g.db, "SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC", cid)
    return render_template("customer.html", c=c, rows=rows, orders=orders_)


# ───────────── purchases & production ─────────────
@bp.route("/purchases")
@roles(*OFFICE)
def purchases():
    rows = ALL(g.db, "SELECT p.*, s.name sname, (SELECT SUM(amount_paise) FROM purchase_lines l WHERE l.purchase_id=p.id) amt"
                     " FROM purchases p JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC")
    return render_template("purchases.html", rows=rows)


@bp.route("/purchases/new", methods=["GET", "POST"])
@roles("supervisor")
def purchase_new():
    if request.method == "POST":
        def go():
            ls = [(int(m), to_milli(q or "0"), to_paise(r or "0")) for m, q, r in
                  zip(request.form.getlist("material_id"), request.form.getlist("qty"), request.form.getlist("rate")) if m]
            return S.post_purchase(g.db, int(request.form.get("supplier_id") or 0), fdate(), ls, g.user["id"], (request.form.get("inv_no") or "").strip())
        ok, _ = do(go, "Purchase posted: stock lot created and ledger updated")
        if ok:
            return redirect(url_for("main.purchases"))
    return render_template("purchase_new.html", suppliers=ALL(g.db, "SELECT * FROM suppliers WHERE active=1 ORDER BY name"),
                           materials=ALL(g.db, "SELECT * FROM materials WHERE active=1 ORDER BY category DESC, name"))


@bp.route("/production", methods=["GET", "POST"])
@roles(*OFFICE)
def production():
    if request.method == "POST":
        if g.user["role"] != "supervisor":
            abort(403)
        ok, rid = do(lambda: S.create_run(g.db, int(request.form["product_id"]), fint("planned"), fdate(), g.user["id"]), "Batch created")
        if ok:
            return redirect(url_for("main.run", rid=rid))
    runs = ALL(g.db, "SELECT r.*, p.name pname FROM production_runs r JOIN products p ON p.id=r.product_id WHERE kind='PRODUCTION' ORDER BY r.id DESC")
    sums = {r["id"]: S.run_summary(g.db, r["id"]) for r in runs}
    return render_template("production.html", runs=runs, sums=sums, products=ALL(g.db, "SELECT * FROM products WHERE active=1 ORDER BY name"))


@bp.route("/production/<int:rid>")
@roles(*OFFICE)
def run(rid):
    try:
        s = S.run_summary(g.db, rid)
    except BizError:
        abort(404)
    p = ONE(g.db, "SELECT * FROM products WHERE id=?", s["run"]["product_id"])
    return render_template("run.html", s=s, p=p, findings=S.analyze_run(g.db, rid), thr=S.THRESHOLD_BP)


@bp.route("/production/<int:rid>/<action>", methods=["POST"])
@roles("supervisor")
def run_action(rid, action):
    s = S.run_summary(g.db, rid)
    uid = g.user["id"]
    if action == "issue":
        items = {l["material_id"]: to_milli(request.form.get(f"q_{l['material_id']}") or "0") for l in s["lines"] if l["category"] == "RM"}
        do(lambda: S.issue_to_run(g.db, rid, items, fdate(), uid), "Raw materials issued (FIFO lots, ledger posted)")
    elif action == "complete":
        def go():
            pm = {l["material_id"]: to_milli(request.form[f"q_{l['material_id']}"]) for l in s["lines"]
                  if l["category"] == "PM" and not l["is_primary_pack"] and request.form.get(f"q_{l['material_id']}", "").strip()}
            S.complete_run(g.db, rid, fint("good"), fint("damaged"), fdate(), pm, request.form.get("notes", ""),
                           to_paise(request.form["labour"]) if request.form.get("labour", "").strip() else None,
                           to_paise(request.form["power"]) if request.form.get("power", "").strip() else None, uid)
        do(go, "Batch completed: packaging consumed, finished goods received, ledger posted")
    elif action == "costs":
        do(lambda: S.set_run_costs(g.db, rid, to_paise(request.form.get("labour") or "0"), to_paise(request.form.get("power") or "0")), "Overhead costs saved")
    else:
        abort(404)
    return redirect(url_for("main.run", rid=rid))


# ───────────── stock & traceability ─────────────
@bp.route("/stock")
@roles(*OFFICE)
def stock():
    rep = RP.stock_summary(g.db, {})
    batches = [dict(b) | dict(pname=n["name"]) for n in ALL(g.db, "SELECT * FROM products") for b in S.batch_rows(g.db, n["id"])]
    return render_template("stock.html", rep=rep, batches=batches, materials=ALL(g.db, "SELECT * FROM materials WHERE active=1 ORDER BY name"),
                           products=ALL(g.db, "SELECT * FROM products ORDER BY name"),
                           lots=ALL(g.db, "SELECT l.*, m.name, m.unit, l.qty_milli - COALESCE((SELECT SUM(qty_milli) FROM lot_allocations a WHERE a.lot_id=l.id),0) rem FROM material_lots l JOIN materials m ON m.id=l.material_id ORDER BY m.name, l.received_date, l.id"))


@bp.route("/stock/adjust", methods=["POST"])
@roles("supervisor")
def stock_adjust():
    item = request.form.get("item", "")
    def go():
        if item.startswith("m"):
            S.post_adjustment(g.db, fdate(), to_milli(request.form.get("qty") or "0"), request.form.get("reason", ""), g.user["id"], material_id=int(item[1:]))
        else:
            S.post_adjustment(g.db, fdate(), fint("qty"), request.form.get("reason", ""), g.user["id"], product_id=int(item[1:]), run_id=fint("run_id"))
    do(go, "Adjustment posted to the ledger")
    return redirect(url_for("main.stock"))


@bp.route("/trace")
@roles(*OFFICE)
def trace_index():
    batches = ALL(g.db, "SELECT r.*, p.name pname FROM production_runs r JOIN products p ON p.id=r.product_id ORDER BY r.id DESC")
    invs = ALL(g.db, "SELECT inv.id, inv.number, inv.date, c.name cname FROM invoices inv JOIN customers c ON c.id=inv.customer_id ORDER BY inv.id DESC")
    return render_template("trace_index.html", batches=batches, invs=invs)


@bp.route("/trace/batch/<int:rid>")
@roles(*OFFICE)
def trace_batch(rid):
    try:
        t = S.trace_batch(g.db, rid)
    except BizError:
        abort(404)
    return render_template("trace_batch.html", t=t, findings=S.analyze_run(g.db, rid) if t["summary"] else [])


@bp.route("/trace/invoice/<int:iid>")
@roles(*OFFICE)
def trace_invoice(iid):
    try:
        t = S.trace_invoice(g.db, iid)
    except BizError:
        abort(404)
    return render_template("trace_invoice.html", t=t)


# ───────────── reports & AURA ─────────────
FINANCE = {"sales_register", "customer_sales", "product_sales", "gst_outward", "purchase_register", "collections", "outstanding", "ageing"}
TITLES = {"sales_register": "Sales register", "customer_sales": "Customer-wise sales", "product_sales": "Product-wise sales", "gst_outward": "GST outward supply summary",
          "purchase_register": "Purchase register", "stock_summary": "Stock summary", "stock_ledger": "Stock ledger", "stock_rollforward": "Stock roll-forward",
          "production_variance": "Production variance", "cost_per_packet": "Cost per packet", "collections": "Collections (receipts)",
          "outstanding": "Outstanding receivables", "ageing": "Customer ageing"}


def _allowed(key):
    return key not in FINANCE or g.user["role"] in ("admin", "owner", "auditor")


@bp.route("/reports")
@roles(*OFFICE)
def reports_index():
    return render_template("reports_index.html", items=[(k, t) for k, t in TITLES.items() if _allowed(k)])


@bp.route("/reports/<key>")
@roles(*OFFICE)
def report(key):
    if key not in RP.REPORTS or not _allowed(key):
        abort(404 if key not in RP.REPORTS else 403)
    a = {k: request.args.get(k, "") for k in ("date_from", "date_to", "item")}
    rep = RP.REPORTS[key](g.db, a)
    fmt = request.args.get("format", "html")
    if fmt == "csv":
        return Response(RP.to_csv(rep), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={key}.csv"})
    if fmt == "xlsx":
        return Response(RP.to_xlsx(rep), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={key}.xlsx"})
    if fmt == "pdf":
        return Response(RP.to_pdf(rep), mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename={key}.pdf"})
    items = [("m%d" % m["id"], m["name"]) for m in ALL(g.db, "SELECT * FROM materials ORDER BY name")] + [("p%d" % p["id"], p["name"]) for p in ALL(g.db, "SELECT * FROM products")]
    return render_template("report.html", rep=rep, key=key, a=a, items=items, cell=RP.cell_text)


@bp.route("/aura")
@roles(*OFFICE)
def aura_page():
    return render_template("aura.html", items=aura.insights(g.db), label=aura.LABEL)
