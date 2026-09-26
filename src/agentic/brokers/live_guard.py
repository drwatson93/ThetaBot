"""Single live-mode gate for every real broker order.

Paper / un-armed mode must never place a real order through any path. The only
optional exception is an explicit 1-share SGOV connectivity test, off by default.
"""
from __future__ import annotations

from typing import Any


class LiveOrderBlocked(RuntimeError):
    """Raised when a real broker is asked to place an order while not live-armed."""


def _allow_sgov_test_buy(settings: Any, *, kind: str, symbol: str | None, quantity: float | None,
                         side: str | None) -> bool:
    tr = getattr(settings, "tax_reserve", None)
    if tr is None or not getattr(tr, "allow_sgov_test_buy", False):
        return False
    return (
        kind == "equity"
        and (symbol or "").upper() == "SGOV"
        and side == "buy"
        and quantity == 1
    )


def assert_live_order_allowed(
    settings: Any,
    *,
    kind: str = "option",
    symbol: str | None = None,
    quantity: float | None = None,
    side: str | None = None,
) -> None:
    """Refuse any real order unless live-armed (or the optional 1-share SGOV test)."""
    if settings is None:
        raise LiveOrderBlocked("broker has no settings; refusing to place a real order")
    if getattr(settings, "is_live", False):
        return
    if _allow_sgov_test_buy(settings, kind=kind, symbol=symbol, quantity=quantity, side=side):
        return
    mode = getattr(settings, "mode", "?")
    armed = getattr(settings, "i_understand_live_trading", False)
    raise LiveOrderBlocked(
        f"NOT ARMED (mode={mode}, i_understand_live_trading={armed}); "
        f"refusing real {kind} order"
    )
