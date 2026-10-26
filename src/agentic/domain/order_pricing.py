"""Read-only pricing snapshots for dashboard and journal JSON.

These copy stored order fields onto API responses. Nothing here submits an order.
"""
from __future__ import annotations

from typing import Any

PRICING_FIELDS = (
    "order_type",
    "limit_price",
    "bid",
    "ask",
    "mid",
    "time_in_force",
)
CLOSE_PRICING_FIELDS = (
    "close_order_type",
    "close_limit_price",
    "close_bid",
    "close_ask",
    "close_mid",
    "close_time_in_force",
)


def normalize_order_type(order_type: str | None) -> str:
    return (order_type or "").strip().lower()


def public_pricing_fields(obj: Any | None, *, prefix: str = "") -> dict[str, Any]:
    """JSON snapshot of how an order was priced. Keys are always present (None if unknown)."""
    if obj is None:
        return {f"{prefix}{k}": None for k in PRICING_FIELDS}
    ot = getattr(obj, "order_type", None)
    return {
        f"{prefix}order_type": (normalize_order_type(ot) or None) if ot is not None else None,
        f"{prefix}limit_price": getattr(obj, "limit_price", None),
        f"{prefix}bid": getattr(obj, "bid", None),
        f"{prefix}ask": getattr(obj, "ask", None),
        f"{prefix}mid": getattr(obj, "mid", None),
        f"{prefix}time_in_force": getattr(obj, "time_in_force", None),
    }


def public_close_pricing_fields(obj: Any | None) -> dict[str, Any]:
    """JSON snapshot of the close/buy-to-close order stored on a journal row."""
    if obj is None:
        return {k: None for k in CLOSE_PRICING_FIELDS}
    ot = getattr(obj, "close_order_type", None)
    return {
        "close_order_type": (normalize_order_type(ot) or None) if ot is not None else None,
        "close_limit_price": getattr(obj, "close_limit_price", None),
        "close_bid": getattr(obj, "close_bid", None),
        "close_ask": getattr(obj, "close_ask", None),
        "close_mid": getattr(obj, "close_mid", None),
        "close_time_in_force": getattr(obj, "close_time_in_force", None),
    }


def order_as_close_pricing(order: Any | None) -> dict[str, Any]:
    """Map an Order's unprefixed pricing fields onto journal close_* keys."""
    base = public_pricing_fields(order)
    return {f"close_{k}": base[k] for k in PRICING_FIELDS}
