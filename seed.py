"""Seed / reset helpers. `load_masters` = users + masters + BOM + opening stock (walkthrough mode).
`run_benchmark` replays the 10 mandatory benchmark transactions through the same service functions the UI uses."""
from datetime import date
from werkzeug.security import generate_password_hash
from . import services as S
from .core import to_paise, to_milli
from .db import atomic, connect, init_schema
import os

DEMO_PASSWORD = "demo123"
USERS = [("salesman_a", "Salesman A (Arun)", "salesman"), ("salesman_b", "Salesman B (Bala)", "salesman"),
         ("admin", "Factory Admin (Meena)", "admin"), ("supervisor", "Production Supervisor (Karthik)", "supervisor"),
         ("auditor", "Accountant / Auditor (Lakshmi)", "auditor"), ("owner", "Owner (Murugan)", "owner")]
# code, name, category, unit, last cost (₹/unit), reorder level, primary pack, BOM qty, opening qty
MATERIALS = [("FLOUR", "Flour (rice/urad blend)", "RM", "kg", "45", "50", 0, "40", "100"),
             ("OIL", "Cooking oil", "RM", "L", "130", "15", 0, "10", "30"),
             ("SPICE", "Salt & spice mix", "RM", "kg", "250", "2", 0, "1.2", "5"),
             ("POUCH", "Pouch 200 g", "PM", "pcs", "2.50", "400", 1, "200", "500"),
             ("CARTON", "Carton (20 packs)", "PM", "pcs", "20", "25", 0, "10", "30")]
# NOTE: the brief gives only the flour price (₹45/kg). The other unit costs above are ASSUMED so that the
# benchmark's stated ₹5,930 production cost reconciles exactly (see README / DESIGN_NOTE).


def load_masters(conn, today=None):
    today = today or date.today()
    with atomic(conn):
        pw = generate_password_hash(DEMO_PASSWORD)
        for u, n, r in USERS:
            conn.execute("INSERT INTO users(username,name,role,password_hash) VALUES(?,?,?,?)", (u, n, r, pw))
        pid = conn.execute("INSERT INTO products(sku,name,pack_size,price_paise,gst_bp,hsn,std_batch_output)"
                           " VALUES('MUR200','Murukku','200 g',?,500,'19059030',200)", (to_paise("50"),)).lastrowid
        conn.execute("INSERT INTO customers(name,ctype,state,gstin,address) VALUES(?,?,?,?,?)",
                     ("Retailer A (Sri Lakshmi Stores)", "retailer", "Tamil Nadu", "33BBBBB1111B1Z5", "Madurai, Tamil Nadu"))
        conn.execute("INSERT INTO customers(name,ctype,state,gstin,address) VALUES(?,?,?,?,?)",
                     ("Distributor B (Karnataka Snacks Co.)", "distributor", "Karnataka", "29CCCCC2222C1Z5", "Bengaluru, Karnataka"))
        conn.execute("INSERT INTO suppliers(name,state,gstin) VALUES('Kaveri Flour Mills','Tamil Nadu','33DDDDD3333D1Z5')")
        conn.execute("INSERT INTO suppliers(name,state,gstin) VALUES('Annam Oils & Packaging','Tamil Nadu','33EEEEE4444E1Z5')")
        for code, name, cat, unit, cost, reorder, prim, bom, opening in MATERIALS:
            mid = conn.execute("INSERT INTO materials(code,name,category,unit,last_cost_paise,reorder_milli,is_primary_pack)"
                               " VALUES(?,?,?,?,?,?,?)", (code, name, cat, unit, to_paise(cost), to_milli(reorder), prim)).lastrowid
            conn.execute("INSERT INTO bom_lines(product_id,material_id,std_milli) VALUES(?,?,?)", (pid, mid, to_milli(bom)))
            S.set_opening_material(conn, mid, to_milli(opening), today)
        S.set_opening_fg(conn, pid, 50, today)


def _id(conn, table, col, val):
    return conn.execute(f"SELECT id FROM {table} WHERE {col}=?", (val,)).fetchone()[0]


def run_benchmark(conn, today=None):
    """Replays benchmark steps 1-10. Returns the ids needed by tests/demo."""
    today = today or date.today()
    uid = lambda n: _id(conn, "users", "username", n)
    mat = lambda c: _id(conn, "materials", "code", c)
    pid = _id(conn, "products", "sku", "MUR200")
    ret = conn.execute("SELECT id FROM customers WHERE ctype='retailer'").fetchone()[0]
    dist = conn.execute("SELECT id FROM customers WHERE ctype='distributor'").fetchone()[0]
    sup = conn.execute("SELECT id FROM suppliers ORDER BY id").fetchone()[0]
    out = {}
    with atomic(conn):
        S.post_purchase(conn, sup, today, [(mat("FLOUR"), to_milli("60"), to_paise("45"))], uid("supervisor"), "DEMO-INV-001")   # 1
        run = S.create_run(conn, pid, 200, today, uid("supervisor"))                                                              # 2
        S.issue_to_run(conn, run, {mat("FLOUR"): to_milli("40"), mat("OIL"): to_milli("10.5"),
                                   mat("SPICE"): to_milli("1.2")}, today, uid("supervisor"))                                       # 3
        S.complete_run(conn, run, 188, 6, today, pm_actuals={mat("CARTON"): to_milli("9")},                                       # 4, 5
                       wastage_notes="6 pouches damaged at sealing", user=uid("supervisor"))
        o1 = S.create_order(conn, ret, uid("salesman_a"), [(pid, 150)], today)                                                    # 6
        S.approve_order(conn, o1, user=uid("admin"))                                                                              # 7
        inv = S.create_invoice(conn, o1, today, uid("admin"))
        S.dispatch_invoice(conn, inv, today, "TN-38-AB-1234", uid("admin"))
        o2 = S.create_order(conn, dist, uid("salesman_b"), [(pid, 120)], today)                                                   # 8
        S.record_payment(conn, inv, to_paise("4000"), today, "UPI", "DEMO-UTR-001", uid("admin"), "bench-pay-1")                  # 9
        S.set_run_costs(conn, run, to_paise("1200"), to_paise("600"))                                                              # 10
    out.update(run=run, order_a=o1, order_b=o2, invoice=inv, product=pid, retailer=ret, distributor=dist)
    return out


def reset_database(path, benchmark=False, today=None):
    if os.path.exists(path):
        os.remove(path)
    conn = connect(path)
    init_schema(conn)
    load_masters(conn, today)
    ids = run_benchmark(conn, today) if benchmark else {}
    conn.close()
    return ids
