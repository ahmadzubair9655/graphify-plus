"""HTTP API for billing — fixture only."""
from __future__ import annotations

from .billing.invoice import Invoice, LineItem, make_invoice


def post_invoice(customer_id: str, raw_items: list[dict]) -> dict:
    items = [LineItem(sku=r["sku"], qty=int(r["qty"]),
                      unit_price=float(r["unit_price"])) for r in raw_items]
    inv: Invoice = make_invoice(customer_id, items)
    return {"customer_id": customer_id, "total": inv.total()}
