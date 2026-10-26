"""Read-only performance dashboard: JSON APIs + one self-contained HTML page.

  GET /                -> the dashboard page
  GET /dashboard       -> the dashboard page
  GET /api/stats       -> win rate, realized/unrealized P&L, per-rule rollup
  GET /api/positions   -> open + closed positions with P&L, percent P/L, the exit plan, underlying last, and the rule that closed them
  GET /api/decisions   -> recent close decisions with reasons (the "why", for memory tuning)
  GET /api/audit       -> recent audit events

Everything here is read-only; mutations stay in /control/*. The page is dashboard.html,
loaded once at import. It polls the APIs with vanilla JS so there is no build step and
nothing to bundle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.resources import files
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..domain.models import utcnow
from ..domain.order_pricing import public_close_pricing_fields, public_pricing_fields
from ..services.stats import compute_stats, position_rows
from .auth import require_auth, require_owner
from .position_cards import card_pnl_percent, holding_plan, live_profit_pct
from .calc_page import CALC_PAGE

if TYPE_CHECKING:
    from .app import WebDeps


def _alerts_status(deps: "WebDeps") -> dict:
    from ..notify.trade_alerts import alerts_from_deps
    return alerts_from_deps(deps).status()


def _sector_exposure(positions, riskcfg, account_value):
    """Current short-put collateral grouped by sector — a concentration signal for the tactical read.
    Filters to open positions, then reuses risk_breaker.sector_exposure so the math never drifts."""
    from ..services.risk_breaker import sector_exposure
    open_pos = [p for p in positions
                if str(getattr(getattr(p, "status", None), "value", getattr(p, "status", None)))
                in ("OPEN", "CLOSING")]
    exp = sector_exposure(open_pos, getattr(riskcfg, "sector_map", None))
    return [{"sector": k, "collateral": round(v, 0),
             "pct_of_account": round(v / account_value * 100, 1) if account_value else None}
            for k, v in sorted(exp.items(), key=lambda x: -x[1])]


def _md_price_source(md) -> str:
    """Short label for the live equity-quote provider (crew-bot note, not a new feed)."""
    name = type(md).__name__.lower()
    if "robinhood" in name:
        return "robinhood"
    if "alpaca" in name:
        return "alpaca"
    return "market_data"


def _ctx_last_price(ctx_map: dict, symbol: str):
    """Last-scan underlying close from scanner.last_context, if present."""
    if not ctx_map:
        return None
    by_upper = {str(k).upper(): v for k, v in ctx_map.items()}
    c = by_upper.get((symbol or "").upper())
    if c is None:
        return None
    px = c.get("price") if isinstance(c, dict) else getattr(c, "price", None)
    try:
        return float(px) if px is not None else None
    except (TypeError, ValueError):
        return None


async def load_underlying_quotes(deps: "WebDeps", positions) -> dict[str, dict]:
    """Live equity last/mark per unique underlying, reused from MarketDataProvider.

    OPEN/CLOSING names, and closed names still in the last scan context, call
    ``get_underlying_price`` (Robinhood ``get_equity_quotes`` last trade / mark, or Alpaca
    last trade — same path the scanner already uses). Remaining rows fall back to the last
    scan's daily close. Fail-open: a quote miss leaves the symbol out of the map so the row
    serializes ``underlying_price: null``.
    """
    quotes: dict[str, dict] = {}
    sc = getattr(deps, "scanner", None)
    md = getattr(sc, "market_data", None) if sc is not None else None
    ctx = getattr(sc, "last_context", {}) if sc is not None else {}
    scanned = getattr(sc, "last_scan_at", None) if sc is not None else None
    scan_as_of = scanned.isoformat() if scanned is not None and hasattr(scanned, "isoformat") else None

    all_syms: list[str] = []
    seen: set[str] = set()
    open_set: set[str] = set()
    for p in positions:
        sym = (getattr(p, "underlying", None) or "").upper()
        if not sym:
            continue
        if sym not in seen:
            seen.add(sym)
            all_syms.append(sym)
        status = getattr(getattr(p, "status", None), "value", getattr(p, "status", None))
        if str(status) in ("OPEN", "CLOSING"):
            open_set.add(sym)
    ctx_syms = {str(k).upper() for k in (ctx or {})}
    live_syms = [s for s in all_syms if s in open_set or s in ctx_syms]

    now_iso = utcnow().isoformat()
    source = _md_price_source(md) if md is not None else "market_data"
    getter = getattr(md, "get_underlying_price", None) if md is not None else None
    for sym in live_syms:
        px = None
        if getter is not None:
            try:
                px = await getter(sym)
            except Exception:  # noqa: BLE001 — advisory quote; never fail the positions API
                px = None
        try:
            px = float(px) if px is not None else None
        except (TypeError, ValueError):
            px = None
        if px is not None:
            quotes[sym] = {"price": px, "source": source, "as_of": now_iso}

    for sym in all_syms:
        if sym in quotes:
            continue
        px = _ctx_last_price(ctx or {}, sym)
        if px is not None:
            quotes[sym] = {"price": px, "source": "last_scan", "as_of": scan_as_of}
    return quotes


def make_dashboard_router(deps: "WebDeps") -> APIRouter:
    # All dashboard + data routes require auth (when a password is configured).
    router = APIRouter(dependencies=[Depends(require_auth)])

    def _orders() -> list:
        store = getattr(deps, "orders", None)
        return store.list_all() if store is not None else []

    @router.get("/api/stats")
    async def api_stats() -> dict:
        positions = deps.positions.list_all()
        orders = _orders()
        decisions = deps.decisions.recent(1000)
        real_only = False
        # Top-level numbers are ALL-TIME (the dashboard's headline P&L). ``this_week`` re-runs the
        # same aggregation windowed to trades resolved since the most recent Monday 00:00 UTC — the
        # market week — so the card can show both without double-counting or a separate code path.
        stats = compute_stats(positions, orders, decisions, real_only=real_only)
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        wk = compute_stats(positions, orders, decisions, since=week_start, real_only=real_only)
        stats["this_week"] = {
            "since": week_start.date().isoformat(),
            "realized_pnl": wk["realized_pnl"],
            "wins": wk["wins"],
            "losses": wk["losses"],
            "resolved_count": wk["resolved_count"],
            "win_rate": wk["win_rate"],
        }
        return stats

    @router.get("/api/positions")
    async def api_positions() -> dict:
        positions = deps.positions.list_all()
        quotes = await load_underlying_quotes(deps, positions)
        rows = position_rows(
            positions, _orders(), deps.decisions.recent(1000),
            real_only=False,
            underlying_quotes=quotes,
        )
        for row in rows:
            # Same dollars the card already shows. Does not recompute P&L.
            row["pnl_pct"] = card_pnl_percent(
                row.get("unrealized_pnl"), row.get("credit_received"), row.get("quantity"),
            )
        return {
            "positions": rows,
            "plan": holding_plan(deps.settings.rules),
            # Live profit-target arm (a fraction). The gauge tick uses this; null when that rule is off.
            "profit_pct": live_profit_pct(deps.settings.rules),
        }

    @router.get("/api/decisions")
    async def api_decisions(limit: int = 100) -> dict:
        ds = deps.decisions.recent(limit)
        return {"decisions": [{
            "created_at": d.created_at.isoformat(),
            "rule_name": d.rule_name,
            "rule_type": d.rule_type.value,
            "reason": d.reason,
            "requires_approval": d.requires_approval,
            "status": d.status.value,
            "position_id": d.position_id,
            "decided_at": d.decided_at.isoformat() if d.decided_at else None,
            **public_pricing_fields(d),
        } for d in ds]}

    @router.get("/api/audit")
    async def api_audit(limit: int = 50) -> dict:
        return {"events": deps.audit.recent(limit)}

    @router.get("/api/candidates")
    async def api_candidates() -> dict:
        sc = deps.scanner
        cands = list(getattr(sc, "last_candidates", []) or []) if sc else []
        ctx = getattr(sc, "last_context", {}) if sc else {}
        scanned = getattr(sc, "last_scan_at", None) if sc else None
        return {
            "scanned_at": scanned.isoformat() if scanned else None,
            "candidates": [{
                "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
                "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta,
                "iv": c.iv, "premium": c.premium, "annualized_ror": c.annualized_ror,
                "theta": c.theta, "gamma": c.gamma, "theta_efficiency": c.theta_efficiency,
                "open_interest": c.open_interest, "volume": c.volume,
                "iv_rank": ctx[c.underlying].iv_rank if c.underlying in ctx else None,
            } for c in cands[:100]],
        }

    @router.get("/api/candidate-log")
    async def api_candidate_log(limit: int = 200) -> dict:
        """Historical scan dispositions: every screened candidate + why it was approved/rejected.
        The negative-example dataset for refining entry logic."""
        store = deps.entry_candidates
        return {"candidates": store.recent(limit) if store is not None else []}

    @router.get("/api/option-oi")
    async def api_option_oi(symbol: str, dte_max: int = 75) -> dict:
        """Read-only option-chain OPEN INTEREST + volume (calls AND puts) for one underlying.

        Sourced via the LIVE Robinhood MCP — Alpaca's option snapshots do not carry open interest.
        Reuses the bot's OWN broker instance so the OAuth session lock is shared (no separate session
        that could race a token refresh). Fail-open: returns available=False when RH isn't the broker
        or the fetch fails, so it never disturbs trading."""
        sym = symbol.upper()
        sc = deps.scanner
        broker = getattr(sc, "broker", None) if sc else None
        if broker is None or not hasattr(broker, "_call_tool"):
            return {"symbol": sym, "available": False,
                    "reason": "Robinhood MCP broker not active (Alpaca carries no OI)."}
        try:
            from datetime import date as _date

            from ..marketdata.robinhood_md import RobinhoodMarketData
            md = RobinhoodMarketData(broker, dte_window_days=dte_max)
            chain = await md.get_chain(sym)
        except Exception as exc:  # noqa: BLE001 — advisory read; never crash the dashboard
            return {"symbol": sym, "available": False, "reason": f"chain fetch failed: {exc}"}
        today = _date.today()
        rows, call_oi, put_oi, call_vol, put_vol = [], 0, 0, 0, 0
        for c in chain:
            ot = str(getattr(c.option_type, "value", c.option_type)).lower()
            oi, vol = (c.open_interest or 0), (c.volume or 0)
            if ot == "call":
                call_oi += oi; call_vol += vol
            elif ot == "put":
                put_oi += oi; put_vol += vol
            rows.append({"type": ot, "strike": c.strike, "expiration": c.expiration.isoformat(),
                         "dte": (c.expiration - today).days, "delta": c.delta, "iv": c.iv,
                         "open_interest": c.open_interest, "volume": c.volume, "mark": c.mark})
        rows.sort(key=lambda r: (r["open_interest"] or 0), reverse=True)
        return {"symbol": sym, "available": True, "count": len(rows),
                "summary": {"call_oi": call_oi, "put_oi": put_oi,
                            "call_put_oi_ratio": round(call_oi / put_oi, 2) if put_oi else None,
                            "call_vol": call_vol, "put_vol": put_vol,
                            "call_put_vol_ratio": round(call_vol / put_vol, 2) if put_vol else None},
                "contracts": rows[:150]}

    @router.post("/api/screen", dependencies=[Depends(require_owner)])
    async def api_screen(body: dict = Body(default={})) -> dict:
        """On-demand CSP screener over an arbitrary universe with adjustable filters. Body:
        {symbols?:[..], delta_min?, delta_max?, dte_min?, dte_max?, min_annualized_yield?,
         min_open_interest?, min_volume?, max_spread_pct?, limit?}. Symbols default to the
        watchlist; filters default to the configured entry criteria. Read-only."""
        sc = deps.scanner
        md = getattr(sc, "market_data", None) if sc is not None else None
        if md is None:
            return {"ok": False, "error": "market data unavailable", "candidates": []}
        from ..services.screening import screen_universe
        syms = body.get("symbols") or list(deps.settings.entry.watchlist)
        syms = [str(s).upper().strip() for s in syms if str(s).strip()][:30]
        over = {k: body[k] for k in (
            "delta_min", "delta_max", "dte_min", "dte_max", "min_annualized_yield",
            "min_open_interest", "min_volume", "max_spread_pct") if body.get(k) is not None}
        try:
            crit = deps.settings.entry.criteria.model_copy(update=over)
        except Exception as exc:  # noqa: BLE001 — bad filter value -> 400-ish, don't crash
            return {"ok": False, "error": f"invalid filter: {exc}", "candidates": []}
        limit = min(int(body.get("limit") or 50), 200)
        cands = await screen_universe(md, syms, crit, limit=limit)
        return {"ok": True, "symbols": syms, "count": len(cands), "candidates": [{
            "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
            "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta, "iv": c.iv,
            "premium": c.premium, "annualized_ror": c.annualized_ror, "theta": c.theta,
            "theta_efficiency": c.theta_efficiency, "open_interest": c.open_interest,
            "volume": c.volume, "break_even": c.break_even,
        } for c in cands]}

    @router.post("/api/opportunities", dependencies=[Depends(require_owner)])
    async def api_opportunities(body: dict = Body(default={})) -> dict:
        """Curated opportunity scan: discover Alpaca's most-active names in a price band, screen
        each for CSPs, rank by theta-efficiency. Body: {price_min?, price_max?, universe_size?,
        max_screen?, limit?, + the same filter overrides as /api/screen}. Read-only."""
        sc = deps.scanner
        md = getattr(sc, "market_data", None) if sc is not None else None
        if md is None:
            return {"ok": False, "error": "market data unavailable", "candidates": []}
        from ..services.screening import opportunity_scan
        over = {k: body[k] for k in (
            "delta_min", "delta_max", "dte_min", "dte_max", "min_annualized_yield",
            "min_open_interest", "min_volume", "max_spread_pct") if body.get(k) is not None}
        try:
            crit = deps.settings.entry.criteria.model_copy(update=over)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"invalid filter: {exc}", "candidates": []}
        res = await opportunity_scan(
            md, crit,
            price_min=float(body.get("price_min", 7)), price_max=float(body.get("price_max", 20)),
            universe_size=min(int(body.get("universe_size") or 100), 300),
            max_screen=min(int(body.get("max_screen") or 25), 40),
            limit=min(int(body.get("limit") or 60), 200))
        prices = res["prices"]
        return {"ok": True, "universe": res["universe"], "scanned": res["scanned"],
                "count": len(res["candidates"]), "candidates": [{
                    "underlying": c.underlying, "price": prices.get(c.underlying),
                    "occ_symbol": c.occ_symbol, "strike": c.strike, "dte": c.dte, "delta": c.delta,
                    "iv": c.iv, "premium": c.premium, "annualized_ror": c.annualized_ror,
                    "theta": c.theta, "theta_efficiency": c.theta_efficiency,
                    "open_interest": c.open_interest, "volume": c.volume, "break_even": c.break_even,
                } for c in res["candidates"]]}

    @router.get("/api/technicals")
    async def api_technicals() -> dict:
        """Per-watchlist-symbol technicals from the last scan (rsi, sma50/200, above_sma200, atr,
        iv_rank, drawdown, days_to_earnings) — the decision inputs, for transparency + gate setup."""
        sc = deps.scanner
        ctx = getattr(sc, "last_context", {}) if sc is not None else {}
        scanned = getattr(sc, "last_scan_at", None) if sc is not None else None
        return {
            "scanned_at": scanned.isoformat() if scanned else None,
            "symbols": {sym: c.as_dict() for sym, c in ctx.items()},
        }

    @router.get("/api/quality")
    async def api_quality() -> dict:
        """Per-watchlist-symbol company-quality readout from the last scan — the 0-100 score plus
        the raw fundamentals behind it (margins, FCF, revenue growth, insider buys, sector).
        INFORMATIONAL ONLY: this is a 'is this name actually profitable/decent?' view and does not
        affect which trades are placed (see entry.quality_scoring). Empty unless scoring is enabled."""
        sc = deps.scanner
        q = getattr(sc, "last_quality", {}) if sc is not None else {}
        scanned = getattr(sc, "last_scan_at", None) if sc is not None else None
        return {
            "enabled": bool(getattr(deps.settings.entry, "quality_scoring", False)),
            "scanned_at": scanned.isoformat() if scanned else None,
            "symbols": q,
        }

    @router.get("/api/alerts")
    async def api_alerts() -> dict:
        """Scan alerts for the list. Status and flags only — no balances or prices paid."""
        return {"alerts": _alert_rows(deps)}

    @router.get("/api/scan-status")
    async def api_scan_status() -> dict:
        from ..services.market_hours import is_market_hours
        sc = deps.scanner
        scanned = getattr(sc, "last_scan_at", None) if sc else None
        return {
            "enabled": deps.settings.entry.enabled,
            "market_open": is_market_hours(),
            "paused": deps.killswitch.is_paused(),
            "last_scan_at": scanned.isoformat() if scanned else None,
            "watchlist": len(deps.settings.entry.watchlist),
            "feed": deps.settings.entry.feed,
            "last_skips": list(getattr(sc, "last_skips", []) or []) if sc else [],
            "last_error": getattr(sc, "last_error", None) if sc else None,
        }

    @router.get("/api/tv-indicators")
    async def api_tv_indicators() -> dict:
        store = deps.tv_indicators
        return {"indicators": store.recent(50) if store is not None else []}

    @router.get("/api/tv-health")
    async def api_tv_health() -> dict:
        from ..services.tv_health import build_tv_health
        return build_tv_health(
            deps.tv_indicators, deps.settings.entry.watchlist,
            deps.settings.ai.tv_indicator_max_age_seconds,
        )

    @router.get("/api/setups")
    async def api_setups() -> dict:
        """Technical setup detections per watchlist name (deterministic daily-bar reads: washout /
        coiling / breakout / breakdown / support tests). Served from the scanner's cache -- no
        market-data calls. Descriptive; the opt-in setup gates and prefer_setups tilt are what
        feed trading."""
        from ..services.setups_view import build_setups_view
        sc = deps.scanner
        return build_setups_view(
            getattr(sc, "last_setups", {}) if sc else {},
            getattr(sc, "last_context", {}) if sc else {},
            getattr(sc, "last_skips", []) if sc else [],
            deps.settings.entry, getattr(sc, "last_scan_at", None) if sc else None,
        )

    @router.get("/api/risk-profile")
    async def api_risk_profile() -> dict:
        """Per-name strike-survival risk profile (computed daily by the scanner from ~1y of bars) and
        the tightening-only per-ticker cushion suggestions derived from it. Read-only."""
        from ..entry.setups import PUT_SELLER_AVOID_PRESET
        from ..services.risk_profile import propose_ticker_cushions
        sc = deps.scanner
        profiles = dict(getattr(sc, "last_risk_profile", {}) or {}) if sc else {}
        e = deps.settings.entry
        proposals = propose_ticker_cushions(profiles, e.per_ticker, base_cushion=e.setups.profile_base_cushion)
        return {
            "config": {"base_cushion": e.setups.profile_base_cushion, "horizon": e.setups.profile_horizon,
                       "target_itm_rate": e.setups.target_itm_rate,
                       "put_seller_avoid_preset": list(PUT_SELLER_AVOID_PRESET)},
            "profiles": [profiles[s] for s in sorted(profiles)],
            "proposals": proposals,
        }

    @router.get("/api/setups/accuracy")
    async def api_setups_accuracy() -> dict:
        """Measured forward outcomes per setup label (the setup-accuracy tracker): n, 5-day hit rate,
        average 5/10-day returns, average 10-day max adverse excursion. Descriptive; small n = weak."""
        store = getattr(deps.scanner, "setup_events", None) if deps.scanner else None
        if store is None:
            return {"available": False, "rows": [], "recent": []}
        try:
            return {"available": True, "rows": store.accuracy(), "recent": store.recent(50)}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "rows": [], "recent": [], "error": str(exc)}

    @router.get("/api/ops")
    async def api_ops() -> dict:
        """One-call operational snapshot: loops, killswitch, sync, scan health, TV freshness."""
        from datetime import datetime, timezone
        from ..domain.enums import AuditEventType
        from ..services.market_hours import is_market_hours
        from ..services.tv_health import build_tv_health

        now = datetime.now(timezone.utc)

        def lag(event_type, source):
            row = deps.audit.latest(event_type, source)
            if not row:
                return {"last_at": None, "lag_seconds": None}
            ts = datetime.fromisoformat(row["ts"])
            return {"last_at": row["ts"], "lag_seconds": round((now - ts).total_seconds(), 1)}

        recon = deps.audit.latest(AuditEventType.RECONCILE, "reconcile")
        sync = None
        if recon:
            p = recon["payload"]
            sync = {
                "broker_open": p.get("broker_open"), "store_open": p.get("store_open"),
                "in_sync": p.get("broker_open") == p.get("store_open"),
                "healed": p.get("entry_decisions_healed", []),
            }
        sc = deps.scanner
        scanned = getattr(sc, "last_scan_at", None) if sc else None
        ks = deps.killswitch
        last_err = deps.audit.latest(AuditEventType.ERROR)
        tvh = build_tv_health(
            deps.tv_indicators, deps.settings.entry.watchlist,
            deps.settings.ai.tv_indicator_max_age_seconds,
        ) if deps.tv_indicators is not None else None
        brk = getattr(sc, "broker", None) if sc is not None else None
        broker_block = None
        if brk is not None:
            from ..brokers.factory import broker_degraded
            caps = brk.capabilities()
            broker_block = {"name": caps.name, "is_paper": caps.is_paper,
                            "degraded": broker_degraded(deps.settings, brk)}
        return {
            "paused": ks.is_paused(), "pause_reason": ks.reason(),
            "broker": broker_block,
            "killswitch": {
                "consecutive_errors": ks.consecutive_errors(),
                "auto_trip_after": deps.settings.auto_trip_after_errors,
            },
            "loops": {
                "monitor_poll": lag(AuditEventType.POLL, "monitor"),
                "reconcile": lag(AuditEventType.RECONCILE, "reconcile"),
                "scanner": lag(AuditEventType.POLL, "scanner"),
            },
            "sync": sync,
            "scan": {
                "enabled": deps.settings.entry.enabled,
                "market_open": is_market_hours(),
                "last_scan_at": scanned.isoformat() if scanned else None,
                "last_error": getattr(sc, "last_error", None) if sc else None,
                "skips": len(getattr(sc, "last_skips", []) or []) if sc else 0,
            },
            "last_error": (
                {"where": last_err["payload"].get("where"),
                 "error": last_err["payload"].get("error"),
                 "root_causes": last_err["payload"].get("root_causes"),
                 "at": last_err["ts"]} if last_err else None
            ),
            "tv_health": tvh,
            "alerts": _alerts_status(deps),
        }

    @router.get("/api/refinement-export")
    async def api_refinement_export(limit: int = 1000, format: str = "json"):
        """Joined feature->outcome table (journal x AI verdict) for offline model tuning.
        ``format=csv`` returns text/csv; default JSON."""
        from ..services.refinement import build_refinement_rows, rows_to_csv
        rows = build_refinement_rows(deps.trade_journal, deps.ai_reviews, limit) \
            if deps.trade_journal is not None else []
        if format == "csv":
            return PlainTextResponse(rows_to_csv(rows), media_type="text/csv")
        return {"count": len(rows), "rows": rows}

    @router.get("/api/news")
    async def api_news(symbol: str | None = None, limit: int = 50) -> dict:
        """Recent stored news/catalyst items (advisory channel: Alpaca pull + webhook push). Pass
        ``?symbol=`` for one name, else the newest across all names. Verifies the feed is flowing."""
        store = deps.news
        if store is None:
            return {"enabled": deps.settings.news.enabled, "items": []}
        items = (store.recent_for(symbol, limit=limit) if symbol else store.recent(limit))
        return {"enabled": deps.settings.news.enabled, "count": len(items), "items": items}

    @router.get("/api/analytics/features")
    async def api_analytics_features(limit: int = 1000) -> dict:
        """Descriptive win-rate + avg realized P&L bucketed by entry feature (ticker, kind, delta,
        DTE, IV, RSI, 200-SMA trend, regime, AI verdict, exit rule) over resolved trades. Read as
        hints — noisy until many trades accumulate. Reuses the refinement (feature->outcome) table."""
        from ..services.analytics import build_feature_analytics
        from ..services.refinement import build_refinement_rows
        rows = build_refinement_rows(deps.trade_journal, deps.ai_reviews, limit) \
            if deps.trade_journal is not None else []
        return build_feature_analytics(rows)

    @router.get("/api/accounts")
    async def api_accounts() -> dict:
        """All Robinhood accounts on the login, with balances (for the advisory calculator).
        READ-ONLY: the bot only TRADES the agentic_allowed account; the rest are advisory."""
        sc = deps.scanner
        broker = getattr(sc, "broker", None) if sc is not None else None
        if broker is None or not hasattr(broker, "list_accounts"):
            return {"ok": False, "error": "broker unavailable", "accounts": []}
        try:
            raw_accts = await broker.list_accounts()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "accounts": []}
        out = []
        for a in raw_accts:
            num = a.get("account_number")
            if not num:
                continue
            try:
                bp = await broker.get_buying_power(num)
                av = await broker.get_account_value(num)
            except Exception:  # noqa: BLE001
                bp = av = 0.0
            out.append({
                "account_number": num,
                "type": a.get("brokerage_account_type") or a.get("type"),
                "nickname": a.get("nickname"),
                "agentic": bool(a.get("agentic_allowed")),
                "option_level": a.get("option_level"),
                "buying_power": round(bp, 2), "account_value": round(av, 2),
            })
        return {"ok": True, "accounts": out}

    @router.get("/api/account-options")
    async def api_account_options(account: str = "all") -> dict:
        """Advisory 'what could I sell' per account: covered calls above cost basis on held shares +
        cash-secured puts sized to buying power. READ-ONLY — suggests only, never places orders.
        ``?account=<num>`` for one account, ``all`` to aggregate across every account."""
        sc = deps.scanner
        broker = getattr(sc, "broker", None) if sc is not None else None
        md = getattr(sc, "market_data", None) if sc is not None else None
        if broker is None or md is None:
            return {"ok": False, "error": "broker/market data unavailable", "accounts": []}
        from ..services.account_options import _advisory_criteria, account_option_suggestions
        from ..services.screening import screen_universe
        try:  # pre-screen watchlist puts once — shared across accounts, differ only by affordability
            csp = await screen_universe(
                md, list(deps.settings.entry.watchlist),
                _advisory_criteria(deps.settings.entry.criteria), limit=60)
        except Exception:  # noqa: BLE001
            csp = []
        if account == "all":
            try:
                nums = [a.get("account_number") for a in await broker.list_accounts()
                        if a.get("account_number")]
            except Exception:  # noqa: BLE001
                nums = []
        else:
            nums = [account]
        results = []
        for num in nums:
            try:
                results.append(await account_option_suggestions(
                    broker, md, deps.settings, num, csp_candidates=csp))
            except Exception as exc:  # noqa: BLE001
                results.append({"account_number": num, "error": str(exc)})
        ok_rows = [r for r in results if "error" not in r]
        tot = lambda key: round(sum(s["weekly_dollars"] for r in ok_rows for s in r.get(key, [])), 2)
        return {"ok": True, "accounts": results,
                "total_cc_weekly": tot("covered_calls"),
                "total_csp_weekly": tot("cash_secured_puts"),
                "total_weekly_target": round(sum(r.get("weekly_target", 0) for r in ok_rows), 2)}

    @router.get("/api/regime")
    async def api_regime() -> dict:
        sc = deps.scanner
        reg = getattr(sc, "last_regime", None) if sc else None
        return {
            "enabled": deps.settings.macro.enabled,
            "hard_gate": deps.settings.macro.hard_gate,
            "skip_confirmed_downtrend": deps.settings.macro.skip_confirmed_downtrend,
            "downtrend_confirm_days": deps.settings.macro.downtrend_confirm_days,
            "regime": reg.as_dict() if reg is not None else None,
        }

    @router.get("/api/brief")
    async def api_brief() -> dict:
        """On-demand weekly tactical prep brief (markdown): market backdrop, this week's catalysts,
        per-watchlist levels + the bot's mechanical rule-based strike targets, and advisory CC/CSP
        ideas across every account. DESCRIPTIVE ONLY — not financial advice. Read-only; hits the broker
        for cross-account holdings, so it is deliberately not part of the auto-refresh poll."""
        from datetime import datetime, timezone

        from ..marketdata.econ_calendar import load_econ_calendar, upcoming_events
        from ..services.weekly_brief import build_weekly_brief

        s, sc = deps.settings, deps.scanner
        now = datetime.now(timezone.utc)
        watchlist = list(s.entry.watchlist)
        try:
            ctxs = getattr(sc, "last_context", {}) if sc else {}
            contexts = {sym: ctxs[sym].as_dict() for sym in watchlist if sym in ctxs}
            cands = list(getattr(sc, "last_candidates", []) or []) if sc else []
            candidates = [{"underlying": c.underlying, "strike": c.strike, "delta": c.delta,
                           "dte": c.dte, "premium": c.premium, "iv": c.iv,
                           "annualized_ror": c.annualized_ror, "break_even": c.break_even,
                           "theta_efficiency": c.theta_efficiency, "open_interest": c.open_interest}
                          for c in cands]

            tv_by_symbol: dict = {}
            if deps.tv_indicators is not None:
                max_age = s.ai.tv_indicator_max_age_seconds
                for sym in watchlist:
                    snap = deps.tv_indicators.get_latest(sym, max_age)
                    tv_by_symbol[sym] = (snap or {}).get("payload", {}) if snap else {}

            reg = getattr(sc, "last_regime", None) if sc else None
            regime = reg.as_dict() if reg is not None else None

            news_by_symbol: dict = {}
            news_store = getattr(deps, "news", None)
            news_cfg = getattr(s, "news", None)
            if news_store is not None and getattr(news_cfg, "enabled", False):
                for sym in watchlist:
                    try:
                        items = news_store.recent_for(
                            sym, max_age_seconds=news_cfg.max_age_hours * 3600, limit=1)
                        if items:
                            news_by_symbol[sym] = items[0].get("headline") or ""
                    except Exception:  # noqa: BLE001
                        pass

            accounts: list = []
            broker = getattr(sc, "broker", None) if sc else None
            md = getattr(sc, "market_data", None) if sc else None
            if broker is not None and md is not None:
                from ..services.account_options import _advisory_criteria, account_option_suggestions
                from ..services.screening import screen_universe
                try:
                    csp = await screen_universe(md, watchlist, _advisory_criteria(s.entry.criteria), limit=60)
                except Exception:  # noqa: BLE001
                    csp = []
                try:
                    nums = [a.get("account_number") for a in await broker.list_accounts()
                            if a.get("account_number")]
                except Exception:  # noqa: BLE001
                    nums = []
                for num in nums:
                    try:
                        accounts.append(await account_option_suggestions(broker, md, s, num, csp_candidates=csp))
                    except Exception as exc:  # noqa: BLE001
                        accounts.append({"account_number": num, "error": str(exc)})

            econ_events = upcoming_events(load_econ_calendar(), now.date(), 7)

            # Rich context for the AI tactical read — all in-process, each fail-open.
            skips = list(getattr(sc, "last_skips", []) or []) if sc else []
            try:
                from ..services.analytics import build_feature_analytics
                from ..services.refinement import build_refinement_rows
                flywheel = build_feature_analytics(
                    build_refinement_rows(deps.trade_journal, deps.ai_reviews, 1000)
                ) if deps.trade_journal is not None else None
            except Exception:  # noqa: BLE001
                flywheel = None
            try:
                pos_all = deps.positions.list_all()  # fetched once, shared by stats + exposure
            except Exception:  # noqa: BLE001
                pos_all = []
            try:
                from ..services.stats import compute_stats
                stats = compute_stats(pos_all, _orders(),
                                      deps.decisions.recent(1000), real_only=False)
            except Exception:  # noqa: BLE001
                stats = None
            try:
                acct_no = getattr(s.robinhood, "account_number", None)
                acct_val = next((a.get("account_value") for a in accounts
                                 if a.get("account_number") == acct_no), None)
                if acct_val is None:
                    acct_val = sum(a.get("account_value", 0) or 0 for a in accounts if "error" not in a) or None
                risk = {"sector_exposure": _sector_exposure(pos_all, s.risk, acct_val)}
            except Exception:  # noqa: BLE001
                risk = None
            try:
                ai_reviews = (deps.ai_reviews.recent(8) if deps.ai_reviews is not None else []) or []
            except Exception:  # noqa: BLE001
                ai_reviews = []
            try:  # measured setup outcomes on these names (setup-accuracy tracker); advisory
                _se = getattr(sc, "setup_events", None) if sc else None
                setup_accuracy = _se.accuracy() if _se is not None else None
            except Exception:  # noqa: BLE001
                setup_accuracy = None
            risk_profiles = dict(getattr(sc, "last_risk_profile", {}) or {}) if sc else None

            # Open short options (for the management + assignment-capacity reads) and total buying
            # power across accounts. Drop paper positions in live mode.
            try:
                open_positions = [p for p in pos_all
                                  if str(getattr(getattr(p, "status", None), "value",
                                                 getattr(p, "status", None))).upper() in ("OPEN", "CLOSING")
                                  ]
            except Exception:  # noqa: BLE001
                open_positions = []
            total_bp = sum(a.get("buying_power", 0) or 0
                           for a in accounts if "error" not in a) or None

            ai_analysis = None  # optional Claude tactical synthesis; fail-open to deterministic brief
            try:
                from ..ai.brief_analysis import generate_brief_analysis
                from ..ai.client import build_reviewer_client
                client = build_reviewer_client(s.ai)
                if client is not None:
                    ai_analysis = await generate_brief_analysis(
                        client, watchlist=watchlist, contexts=contexts, candidates=candidates,
                        tv_by_symbol=tv_by_symbol, regime=regime, econ_events=econ_events,
                        flywheel=flywheel, skips=skips, accounts=accounts, stats=stats,
                        risk=risk, ai_reviews=ai_reviews, open_positions=open_positions,
                        total_buying_power=total_bp, setup_accuracy=setup_accuracy,
                        risk_profiles=risk_profiles)
            except Exception:  # noqa: BLE001 — AI is advisory; the brief renders without it
                ai_analysis = None

            title, body = build_weekly_brief(
                watchlist=watchlist, contexts=contexts, candidates=candidates,
                tv_by_symbol=tv_by_symbol, regime=regime, news_by_symbol=news_by_symbol,
                accounts=accounts, econ_events=econ_events, now=now, ai_analysis=ai_analysis,
                open_positions=open_positions, total_buying_power=total_bp,
                tier_proposals=(list(getattr(sc, "last_tier_proposals", []) or []) if sc else []))
            out = {"title": title, "body": body, "created_at": now.isoformat(),
                   "has_ai": ai_analysis is not None, "id": None}
            # Persist every generation so the brief can be re-read from a phone later without
            # regenerating (which hits the broker + the AI). Advisory: a save failure never
            # hides the brief that was just built.
            try:
                if deps.briefs is not None:
                    out["id"] = deps.briefs.save(
                        title, body, has_ai=ai_analysis is not None, created_at=now,
                        meta={"watchlist": len(watchlist), "open_positions": len(open_positions),
                              "accounts": len([a for a in accounts if "error" not in a])})
            except Exception:  # noqa: BLE001
                out["id"] = None
            return out
        except Exception as exc:  # noqa: BLE001 — a brief must never 500 the dashboard
            return {"title": "Weekly tactical brief", "body": f"Brief unavailable: {exc}",
                    "created_at": now.isoformat(), "has_ai": False, "id": None}

    @router.get("/api/briefs")
    async def api_briefs(limit: int = 30) -> dict:
        """Saved briefs, newest first (no bodies). ``latest`` carries the newest full brief so the
        Brief tab opens on it without a regeneration."""
        store = deps.briefs
        if store is None:
            return {"available": False, "briefs": [], "latest": None}
        rows = store.recent(max(1, min(int(limit), 200)))
        return {"available": True, "briefs": rows, "latest": store.latest()}

    @router.get("/api/briefs/{brief_id}")
    async def api_brief_one(brief_id: str) -> dict:
        store = deps.briefs
        row = store.get(brief_id) if store is not None else None
        if row is None:
            raise HTTPException(status_code=404, detail="brief not found")
        return row

    @router.get("/api/ai-reviews")
    async def api_ai_reviews(limit: int = 100) -> dict:
        store = deps.ai_reviews
        return {
            "enabled": deps.settings.ai.enabled,
            "mode": deps.settings.ai.mode,
            "model": deps.settings.ai.model,
            "reviews": store.recent(limit) if store is not None else [],
        }

    @router.get("/api/cc-candidates")
    async def api_cc_candidates() -> dict:
        sc = deps.scanner
        cands = list(getattr(sc, "last_cc_candidates", []) or []) if sc else []
        return {"candidates": [{
            "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
            "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta,
            "premium": c.premium, "annualized_ror": c.annualized_ror,
        } for c in cands[:100]]}

    @router.get("/api/holdings")
    async def api_holdings() -> dict:
        sc = deps.scanner
        holds = list(getattr(sc, "last_holdings", []) or []) if sc else []
        from ..domain.enums import Direction, OptionType
        clock = dict(getattr(sc, "last_cc_clock", {}) or {}) if sc else {}
        covered: dict[str, int] = {}
        for p in deps.positions.list_open():
            if p.direction is Direction.SHORT and p.option_type is OptionType.CALL:
                covered[p.underlying] = covered.get(p.underlying, 0) + p.quantity
        return {"holdings": [{
            "symbol": h.symbol, "shares": h.quantity, "average_cost": h.average_cost,
            "coverable": int(h.quantity // 100), "covered": covered.get(h.symbol, 0),
            "clock": clock.get(h.symbol),
        } for h in holds]}

    @router.get("/api/tiers")
    async def api_tiers() -> dict:
        """Quality names the account can now afford (proposal only; the user adds with one tap)."""
        from ..services.tiers import next_unlock
        sc = deps.scanner
        e = deps.settings.entry
        av = getattr(sc, "last_account_value", None) if sc else None
        prices = dict(getattr(sc, "_tier_prices", {}) or {}) if sc else {}
        return {"ready": list(getattr(sc, "last_tier_proposals", []) or []) if sc else [],
                "next": next_unlock(e.watchlist_tiers, e.watchlist, av, e.sizing.max_pct_per_underlying, prices),
                "account_value": av, "per_name_pct": e.sizing.max_pct_per_underlying,
                "tiers": {k: {"min_collateral": v.get("min_collateral"), "note": v.get("note", "")}
                          for k, v in (e.watchlist_tiers or {}).items()}}

    @router.get("/api/journal")
    async def api_journal(limit: int = 200) -> dict:
        store = deps.trade_journal
        rows = store.recent(limit) if store else []
        return {"trades": [{
            "entered_at": j.entered_at.isoformat(), "kind": j.kind, "underlying": j.underlying,
            "occ_symbol": j.occ_symbol, "strike": j.strike, "dte": j.dte, "delta": j.delta,
            "iv": j.iv, "premium": j.premium, "annualized_ror": j.annualized_ror,
            "status": j.status, "realized_pnl": j.realized_pnl, "days_held": j.days_held,
            "exit_reason": j.exit_reason,
            **public_pricing_fields(j),
            **public_close_pricing_fields(j),
        } for j in rows]}

    @router.get("/api/entry-decisions")
    async def api_entry_decisions(limit: int = 100) -> dict:
        store = deps.entry_decisions
        ds = store.recent(limit) if store else []
        return {"entries": [{
            "id": d.id,
            "created_at": d.created_at.isoformat(), "underlying": d.underlying,
            "occ_symbol": d.occ_symbol, "strike": d.strike,
            "expiration": d.expiration.isoformat(), "contracts": d.contracts,
            "premium": d.premium, "status": d.status.value, "reason": d.reason,
            **public_pricing_fields(d),
        } for d in ds]}

    @router.get("/", response_class=HTMLResponse)
    async def index(role: str = Depends(require_auth)) -> str:
        return _page_for(role, deps)

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(role: str = Depends(require_auth)) -> str:
        return _page_for(role, deps)

    @router.get("/calculator", response_class=HTMLResponse)
    async def calculator() -> str:
        return CALC_PAGE

    @router.get("/api/rules")
    async def api_rules() -> dict:
        from .rules_view import describe_active_rules
        from ..notify.trade_alerts import alerts_from_deps
        al = alerts_from_deps(deps)
        return {"rules": describe_active_rules(
            deps.settings, alerts_mode=al.mode(),
            webhook_configured=al.webhook_configured(),
        )}

    @router.get("/rules", response_class=HTMLResponse)
    async def rules_page() -> str:
        return _RULES_PAGE

    return router


def _page_for(role: str, deps: "WebDeps") -> str:
    shown = role if role in ("owner", "viewer") else "viewer"
    return (
        _PAGE.replace("__TB_ROLE__", shown)
        .replace("__HEALTH_INNER__", _health_inner(deps))
        .replace("__ALERT_ROWS__", _alert_rows_html(deps))
        .replace("__PAUSE_BTN__", _pause_button(deps))
        .replace("__QUOTE_MAX_AGE__", str(int(getattr(deps.settings, "max_quote_age_seconds", 90) or 90)))
    )


def _esc(value: object) -> str:
    return (
        str("" if value is None else value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _alert_store(deps: "WebDeps"):
    return getattr(getattr(deps, "scanner", None), "alerts", None)


def _alert_rows(deps: "WebDeps") -> list[dict]:
    store = _alert_store(deps)
    if store is None:
        return []
    out = []
    for row in store.all():
        out.append({
            "kind": row.get("kind"),
            "underlying": row.get("underlying"),
            "occ_symbol": row.get("occ_symbol"),
            "strike": row.get("strike"),
            "expiration": row.get("expiration"),
            "contracts": row.get("contracts"),
            "status": row.get("status") or "new",
            "late": bool(row.get("late")),
            "partial": bool(row.get("partial")),
            "created_at": row.get("created_at"),
        })
    return out


def _status_chips(row: dict) -> str:
    status = str(row.get("status") or "new")
    klass = {"taken": "good", "stale": "warn", "expired": "warn"}.get(status, "")
    parts = [
        f'<span class="chip {klass}" data-status="{_esc(status)}">{_esc(status)}</span>'
    ]
    if row.get("late"):
        parts.append('<span class="chip warn" data-flag="late">late</span>')
    if row.get("partial"):
        parts.append('<span class="chip warn" data-flag="partial">partial</span>')
    return " ".join(parts)


def _alert_rows_html(deps: "WebDeps") -> str:
    rows = _alert_rows(deps)
    if not rows:
        return '<tr><td colspan="6" class="muted">No alerts yet.</td></tr>'
    html = []
    for row in rows:
        html.append(
            "<tr>"
            f"<td>{_esc(row['underlying'])}</td>"
            f"<td>{_esc(row['kind'])}</td>"
            f"<td class=\"num\">{_esc(row['strike'])}</td>"
            f"<td>{_esc(row['expiration'])}</td>"
            f"<td class=\"num\">{_esc(row['contracts'])}</td>"
            f"<td>{_status_chips(row)}</td>"
            "</tr>"
        )
    return "".join(html)


def _health_inner(deps: "WebDeps") -> str:
    from .health import build_health
    body = build_health(deps)
    scanner = body.get("scanner") or {}
    up = bool(scanner.get("up"))
    age = body.get("feed_age_seconds")
    locked = bool(body.get("orders_hard_disabled"))
    paused = bool(body.get("paused"))
    scan_klass = "" if up else "bad"
    scan_chip = "good" if up else "warn"
    scan_label = "Scanner up" if up else "Scanner down"
    if age is None:
        feed_label, feed_chip, feed_attr = "Feed unknown", "warn", ""
    else:
        feed_label = f"Feed {int(round(float(age)))}s"
        limit = int(getattr(deps.settings, "max_quote_age_seconds", 90) or 90)
        feed_chip = "warn" if float(age) > limit else "good"
        feed_attr = _esc(age)
    lock_label = "Orders locked" if locked else "Orders not locked"
    lock_chip = "good" if locked else "warn"
    pause_label = "Paused" if paused else "Running"
    pause_chip = "warn" if paused else ""
    return (
        f'<span class="conn {scan_klass}"><span class="dot"></span> {_esc(scan_label)}</span>'
        '<div class="chips">'
        f'<span class="chip {scan_chip}" id="hb-scanner" data-scanner="{"up" if up else "down"}">{_esc(scan_label)}</span>'
        f'<span class="chip {feed_chip}" id="hb-feed" data-feed-age="{feed_attr}">{_esc(feed_label)}</span>'
        f'<span class="chip {lock_chip}" id="hb-lock" data-lock="{"on" if locked else "off"}">{_esc(lock_label)}</span>'
        f'<span class="chip {pause_chip}" id="hb-pause" data-paused="{"1" if paused else "0"}">{_esc(pause_label)}</span>'
        "</div>"
    )


def _pause_button(deps: "WebDeps") -> str:
    paused = bool(deps.killswitch.is_paused())
    label = "Resume" if paused else "Pause"
    flag = "1" if paused else "0"
    return (
        f'<button class="go" type="button" id="pause-btn" data-paused="{flag}">'
        f"{_esc(label)}</button>"
    )


# --- self-contained page (vanilla JS, no dependencies) -----------------------------------
# dashboard.html is the former inline string, loaded once at import.
_PAGE = files("agentic.web").joinpath("dashboard.html").read_text(encoding="utf-8")


_RULES_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Rules — ThetaBot</title>
<style>
  :root{--bg:#0f1419;--card:#1a222b;--ink:#e8eef4;--muted:#8b9aab;--line:#2a3542;--pos:#3dd68c}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
  .wrap{max-width:820px;margin:0 auto;padding:24px 18px 48px}
  h1{font-size:22px;margin:0 0 6px} .sub{color:var(--muted);margin:0 0 22px}
  a{color:#8ec8ff} .group{margin:22px 0 0;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
  .row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
  .name{font-weight:600} .val{color:var(--pos);margin-top:2px} .detail{color:var(--muted);font-size:13.5px;margin-top:4px}
</style>
</head>
<body>
<div class="wrap">
  <p><a href="/dashboard">&larr; Dashboard</a></p>
  <h1>Active trading rules</h1>
  <p class="sub">Generated from the same config the engine is running. This page is read-only.</p>
  <div id="list">Loading…</div>
</div>
<script>
function esc(s){ return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c])); }
(async () => {
  const r = await fetch("/api/rules", {credentials:"same-origin"});
  const d = await r.json();
  const list = document.getElementById("list");
  let html = "", group = "";
  for (const row of (d.rules || [])) {
    if (row.group !== group) { group = row.group; html += `<div class="group">${esc(group)}</div>`; }
    html += `<div class="row"><div class="name">${esc(row.name)}</div><div class="val">${esc(row.value)}</div>`
         + (row.detail ? `<div class="detail">${esc(row.detail)}</div>` : "") + `</div>`;
  }
  list.innerHTML = html || "<p>No rules loaded.</p>";
})();
</script>
</body>
</html>
"""
