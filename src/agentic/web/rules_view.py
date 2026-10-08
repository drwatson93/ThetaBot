"""Plain-English trading-rules list, generated from the same Settings the engine reads."""
from __future__ import annotations

from typing import Any

from ..brokers.robinhood_mcp import ORDERS_HARD_DISABLED, REAL_ORDERS_LOCK_LABEL
from ..config import Settings


def _pct(x: float | None) -> str:
    if x is None:
        return "off"
    return f"{x:.0%}" if abs(x) >= 0.01 else f"{x:.2%}"


def _on(flag: bool) -> str:
    return "on" if flag else "off"


def _hhmm(value: str) -> str:
    return value or "10:00"


def describe_active_rules(
    settings: Settings,
    *,
    alerts_mode: str | None = None,
    webhook_configured: bool | None = None,
) -> list[dict[str, Any]]:
    """One row per active rule / hard limit. Values come from ``settings`` only."""
    s = settings
    e = s.entry
    sz = e.sizing
    crit = e.criteria
    tr = s.tax_reserve
    risk = s.risk
    rows: list[dict[str, Any]] = []

    def add(group: str, name: str, value: str, detail: str = "") -> None:
        rows.append({"group": group, "name": name, "value": value, "detail": detail})

    mode_label = "paper (no real orders)" if not s.is_live else "LIVE — real orders armed"
    add("Mode", "Trading mode", mode_label,
        "Live requires both mode: live and i_understand_live_trading: true.")
    if ORDERS_HARD_DISABLED:
        add("Mode", "Real orders", "HARD DISABLED in code",
            f"{REAL_ORDERS_LOCK_LABEL}. Hard-coded in the Robinhood MCP client. "
            "Cannot be turned off by config.yaml, env, the dashboard, mode: live, "
            "or i_understand_live_trading.")
    add("Mode", "Broker", s.broker, f"Fallback: {s.broker_fallback or 'none'}.")
    add("Mode", "Market data", s.market_data,
        "Robinhood quotes are treated as real-time. Alpaca needs feed=opra for live entry.")

    add("Hours", "Trading start", f"{_hhmm(s.trading_start)} America/New_York",
        "No new entries, profit-target exits, stop-loss exits, DTE closes, signal closes, "
        "rolls, or tax-reserve equity buys before this time.")
    add("Hours", "Regular session", "09:30–16:00 ET on NYSE trading days",
        "Closed weekends, NYSE full-day holidays, and after 1:00 PM ET on early-close days. "
        "Quotes still refresh from 09:30; orders wait for the trading-start clock so the "
        "open-bell spread is skipped.")
    add("Hours", "Pause-only token", "PAUSE_TOKEN env (optional)",
        "A monitoring bot can POST /control/pause-only with this token to engage the kill "
        "switch. It cannot resume, approve trades, or change trade alerts. Resume and the "
        "alerts switch still need the owner dashboard login (or CONTROL_TOKEN for scripts). "
        "A view-only login cannot change anything.")

    hook = "configured" if webhook_configured else "not configured"
    mode_s = alerts_mode or "instant"
    add("Alerts", "Trade alerts", f"{mode_s} · webhook {hook}",
        "When TRADE_ALERT_URL is set and mode is instant, ThetaBot POSTs a JSON payload "
        "the moment a practice trade opens or closes (STO/BTC one-line summary plus "
        "structured fields). Regular and Off send nothing from ThetaBot; the watcher bot "
        "reads the mode and handles those. Changing mode or sending a test alert is "
        "owner-only (owner dashboard login); PAUSE_TOKEN cannot. Unset "
        "TRADE_ALERT_URL or set mode Off to roll back. Delivery failures never stop trading "
        "and never write the engine last_error field.")

    add("Entries", "Scanner", _on(e.enabled),
        f"Watchlist: {', '.join(e.watchlist) if e.watchlist else '(empty)'}."
        f" Scan every {e.scan_interval_seconds // 60} minutes.")
    add("Entries", "CSP delta band", f"{crit.delta_min:.2f}–{crit.delta_max:.2f}",
        "Short-put |delta| window used to pick a strike.")
    add("Entries", "CSP days to expiration", f"{crit.dte_min}–{crit.dte_max} days", "")
    add("Entries", "Minimum annualized yield", _pct(crit.min_annualized_yield),
        "(premium ÷ strike) ÷ DTE × 365.")
    add("Entries", "Liquidity floors",
        f"OI ≥ {crit.min_open_interest}, volume ≥ {crit.min_volume}, "
        f"spread ≤ {_pct(crit.max_spread_pct)}", "")
    add("Entries", "IV vs realized vol",
        f"IV ≥ {crit.min_iv_rv_ratio:.2f}× realized" if crit.min_iv_rv_ratio else "off",
        "Sell premium only when implied is richer than recent realized vol.")
    add("Entries", "Earnings blackout",
        f"{crit.exclude_earnings_days} days" if e.earnings_gate else "off",
        "Skip a name whose earnings fall inside the hold window.")

    add("Sizing", "Max cash / collateral per trade", _pct(sz.max_position_size_pct),
        "Cap on one cash-secured put versus account value.")
    add("Sizing", "Max concurrent put positions", str(sz.max_concurrent_positions), "")
    add("Sizing", "Max covered-call contracts", str(sz.max_concurrent_cc), "")
    add("Sizing", "Total buying-power use", _pct(sz.total_bp_utilization_target),
        f"Always keep {_pct(sz.buying_power_reserve_pct)} unused.")
    if sz.target_positions:
        add("Sizing", "Target number of names", str(sz.target_positions),
            "Scale-invariant sizing: spread committed collateral across about this many names.")
    if sz.max_pct_per_underlying:
        add("Sizing", "Max per underlying", _pct(sz.max_pct_per_underlying),
            "More than one put per name is allowed up to this share of the account.")
    else:
        add("Sizing", "Puts per name", "one at a time",
            "A name already holding a put is not added to on a later scan.")

    add("Risk", "Daily / window loss limit",
        f"{_pct(risk.max_realized_loss_pct)} of account over {risk.lookback_days} days"
        if risk.loss_breaker_enabled and risk.max_realized_loss_pct else "off",
        "Freezes NEW entries only. Never force-closes open positions.")
    add("Risk", "Consecutive-loss halt",
        f"{risk.max_consecutive_losses} losers in a row" if risk.max_consecutive_losses else "off",
        "Uses journaled realized P&L, including assignment mark-to-market.")

    for rule in s.rules:
        params = rule.params or {}
        if not rule.enabled:
            add("Exits", rule.name, "off",
                "Disabled. The monitor does not evaluate this rule until it is turned back on.")
            continue
        if rule.rule_type == "PROFIT_TARGET":
            pct = params.get("profit_pct")
            detail = (
                f"Close when {float(pct):.0%} of the credit is captured."
                if isinstance(pct, (int, float)) and not isinstance(pct, bool)
                else "Profit target is set."
            )
            if params.get("trailing"):
                detail += f" Trailing: give back {params.get('trail_gap', 0):.0%} from the peak."
            add("Exits", rule.name, "profit target", detail)
        elif rule.rule_type == "STOP_LOSS":
            loss = params.get("loss_mult")
            delta = params.get("delta_stop")
            parts: list[str] = []
            if loss is None:
                parts.append("the loss-multiple trigger is off")
            else:
                parts.append(f"the mid cost-to-close ≥ {float(loss):.1f}× credit")
            if delta is None:
                parts.append("the |delta| trigger is off")
            else:
                parts.append(f"|delta| ≥ {float(delta):.2f}")
            add("Exits", rule.name, "stop loss",
                "Buy back if " + " or ".join(parts) + ". "
                "Null means that trigger is off; the other trigger still closes. "
                "The close is a limit starting at the bid/ask midpoint, never a market order at the ask.")
        elif rule.rule_type == "DTE":
            add("Exits", rule.name, f"{params.get('action', 'close')} at {params.get('dte_threshold')} DTE",
                "Near-expiry close (or alert) so a weekly is not held into expiration by accident.")
        elif rule.rule_type == "SIGNAL":
            add("Exits", rule.name, "TradingView signal",
                f"Match {params.get('match', 'underlying')}. "
                f"Requires approval: {'yes' if rule.requires_approval else 'no'}.")
        else:
            add("Exits", rule.name, rule.rule_type, str(params))

    add("Orders", "Order type", "limit only — never market",
        "Price starts at the bid/ask midpoint and steps toward a fill, capped by slippage.")
    add("Orders", "Quote freshness", f"{s.max_quote_age_seconds}s",
        "A fresh quote is fetched immediately before each order.")
    add("Orders", "Fill timeout / reprice",
        f"{s.execution.fill_timeout_seconds}s timeout, reprice after {s.execution.reprice_after_seconds}s",
        "The working order must actually cancel before a replacement is sent. Unfilled closes are cancelled.")
    add("Orders", "Slippage cap", _pct(s.execution.slippage_cap_pct),
        "A buy-to-close never pays more than ask × (1 + cap). A sell-to-open never goes below bid × (1 − cap).")

    add("Rolls", "Roll tested puts", _on(s.roll.enabled),
        "Both legs must be ready before the old put is bought back. A half-roll is never started on purpose.")

    add("Tax reserve", "Weekly sweep",
        _on(tr.enabled) + (f" · { _pct(tr.pct) } of net gains → {tr.symbol}" if tr.enabled else ""),
        f"Ticker is {tr.symbol}. Dry-run is {_on(tr.dry_run)}. Paper mode never places a real buy.")
    add("Tax reserve", "1-share SGOV test buy", _on(tr.allow_sgov_test_buy),
        "Off by default. When on, the broker may place one 1-share SGOV limit buy to test connectivity.")

    return rows
