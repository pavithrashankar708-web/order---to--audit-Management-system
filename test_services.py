from datetime import date
import sqlite3
from helpers import DBCase, S
from app import reports as RP
from app.core import (BizError, to_paise, to_milli, fmt_money, fmt_qty, fmt_bp, gst_split, div_round, bp_diff)
from app.db import atomic

T = date.today()


class MoneyAndGst(DBCase):
    def test_parsing_and_formatting_have_no_float_drift(self):
        self.assertEqual(to_paise("0.1") + to_paise("0.2"), to_paise("0.3"))
        self.assertEqual(to_paise("1,234.565"), 123457)
        self.assertEqual(fmt_money(1234567890), "₹1,23,45,678.90")
        self.assertEqual(fmt_money(-5), "-₹0.05")
        self.assertEqual(fmt_qty(10500), "10.5")
        self.assertEqual(fmt_bp(-600), "-6.00%")
        with self.assertRaises(BizError):
            to_paise("abc")

    def test_rounding_half_away_from_zero(self):
        self.assertEqual(div_round(5, 2), 3)
        self.assertEqual(div_round(-5, 2), -3)
        self.assertEqual(bp_diff(188, 200), -600)

    def test_cgst_sgst_always_sum_to_tax(self):
        for taxable in (10010, 10030, 7500_00, 1, 99999):
            c, s_, i = gst_split(taxable, 500, False)
            self.assertEqual((c + s_, i), (div_round(taxable * 500, 10000), 0))
        self.assertEqual(gst_split(10010, 500, False), (250, 251, 0))   # odd paise goes to SGST
        self.assertEqual(gst_split(750000, 500, True), (0, 0, 37500))

    def test_interstate_invoice_uses_igst(self):
        self.ids = __import__("app.seed", fromlist=["x"]).run_benchmark(self.conn)
        with atomic(self.conn):
            S.approve_order(self.conn, self.ids["order_b"])           # 88 available
            iid = S.create_invoice(self.conn, self.ids["order_b"], T)
        inv = self.conn.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
        self.assertEqual((inv["interstate"], inv["cgst_paise"], inv["sgst_paise"], inv["igst_paise"]), (1, 0, 0, 22000))
        self.assertEqual((inv["taxable_paise"], inv["total_paise"]), (440000, 462000))


class Prod(DBCase):
    def run_batch(self, good=188, damaged=6, oil="10.5", cartons="9", output=200):
        c = self.conn
        with atomic(c):
            r = S.create_run(c, 1, output, T)
            S.issue_to_run(c, r, {self.mat("FLOUR"): 40000, self.mat("OIL"): to_milli(oil), self.mat("SPICE"): 1200}, T)
            S.complete_run(c, r, good, damaged, T, pm_actuals={self.mat("CARTON"): to_milli(cartons)})
        return r

    def test_three_percent_threshold(self):
        r = self.run_batch(oil="10.3")                       # exactly +3.00% -> not flagged
        oil = next(l for l in S.run_summary(self.conn, r)["lines"] if l["name"] == "Cooking oil")
        self.assertEqual(oil["var_bp"], 300); self.assertFalse(oil["flag"])
        r2 = self.run_batch(oil="10.31")                     # +3.10% -> flagged
        oil2 = next(l for l in S.run_summary(self.conn, r2)["lines"] if l["name"] == "Cooking oil")
        self.assertTrue(oil2["flag"] and oil2["adverse"])

    def test_clean_batch_has_no_findings(self):
        r = self.run_batch(good=200, damaged=0, oil="10", cartons="10")
        self.assertEqual(S.analyze_run(self.conn, r), [])

    def test_batch_ids_are_unique(self):
        a, b = self.run_batch(), self.run_batch()
        ids = [x["batch_id"] for x in self.conn.execute("SELECT batch_id FROM production_runs WHERE kind='PRODUCTION'")]
        self.assertEqual(len(ids), len(set(ids)) ); self.assertTrue(ids[0].endswith("-01") and ids[1].endswith("-02"))

    def test_complete_requires_issue_and_blocks_double_complete(self):
        with atomic(self.conn):
            r = S.create_run(self.conn, 1, 200, T)
        with self.assertRaises(BizError):
            S.complete_run(self.conn, r, 100, 0, T)
        r2 = self.run_batch()
        with self.assertRaises(BizError):
            S.complete_run(self.conn, r2, 100, 0, T)
        self.assertEqual(self.fg(), 50 + 188)                 # unchanged by the blocked attempt

    def test_overhead_costs_are_set_not_added(self):
        r = self.run_batch()
        for _ in range(3):
            S.set_run_costs(self.conn, r, 120000, 60000)
        s = S.run_summary(self.conn, r)
        self.assertEqual((s["labour"], s["power"], s["total_cost"]), (120000, 60000, 593000))


