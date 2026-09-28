"""Limit-only house rule + the quote snapshot recorded with every order.

Monitor bots verify that the engine never sends a market order. Every order path
(paper or live) must stamp ``order_type``, ``limit_price``, bid/ask/mid at pricing
time, and time-in-force, then refuse anything that isn't a limit.
"""
from __future__ import annotations

from typing import Any

DEFAULT_TIME_IN_FORCE = "gfd"

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


class NonLimitOrderRefused(RuntimeError):
    """A code path tried to build or submit a non-limit order."""


def normalize_order_type(order_type: str | None) -> str:
    return (order_type or "").strip().lower()


def is_limit_order(order_type: str | None) -> bool:
    return normalize_order_type(order_type) == "limit"


def assert_limit_only(order_type: str | None, *, where: str, **ctx: Any) -> None:
    """Refuse anything other than a limit order. Safe to call in paper mode too."""
    if is_limit_order(order_type):
        return
    bits = [f"{k}={v}" for k, v in ctx.items() if v is not None]
    extra = f" ({', '.join(bits)})" if bits else ""
    shown = normalize_order_type(order_type) or "missing"
    raise NonLimitOrderRefused(
        f"LIMIT-ONLY RULE: refusing {shown} order at {where}{extra}"
    )


def snapshot_from_quote(quote: Any | None) -> dict[str, float | None]:
    """Bid/ask/mid from an OptionQuote or OptionContractQuote (all None if missing)."""
    if quote is None:
        return {"bid": None, "ask": None, "mid": None}
    mid = getattr(quote, "midpoint", None)
    return {
        "bid": getattr(quote, "bid", None),
        "ask": getattr(quote, "ask", None),
        "mid": mid,
    }


def stamp_order_quote(order: Any, quote: Any | None, *,
                      time_in_force: str = DEFAULT_TIME_IN_FORCE) -> None:
    """Copy the pricing quote onto the order and default time-in-force."""
    snap = snapshot_from_quote(quote)
    order.bid = snap["bid"]
    order.ask = snap["ask"]
    order.mid = snap["mid"]
    if not getattr(order, "time_in_force", None):
        order.time_in_force = time_in_force


def pricing_kwargs(order: Any) -> dict[str, Any]:
    """Keyword args for persisting a pricing snapshot from an Order."""
    return {
        "order_type": normalize_order_type(getattr(order, "order_type", None)) or None,
        "limit_price": getattr(order, "limit_price", None),
        "bid": getattr(order, "bid", None),
        "ask": getattr(order, "ask", None),
        "mid": getattr(order, "mid", None),
        "time_in_force": getattr(order, "time_in_force", None),
    }


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
