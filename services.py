"""Business logic. Every function takes an open sqlite3 connection and does NOT commit:
callers wrap multi-step postings in `db.atomic(conn)` so they succeed or fail as one transaction."""
import sqlite3
from datetime import date as _date, timedelta
from .core import (BizError, div_round, bp_diff, fmt_qty, fmt_bp, fmt_money, gst_split, line_cost)

COMPANY = dict(name="Sri Murugan Snacks", tagline="Taste of Tradition. Powered by Intelligence.",
               address="12, Demo Industrial Estate, Coimbatore, Tamil Nadu - 641 001",
               state="Tamil Nadu", gstin="33AAAAA0000A1Z5")
GSTIN_NOTE = "DEMO / DUMMY GSTINs - not real registrations"
THRESHOLD_BP = 300  # 3 %
RECEIPT_MOVES = ("PURCHASE", "PRODUCTION_RECEIPT")
ISSUE_MOVES = ("ISSUE_TO_PRODUCTION", "PACKAGING_USE", "SALE")


def iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()


def allrows(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def next_number(conn, name, prefix="", width=4):
    conn.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,0)", (name,))
    conn.execute("UPDATE counters SET value=value+1 WHERE name=?", (name,))
    v = one(conn, "SELECT value FROM counters WHERE name=?", name)[0]
    return f"{prefix}{v:0{width}d}"


def fiscal_year(d):
    d = _date.fromisoformat(iso(d))
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


# ───────────────────────────── stock ledger ─────────────────────────────
def ledger_post(conn, *, date, movement, qty, source_type, source_id, source_no, line_key,
                material_id=None, product_id=None, batch_ref="-", note="", user=None):
    if product_id is not None:
        kind, unit = "FG", "pkt"
    else:
        m = one(conn, "SELECT category, unit FROM materials WHERE id=?", material_id)
        if not m:
            raise BizError("Unknown material")
        kind, unit = m["category"], m["unit"]
    try:
        conn.execute(
            "INSERT INTO stock_ledger(date,item_kind,material_id,product_id,batch_ref,movement,qty,unit,"
            "source_type,source_id,source_no,line_key,note,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(date), kind, material_id, product_id, batch_ref, movement, qty, unit,
             source_type, source_id, source_no, str(line_key), note, user))
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise BizError(f"Duplicate posting blocked: {source_no} line {line_key} is already in the ledger")
        raise


def stock_balance(conn, material_id=None, product_id=None, as_of=None):
    col, val = ("material_id", material_id) if material_id is not None else ("product_id", product_id)
    sql, args = f"SELECT COALESCE(SUM(qty),0) FROM stock_ledger WHERE {col}=?", [val]
    if as_of:
        sql += " AND date<=?"
        args.append(iso(as_of))
    return conn.execute(sql, args).fetchone()[0]


def _material(conn, mid):
    m = one(conn, "SELECT * FROM materials WHERE id=?", mid)
    if not m:
        raise BizError("Unknown material")
    return m


# ───────────────────────────── opening stock & lots ─────────────────────────────
def set_opening_material(conn, material_id, qty_milli, date, user=None):
    m = _material(conn, material_id)
    lot_no = f"OPEN-{m['code']}"
    try:
        conn.execute("INSERT INTO material_lots(material_id,lot_no,source,received_date,qty_milli,rate_paise)"
                     " VALUES(?,?,?,?,?,?)", (material_id, lot_no, "OPENING", iso(date), qty_milli, m["last_cost_paise"]))
    except sqlite3.IntegrityError:
        raise BizError(f"Opening stock for {m['name']} already posted")
    ledger_post(conn, date=date, movement="OPENING", qty=qty_milli, source_type="OPENING", source_id=material_id,
                source_no=lot_no, line_key="OPEN", material_id=material_id, batch_ref=lot_no, user=user)


def set_opening_fg(conn, product_id, packets, date, user=None):
    p = one(conn, "SELECT * FROM products WHERE id=?", product_id)
    batch = f"OPEN-{p['sku']}"
    try:
        cur = conn.execute(
            "INSERT INTO production_runs(batch_id,kind,product_id,date,planned_output,actual_good,status)"
            " VALUES(?,?,?,?,?,?,?)", (batch, "OPENING", product_id, iso(date), packets, packets, "COMPLETED"))
    except sqlite3.IntegrityError:
        raise BizError("Opening finished stock already posted")
    ledger_post(conn, date=date, movement="OPENING", qty=packets, source_type="OPENING_FG", source_id=cur.lastrowid,
                source_no=batch, line_key="OPEN", product_id=product_id, batch_ref=batch, user=user)


