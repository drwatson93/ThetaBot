"""Position-card display math and the exit-plan sentence.

The percent is computed here and only here. The dashboard prints that integer;
it does not divide again. Rounding is half away from zero (not Python's
built-in half-to-even ``round()``):

  0.5 → 1,  -0.5 → -1,  2.5 → 3,  -2.5 → -3,  69.5 → 70,  -69.5 → -70.

A credit that is missing, zero, or negative, or a P/L that is missing or
non-finite, returns None. The card shows an em dash. This never returns
Infinity or NaN.

The plan sentence is built from ``settings.rules`` (the same list a hot edit
replaces in memory). Rolls are not in that list: a credit roll is
``settings.roll`` / ``RollManager``, off unless that separate flag is on, and
this sentence does not claim one.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    """Finite Decimal, or None. Bool is rejected (it is an ``int`` subclass)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        dec = Decimal(str(value))
    else:
        return None
    if not dec.is_finite():
        return None
    return dec


def round_half_away_from_zero(value: Decimal) -> int:
    """Whole number, halves away from zero.

    Uses ``Decimal`` ``ROUND_HALF_UP``, which in Python rounds away from zero
    (1.5 → 2, -1.5 → -2). Do not replace this with the built-in ``round()``.
    """
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pnl_percent(unrealized_pnl: Any, credit_dollars: Any) -> int | None:
    """Whole-percent P/L = unrealized dollars / credit dollars.

    ``credit_dollars`` is the credit the card already shows
    (``credit_received * 100 * quantity``). ``unrealized_pnl`` is the unrealized
    dollars the card already shows. This does not recompute either figure.

    Returns None when the credit is missing, zero, or negative, or when P/L is
    missing or not a finite number. Never returns Infinity or NaN.
    """
    pnl = _decimal(unrealized_pnl)
    credit = _decimal(credit_dollars)
    if pnl is None or credit is None or credit <= 0:
        return None
    pct = (pnl / credit) * Decimal(100)
    if not pct.is_finite():
        return None
    return round_half_away_from_zero(pct)


def card_pnl_percent(unrealized_pnl: Any, credit_received: Any, quantity: Any) -> int | None:
    """Percent for one position row, from the card's existing credit product.

    Credit dollars = ``credit_received * 100 * quantity``. That product is the
    number the card labels "Credit collected". The ratio is ``pnl_percent``.
    """
    if credit_received is None or quantity is None:
        return pnl_percent(unrealized_pnl, None)
    if isinstance(credit_received, bool) or isinstance(quantity, bool):
        return pnl_percent(unrealized_pnl, None)
    try:
        credit = credit_received * 100 * quantity
    except TypeError:
        return pnl_percent(unrealized_pnl, None)
    return pnl_percent(unrealized_pnl, credit)


def _number(value: Any) -> float | None:
    dec = _decimal(value)
    if dec is None:
        return None
    return float(dec)


def _whole(value: Any) -> int | None:
    dec = _decimal(value)
    if dec is None or dec != dec.to_integral_value():
        return None
    return int(dec)


def _points(fraction: float) -> int:
    """0.8 → 80, with the same half-away-from-zero rule as P/L percent."""
    return round_half_away_from_zero(Decimal(str(fraction)) * Decimal(100))


def _fixed(value: float, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    dec = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return f"{dec:.{places}f}"


def _days_left(threshold: int) -> str:
    unit = "day" if threshold == 1 else "days"
    return f"{threshold} {unit} left"


def _rule_bits(rule: Any) -> str | None:
    if getattr(rule, "enabled", False) is not True:
        return None
    params = getattr(rule, "params", None) or {}
    if not isinstance(params, dict):
        params = {}
    kind = str(getattr(rule, "rule_type", "") or "")

    if kind == "PROFIT_TARGET":
        pct = _number(params.get("profit_pct"))
        if pct is None:
            return "Closes at the profit target."
        shown = f"{_points(pct)}%"
        if params.get("trailing") is True:
            return f"Arms at {shown} of the credit, then trails."
        return f"Closes at {shown} of the credit."

    if kind == "STOP_LOSS":
        parts: list[str] = []
        loss = _number(params.get("loss_mult"))
        delta = _number(params.get("delta_stop"))
        if loss is not None:
            parts.append(f"cost reaches {_fixed(loss, 1)}× the credit")
        if delta is not None:
            parts.append(f"|delta| reaches {_fixed(delta, 2)}")
        if not parts:
            return None
        return "Buys it back if " + " or ".join(parts) + "."

    if kind == "DTE":
        threshold = _whole(params.get("dte_threshold"))
        when = f"at {_days_left(threshold)}" if threshold is not None else "near expiration"
        action = params.get("action", "alert")
        action_s = str(action if action is not None else "alert").strip().lower()
        if action_s == "close":
            return f"Buys the option back {when}."
        if action_s == "alert":
            return f"Alerts you {when} and does not buy the option back."
        # DteRule buys the option back only when action is close. Anything else
        # is notify-only: it alerts and never sends the close order.
        label = str(action).strip() or "alert"
        spot = _days_left(threshold) if threshold is not None else "expiration"
        return (
            f"At {spot} it alerts you (action: {label}) and does not buy the option back."
        )

    if kind == "SIGNAL":
        if getattr(rule, "requires_approval", False) is True:
            return "A TradingView signal closes it, with your approval."
        return "A TradingView signal closes it."

    name = str(getattr(rule, "name", "") or "").strip() or "a rule"
    return f"Runs {name}."


def holding_plan(rules: list[Any] | None) -> str:
    """Short exit plan from the live rules list. Disabled rules are omitted.

    When every rule is off (or none are configured), returns exactly
    ``No exit rules are on``.
    """
    bits = [bit for bit in (_rule_bits(rule) for rule in (rules or [])) if bit]
    if not bits:
        return "No exit rules are on"
    return " ".join(bits)
