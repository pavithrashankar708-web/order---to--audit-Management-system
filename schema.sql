PRAGMA foreign_keys = ON;
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN('salesman','admin','supervisor','auditor','owner')),
  password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE products(id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pack_size TEXT NOT NULL,
  price_paise INTEGER NOT NULL CHECK(price_paise>=0), distributor_price_paise INTEGER CHECK(distributor_price_paise IS NULL OR distributor_price_paise>=0),
  gst_bp INTEGER NOT NULL DEFAULT 500 CHECK(gst_bp>=0), hsn TEXT NOT NULL,
  std_batch_output INTEGER NOT NULL DEFAULT 0 CHECK(std_batch_output>=0), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, ctype TEXT NOT NULL CHECK(ctype IN('retailer','distributor')),
  state TEXT NOT NULL, gstin TEXT, address TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, state TEXT, gstin TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE materials(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN('RM','PM')), unit TEXT NOT NULL,
  last_cost_paise INTEGER NOT NULL DEFAULT 0 CHECK(last_cost_paise>=0),   -- paise per ONE unit
  reorder_milli INTEGER NOT NULL DEFAULT 0, is_primary_pack INTEGER NOT NULL DEFAULT 0,
  gst_bp INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE bom_lines(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  material_id INTEGER NOT NULL REFERENCES materials(id), std_milli INTEGER NOT NULL CHECK(std_milli>0),
  UNIQUE(product_id, material_id));
CREATE TABLE counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);

CREATE TABLE purchases(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  supplier_inv_no TEXT, date TEXT NOT NULL, created_by INTEGER REFERENCES users(id), UNIQUE(supplier_id, supplier_inv_no));
CREATE TABLE purchase_lines(id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  material_id INTEGER NOT NULL REFERENCES materials(id), qty_milli INTEGER NOT NULL CHECK(qty_milli>0),
  rate_paise INTEGER NOT NULL CHECK(rate_paise>=0), gst_bp INTEGER NOT NULL DEFAULT 0, amount_paise INTEGER NOT NULL);
CREATE TABLE material_lots(id INTEGER PRIMARY KEY, material_id INTEGER NOT NULL REFERENCES materials(id),
  lot_no TEXT UNIQUE NOT NULL, source TEXT NOT NULL CHECK(source IN('OPENING','PURCHASE','ADJUSTMENT')),
  purchase_line_id INTEGER REFERENCES purchase_lines(id), received_date TEXT NOT NULL,
  qty_milli INTEGER NOT NULL CHECK(qty_milli>0), rate_paise INTEGER NOT NULL DEFAULT 0);

CREATE TABLE production_runs(id INTEGER PRIMARY KEY, batch_id TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN('OPENING','PRODUCTION')), product_id INTEGER NOT NULL REFERENCES products(id),
  date TEXT NOT NULL, planned_output INTEGER NOT NULL, actual_good INTEGER NOT NULL DEFAULT 0 CHECK(actual_good>=0),
  status TEXT NOT NULL CHECK(status IN('PLANNED','IN_PROGRESS','COMPLETED')),
  damaged_packs INTEGER NOT NULL DEFAULT 0, wastage_notes TEXT, labour_paise INTEGER NOT NULL DEFAULT 0,
  power_paise INTEGER NOT NULL DEFAULT 0, completed_date TEXT, created_by INTEGER REFERENCES users(id));
CREATE TABLE run_lines(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES production_runs(id),
  material_id INTEGER NOT NULL REFERENCES materials(id), planned_milli INTEGER NOT NULL,
  actual_milli INTEGER NOT NULL DEFAULT 0, cost_paise INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id, material_id));
CREATE TABLE material_issues(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, run_id INTEGER NOT NULL REFERENCES production_runs(id),
  date TEXT NOT NULL, kind TEXT NOT NULL, created_by INTEGER REFERENCES users(id));
CREATE TABLE issue_lines(id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES material_issues(id),
  material_id INTEGER NOT NULL REFERENCES materials(id), qty_milli INTEGER NOT NULL CHECK(qty_milli>0), cost_paise INTEGER NOT NULL);