def lot_rows(conn, material_id):
    return allrows(conn, "SELECT l.*, l.qty_milli - COALESCE((SELECT SUM(qty_milli) FROM lot_allocations a"
                         " WHERE a.lot_id=l.id),0) AS remaining FROM material_lots l WHERE material_id=?"
                         " ORDER BY received_date, id", material_id)


def allocate_lots(conn, material_id, qty, issue_line_id=None, adjustment_id=None):
    """FIFO: consume the oldest lots first. Raises (before writing) if stock is insufficient."""
    lots = lot_rows(conn, material_id)
    have = sum(l["remaining"] for l in lots)
    if have < qty:
        m = _material(conn, material_id)
        raise BizError(f"Insufficient stock of {m['name']}: need {fmt_qty(qty)} {m['unit']}, have {fmt_qty(have)}")
    need, out = qty, []
    for l in lots:
        if need <= 0:
            break
        take = min(need, l["remaining"])
        if take > 0:
            conn.execute("INSERT INTO lot_allocations(lot_id,qty_milli,issue_line_id,adjustment_id) VALUES(?,?,?,?)",
                         (l["id"], take, issue_line_id, adjustment_id))
            out.append((l["id"], take))
            need -= take
    return out


# ───────────────────────────── purchases ─────────────────────────────
def post_purchase(conn, supplier_id, date, lines, user=None, supplier_inv_no=None):
    """lines: [(material_id, qty_milli, rate_paise_per_unit)]"""
    lines = [l for l in lines if l[1] and l[1] > 0]
    if not lines:
        raise BizError("Add at least one purchase line with a quantity")
    if not one(conn, "SELECT 1 FROM suppliers WHERE id=? AND active=1", supplier_id):
        raise BizError("Select an active supplier")
    number = next_number(conn, "purchase", "PUR-")
    try:
        cur = conn.execute("INSERT INTO purchases(number,supplier_id,supplier_inv_no,date,created_by) VALUES(?,?,?,?,?)",
                           (number, supplier_id, supplier_inv_no or None, iso(date), user))
    except sqlite3.IntegrityError:
        raise BizError(f"Supplier invoice {supplier_inv_no} was already booked (duplicate purchase blocked)")
    pid = cur.lastrowid
    for i, (mid, qty, rate) in enumerate(lines, 1):
        m = _material(conn, mid)
        amount = line_cost(qty, rate)
        cur = conn.execute("INSERT INTO purchase_lines(purchase_id,material_id,qty_milli,rate_paise,gst_bp,amount_paise)"
                           " VALUES(?,?,?,?,?,?)", (pid, mid, qty, rate, m["gst_bp"], amount))
        lot_no = f"{number}-{i}"
        conn.execute("INSERT INTO material_lots(material_id,lot_no,source,purchase_line_id,received_date,qty_milli,rate_paise)"
                     " VALUES(?,?,?,?,?,?,?)", (mid, lot_no, "PURCHASE", cur.lastrowid, iso(date), qty, rate))
        conn.execute("UPDATE materials SET last_cost_paise=? WHERE id=?", (rate, mid))  # latest purchase price
        ledger_post(conn, date=date, movement="PURCHASE", qty=qty, source_type="PURCHASE", source_id=pid,
                    source_no=number, line_key=cur.lastrowid, material_id=mid, batch_ref=lot_no, user=user)
    return pid