class Inventory(DBCase):
    def test_insufficient_stock_blocked_and_whole_posting_rolled_back(self):
        c = self.conn
        with atomic(c):
            r = S.create_run(c, 1, 200, T)
        before = (self.bal("FLOUR"), c.execute("SELECT COUNT(*) FROM material_issues").fetchone()[0])
        with self.assertRaises(BizError):
            with atomic(c):
                S.issue_to_run(c, r, {self.mat("FLOUR"): 40000, self.mat("OIL"): to_milli("1000")}, T)   # oil impossible
        after = (self.bal("FLOUR"), c.execute("SELECT COUNT(*) FROM material_issues").fetchone()[0])
        self.assertEqual(before, after)                         # flour issue did NOT survive
        self.assertEqual(c.execute("SELECT SUM(actual_milli) FROM run_lines").fetchone()[0], 0)

    def test_ledger_roll_forward_and_lots_reconcile_after_benchmark(self):
        from app.seed import run_benchmark
        run_benchmark(self.conn)
        S.post_adjustment(self.conn, T, to_milli("-2"), "spillage", None, material_id=self.mat("FLOUR"))
        S.post_adjustment(self.conn, T, to_milli("1"), "count gain", None, material_id=self.mat("OIL"))
        self.conn.commit()
        rep = RP.stock_rollforward(self.conn, {})
        self.assertTrue(all(r[-1] == "OK" for r in rep["rows"]))
        for m in self.conn.execute("SELECT id FROM materials"):
            lots = sum(l["remaining"] for l in S.lot_rows(self.conn, m["id"]))
            self.assertEqual(lots, S.stock_balance(self.conn, material_id=m["id"]))
        pid = 1
        self.assertEqual(sum(b["remaining"] for b in S.batch_rows(self.conn, pid)), S.stock_balance(self.conn, product_id=pid))
        self.assertEqual(self.bal("FLOUR"), to_milli("118"))

    def test_adjustment_needs_reason(self):
        with self.assertRaises(BizError):
            S.post_adjustment(self.conn, T, 1000, "  ", None, material_id=self.mat("FLOUR"))

    def test_every_ledger_row_has_source_document(self):
        from app.seed import run_benchmark
        run_benchmark(self.conn)
        bad = self.conn.execute("SELECT COUNT(*) FROM stock_ledger WHERE source_no='' OR batch_ref='' OR unit='' OR date=''").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_duplicate_ledger_posting_blocked(self):
        S.ledger_post(self.conn, date=T, movement="ADJUSTMENT", qty=1, source_type="X", source_id=1, source_no="X1", line_key="1", material_id=1)
        with self.assertRaises(BizError):
            S.ledger_post(self.conn, date=T, movement="ADJUSTMENT", qty=1, source_type="X", source_id=1, source_no="X1", line_key="1", material_id=1)

    def test_duplicate_supplier_invoice_blocked(self):
        with atomic(self.conn):
            S.post_purchase(self.conn, 1, T, [(self.mat("FLOUR"), 1000, 4500)], None, "INV-9")
        with self.assertRaises(BizError):
            with atomic(self.conn):
                S.post_purchase(self.conn, 1, T, [(self.mat("FLOUR"), 1000, 4500)], None, "INV-9")
        self.assertEqual(self.bal("FLOUR"), to_milli("101"))

    def test_purchase_updates_latest_cost_and_fifo_lots(self):
        with atomic(self.conn):
            S.post_purchase(self.conn, 1, T, [(self.mat("FLOUR"), to_milli("60"), to_paise("48"))])
        self.assertEqual(self.conn.execute("SELECT last_cost_paise FROM materials WHERE code='FLOUR'").fetchone()[0], 4800)
        with atomic(self.conn):
            r = S.create_run(self.conn, 1, 200, T)
            S.issue_to_run(self.conn, r, {self.mat("FLOUR"): to_milli("130")}, T)    # 100 opening + 30 from purchase
        al = self.conn.execute("SELECT l.lot_no, a.qty_milli FROM lot_allocations a JOIN material_lots l ON l.id=a.lot_id ORDER BY a.id").fetchall()
        self.assertEqual([(x[0].startswith("OPEN"), x[1]) for x in al], [(True, 100000), (False, 30000)])


