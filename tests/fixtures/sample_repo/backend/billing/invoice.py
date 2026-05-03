"""Invoice domain model — tests/fixtures/sample_repo backend service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class LineItem:
    """One line on an invoice."""

    sku: str
    qty: int
    unit_price: float


class Invoice:
    """A customer invoice composed of line items."""

    def __init__(self, customer_id: str, items: Iterable[LineItem]) -> None:
        self.customer_id = customer_id
        self.items = list(items)

    def subtotal(self) -> float:
        return sum(li.qty * li.unit_price for li in self.items)

    def total(self, tax_rate: float = 0.20) -> float:
        """Total including tax."""
        return self.subtotal() * (1 + tax_rate)


def make_invoice(customer_id: str, items: list[LineItem]) -> Invoice:
    return Invoice(customer_id, items)


__all__ = ["Invoice", "LineItem", "make_invoice"]
