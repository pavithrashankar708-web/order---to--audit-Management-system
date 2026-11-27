"""Role permission + end-to-end UI tests through the Flask test client."""
import os, sys
from helpers import DBCase, S
from app import create_app

ALL_GET_OFFICE = ["/", "/orders", "/invoices", "/production", "/bom", "/purchases", "/stock", "/trace", "/reports", "/aura",
                  "/master/products", "/master/customers", "/master/suppliers", "/master/materials"]


class Web(DBCase):
    benchmark = True

    def client(self, user=None, csrf=False):
        app = create_app({"DATABASE": self.path, "TESTING": True, "CSRF": csrf, "SECRET_KEY": "t"})
        c = app.test_client()
        if user:
            r = c.post("/login", data={"username": user, "password": "demo123"})
            assert r.status_code == 302, r.data
        return c

    def code(self, c, path, method="get", **kw):
        return getattr(c, method)(path, follow_redirects=False, **kw).status_code

    def test_login_required_and_bad_password(self):
        c = self.client()
        r = c.get("/orders"); self.assertEqual(r.status_code, 302); self.assertIn("/login", r.headers["Location"])
        r = c.post("/login", data={"username": "admin", "password": "wrong"}); self.assertEqual(r.status_code, 200)
        self.assertIn(b"Invalid username", r.data)

    def test_csrf_enforced_when_enabled(self):
        c = self.client(csrf=True)
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "demo123"}).status_code, 400)

    def test_all_pages_render_for_office_roles(self):
        for u in ("admin", "supervisor", "owner", "auditor"):
            c = self.client(u)
            for p in ALL_GET_OFFICE + ["/trace/batch/%d" % self.ids["run"], "/production/%d" % self.ids["run"], "/bom/1"]:
                if u == "supervisor" and p in ("/orders", "/invoices"):   # supervisor has no sales/billing access
                    self.assertEqual(self.code(c, p), 403, (u, p)); continue
                self.assertEqual(self.code(c, p), 200, (u, p))
        c = self.client("admin")
        for p in ["/orders/%d" % self.ids["order_a"], "/orders/%d" % self.ids["order_b"], "/invoices/%d" % self.ids["invoice"],
                  "/trace/invoice/%d" % self.ids["invoice"], "/customers/%d" % self.ids["retailer"], "/orders/new", "/master/products/new", "/master/products/1/edit"]:
            self.assertEqual(self.code(c, p), 200, p)

    def test_salesman_restrictions(self):
        c = self.client("salesman_a")
        self.assertEqual(self.code(c, "/"), 302)
        for p in ("/orders", "/orders/new"):
            self.assertEqual(self.code(c, p), 200, p)
        for p in ("/stock", "/reports", "/production", "/purchases", "/invoices", "/trace", "/aura", "/bom", "/master/products", "/reports/stock_ledger"):
            self.assertEqual(self.code(c, p), 403, p)
        for p in ("/orders/%d/approve" % self.ids["order_a"], "/invoices/%d/pay" % self.ids["invoice"]):
            self.assertEqual(self.code(c, p, "post"), 403, p)

    def test_salesman_sees_only_own_orders(self):
        a, b = self.client("salesman_a"), self.client("salesman_b")
        self.assertIn(b"SO-0001", a.get("/orders").data); self.assertNotIn(b"SO-0002", a.get("/orders").data)
        self.assertEqual(self.code(a, "/orders/%d" % self.ids["order_b"]), 404)
        self.assertEqual(self.code(b, "/orders/%d" % self.ids["order_b"]), 200)

    def test_readonly_roles_cannot_post(self):
        for u in ("auditor", "owner"):
            c = self.client(u)
            for p in ("/orders/%d/approve" % self.ids["order_b"], "/master/products/1/toggle", "/production/%d/costs" % self.ids["run"],
                      "/invoices/%d/pay" % self.ids["invoice"], "/orders/new", "/stock/adjust", "/purchases/new"):
                self.assertEqual(self.code(c, p, "post"), 403, (u, p))
        self.assertEqual(S.invoice_balance(self.conn, self.ids["invoice"])[1], 387500)

    def test_supervisor_and_admin_boundaries(self):
        sup, adm = self.client("supervisor"), self.client("admin")
        self.assertEqual(self.code(sup, "/purchases/new"), 200); self.assertEqual(self.code(adm, "/purchases/new"), 403)
        self.assertEqual(self.code(sup, "/master/products/new"), 403); self.assertEqual(self.code(adm, "/master/products/new"), 200)
        self.assertEqual(self.code(sup, "/reports/sales_register"), 403); self.assertEqual(self.code(adm, "/reports/sales_register"), 200)
        self.assertEqual(self.code(sup, "/invoices"), 403)
        self.assertEqual(self.code(adm, "/production/%d/issue" % self.ids["run"], "post"), 403)
        self.assertEqual(self.code(sup, "/orders/%d/approve" % self.ids["order_b"], "post"), 403)

    def test_inactive_product_not_selectable(self):
        adm = self.client("admin")
        self.assertIn(b"Murukku", adm.get("/orders/new").data)
        adm.post("/master/products/1/toggle")
        self.assertNotIn(b"Murukku", adm.get("/orders/new").data)
        before = self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        adm.post("/orders/new", data={"customer_id": self.ids["retailer"], "product_id": ["1"], "qty": ["5"]})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], before)

    def test_new_product_in_settings_reaches_order_entry_and_invoice(self):
        adm, sup = self.client("admin"), self.client("supervisor")
        r = adm.post("/master/products/new", data={"sku": "MIX200", "name": "Mixture", "pack_size": "200 g", "price_paise": "40", "gst_bp": "12",
                                                    "hsn": "21069099", "std_batch_output": "100"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"Mixture", adm.get("/orders/new").data)
        pid = self.conn.execute("SELECT id FROM products WHERE sku='MIX200'").fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT gst_bp, price_paise FROM products WHERE id=?", (pid,)).fetchone()[:], (1200, 4000))

    def test_ui_order_to_dispatch_flow(self):
        adm = self.client("admin")
        r = adm.post("/orders/%d/approve" % self.ids["order_b"]); self.assertEqual(r.status_code, 302)
        r = adm.post("/orders/%d/invoice" % self.ids["order_b"]); self.assertEqual(r.status_code, 302)
        r = adm.post("/orders/%d/dispatch" % self.ids["order_b"], data={"vehicle": "KA-01-1234"}); self.assertEqual(r.status_code, 302)
        self.assertEqual(self.fg(), 0)
        again = adm.post("/orders/%d/dispatch" % self.ids["order_b"], follow_redirects=True)
        self.assertIn(b"already dispatched", again.data); self.assertEqual(self.fg(), 0)
        inv_b = self.conn.execute("SELECT id FROM invoices WHERE order_id=?", (self.ids["order_b"],)).fetchone()[0]
        self.assertEqual(self.code(adm, "/invoices/%d/pay" % inv_b, "post", data={"amount": "100", "idem": "z1"}), 302)
        again = adm.post("/invoices/%d/pay" % inv_b, data={"amount": "100", "idem": "z1"}, follow_redirects=True)
        self.assertIn(b"Duplicate payment", again.data)
        self.assertEqual(S.invoice_balance(self.conn, inv_b)[0], 10000)

    def test_ui_production_flow_and_duplicate_guard(self):
        sup = self.client("supervisor")
        sup.post("/purchases/new", data={"supplier_id": "1", "inv_no": "UI-1", "material_id": [str(self.mat("FLOUR"))], "qty": ["60"], "rate": ["45"]})
        self.assertEqual(self.bal("FLOUR"), 180000)
        sup.post("/production", data={"product_id": "1", "planned": "200"})
        rid = self.conn.execute("SELECT MAX(id) FROM production_runs").fetchone()[0]
        sup.post("/production/%d/issue" % rid, data={"q_%d" % self.mat("FLOUR"): "40", "q_%d" % self.mat("OIL"): "10", "q_%d" % self.mat("SPICE"): "1.2"})
        r = sup.post("/production/%d/complete" % rid, data={"good": "200", "damaged": "0", "labour": "1200", "power": "600"})
        self.assertEqual(r.status_code, 302)
        fg = self.fg()
        again = sup.post("/production/%d/complete" % rid, data={"good": "200", "damaged": "0"}, follow_redirects=True)
        self.assertIn(b"already completed", again.data); self.assertEqual(self.fg(), fg)

    def test_exports(self):
        c = self.client("auditor")
        r = c.get("/reports/sales_register?format=csv"); self.assertEqual(r.status_code, 200); self.assertIn(b"7500.00", r.data); self.assertIn("text/csv", r.content_type)
        self.assertEqual(c.get("/reports/stock_ledger?format=xlsx").data[:2], b"PK")
        self.assertEqual(c.get("/reports/production_variance?format=pdf").data[:4], b"%PDF")
        self.assertEqual(c.get("/invoices/%d/pdf" % self.ids["invoice"]).data[:4], b"%PDF")
        self.assertEqual(self.code(c, "/reports/nonexistent"), 404)
        for k in __import__("app.reports", fromlist=["x"]).REPORTS:
            self.assertEqual(self.code(c, "/reports/" + k), 200, k)


if __name__ == "__main__":
    import unittest; unittest.main()
