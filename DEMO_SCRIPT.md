# Demo walkthrough (about 10 minutes)

Start from a fresh DB: `python manage.py reset`, then `python run.py`. All passwords: `demo123`.
(`tests/test_demo_walkthrough.py` replays exactly these steps through the web forms and checks the numbers.)

| # | Sign in as | Do this | You should see |
|---|---|---|---|
| 0 | `owner` | Dashboard, Stock | Opening stock: flour 100 kg, oil 30 L, spice 5 kg, pouches 500, cartons 30, FG 50 |
| 1 | `supervisor` | Purchases → New: *Kaveri Flour Mills*, inv `DEMO-INV-001`, Flour 60 @ 45 | Flour 160 kg; new lot `PUR-0001-1`. Re-posting the same invoice no. is refused |
| 2 | `supervisor` | Production → plan Murukku, 200 packets | Batch `B<date>-MUR200-01` (PLANNED) |
| 3 | `supervisor` | Open batch → *Issue raw materials*: flour 40, oil 10.5, spice 1.2 | Oil shows **+5.00 %** (flagged) |
| 4-5 | `supervisor` | *Record output*: good 188, damaged 6, Carton used 9 | FG **238**; pouches **306**; yield **−6.00 %**; loss findings with ₹ impact and recommendations |
| 6 | `salesman_a` (phone-width window) | New order: Retailer A, Murukku × 150 | Order `SO-0001`, status SUBMITTED |
| 7 | `admin` | Open SO-0001 → Approve → Generate invoice → Dispatch | Taxable ₹7,500; CGST ₹187.50; SGST ₹187.50; total **₹7,875**; PDF button; FG **88** |
| 8 | `salesman_b` | New order: Distributor B × 120 | `SO-0002`; admin view shows available 88, **shortage 32** |
| 9 | `admin` | Invoices → open the invoice → record payment ₹4,000 | Outstanding **₹3,875** |
| 10 | `supervisor` | Batch → Overhead costs: labour 1200, gas/power 600 | Total **₹5,930**; cost/packet **₹31.54** |
| ✔ | `auditor` | Traceability → open the batch | Materials (lot → purchase/opening), planned vs actual, output & wastage, FG → SO-0001 → invoice → dispatch (100 pkt) |
| ✔ | `auditor` | Traceability → open the invoice | Allocation: 50 from `OPEN-MUR200` + 100 from the new batch |
| ✔ | `auditor` | Reports → Stock roll-forward; Stock ledger; export CSV/Excel/PDF | Every row "Reconciles: OK"; ledger rows show source documents |
| ✔ | `owner` | Dashboard, AURA | Low pouches/cartons, yield loss, excess oil, damaged packaging, pending order, unpaid invoice (labelled rule-based) |
| ✔ | `salesman_a` | Try `/stock` or `/reports` | 403 - role restriction |
| ✔ | `admin` | Settings → Products: add a product / deactivate Murukku | New product appears in order entry; inactive one disappears |

Shortcut: `python manage.py reset --benchmark` loads all of the above at once.
