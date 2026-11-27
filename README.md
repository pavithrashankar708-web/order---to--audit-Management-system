# Sri Murugan Snacks - Order-to-Audit Management System (prototype)

*Taste of Tradition. Powered by Intelligence.*
Prototype for the Centre for Digital Infrastructure, NIT Tiruchirappalli internship challenge.
Flask + SQLite, integer-paise money, source-linked stock ledger, batch-to-sale traceability.

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                       # Flask, ReportLab, openpyxl
python run.py                                         # http://127.0.0.1:5000
```
The first start creates `instance/sri_murugan.db` with users, masters, BOM and **opening stock only**
(so you can perform the benchmark yourself - see `DEMO_SCRIPT.md`).

| Command | What it does |
|---|---|
| `python manage.py reset` | Fresh DB: masters + opening stock |
| `python manage.py reset --benchmark` | Fresh DB, then replays all 10 benchmark transactions |
| `python manage.py test` | Runs the automated tests (`python -m unittest discover -s tests -v`) |

## Demo credentials (all passwords: `demo123`)
| Username | Role | Can do |
|---|---|---|
| `salesman_a`, `salesman_b` | Salesman | Mobile order entry; own orders and status only |
| `admin` | Factory Admin | Approve/reject (with stock check), invoice, dispatch, payments; Product & Customer settings |
| `supervisor` | Production Supervisor | Purchases, BOM, material issues, production runs, stock adjustments; Supplier & Material settings |
| `auditor` | Accountant/Auditor | Read-only: all reports, stock ledger, traceability, exports |
| `owner` | Owner | Read-only: dashboard, AURA alerts, cost per packet, receivables |

The login page lists these accounts for convenience - **remove that panel and set `SECRET_KEY` before any real use.**
GSTINs in the demo data are dummy values and are labelled so on invoices.

## What is implemented (covered by automated tests unless noted under limitations)
Role-based access (server-side, CSRF-protected forms) · product/customer/supplier/material masters with
add/edit/deactivate (inactive products cannot be ordered) · BOM master · multi-line orders with customer-type price list ·
admin approval with stock-availability check, partial approval and reservation · GST invoices (CGST+SGST intra-state,
IGST inter-state) as HTML and PDF · FIFO batch allocation at dispatch · payments with outstanding balance and duplicate guard ·
purchases creating FIFO stock lots · production runs (issue → output → packaging → FG receipt) with material/yield variance,
3 % flagging, cost per packet and rule-based loss detection · stock ledger · batch↔invoice traceability (both directions) ·
13 reports with CSV / Excel / PDF export · AURA rule-based insights · seed/reset script.

## Assumptions (please read)
1. **Unit costs other than flour are assumed.** The brief gives only flour (₹45/kg). To make the stated
   production cost of **₹5,930** (₹4,130 materials + ₹1,200 labour + ₹600 gas/power → ₹31.54/packet) reconcile exactly I used
   oil ₹130/L, spice mix ₹250/kg, pouch ₹2.50, carton ₹20 (editable in Settings → Materials). With other prices the
   inventory/GST/variance results are unchanged but the cost figures will differ.
2. Cost basis = **latest purchase price** at the moment of issue (as the brief says), not FIFO/average cost.
3. Cartons used (9) are entered by the supervisor, as in the benchmark; pouches consumed = good packets + damaged pouches.
4. Material variance is measured against the batch plan (BOM scaled to planned output); yield against planned output.
5. Dispatch allocates finished goods **FIFO by batch** (oldest completed batch with stock first; the opening 50 packets are an
   "OPEN-…" batch). Material issues draw **FIFO from stock lots**; in the benchmark flour therefore comes from the opening lot,
   leaving the 60 kg purchase lot untouched.
6. Stock quantities of materials are stored as integer thousandths (1 kg = 1000); finished goods as whole packets.

## Known limitations (honest list)
* **Database layer is Python's built-in `sqlite3`, not SQLAlchemy**, and tests use `unittest`, not pytest: SQLAlchemy and pytest
  could not be installed in the build sandbox (no network), and I chose not to ship code I could not run. The schema is
  plain relational SQL with foreign keys, CHECK/UNIQUE constraints and transactions; porting to SQLAlchemy models would be mechanical.
  The tests are standard `unittest.TestCase` classes, so `pytest` should also collect them, but I have **not** run them under pytest.
* Verified by the automated tests and by driving every page through Flask's test client (see `TEST_RESULTS.txt`). **Not tested:** real
  browsers/phones, the small JavaScript file (row add/remove and order preview), print layout, concurrent users, performance.
* Security is prototype-grade: demo passwords, no password change/lockout/rate limiting, no HTTPS, default dev `SECRET_KEY`.
* GST: no e-invoice/IRN, e-way bill, cess, reverse charge, credit/debit notes, returns or invoice cancellation. GSTIN format is not
  validated. Place of supply = customer's state. Purchase GST is shown in the purchase register but input-tax credit is not computed.
* Orders cannot be edited after submission; an invoice is dispatched in one go (no partial dispatch); a shortage is not carried as a back-order.
* No supplier payables, no work-in-progress stock (not modelled), no finished-goods valuation, no period locking (back-dated entries allowed).
* Loss-detection financial impacts are indicative and overlap (yield loss is valued at standard material cost per packet; excess
  oil is also part of material cost).
* PDFs print "Rs." because the built-in PDF fonts have no rupee glyph. No pagination/search on long lists.
* AURA is deterministic rules only - it is **not** generative AI and does not claim to be.

## Project layout
```
app/            Flask app: __init__.py (auth, CSRF, roles) · views.py · services.py (all business rules) · core.py (money/GST)
                reports.py · aura.py · pdf.py · seed.py · schema.sql · templates/ · static/
tests/          unittest suites (benchmark, services, web/permissions, UI walkthrough)
DESIGN_NOTE.md  data model, stock & costing calculations, design decisions
DEMO_SCRIPT.md  step-by-step walkthrough of the benchmark through the UI
TEST_RESULTS.txt  output of the last full test run
```
