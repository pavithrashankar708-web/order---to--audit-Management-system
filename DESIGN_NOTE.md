# Design note

## 1. Principles
* **Money = integer paise, quantities = integer milli-units.** No float arithmetic in Python or SQL
  (the order-entry preview in JS also uses integer paise). Rounding is explicit: `div_round` = half away from zero.
* **Services never commit; callers wrap postings in `atomic()`** so a multi-step posting (e.g. issue 3 materials, or complete a batch:
  packaging issue + FG receipt + status change) succeeds or rolls back as one transaction. Tested: a failing second line leaves no trace of the first.
* **Duplicate-posting protection is in the database**, not only the UI: UNIQUE on invoice per order, dispatch per invoice,
  `(source_type, source_id, line_key)` on the ledger, supplier invoice per supplier, payment idempotency key; plus status guards (a batch can be completed once).

## 2. Data model (tables → relationships)
```
users                       products ──< bom_lines >── materials
customers   suppliers       products.std_batch_output = expected yield per BOM batch

purchases ──< purchase_lines ──┐
                               └─► material_lots (FIFO stock lots; source OPENING | PURCHASE | ADJUSTMENT)
production_runs (batch_id UNIQUE; kind OPENING | PRODUCTION) ──< run_lines (planned / actual / cost per material)
        │
        └──< material_issues ──< issue_lines ──< lot_allocations >── material_lots     (issue → which lot → which purchase)
orders ──< order_lines            invoices(1:1 order) ──< invoice_lines
                                  dispatches(1:1 invoice) ──< dispatch_lines >── production_runs   (invoice line → batch)
payments >── invoices             stock_adjustments        stock_ledger (every movement, linked to its source document)
```
**Traceability chain (real foreign keys):**
`purchase_lines → material_lots → lot_allocations → issue_lines → material_issues → production_runs → dispatch_lines → invoice_lines → invoices → orders`.
`services.trace_batch` walks it forward/backward for one batch; `trace_invoice` starts from an invoice and lists its batches with their materials.

## 3. Stock ledger and balances
`stock_ledger(date, item, batch_ref, movement, qty (+in/−out), unit, source_type, source_id, source_no, line_key)`.
Movements: OPENING, PURCHASE, ISSUE_TO_PRODUCTION, PACKAGING_USE, PRODUCTION_RECEIPT, SALE, ADJUSTMENT.
**Balance = Σ qty.** The roll-forward report shows, per item and period:
`Opening + Receipts − Issues/Sales ± Adjustments = Closing`, with a reconciliation flag; tests assert it holds, and that
Σ(lot remaining) = ledger balance for every material and Σ(batch remaining) = FG balance.

Benchmark check (flour): 100 opening + 60 purchased − 40 issued = **120 kg**. Pouches: 500 − (188 good + 6 damaged) = **306**
(6 of the standard 200 are unused). Finished goods: 50 + 188 = 238; − 150 dispatched = **88**.

## 4. Production, variance, costing
* Run lifecycle: PLANNED → (issue) IN_PROGRESS → (output) COMPLETED. Planned consumption per material = BOM qty × planned output ÷ BOM batch output.
* Variance (basis points, shown as %) = (actual − planned) ÷ planned. Flag when |variance| > 3 % (300 bp); excess = adverse (a loss), under-use is flagged but not a loss.
  Benchmark: yield (188−200)/200 = **−6.00 %**; oil (10.5−10)/10 = **+5.00 %**.
* Cost per packet = (Σ material qty × latest purchase price at issue + labour + gas/power) ÷ good packets.
  Benchmark: 1,800 + 1,365 + 300 + 485 + 180 = ₹4,130; + 1,200 + 600 = **₹5,930**; ÷ 188 = ₹31.5425 → **₹31.54** (stored as 3,154 paise).
* Overheads are *set*, not added, so re-saving cannot double-count.
* **Smart loss detection** (rule-based): excess RM (impact = excess qty × unit cost), yield loss (shortfall × standard material cost/packet),
  damaged packaging (damaged × pouch cost), each with a corrective recommendation (oil measurement, frying temperature, operator handling, pouch sealing…).
  Benchmark impacts: oil ₹65.00, yield 12 × ₹20.50 = ₹246.00, pouches ₹15.00.

## 5. Orders, availability, GST, dispatch
* Availability = FG ledger balance − quantities already approved for other undispatched orders (reservation). Approval can never exceed it (partial approval allowed).
  Benchmark: Distributor B orders 120, 88 available → **shortage 32**.
* Invoice per line: taxable = qty × rate; tax = round(taxable × GST bp ÷ 10 000); intra-state (customer state = Tamil Nadu) → CGST = ⌊tax/2⌋, SGST = tax − CGST; otherwise IGST.
  Retailer A: 150 × ₹50 = ₹7,500; CGST ₹187.50; SGST ₹187.50; total **₹7,875**; paid ₹4,000 → outstanding **₹3,875**.
* Dispatch allocates FIFO across batches (oldest completed batch with remaining qty), writes `dispatch_lines` (invoice line → batch) and one SALE ledger row per allocation.
  Benchmark: 150 = 50 from the opening batch + 100 from the new batch.

## 6. Access control
Server-side `@roles(...)` on every route; auditor/owner are blocked from all POSTs globally; salesmen only see their own orders (404 otherwise);
finance reports are hidden from the supervisor; CSRF token on every POST (enabled by default). Role tests are in `tests/test_web.py`.

## 7. Consciously out of scope
See "Known limitations" in `README.md`.
