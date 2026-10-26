"""Position-card display math and the exit-plan sentence.

The percent is computed here and only here. The dashboard prints that integer;
it does not divide again. Rounding is half away from zero (not Python's
built-in half-to-even ``round()``):

  0.5 → 1,  -0.5 → -1,  2.5 → 3,  -2.5 → -3,  69.5 → 70,  -69.5 → -70.

A credit that is missing, zero, or negative, or a P/L that is missing or
non-finite, returns None. The card shows an em dash. This never returns
Infinity or NaN.

The plan sentence is built from ``settings.rules`` (the same list a hot edit
replaces in memory). A credit roll is not one of those rules, and this
sentence does not claim one.
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


def _trim(dec: Decimal) -> str:
    """Decimal text with no extra rounding and no trailing zeros."""
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _num_label(value: float) -> str:
    """Configured number as written. 2.25 stays 2.25; 2.0 stays 2."""
    return _trim(Decimal(str(value)))


def _pct_label(fraction: float) -> str:
    """0.8 → 80%, 0.575 → 57.5%. Does not round to a whole percent."""
    return _trim(Decimal(str(fraction)) * Decimal(100)) + "%"


def _days_left(threshold: int) -> str:
    unit = "day" if threshold == 1 else "days"
    return f"{threshold} {unit} left"


def _params(rule: Any) -> dict:
    params = getattr(rule, "params", None) or {}
    return params if isinstance(params, dict) else {}


def _asks(rule: Any) -> bool:
    return getattr(rule, "requires_approval", False) is True


def _dte_days(params: dict) -> int:
    """Days-left threshold DteRule actually uses. Missing means 21."""
    if "dte_threshold" not in params or params.get("dte_threshold") is None:
        return 21
    n = _number(params.get("dte_threshold"))
    if n is None:
        return 21
    return int(n)


def _profit_bit(rule: Any, params: dict) -> str:
    pct = _number(params.get("profit_pct"))
    shown = _pct_label(pct) if pct is not None else None
    asks = _asks(rule)
    # Truthy, same as ProfitTargetRule: anything other than a real false arms the trail.
    if params.get("trailing"):
        gap = _number(params.get("trail_gap"))
        gap_label = _pct_label(0.20 if gap is None else gap)
        if shown is None:
            prefix = "Arms at the profit target"
            floor = "the arm"
        else:
            prefix = f"Arms at {shown} of the credit"
            floor = shown
        giveback = f"after a {gap_label} give-back from the peak, floored at {floor}"
        if asks:
            return f"{prefix}, then asks for approval to buy it back {giveback}."
        return f"{prefix}, then buys it back {giveback}."
    if shown is None:
        if asks:
            return "Asks for approval to close at the profit target."
        return "Closes at the profit target."
    if asks:
        return f"Asks for approval to close at {shown} of the credit."
    return f"Closes at {shown} of the credit."


def _stop_bit(rule: Any, params: dict) -> str | None:
    parts: list[str] = []
    loss = _number(params.get("loss_mult"))
    delta = _number(params.get("delta_stop"))
    if loss is not None:
        parts.append(f"cost reaches {_num_label(loss)}× the credit")
    if delta is not None:
        parts.append(f"|delta| reaches {_num_label(delta)}")
    if not parts:
        return None
    joined = " or ".join(parts)
    if _asks(rule):
        return f"Asks for approval to buy it back if {joined}."
    return f"Buys it back if {joined}."


def _dte_bit(rule: Any, params: dict) -> str:
    when = _days_left(_dte_days(params))
    # DteRule buys back only when action == "close" exactly. "Close" and
    # " close" are notify-only. A missing action defaults to "alert".
    if "action" not in params or params.get("action") is None:
        action = "alert"
    else:
        action = params.get("action")
    if action == "close":
        if _asks(rule):
            return f"Asks for approval to buy the option back at {when}."
        return f"Buys the option back at {when}."
    if action == "alert":
        return f"Alerts you at {when} and does not buy the option back."
    label = action if isinstance(action, str) else str(action)
    return f"At {when} it alerts you (action: {label}) and does not buy the option back."


def _signal_bit(rule: Any) -> str:
    if _asks(rule):
        return "A TradingView signal closes it, with your approval."
    # SignalMatcher forces approval when one symbol matches more than one open position.
    return (
        "A TradingView signal closes it. "
        "A symbol matching more than one open position still asks for approval."
    )


def _rule_bits(rule: Any) -> str | None:
    # Profit, stop, and DTE are already skipped by RulesEngine when disabled.
    # tv-signal is not: on main, SignalProcessor ignores enabled and still closes.
    # Leaving a disabled tv-signal out of this sentence assumes PR #13
    # (branch cursor/rules-tab-editor-3b4c), which does not match or close when
    # that rule is off. This card does not change the processor.
    if getattr(rule, "enabled", False) is not True:
        return None
    params = _params(rule)
    kind = str(getattr(rule, "rule_type", "") or "")
    if kind == "PROFIT_TARGET":
        return _profit_bit(rule, params)
    if kind == "STOP_LOSS":
        return _stop_bit(rule, params)
    if kind == "DTE":
        return _dte_bit(rule, params)
    if kind == "SIGNAL":
        return _signal_bit(rule)
    name = str(getattr(rule, "name", "") or "").strip() or "a rule"
    return f"Runs {name}."


def live_profit_pct(rules: list[Any] | None) -> float | None:
    """Arm level of the first enabled profit target, or None when that rule is off.

    A missing ``profit_pct`` is 0.5, the same default ProfitTargetRule uses.
    The value is not rounded.
    """
    for rule in rules or []:
        if getattr(rule, "enabled", False) is not True:
            continue
        if str(getattr(rule, "rule_type", "") or "") != "PROFIT_TARGET":
            continue
        pct = _number(_params(rule).get("profit_pct"))
        return 0.5 if pct is None else pct
    return None


def holding_plan(rules: list[Any] | None) -> str:
    """Short exit plan from the live rules list. Disabled rules are omitted.

    When every rule is off (or none are configured), returns exactly
    ``No exit rules are on``.

    A disabled tv-signal is omitted on purpose. That sentence is true once
    PR #13 honors ``enabled`` in SignalProcessor. On main a disabled
    tv-signal still closes. This function does not change that runtime.
    """
    bits = [bit for bit in (_rule_bits(rule) for rule in (rules or [])) if bit]
    if not bits:
        return "No exit rules are on"
    return " ".join(bits)