# ───────────────────────────── production ─────────────────────────────
def create_run(conn, product_id, planned_output, date, user=None):
    p = one(conn, "SELECT * FROM products WHERE id=?", product_id)
    if not p:
        raise BizError("Unknown product")
    bom = allrows(conn, "SELECT * FROM bom_lines WHERE product_id=?", product_id)
    if not bom or p["std_batch_output"] <= 0:
        raise BizError("Define the BOM and standard batch output for this product first")
    if planned_output <= 0:
        raise BizError("Planned output must be positive")
    prefix = f"B{iso(date).replace('-', '')}-{p['sku']}-"
    seq = one(conn, "SELECT COUNT(*) FROM production_runs WHERE batch_id LIKE ?", prefix + "%")[0] + 1
    batch = f"{prefix}{seq:02d}"
    cur = conn.execute("INSERT INTO production_runs(batch_id,kind,product_id,date,planned_output,status,created_by)"
                       " VALUES(?,?,?,?,?,?,?)", (batch, "PRODUCTION", product_id, iso(date), planned_output, "PLANNED", user))
    rid = cur.lastrowid
    for b in bom:
        conn.execute("INSERT INTO run_lines(run_id,material_id,planned_milli) VALUES(?,?,?)",
                     (rid, b["material_id"], div_round(b["std_milli"] * planned_output, p["std_batch_output"])))
    return rid


def _run(conn, run_id):
    r = one(conn, "SELECT * FROM production_runs WHERE id=?", run_id)
    if not r or r["kind"] != "PRODUCTION":
        raise BizError("Production run not found")
    return r


def issue_to_run(conn, run_id, items, date, user=None, packaging=False):
    """items: {material_id: qty_milli}. FIFO lot allocation + ledger + run actuals, one issue document."""
    run = _run(conn, run_id)
    if run["status"] == "COMPLETED":
        raise BizError("Batch is already completed - no more issues allowed")
    items = {m: q for m, q in items.items() if q and q > 0}
    if not items:
        raise BizError("Nothing to issue - enter at least one quantity")
    number = next_number(conn, "issue", "ISS-")
    cur = conn.execute("INSERT INTO material_issues(number,run_id,date,kind,created_by) VALUES(?,?,?,?,?)",
                       (number, run_id, iso(date), "PACKAGING_ISSUE" if packaging else "PRODUCTION_ISSUE", user))
    issue_id = cur.lastrowid
    for mid, qty in items.items():
        rl = one(conn, "SELECT * FROM run_lines WHERE run_id=? AND material_id=?", run_id, mid)
        if not rl:
            raise BizError("Material is not part of this batch's BOM")
        m = _material(conn, mid)
        cost = line_cost(qty, m["last_cost_paise"])
        il = conn.execute("INSERT INTO issue_lines(issue_id,material_id,qty_milli,cost_paise) VALUES(?,?,?,?)",
                          (issue_id, mid, qty, cost)).lastrowid
        allocate_lots(conn, mid, qty, issue_line_id=il)
        ledger_post(conn, date=date, movement="PACKAGING_USE" if m["category"] == "PM" else "ISSUE_TO_PRODUCTION",
                    qty=-qty, source_type="ISSUE", source_id=issue_id, source_no=number, line_key=il,
                    material_id=mid, batch_ref=run["batch_id"], user=user)
        conn.execute("UPDATE run_lines SET actual_milli=actual_milli+?, cost_paise=cost_paise+? WHERE id=?",
                     (qty, cost, rl["id"]))
    conn.execute("UPDATE production_runs SET status='IN_PROGRESS' WHERE id=? AND status='PLANNED'", (run_id,))
    return issue_id


def complete_run(conn, run_id, good, damaged_packs, date, pm_actuals=None, wastage_notes="",
                 labour_paise=None, power_paise=None, user=None):
    """Record good output + damaged packs. Packaging is consumed here: the primary pouch = good + damaged
    (unless overridden); other packaging defaults to BOM standard scaled to good output (or explicit input)."""
    run = _run(conn, run_id)
    if run["status"] == "COMPLETED":
        raise BizError("Batch already completed (duplicate posting blocked)")
    if run["status"] == "PLANNED":
        raise BizError("Issue raw materials to this batch before recording output")
    if good < 0 or damaged_packs < 0:
        raise BizError("Quantities cannot be negative")
    pm_actuals = pm_actuals or {}
    pm = {}
    for rl in allrows(conn, "SELECT rl.*, m.is_primary_pack FROM run_lines rl JOIN materials m ON m.id=rl.material_id"
                            " WHERE rl.run_id=? AND m.category='PM'", run_id):
        mid = rl["material_id"]
        if mid in pm_actuals and pm_actuals[mid] is not None:
            pm[mid] = pm_actuals[mid]
        elif rl["is_primary_pack"]:
            pm[mid] = (good + damaged_packs) * 1000
        else:
            pm[mid] = div_round(rl["planned_milli"] * good, run["planned_output"])
    if any(pm.values()):
        issue_to_run(conn, run_id, pm, date, user, packaging=True)
    conn.execute("UPDATE production_runs SET actual_good=?, damaged_packs=?, wastage_notes=?, status='COMPLETED',"
                 " completed_date=? WHERE id=?", (good, damaged_packs, wastage_notes or None, iso(date), run_id))
    if good > 0:
        ledger_post(conn, date=date, movement="PRODUCTION_RECEIPT", qty=good, source_type="PRODUCTION",
                    source_id=run_id, source_no=run["batch_id"], line_key="FG", product_id=run["product_id"],
                    batch_ref=run["batch_id"], user=user)
    if labour_paise is not None or power_paise is not None:
        set_run_costs(conn, run_id, labour_paise or 0, power_paise or 0)