CREATE TABLE stock_adjustments(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, date TEXT NOT NULL,
  material_id INTEGER REFERENCES materials(id), product_id INTEGER REFERENCES products(id),
  run_id INTEGER REFERENCES production_runs(id), qty INTEGER NOT NULL CHECK(qty<>0), reason TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id));
CREATE TABLE lot_allocations(id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES material_lots(id),
  qty_milli INTEGER NOT NULL CHECK(qty_milli>0), issue_line_id INTEGER REFERENCES issue_lines(id),
  adjustment_id INTEGER REFERENCES stock_adjustments(id));

CREATE TABLE orders(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
  salesman_id INTEGER NOT NULL REFERENCES users(id), date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('SUBMITTED','APPROVED','REJECTED','INVOICED','DISPATCHED')),
  notes TEXT, reject_reason TEXT, approved_by INTEGER REFERENCES users(id));
CREATE TABLE order_lines(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL CHECK(qty>0),
  approved_qty INTEGER NOT NULL DEFAULT 0, price_paise INTEGER NOT NULL, gst_bp INTEGER NOT NULL, hsn TEXT NOT NULL,
  UNIQUE(order_id, product_id));
CREATE TABLE invoices(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, order_id INTEGER UNIQUE NOT NULL REFERENCES orders(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id), date TEXT NOT NULL, place_of_supply TEXT NOT NULL,
  interstate INTEGER NOT NULL, taxable_paise INTEGER NOT NULL, cgst_paise INTEGER NOT NULL, sgst_paise INTEGER NOT NULL,
  igst_paise INTEGER NOT NULL, total_paise INTEGER NOT NULL, created_by INTEGER REFERENCES users(id));
CREATE TABLE invoice_lines(id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  order_line_id INTEGER NOT NULL REFERENCES order_lines(id), product_id INTEGER NOT NULL REFERENCES products(id),
  description TEXT NOT NULL, hsn TEXT NOT NULL, qty INTEGER NOT NULL, rate_paise INTEGER NOT NULL, gst_bp INTEGER NOT NULL,
  taxable_paise INTEGER NOT NULL, cgst_paise INTEGER NOT NULL, sgst_paise INTEGER NOT NULL, igst_paise INTEGER NOT NULL,
  total_paise INTEGER NOT NULL);
CREATE TABLE dispatches(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, invoice_id INTEGER UNIQUE NOT NULL REFERENCES invoices(id),
  date TEXT NOT NULL, vehicle TEXT, created_by INTEGER REFERENCES users(id));
CREATE TABLE dispatch_lines(id INTEGER PRIMARY KEY, dispatch_id INTEGER NOT NULL REFERENCES dispatches(id),
  invoice_line_id INTEGER NOT NULL REFERENCES invoice_lines(id), run_id INTEGER NOT NULL REFERENCES production_runs(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL CHECK(qty>0));
CREATE TABLE payments(id INTEGER PRIMARY KEY, receipt_no TEXT UNIQUE NOT NULL, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id), date TEXT NOT NULL, amount_paise INTEGER NOT NULL CHECK(amount_paise>0),
  mode TEXT NOT NULL, reference TEXT, idem_key TEXT UNIQUE, created_by INTEGER REFERENCES users(id));

-- Source-linked stock ledger. qty: milli-units for materials (1 kg = 1000), whole packets for finished goods.
CREATE TABLE stock_ledger(id INTEGER PRIMARY KEY, date TEXT NOT NULL,
  item_kind TEXT NOT NULL CHECK(item_kind IN('RM','PM','FG')),
  material_id INTEGER REFERENCES materials(id), product_id INTEGER REFERENCES products(id),
  batch_ref TEXT NOT NULL, movement TEXT NOT NULL CHECK(movement IN
   ('OPENING','PURCHASE','ISSUE_TO_PRODUCTION','PACKAGING_USE','PRODUCTION_RECEIPT','SALE','ADJUSTMENT')),
  qty INTEGER NOT NULL, unit TEXT NOT NULL, source_type TEXT NOT NULL, source_id INTEGER NOT NULL,
  source_no TEXT NOT NULL, line_key TEXT NOT NULL, note TEXT, created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK((material_id IS NOT NULL) <> (product_id IS NOT NULL)),
  UNIQUE(source_type, source_id, line_key));
CREATE INDEX ix_ledger_item ON stock_ledger(item_kind, material_id, product_id, date);