class OrdersAndDispatch(DBCase):
    benchmark = True

    def test_approval_cannot_exceed_available_stock(self):
        line = self.conn.execute("SELECT id FROM order_lines WHERE order_id=?", (self.ids["order_b"],)).fetchone()[0]
        with self.assertRaises(BizError):
            S.approve_order(self.conn, self.ids["order_b"], {line: 120})
        with atomic(self.conn):
            S.approve_order(self.conn, self.ids["order_b"])
        self.assertEqual(self.conn.execute("SELECT approved_qty FROM order_lines WHERE id=?", (line,)).fetchone()[0], 88)

    def test_reserved_stock_is_not_double_sold(self):
        with atomic(self.conn):
            S.approve_order(self.conn, self.ids["order_b"])
            o3 = S.create_order(self.conn, self.ids["retailer"], 1, [(self.ids["product"], 10)], T)
        self.assertEqual(S.order_availability(self.conn, o3)[0]["available"], 0)
        with self.assertRaises(BizError):
            S.approve_order(self.conn, o3)

    def test_one_invoice_and_one_dispatch_per_order(self):
        with self.assertRaises(BizError):
            S.create_invoice(self.conn, self.ids["order_a"], T)      # already INVOICED/DISPATCHED
        with self.assertRaises(BizError):
            S.dispatch_invoice(self.conn, self.ids["invoice"], T)
        self.assertEqual(self.fg(), 88)

    def test_cannot_invoice_unapproved_order(self):
        with self.assertRaises(BizError):
            S.create_invoice(self.conn, self.ids["order_b"], T)

    def test_inactive_product_rejected_in_service(self):
        self.conn.execute("UPDATE products SET active=0"); self.conn.commit()
        with self.assertRaises(BizError):
            S.create_order(self.conn, self.ids["retailer"], 1, [(self.ids["product"], 5)], T)

    def test_reject_flow(self):
        with atomic(self.conn):
            S.reject_order(self.conn, self.ids["order_b"], "Credit hold")
        self.assertEqual(self.conn.execute("SELECT status FROM orders WHERE id=?", (self.ids["order_b"],)).fetchone()[0], "REJECTED")

    def test_payment_rules(self):
        inv = self.ids["invoice"]
        with self.assertRaises(BizError): S.record_payment(self.conn, inv, 0, T)
        with self.assertRaises(BizError): S.record_payment(self.conn, inv, to_paise("3875.01"), T)   # exceeds outstanding
        with self.assertRaises(BizError): S.record_payment(self.conn, inv, 100, T, idem_key="bench-pay-1")   # replayed submission
        with atomic(self.conn):
            S.record_payment(self.conn, inv, to_paise("3875"), T, idem_key="k2")
        self.assertEqual(S.invoice_balance(self.conn, inv)[1], 0)

    def test_fifo_across_three_batches(self):
        c = self.conn
        with atomic(c):
            S.post_purchase(c, 1, T, [(self.mat("FLOUR"), to_milli("80"), 4500), (self.mat("OIL"), to_milli("20"), 13000),
                                      (self.mat("POUCH"), to_milli("400"), 250)])
            r2 = S.create_run(c, 1, 200, T)
            S.issue_to_run(c, r2, {self.mat("FLOUR"): 40000, self.mat("OIL"): 10000, self.mat("SPICE"): 1200}, T)
            S.complete_run(c, r2, 100, 0, T)
            o = S.create_order(c, self.ids["retailer"], 1, [(self.ids["product"], 150)], T)
            S.approve_order(c, o, {})
        # 88 remaining of batch 1 first, then 62 from batch 2; but approval was capped by availability (88+100=188)
        with atomic(c):
            inv = S.create_invoice(c, o, T); S.dispatch_invoice(c, inv, T)
        rows = c.execute("SELECT r.batch_id, dl.qty FROM dispatch_lines dl JOIN production_runs r ON r.id=dl.run_id WHERE dl.dispatch_id="
                         "(SELECT id FROM dispatches WHERE invoice_id=?) ORDER BY dl.id", (inv,)).fetchall()
        self.assertEqual([r["qty"] for r in rows], [88, 62])
        self.assertNotEqual(rows[0]["batch_id"], rows[1]["batch_id"])
        self.assertEqual(self.fg(), 38)          # 88 + 100 - 150

    def test_customer_ageing_and_outstanding_reports(self):
        rep = RP.ageing(self.conn, {})
        self.assertEqual(rep["rows"][0][1], to_paise("3875"))         # today's invoice sits in 0-30 bucket
        self.assertEqual(RP.outstanding(self.conn, {})["rows"][0][-1], to_paise("3875"))

    def test_reports_numbers(self):
        self.assertEqual(RP.gst_outward(self.conn, {})["rows"][0][3:], [750000, 18750, 18750, 0, 787500])
        self.assertEqual(RP.product_sales(self.conn, {})["rows"][0][2], 150)
        self.assertEqual(RP.cost_per_packet(self.conn, {})["rows"][0][6], 3154)


if __name__ == "__main__":
    import unittest; unittest.main()