def set_run_costs(conn, run_id, labour_paise, power_paise):
    """Overheads are SET (not added), so re-submitting cannot double-count."""
    _run(conn, run_id)
    if labour_paise < 0 or power_paise < 0:
        raise BizError("Costs cannot be negative")
    conn.execute("UPDATE production_runs SET labour_paise=?, power_paise=? WHERE id=?", (labour_paise, power_paise, run_id))


def run_summary(conn, run_id):
    run = _run(conn, run_id)
    done = run["status"] == "COMPLETED"
    lines = []
    for r in allrows(conn, "SELECT rl.*, m.name, m.unit, m.category, m.last_cost_paise, m.is_primary_pack FROM run_lines rl"
                           " JOIN materials m ON m.id=rl.material_id WHERE rl.run_id=? ORDER BY m.category DESC, m.name", run_id):
        d = dict(r)
        d["var_bp"] = bp_diff(r["actual_milli"], r["planned_milli"]) if (r["actual_milli"] or done) else None
        d["flag"] = d["var_bp"] is not None and abs(d["var_bp"]) > THRESHOLD_BP
        d["adverse"] = d["flag"] and d["var_bp"] > 0
        lines.append(d)
    material_cost = sum(l["cost_paise"] for l in lines)
    total = material_cost + run["labour_paise"] + run["power_paise"]
    std_cost = sum(line_cost(l["planned_milli"], l["last_cost_paise"]) for l in lines)
    return dict(run=run, lines=lines, material_cost=material_cost, labour=run["labour_paise"], power=run["power_paise"],
                total_cost=total, completed=done,
                yield_bp=bp_diff(run["actual_good"], run["planned_output"]) if done else None,
                yield_flag=done and abs(bp_diff(run["actual_good"], run["planned_output"])) > THRESHOLD_BP,
                cost_per_packet=div_round(total, run["actual_good"]) if done and run["actual_good"] else None,
                std_material_cost_per_packet=div_round(std_cost, run["planned_output"]))


ADVICE = {"oil": "Check the oil measuring vessel / flow meter, frying temperature control and oil top-up practice.",
          "flour": "Verify the weighing scale calibration, dough water ratio and spillage during mixing.",
          "spice": "Check spice-mix weighing and batch-wise blending discipline."}


