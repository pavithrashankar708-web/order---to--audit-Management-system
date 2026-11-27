"""Mandatory benchmark scenario (brief section 9): every expected result asserted."""
from helpers import DBCase, S
from app.core import to_milli, to_paise, fmt_bp


class Benchmark(DBCase):
    benchmark = True

    def test_inventory_closing_balances(self):
        self.assertEqual(self.bal("FLOUR"), to_milli("120"))      # 100 + 60 - 40
        self.assertEqual(self.bal("OIL"), to_milli("19.5"))       # 30 - 10.5
        self.assertEqual(self.bal("SPICE"), to_milli("3.8"))
        self.assertEqual(self.bal("POUCH"), to_milli("306"))      # 500 - 188 good - 6 damaged
        self.assertEqual(self.bal("CARTON"), to_milli("21"))      # 30 - 9
        self.assertEqual(self.fg(), 88)                           # 50 + 188 - 150

    def test_variances(self):
        s = S.run_summary(self.conn, self.ids["run"])
        self.assertEqual(s["yield_bp"], -600)
        self.assertEqual(fmt_bp(s["yield_bp"]), "-6.00%")
        oil = next(l for l in s["lines"] if l["name"] == "Cooking oil")
        self.assertEqual(oil["var_bp"], 500)
        self.assertTrue(oil["flag"] and oil["adverse"])
        flour = next(l for l in s["lines"] if l["name"].startswith("Flour"))
        self.assertEqual(flour["var_bp"], 0)
        self.assertTrue(s["yield_flag"])

    def test_pouch_arithmetic(self):
        s = S.run_summary(self.conn, self.ids["run"])
        pouch = next(l for l in s["lines"] if l["name"] == "Pouch 200 g")
        self.assertEqual(pouch["actual_milli"], to_milli("194"))   # 188 good + 6 damaged
        self.assertEqual(pouch["planned_milli"] - pouch["actual_milli"], to_milli("6"))  # 6 of 200 unused

    def test_invoice_gst_retailer_a(self):
        inv = self.conn.execute("SELECT * FROM invoices WHERE id=?", (self.ids["invoice"],)).fetchone()
        self.assertEqual(inv["interstate"], 0)
        self.assertEqual(inv["taxable_paise"], to_paise("7500"))
        self.assertEqual(inv["cgst_paise"], to_paise("187.50"))
        self.assertEqual(inv["sgst_paise"], to_paise("187.50"))
        self.assertEqual(inv["igst_paise"], 0)
        self.assertEqual(inv["total_paise"], to_paise("7875"))

    def test_outstanding(self):
        paid, out = S.invoice_balance(self.conn, self.ids["invoice"])
        self.assertEqual((paid, out), (to_paise("4000"), to_paise("3875")))

    def test_distributor_b_shortage(self):
        av = S.order_availability(self.conn, self.ids["order_b"])
        self.assertEqual(av[0]["available"], 88)
        self.assertEqual(av[0]["shortage"], 32)

    def test_production_cost(self):
        s = S.run_summary(self.conn, self.ids["run"])
        self.assertEqual(s["material_cost"], to_paise("4130"))
        self.assertEqual(s["total_cost"], to_paise("5930"))
        self.assertEqual(s["cost_per_packet"], 3154)               # ₹31.54 (5930 / 188 = 31.5425)

    def test_fifo_batch_allocation(self):
        rows = self.conn.execute("""SELECT r.batch_id, dl.qty FROM dispatch_lines dl
                                    JOIN production_runs r ON r.id=dl.run_id ORDER BY dl.id""").fetchall()
        self.assertEqual([(r["batch_id"].startswith("OPEN-"), r["qty"]) for r in rows], [(True, 50), (False, 100)])

    def test_loss_findings(self):
        kinds = {f["kind"]: f for f in S.analyze_run(self.conn, self.ids["run"])}
        self.assertEqual(set(kinds), {"excess_material", "yield_loss", "damaged_packaging"})
        self.assertEqual(kinds["excess_material"]["impact_paise"], 6500)    # 0.5 L x ₹130
        self.assertEqual(kinds["damaged_packaging"]["impact_paise"], 1500)  # 6 x ₹2.50
        self.assertEqual(kinds["yield_loss"]["impact_paise"], 12 * 2050)    # 12 pkts x ₹20.50 std material cost

    def test_traceability_chain_is_real(self):
        t = S.trace_batch(self.conn, self.ids["run"])
        self.assertTrue(any(i["material"].startswith("Flour") for i in t["issues"]))
        self.assertEqual(t["sales"][0]["invoice_no"].split("/")[0], "SMS")
        self.assertEqual((t["dispatched"], t["remaining"]), (100, 88))
        ti = S.trace_invoice(self.conn, self.ids["invoice"])
        self.assertEqual(len(ti["batches"]), 2)                     # opening batch + production batch


class OpeningStock(DBCase):
    def test_opening(self):
        self.assertEqual(self.bal("FLOUR"), to_milli("100"))
        self.assertEqual(self.bal("OIL"), to_milli("30"))
        self.assertEqual(self.bal("SPICE"), to_milli("5"))
        self.assertEqual(self.bal("POUCH"), to_milli("500"))
        self.assertEqual(self.bal("CARTON"), to_milli("30"))
        self.assertEqual(self.fg(), 50)

    def test_after_production_238(self):
        from helpers import date
        c = self.conn
        run = S.create_run(c, 1, 200, date.today())
        S.issue_to_run(c, run, {self.mat("FLOUR"): 40000, self.mat("OIL"): 10500, self.mat("SPICE"): 1200}, date.today())
        S.complete_run(c, run, 188, 6, date.today(), pm_actuals={self.mat("CARTON"): 9000})
        self.assertEqual(self.fg(), 238)


if __name__ == "__main__":
    import unittest; unittest.main()
