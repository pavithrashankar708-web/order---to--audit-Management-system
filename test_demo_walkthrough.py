"""Replays DEMO_SCRIPT.md purely through the web forms (as each role) and checks the benchmark numbers."""
from helpers import DBCase, S
from app import create_app
from app.core import to_milli, to_paise


class Walkthrough(DBCase):
    def login(self, user):
        c = create_app({"DATABASE": self.path, "TESTING": True, "CSRF": False, "SECRET_KEY": "t"}).test_client()
        assert c.post("/login", data={"username": user, "password": "demo123"}).status_code == 302
        return c

    def test_ten_benchmark_steps_through_the_ui(self):
        sup, adm, sa, sb = self.login("supervisor"), self.login("admin"), self.login("salesman_a"), self.login("salesman_b")
        m = self.mat
        ret = self.conn.execute("SELECT id FROM customers WHERE ctype='retailer'").fetchone()[0]
        dist = self.conn.execute("SELECT id FROM customers WHERE ctype='distributor'").fetchone()[0]
        # 1 purchase
        sup.post("/purchases/new", data={"supplier_id": "1", "inv_no": "DEMO-INV-001", "material_id": [str(m("FLOUR"))], "qty": ["60"], "rate": ["45"]})
        # 2-5 production
        sup.post("/production", data={"product_id": "1", "planned": "200"})
        rid = self.conn.execute("SELECT MAX(id) FROM production_runs").fetchone()[0]
        sup.post(f"/production/{rid}/issue", data={f"q_{m('FLOUR')}": "40", f"q_{m('OIL')}": "10.5", f"q_{m('SPICE')}": "1.2"})
        sup.post(f"/production/{rid}/complete", data={"good": "188", "damaged": "6", f"q_{m('CARTON')}": "9", "notes": "6 pouches damaged"})
        self.assertEqual(self.fg(), 238)
        # 6-7 order, approve, invoice, dispatch
        sa.post("/orders/new", data={"customer_id": str(ret), "product_id": ["1"], "qty": ["150"]})
        oa = self.conn.execute("SELECT id FROM orders WHERE customer_id=?", (ret,)).fetchone()[0]
        adm.post(f"/orders/{oa}/approve"); adm.post(f"/orders/{oa}/invoice"); adm.post(f"/orders/{oa}/dispatch", data={"vehicle": "TN-01"})
        self.assertEqual(self.fg(), 88)
        # 8 distributor order -> shortage 32
        sb.post("/orders/new", data={"customer_id": str(dist), "product_id": ["1"], "qty": ["120"]})
        ob = self.conn.execute("SELECT id FROM orders WHERE customer_id=?", (dist,)).fetchone()[0]
        self.assertEqual(S.order_availability(self.conn, ob)[0]["shortage"], 32)
        # 9 payment, 10 overheads
        inv = self.conn.execute("SELECT * FROM invoices WHERE order_id=?", (oa,)).fetchone()
        adm.post(f"/invoices/{inv['id']}/pay", data={"amount": "4000", "mode": "UPI"})
        sup.post(f"/production/{rid}/costs", data={"labour": "1200", "power": "600"})
        self.assertEqual((inv["taxable_paise"], inv["cgst_paise"], inv["sgst_paise"], inv["total_paise"]), (750000, 18750, 18750, 787500))
        self.assertEqual(S.invoice_balance(self.conn, inv["id"]), (400000, 387500))
        s = S.run_summary(self.conn, rid)
        self.assertEqual((s["yield_bp"], s["total_cost"], s["cost_per_packet"]), (-600, 593000, 3154))
        self.assertEqual([self.bal(c) for c in ("FLOUR", "OIL", "POUCH", "CARTON")], [to_milli("120"), to_milli("19.5"), to_milli("306"), to_milli("21")])
        # the UI-driven database matches the scripted benchmark database
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stock_ledger").fetchone()[0], 15)