def analyze_run(conn, run_id):
    """Smart Production Loss Detection (rule-based). Returns findings with financial impact in paise."""
    s = run_summary(conn, run_id)
    run, out = s["run"], []
    if not s["completed"]:
        return out
    for l in s["lines"]:
        if l["category"] == "RM" and l["adverse"]:
            excess = l["actual_milli"] - l["planned_milli"]
            advice = next((v for k, v in ADVICE.items() if k in l["name"].lower()),
                          "Verify weighing and issue controls for this material.")
            out.append(dict(kind="excess_material", severity="warn", impact_paise=line_cost(excess, l["last_cost_paise"]),
                            title=f"Excess {l['name']}: {fmt_bp(l['var_bp'])}",
                            detail=f"Used {fmt_qty(l['actual_milli'])} {l['unit']} against plan {fmt_qty(l['planned_milli'])}; "
                                   f"excess {fmt_qty(excess)} {l['unit']}.", advice=advice))
    if s["yield_bp"] is not None and s["yield_bp"] < -THRESHOLD_BP:
        short = run["planned_output"] - run["actual_good"]
        out.append(dict(kind="yield_loss", severity="high", impact_paise=short * s["std_material_cost_per_packet"],
                        title=f"Yield loss: {fmt_bp(s['yield_bp'])}",
                        detail=f"{run['actual_good']} good packets vs {run['planned_output']} planned ({short} short). "
                               f"Impact valued at standard material cost per packet.",
                        advice="Check frying temperature and time, dough consistency, operator handling and broken/rejected pieces."))
    if run["damaged_packs"] > 0:
        pouch = next((l for l in s["lines"] if l["is_primary_pack"]), None)
        rate = pouch["last_cost_paise"] if pouch else 0
        ratio = div_round(run["damaged_packs"] * 10000, max(run["actual_good"], 1))
        out.append(dict(kind="damaged_packaging", severity="high" if ratio > THRESHOLD_BP else "warn",
                        impact_paise=run["damaged_packs"] * rate,
                        title=f"Damaged packaging: {run['damaged_packs']} pouches ({fmt_bp(ratio).lstrip('+')} of good output)",
                        detail="Pouches spoiled during packing were consumed but produced no saleable packet.",
                        advice="Check pouch sealing temperature/pressure, pouch alignment on the machine and operator handling."))
    return out


# ───────────────────────────── orders ─────────────────────────────
def price_for(product, customer):
    if customer["ctype"] == "distributor" and product["distributor_price_paise"] is not None:
        return product["distributor_price_paise"]
    return product["price_paise"]


def create_order(conn, customer_id, salesman_id, lines, date, notes=""):
    """lines: [(product_id, qty)] - duplicate products are merged; inactive products are rejected."""
    cust = one(conn, "SELECT * FROM customers WHERE id=? AND active=1", customer_id)
    if not cust:
        raise BizError("Select an active customer")
    merged = {}
    for pid, qty in lines:
        if qty and qty > 0:
            merged[pid] = merged.get(pid, 0) + qty
    if not merged:
        raise BizError("Add at least one product with a quantity")
    number = next_number(conn, "order", "SO-")
    oid = conn.execute("INSERT INTO orders(number,customer_id,salesman_id,date,status,notes) VALUES(?,?,?,?,?,?)",
                       (number, customer_id, salesman_id, iso(date), "SUBMITTED", notes or None)).lastrowid
    for pid, qty in merged.items():
        p = one(conn, "SELECT * FROM products WHERE id=?", pid)
        if not p or not p["active"]:
            raise BizError("Inactive or unknown product cannot be ordered")
        conn.execute("INSERT INTO order_lines(order_id,product_id,qty,price_paise,gst_bp,hsn) VALUES(?,?,?,?,?,?)",
                     (oid, pid, qty, price_for(p, cust), p["gst_bp"], p["hsn"]))
    return oid


def reserved_fg(conn, product_id, exclude_order=None):
    return one(conn, "SELECT COALESCE(SUM(ol.approved_qty),0) FROM order_lines ol JOIN orders o ON o.id=ol.order_id"
                     " WHERE ol.product_id=? AND o.status IN('APPROVED','INVOICED') AND o.id<>?",
               product_id, exclude_order or 0)[0]


def order_availability(conn, order_id):
    out = []
    o = one(conn, "SELECT * FROM orders WHERE id=?", order_id)
    for l in allrows(conn, "SELECT ol.*, p.name, p.sku FROM order_lines ol JOIN products p ON p.id=ol.product_id"
                           " WHERE order_id=? ORDER BY ol.id", order_id):
        stock = stock_balance(conn, product_id=l["product_id"])
        avail = stock - reserved_fg(conn, l["product_id"], order_id)
        d = dict(l)
        d.update(stock=stock, available=max(avail, 0), shortage=max(l["qty"] - max(avail, 0), 0))
        out.append(d)
    return out


