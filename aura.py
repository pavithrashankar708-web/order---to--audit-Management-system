"""AURA - rule-based business assistant. NO generative AI: every insight is a deterministic rule over live data."""
from datetime import date
from . import services as S
from .core import fmt_qty, fmt_money, fmt_bp

LABEL = "Rule-based guidance (deterministic rules, not generative AI)"


def insights(conn):
    out = []
    add = lambda level, area, title, detail, action="": out.append(dict(level=level, area=area, title=title, detail=detail, action=action))
    for m in S.allrows(conn, "SELECT * FROM materials WHERE active=1 AND reorder_milli>0"):
        bal = S.stock_balance(conn, material_id=m["id"])
        if bal <= m["reorder_milli"]:
            add("warn", "Low stock", f"{m['name']} is low: {fmt_qty(bal)} {m['unit']}",
                f"At or below the reorder level of {fmt_qty(m['reorder_milli'])} {m['unit']}.", "Raise a purchase before the next batch.")
    for r in S.allrows(conn, "SELECT id, batch_id FROM production_runs WHERE kind='PRODUCTION' AND status='COMPLETED' ORDER BY id DESC"):
        for f in S.analyze_run(conn, r["id"]):
            area = {"yield_loss": "Yield loss", "excess_material": "Excess material", "damaged_packaging": "Damaged packaging"}[f["kind"]]
            add("high" if f["severity"] == "high" else "warn", area, f"{r['batch_id']}: {f['title']}",
                f"{f['detail']} Estimated loss {fmt_money(f['impact_paise'])}.", f["advice"])
    for o in S.allrows(conn, "SELECT o.id, o.number, c.name FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.status='SUBMITTED' ORDER BY o.id"):
        short = sum(l["shortage"] for l in S.order_availability(conn, o["id"]))
        add("warn" if short else "info", "Pending order", f"{o['number']} for {o['name']} awaits approval",
            f"Stock shortage of {short} packets - approve partially or schedule production." if short else "Stock is available.",
            "Open the order and approve or reject.")
    today = date.today()
    for r in S.allrows(conn, "SELECT inv.id, inv.number, inv.date, inv.total_paise, c.name FROM invoices inv JOIN customers c ON c.id=inv.customer_id"):
        _, out_p = S.invoice_balance(conn, r["id"])
        if out_p > 0:
            days = (today - date.fromisoformat(r["date"])).days
            add("warn" if days > 30 else "info", "Unpaid invoice", f"{r['number']} - {r['name']} owes {fmt_money(out_p)}",
                f"Invoice is {days} day(s) old.", "Follow up for collection.")
    if not out:
        add("ok", "All clear", "No alerts right now", "Stock, production and receivables are within the configured rules.")
    return out
