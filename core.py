"""Money (integer paise), quantity (integer milli-units) and GST helpers. No floats anywhere."""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


class BizError(Exception):
    """A business-rule violation that should be shown to the user (and rolls the transaction back)."""


def div_round(n, d):
    """Integer division rounded half away from zero."""
    if d == 0:
        raise ZeroDivisionError("division by zero")
    sign = -1 if (n < 0) != (d < 0) else 1
    n, d = abs(n), abs(d)
    return sign * ((2 * n + d) // (2 * d))


def _dec(v):
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        raise BizError(f"Invalid number: {v!r}")


def to_paise(v):
    return int((_dec(v) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_milli(v):
    return int((_dec(v) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_bp(v):
    """Percent (e.g. '5' or '2.5') -> basis points (500 / 250)."""
    return int((_dec(v) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _indian(n):
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail, parts = s[:-3], s[-3:], []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def fmt_money(p, symbol="₹"):
    p = int(p or 0)
    sign, p = ("-" if p < 0 else ""), abs(p)
    return f"{sign}{symbol}{_indian(p // 100)}.{p % 100:02d}"


def plain_money(p):
    """Rupees as a plain decimal string (CSV / form fields)."""
    p = int(p or 0)
    sign, p = ("-" if p < 0 else ""), abs(p)
    return f"{sign}{p // 100}.{p % 100:02d}"


def fmt_qty(m):
    m = int(m or 0)
    sign, m = ("-" if m < 0 else ""), abs(m)
    whole, frac = divmod(m, 1000)
    return f"{sign}{whole}" + (("." + f"{frac:03d}".rstrip("0")) if frac else "")


def fmt_bp(bp):
    if bp is None:
        return "—"
    sign = "+" if bp > 0 else "-" if bp < 0 else ""
    bp = abs(bp)
    return f"{sign}{bp // 100}.{bp % 100:02d}%"


def fmt_gst(bp):
    return f"{bp // 100}.{bp % 100:02d}".rstrip("0").rstrip(".") + "%"


def bp_diff(actual, planned):
    """(actual - planned) / planned in basis points (1% = 100 bp), rounded half away from zero."""
    return 0 if not planned else div_round((actual - planned) * 10000, planned)


def gst_split(taxable_paise, gst_bp, interstate):
    """Return (cgst, sgst, igst) in paise. Intra-state: tax split equally into CGST+SGST."""
    tax = div_round(taxable_paise * gst_bp, 10000)
    if interstate:
        return 0, 0, tax
    cgst = tax // 2
    return cgst, tax - cgst, 0


def line_cost(qty_milli, rate_paise_per_unit):
    return div_round(qty_milli * rate_paise_per_unit, 1000)