def approve_order(conn, order_id, approved=None, user=None):
    """approved: {order_line_id: qty}. Default = min(ordered, available). Never exceeds available stock."""
    o = one(conn, "SELECT * FROM orders WHERE id=?", order_id)
    if not o or o["status"] != "SUBMITTED":
        raise BizError("Only SUBMITTED orders can be approved")
    approved, total = approved or {}, 0
    for l in order_availability(conn, order_id):
        q = approved.get(l["id"], min(l["qty"], l["available"]))
        if q < 0 or q > l["qty"]:
            raise BizError("Approved quantity must be between 0 and the ordered quantity")
        if q > l["available"]:
            raise BizError(f"Cannot approve {q} x {l['name']}: only {l['available']} available (shortage {q - l['available']})")
        conn.execute("UPDATE order_lines SET approved_qty=? WHERE id=?", (q, l["id"]))
        total += q
    if total == 0:
        raise BizError("No stock available to approve - reject or wait for production")
    conn.execute("UPDATE orders SET status='APPROVED', approved_by=? WHERE id=?", (user, order_id))


def reject_order(conn, order_id, reason, user=None):
    o = one(conn, "SELECT * FROM orders WHERE id=?", order_id)
    if not o or o["status"] != "SUBMITTED":
        raise BizError("Only SUBMITTED orders can be rejected")
    conn.execute("UPDATE orders SET status='REJECTED', reject_reason=?, approved_by=? WHERE id=?",
                 (reason or "Rejected by admin", user, order_id))


def create_invoice(conn, order_id, date, user=None):
    o = one(conn, "SELECT * FROM orders WHERE id=?", order_id)
    if not o or o["status"] != "APPROVED":
        raise BizError("Invoice can only be raised for an APPROVED order (one invoice per order)")
    c = one(conn, "SELECT * FROM customers WHERE id=?", o["customer_id"])
    interstate = int(c["state"].strip().lower() != COMPANY["state"].lower())
    number = next_number(conn, f"invoice-{fiscal_year(date)}", f"SMS/{fiscal_year(date)}/")
    iid = conn.execute("INSERT INTO invoices(number,order_id,customer_id,date,place_of_supply,interstate,taxable_paise,"
                       "cgst_paise,sgst_paise,igst_paise,total_paise,created_by) VALUES(?,?,?,?,?,?,0,0,0,0,0,?)",
                       (number, order_id, o["customer_id"], iso(date), c["state"], interstate, user)).lastrowid
    tot = [0, 0, 0, 0]
    for l in allrows(conn, "SELECT ol.*, p.name, p.pack_size FROM order_lines ol JOIN products p ON p.id=ol.product_id"
                           " WHERE order_id=? AND approved_qty>0 ORDER BY ol.id", order_id):
        taxable = l["approved_qty"] * l["price_paise"]
        cg, sg, ig = gst_split(taxable, l["gst_bp"], interstate)
        total = taxable + cg + sg + ig
        conn.execute("INSERT INTO invoice_lines(invoice_id,order_line_id,product_id,description,hsn,qty,rate_paise,gst_bp,"
                     "taxable_paise,cgst_paise,sgst_paise,igst_paise,total_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (iid, l["id"], l["product_id"], f"{l['name']} {l['pack_size']}", l["hsn"], l["approved_qty"],
                      l["price_paise"], l["gst_bp"], taxable, cg, sg, ig, total))
        tot = [tot[0] + taxable, tot[1] + cg, tot[2] + sg, tot[3] + ig]
    conn.execute("UPDATE invoices SET taxable_paise=?,cgst_paise=?,sgst_paise=?,igst_paise=?,total_paise=? WHERE id=?",
                 (*tot, sum(tot), iid))
    conn.execute("UPDATE orders SET status='INVOICED' WHERE id=?", (order_id,))
    return iid


def batch_rows(conn, product_id):
    return allrows(conn, "SELECT r.*, r.actual_good - COALESCE((SELECT SUM(qty) FROM dispatch_lines d WHERE d.run_id=r.id),0)"
                         " + COALESCE((SELECT SUM(qty) FROM stock_adjustments a WHERE a.run_id=r.id),0) AS remaining"
                         " FROM production_runs r WHERE product_id=? AND status='COMPLETED' ORDER BY date, id", product_id)


def dispatch_invoice(conn, invoice_id, date, vehicle="", user=None):
    """FIFO batch allocation: oldest completed batch with remaining stock is consumed first."""
    inv = one(conn, "SELECT * FROM invoices WHERE id=?", invoice_id)
    if not inv:
        raise BizError("Invoice not found")
    if one(conn, "SELECT 1 FROM dispatches WHERE invoice_id=?", invoice_id):
        raise BizError("This invoice is already dispatched (duplicate dispatch blocked)")
    number = next_number(conn, "dispatch", "DSP-")
    did = conn.execute("INSERT INTO dispatches(number,invoice_id,date,vehicle,created_by) VALUES(?,?,?,?,?)",
                       (number, invoice_id, iso(date), vehicle or None, user)).lastrowid
    for il in allrows(conn, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", invoice_id):
        need = il["qty"]
        for b in batch_rows(conn, il["product_id"]):
            if need <= 0:
                break
            take = min(need, b["remaining"])
            if take <= 0:
                continue
            dl = conn.execute("INSERT INTO dispatch_lines(dispatch_id,invoice_line_id,run_id,product_id,qty) VALUES(?,?,?,?,?)",
                              (did, il["id"], b["id"], il["product_id"], take)).lastrowid
            ledger_post(conn, date=date, movement="SALE", qty=-take, source_type="DISPATCH", source_id=did,
                        source_no=f"{number} / {inv['number']}", line_key=dl, product_id=il["product_id"],
                        batch_ref=b["batch_id"], user=user)
            need -= take
        if need > 0:
            raise BizError(f"Insufficient finished-goods stock for {il['description']}: short by {need} packets")
    conn.execute("UPDATE orders SET status='DISPATCHED' WHERE id=?", (inv["order_id"],))
    return did


# ───────────────────────────── payments ─────────────────────────────
def invoice_balance(conn, invoice_id):
    inv = one(conn, "SELECT total_paise FROM invoices WHERE id=?", invoice_id)
    paid = one(conn, "SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE invoice_id=?", invoice_id)[0]
    return paid, inv["total_paise"] - paid


def record_payment(conn, invoice_id, amount_paise, date, mode="Cash", reference="", user=None, idem_key=None):
    inv = one(conn, "SELECT * FROM invoices WHERE id=?", invoice_id)
    if not inv:
        raise BizError("Invoice not found")
    if amount_paise <= 0:
        raise BizError("Payment amount must be positive")
    _, out = invoice_balance(conn, invoice_id)
    if amount_paise > out:
        raise BizError(f"Payment exceeds outstanding balance of {fmt_money(out)}")
    receipt = next_number(conn, "receipt", "RCT-")
    try:
        return conn.execute("INSERT INTO payments(receipt_no,invoice_id,customer_id,date,amount_paise,mode,reference,idem_key,"
                            "created_by) VALUES(?,?,?,?,?,?,?,?,?)",
                            (receipt, invoice_id, inv["customer_id"], iso(date), amount_paise, mode, reference or None,
                             idem_key, user)).lastrowid
    except sqlite3.IntegrityError:
        raise BizError("Duplicate payment submission blocked")


# ───────────────────────────── adjustments ─────────────────────────────
def post_adjustment(conn, date, qty, reason, user=None, material_id=None, product_id=None, run_id=None):
    if not qty:
        raise BizError("Adjustment quantity cannot be zero")
    if not reason or not reason.strip():
        raise BizError("A reason is mandatory for stock adjustments")
    number = next_number(conn, "adjustment", "ADJ-")
    if product_id is not None:
        r = one(conn, "SELECT * FROM production_runs WHERE id=? AND product_id=? AND status='COMPLETED'", run_id, product_id)
        if not r:
            raise BizError("Select the finished-goods batch being adjusted")
        if qty < 0 and -qty > next(b["remaining"] for b in batch_rows(conn, product_id) if b["id"] == run_id):
            raise BizError("Adjustment exceeds the batch's remaining stock")
        aid = conn.execute("INSERT INTO stock_adjustments(number,date,product_id,run_id,qty,reason,created_by) VALUES(?,?,?,?,?,?,?)",
                           (number, iso(date), product_id, run_id, qty, reason, user)).lastrowid
        ledger_post(conn, date=date, movement="ADJUSTMENT", qty=qty, source_type="ADJUSTMENT", source_id=aid,
                    source_no=number, line_key="ADJ", product_id=product_id, batch_ref=r["batch_id"], note=reason, user=user)
        return aid
    m = _material(conn, material_id)
    aid = conn.execute("INSERT INTO stock_adjustments(number,date,material_id,qty,reason,created_by) VALUES(?,?,?,?,?,?)",
                       (number, iso(date), material_id, qty, reason, user)).lastrowid
    if qty > 0:
        conn.execute("INSERT INTO material_lots(material_id,lot_no,source,received_date,qty_milli,rate_paise) VALUES(?,?,?,?,?,?)",
                     (material_id, number, "ADJUSTMENT", iso(date), qty, m["last_cost_paise"]))
    else:
        allocate_lots(conn, material_id, -qty, adjustment_id=aid)
    ledger_post(conn, date=date, movement="ADJUSTMENT", qty=qty, source_type="ADJUSTMENT", source_id=aid,
                source_no=number, line_key="ADJ", material_id=material_id, batch_ref=number, note=reason, user=user)
    return aid


# ───────────────────────────── traceability ─────────────────────────────
def trace_batch(conn, run_id):
    run = one(conn, "SELECT r.*, p.name AS product, p.sku FROM production_runs r JOIN products p ON p.id=r.product_id WHERE r.id=?", run_id)
    if not run:
        raise BizError("Batch not found")
    issues = allrows(conn, """SELECT i.number AS issue_no, i.date AS issue_date, m.name AS material, m.unit, a.qty_milli,
        lot.lot_no, lot.source AS lot_source, p.number AS purchase_no, p.id AS purchase_id, s.name AS supplier,
        p.supplier_inv_no, lot.rate_paise
        FROM lot_allocations a JOIN issue_lines il ON il.id=a.issue_line_id JOIN material_issues i ON i.id=il.issue_id
        JOIN materials m ON m.id=il.material_id JOIN material_lots lot ON lot.id=a.lot_id
        LEFT JOIN purchase_lines pl ON pl.id=lot.purchase_line_id LEFT JOIN purchases p ON p.id=pl.purchase_id
        LEFT JOIN suppliers s ON s.id=p.supplier_id WHERE i.run_id=? ORDER BY i.id, il.id, a.id""", run_id)
    sales = allrows(conn, """SELECT dl.qty, d.number AS dispatch_no, d.date AS dispatch_date, d.vehicle, inv.id AS invoice_id,
        inv.number AS invoice_no, o.number AS order_no, o.id AS order_id, c.name AS customer, c.state
        FROM dispatch_lines dl JOIN dispatches d ON d.id=dl.dispatch_id JOIN invoice_lines il ON il.id=dl.invoice_line_id
        JOIN invoices inv ON inv.id=il.invoice_id JOIN orders o ON o.id=inv.order_id JOIN customers c ON c.id=inv.customer_id
        WHERE dl.run_id=? ORDER BY d.id, dl.id""", run_id)
    remaining = next((b["remaining"] for b in batch_rows(conn, run["product_id"]) if b["id"] == run_id), 0)
    summary = run_summary(conn, run_id) if run["kind"] == "PRODUCTION" else None
    return dict(run=run, issues=issues, sales=sales, remaining=remaining, summary=summary,
                dispatched=sum(s["qty"] for s in sales))


def trace_invoice(conn, invoice_id):
    inv = one(conn, """SELECT inv.*, o.number AS order_no, c.name AS customer, c.state FROM invoices inv
        JOIN orders o ON o.id=inv.order_id JOIN customers c ON c.id=inv.customer_id WHERE inv.id=?""", invoice_id)
    if not inv:
        raise BizError("Invoice not found")
    disp = one(conn, "SELECT * FROM dispatches WHERE invoice_id=?", invoice_id)
    allocs = allrows(conn, """SELECT dl.qty, dl.run_id, r.batch_id, il.description FROM dispatch_lines dl
        JOIN production_runs r ON r.id=dl.run_id JOIN invoice_lines il ON il.id=dl.invoice_line_id
        WHERE il.invoice_id=? ORDER BY dl.id""", invoice_id)
    batches = [trace_batch(conn, rid) for rid in dict.fromkeys(a["run_id"] for a in allocs)]
    return dict(invoice=inv, dispatch=disp, allocations=allocs, batches=batches)
