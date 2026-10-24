"""OpportunityScanner: the entry-side loop (universe-centric counterpart to MonitorLoop).

Each cycle (market-hours only, when entry.enabled): scan the watchlist for option chains,
screen for CSP candidates, run the risk/sizing layer against live buying power + open
positions, then record one alert per new play and send it on the webhook.

The order path (``_submit``, the executor, the weekly throttle, approval parking) is still
here. The scan does not call it.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta

from ..config import Settings
from ..brokers.base import ExecutionBroker
from ..brokers.factory import broker_degraded
from ..errors import describe_exception
from ..domain.enums import AuditEventType, DecisionStatus, OrderStatus
from ..domain.models import EntryDecision, TradeJournalEntry, utcnow
from ..domain.order_pricing import pricing_kwargs
from ..marketdata.base import MarketDataProvider
from ..marketdata.quote import OptionContractQuote
from ..store.alerts import AlertStore
from ..store.audit import AuditStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.trade_journal import TradeJournalStore
from .alert_score import (
    as_utc, clears_anytime, exit_due, expiry_at, parse_time, play_pnl, profit_fraction,
)
from .risk_breaker import apply_sector_cap, evaluate_risk_breaker
from .executor import OrderExecutor
from .killswitch import KillSwitch
from .market_hours import is_market_hours, is_order_window
from ..entry.context import UnderlyingContext, build_context, passes_underlying_gates
from ..entry.regime import MarketRegime, build_market_regime, classify_move
from ..entry.risk import RiskSizer
from ..entry.screener import EntryCandidate, passes_candidate_gates, screen_candidates
from ..entry.setups import (
    MIN_BARS, compose, detect_flags, detect_setups, merge_tv, parse_tv_setups, primary_setup,
    setup_bias, setup_score, setup_sort_key,
)
from ..marketdata.earnings import NullEarningsProvider, earnings_blackout
from ..marketdata.company_data import NullCompanyDataProvider
from ..scoring.quality import quality_breakdown

log = logging.getLogger("agentic.scanner")


def iv_rank_sort_key(candidate, context_by_underlying) -> tuple[float, float]:
    """Ranking key for ``prefer_iv_rank``: prefer richer premium (higher underlying IV rank), with
    theta-efficiency as the tiebreak. Unknown IV rank (too little history) maps to 50 — neutral, so
    names still accumulating history are never penalized. Sort descending on this tuple."""
    ctx = context_by_underlying.get(candidate.underlying)
    ivr = ctx.iv_rank if (ctx is not None and ctx.iv_rank is not None) else 50.0
    return (float(ivr), float(candidate.theta_efficiency))


def quality_sort_key(candidate, context_by_underlying, prefer_iv_rank: bool = False) -> tuple:
    """Ranking key for ``prefer_quality``: prefer higher-quality names, but only in COARSE tiers
    (rounded to ~10 points) so premium richness still orders names of similar quality. Unknown
    quality maps to 50 (neutral). Secondary key is IV rank (if ``prefer_iv_rank``) then
    theta-efficiency, else theta-efficiency. Sort descending on this tuple."""
    ctx = context_by_underlying.get(candidate.underlying)
    q = ctx.quality_score if (ctx is not None and ctx.quality_score is not None) else 50.0
    q_tier = round(q / 10.0)
    if prefer_iv_rank:
        ivr = ctx.iv_rank if (ctx is not None and ctx.iv_rank is not None) else 50.0
        return (float(q_tier), float(ivr), float(candidate.theta_efficiency))
    return (float(q_tier), float(candidate.theta_efficiency), 0.0)


def _broker_average(position) -> float | None:
    """Broker average price per share. Falls back to the credit the mapper stored."""
    avg = getattr(position, "open_avg_price", None)
    if avg is not None:
        return float(avg)
    credit = getattr(position, "credit_received", None)
    if credit is None:
        return None
    return float(credit)


def _is_short(position) -> bool:
    direction = getattr(position, "direction", None)
    if direction is None:
        return True
    return str(getattr(direction, "value", direction)).upper() != "LONG"


def _is_short_open(position) -> bool:
    if not _is_short(position):
        return False
    try:
        qty = int(position.quantity)
    except (TypeError, ValueError):
        return False
    if qty <= 0:
        return False
    status = getattr(position, "status", None)
    if status is None:
        return True
    return str(getattr(status, "value", status)).upper() in ("OPEN", "CLOSING")


def _option_name(position) -> str:
    opt = getattr(position, "option_type", "")
    return str(getattr(opt, "value", opt)).upper()


def _entry_match(alert: dict, positions):
    """Open short with the same OCC and the same side as the CSP or CC alert."""
    kind = alert["kind"]
    occ = alert["occ_symbol"]
    for position in positions or []:
        if getattr(position, "occ_symbol", None) != occ or not _is_short_open(position):
            continue
        name = _option_name(position)
        if kind == "CSP" and not name.startswith("P"):
            continue
        if kind == "CC" and not name.startswith("C"):
            continue
        return position
    return None


class OpportunityScanner:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        market_data: MarketDataProvider,
        entry_decisions: EntryDecisionStore,
        executor: OrderExecutor,
        audit: AuditStore,
        killswitch: KillSwitch,
        trade_journal: TradeJournalStore | None = None,
        ai_reviewer=None,           # ai.reviewer.AIReviewer | None (loose to avoid an import cycle)
        tv_indicators=None,         # store.tv_indicators.TVIndicatorStore | None
        ai_reviews=None,            # store.ai_reviews.AIReviewStore | None
        earnings=None,              # marketdata.earnings.EarningsProvider | None
        company_data=None,          # marketdata.company_data.CompanyDataProvider | None
        entry_candidates=None,      # store.entry_candidates.EntryCandidateStore | None
        news_provider=None,         # marketdata.news.NewsProvider | None (pull)
        news=None,                  # store.news.NewsStore | None
        setup_events=None,          # store.setup_events.SetupEventStore | None (setup-accuracy tracker)
        alerts=None,                # store.alerts.AlertStore | None
        trade_alerts=None,          # notify.trade_alerts.TradeAlerts | None
        read_broker=None,           # account reads (buying power, options, shares)
    ):
        self.setup_events = setup_events
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.entry_decisions = entry_decisions
        self.executor = executor
        self.audit = audit
        self.killswitch = killswitch
        self.trade_journal = trade_journal
        self.ai_reviewer = ai_reviewer
        self.tv_indicators = tv_indicators
        self.ai_reviews = ai_reviews
        self.entry_candidates = entry_candidates
        self.earnings = earnings or NullEarningsProvider()
        self.company_data = company_data or NullCompanyDataProvider()
        self.news_provider = news_provider
        self.news = news
        self.alerts = alerts if alerts is not None else AlertStore(audit.db)
        self.trade_alerts = trade_alerts
        # Robinhood session for account reads when market_data is robinhood.
        # None on the paper-data path: those scans keep reading ``broker``.
        self.read_broker = read_broker
        self.sizer = RiskSizer(settings.entry.sizing)
        self._stop = asyncio.Event()
        # Latest scan snapshot for the dashboard.
        self.last_candidates: list[EntryCandidate] = []       # CSP candidates
        self.last_cc_candidates: list[EntryCandidate] = []    # covered-call candidates
        self.last_holdings: list = []                         # EquityHolding snapshot
        self.last_context: dict[str, UnderlyingContext] = {}  # per-symbol technicals/IV-rank
        self.last_quality: dict[str, dict] = {}               # per-symbol company-quality readout (informational)
        self.last_skips: list[dict] = []                      # names skipped by underlying gates
        self.last_error: str | None = None
        self.last_scan_at = None
        self.last_regime: MarketRegime | None = None          # market-wide regime snapshot
        self.last_breaker: dict | None = None                 # loss circuit breaker state (freezes new entries)
        self.last_setups: dict[str, dict] = {}                # per-symbol technical setup read (for /api/setups)
        self.last_risk_profile: dict[str, dict] = {}          # per-symbol strike-survival profile (daily)
        self.last_reserve: dict | None = None                 # tax-reserve holding (caps only)
        self.last_csp_approved: list = []                     # sized CSPs from the latest scan
        self.last_cc_approved: list = []                      # sized CCs from the latest scan
        self.last_quotes: dict = {}                           # occ -> quote from the latest scan
        self.last_tier_proposals: list[dict] = []             # quality names the account can now afford
        self.last_cc_clock: dict[str, dict] = {}              # per held name: days since assignment, below-basis allowed
        self.tax_reserve_store = None                         # set by main.py (ledger; for the dashboard)
        self._tiers_computed_on: str | None = None
        self._tier_prices: dict[str, float] = {}
        self.last_account_value: float | None = None

    async def _reserve_value(self, holdings, reserve: set[str]) -> float:
        """Market value of the tax-reserve holding (0 when none).

        The price read is best-effort and falls back to cost basis. The caller
        subtracts this from account value, which is the base for the per-name
        and portfolio caps. Buying power passed to the sizer is the broker
        figure, and this value is not taken out of it.
        """
        held = [h for h in holdings if h.symbol.upper() in reserve]
        if not held:
            self.last_reserve = None
            return 0.0
        h = held[0]
        price = None
        try:
            price = await self.market_data.get_underlying_price(h.symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("reserve price read failed for %s: %s", h.symbol, exc)
        px = float(price) if price else float(h.average_cost or 0.0)
        value = round(px * float(h.quantity), 2)
        self.last_reserve = {"symbol": h.symbol, "shares": float(h.quantity), "price": px,
                             "avg_cost": float(h.average_cost or 0.0), "value": value,
                             "as_of": utcnow().isoformat()}
        return value

    async def _refresh_tiers(self, account_value: float) -> None:
        """Once a day: which vetted quality names now fit under the per-name cap (proposal only)."""
        cfg = self.settings.entry
        if not cfg.watchlist_tiers:
            self.last_tier_proposals = []
            return
        today = utcnow().date().isoformat()
        if self._tiers_computed_on == today:
            return
        from .tiers import ready_to_add
        prices: dict[str, float] = {}
        for sym in cfg.watchlist_tiers:
            if sym.upper() in {w.upper() for w in cfg.watchlist}:
                continue
            try:
                p = await self.market_data.get_underlying_price(sym)
                if p:
                    prices[sym.upper()] = float(p)
            except Exception as exc:  # noqa: BLE001
                log.debug("tier price read failed for %s: %s", sym, exc)
        self._tier_prices = prices
        s = cfg.sizing
        self.last_tier_proposals = ready_to_add(
            cfg.watchlist_tiers, cfg.watchlist, account_value, s.max_pct_per_underlying, prices,
            backstop_pct=s.max_position_size_pct)
        self._tiers_computed_on = today
        if self.last_tier_proposals:
            log.info("Capital unlocks: %s now fit under the per-name cap (proposal only).",
                     ", ".join(r["symbol"] for r in self.last_tier_proposals))

    async def _compute_regime(self) -> MarketRegime | None:
        """Best-effort market-regime snapshot from index-ETF daily bars. Never raises (advisory)."""
        cfg = self.settings.macro
        syms = cfg.symbols or ["SPY", "QQQ"]
        spy_sym = syms[0]
        qqq_sym = syms[1] if len(syms) > 1 else syms[0]
        try:
            spy_bars = await self.market_data.get_underlying_bars(spy_sym, cfg.lookback_days)
            qqq_bars = await self.market_data.get_underlying_bars(qqq_sym, cfg.lookback_days)
        except Exception as exc:  # noqa: BLE001 — regime is advisory; a data gap must not break the scan
            log.warning("Regime data fetch failed: %s", exc)
            return None
        vix = None
        try:
            if hasattr(self.broker, "get_index_quote"):
                vix = await self.broker.get_index_quote("VIX")
        except Exception as exc:  # noqa: BLE001 — VIX is advisory
            log.warning("VIX fetch failed: %s", exc)
        return build_market_regime(spy_bars or [], qqq_bars or [], cfg, vix=vix)

    async def run(self) -> None:
        if not self.settings.entry.enabled:
            log.info("Entry scanner disabled (entry.enabled=false); not starting.")
            return
        log.info("Opportunity scanner started (watchlist=%d names, feed=%s).",
                 len(self.settings.entry.watchlist), self.settings.entry.feed)
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.last_error = None
                self.killswitch.record_success()
            except Exception as exc:  # noqa: BLE001 — a bad scan must not kill the loop
                self.last_error = str(exc)
                log.exception("Scanner cycle error: %s", exc)
                self.audit.record(
                    AuditEventType.ERROR,
                    {"where": "scanner", **describe_exception(exc)},
                )
                self.killswitch.record_broker_error("scanner")
            delay = self.settings.entry.scan_interval_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        """One scan cycle: a CSP pass (watchlist) + a CC pass (shares held). Returns new alerts."""
        cfg = self.settings.entry
        if not cfg.enabled:
            return 0
        # Pause does not skip the scan. It mutes the webhook in _emit_play.
        # Rows stay pending and the next unpaused scan sends each one once.
        if not is_market_hours():
            return 0
        if not is_order_window(start=self.settings.trading_start):
            return 0
        if self._broker_down():
            return self._skip_scan("broker degraded")

        # Account state for sizing. Reads only — this cycle does not place an order.
        # With market_data robinhood these hit that session's read methods.
        book = self._account_broker()
        buying_power = await book.get_buying_power()
        account_value = await book.get_account_value()

        # Loss circuit breaker: freeze NEW entries when realized losses pile up. Does NOT close
        # anything — the monitor keeps managing/closing existing positions. Evaluated fresh each
        # cycle (self-clearing as losing trades roll out of the window).
        breaker = evaluate_risk_breaker(self.trade_journal, self.settings.risk, account_value, utcnow())
        self.last_breaker = breaker
        if breaker.get("tripped"):
            log.warning("Loss circuit breaker ENGAGED — freezing new entries: %s", breaker["reason"])
            return 0

        open_positions = await book.get_open_positions()
        holdings = await book.get_equity_positions()
        self.last_holdings = holdings

        # The tax-reserve ETF is not written against. Its market value comes out of
        # account value (the cap base) and stays inside buying power. CSP size uses
        # the broker buying-power figure.
        from .holdings import reserve_symbols, tradable_holdings
        reserve = reserve_symbols(self.settings)
        holdings_tradable = tradable_holdings(holdings, reserve)
        reserve_val = await self._reserve_value(holdings, reserve)
        account_value = max(0.0, account_value - reserve_val)
        self.last_account_value = account_value
        await self._refresh_tiers(account_value)

        # Market-regime read (systemic-vs-idiosyncratic context for the AI reviewer / optional gate).
        if self.settings.macro.enabled:
            self.last_regime = await self._compute_regime()

        # Chain cache so a symbol in both the watchlist and holdings is fetched once.
        chain_cache: dict[str, list[OptionContractQuote]] = {}
        quote_by_occ: dict[str, OptionContractQuote] = {}

        async def chain_for(symbol: str) -> list[OptionContractQuote]:
            if symbol not in chain_cache:
                ch = await self.market_data.get_chain(symbol)
                chain_cache[symbol] = ch
                for c in ch:
                    if c.occ_symbol:
                        quote_by_occ[c.occ_symbol] = c
            return chain_cache[symbol]

        context_by_underlying: dict[str, UnderlyingContext] = {}
        self.last_quality = {}  # rebuilt this scan by _overlay_quality (informational readout)
        skips: list[dict] = []

        # CSP pass — watchlist puts, per-ticker criteria + underlying gates (entry intelligence).
        csp_cands: list[EntryCandidate] = []
        today = utcnow().date()
        await self._refresh_news(cfg.watchlist)
        for underlying in cfg.watchlist:
            crit = cfg.criteria_for(underlying, cfg.criteria)
            chain = await chain_for(underlying)
            ctx = await self._context_for(underlying, chain, crit)
            self._enrich_ctx_from_tv(underlying, ctx)
            ctx.recent_news_count = len(self._recent_news(underlying))
            earn_date = await self.earnings.next_earnings(underlying)
            if earn_date is not None:
                ctx.days_to_earnings = (earn_date - today).days
            context_by_underlying[underlying] = ctx
            reason = passes_underlying_gates(ctx, crit)
            if reason is None and cfg.earnings_gate and earnings_blackout(
                    earn_date, today, crit.dte_max, crit.exclude_earnings_days):
                reason = f"earnings in {ctx.days_to_earnings}d (blackout)"
            if reason is not None:
                skips.append({"symbol": underlying, "reason": reason})
                continue
            cands = screen_candidates(underlying, chain, crit)
            ceiling = self._support_ceiling(underlying, crit)
            if ceiling is not None:
                kept = [c for c in cands if c.strike <= ceiling]
                if len(kept) < len(cands):
                    skips.append({"symbol": underlying, "reason":
                                  f"{len(cands) - len(kept)} strike(s) above support {ceiling:.2f}"})
                cands = kept
            # Context-aware cushion (expected-move) + variance-risk-premium gates (both opt-in).
            passed, gate_reason = [], None
            for c in cands:
                r = passes_candidate_gates(c, ctx, crit)
                if r is None:
                    passed.append(c)
                elif gate_reason is None:
                    gate_reason = r
            if len(passed) < len(cands):
                skips.append({"symbol": underlying, "reason":
                              f"{len(cands) - len(passed)} strike(s) failed cushion/VRP: {gate_reason}"})
            cands = passed
            csp_cands.extend(cands)
        if cfg.prefer_quality:
            inner = lambda c: quality_sort_key(c, context_by_underlying, cfg.prefer_iv_rank)  # noqa: E731
        elif cfg.prefer_iv_rank:
            inner = lambda c: iv_rank_sort_key(c, context_by_underlying)  # noqa: E731
        else:
            inner = lambda c: (float(c.theta_efficiency),)  # noqa: E731 -- decay-per-collateral
        # prefer_setups: favorable technical setups first, AVOID reads last, the inner key breaks
        # ties. It only REORDERS -- the screen and the sizer still decide what is eligible.
        key = (lambda c: setup_sort_key(c, context_by_underlying, inner)) if cfg.prefer_setups else inner
        csp_cands.sort(key=key, reverse=True)
        self.last_candidates = csp_cands
        csp_result = self.sizer.evaluate(
            csp_cands, buying_power=buying_power, account_value=account_value,
            open_positions=open_positions,
        )
        csp_approved = csp_result.approved
        csp_rejected = list(csp_result.rejected)

        # Optional hard regime gate (default off): pause NEW CSP entries while the market is risk-off.
        if (self.settings.macro.enabled and self.settings.macro.hard_gate
                and self.last_regime is not None and self.last_regime.risk_off):
            if csp_approved:
                log.info("Regime risk_off + hard_gate: suppressing %d CSP entr(ies).",
                         len(csp_approved))
            skips.append({"symbol": "*", "reason": f"regime risk_off ({self.last_regime.label})"})
            # Sizing approved them but the regime gate vetoes — log them as rejected, not approved.
            csp_rejected.extend(
                (e.candidate, f"regime risk_off hard_gate ({self.last_regime.label})")
                for e in csp_approved
            )
            csp_approved = []

        # Confirmed-downtrend skip (opt-in): no NEW short puts while SPY has closed below its
        # 200-day for N straight sessions. Crisis/risk_off is deliberately NOT part of this gate --
        # measured, those days paid best; the quiet grind below the 200-day is the one that didn't.
        mac = self.settings.macro
        reg = self.last_regime
        if (mac.enabled and mac.skip_confirmed_downtrend and reg is not None
                and reg.confirmed_downtrend):
            reason = (f"market: SPY below its 200-day for {reg.spy_days_below_sma200} sessions "
                      f"(confirmed downtrend >= {mac.downtrend_confirm_days}d) -- new puts paused")
            if csp_approved:
                log.info("Confirmed downtrend: suppressing %d CSP entr(ies).", len(csp_approved))
            skips.append({"symbol": "*", "reason": reason})
            csp_rejected.extend((e.candidate, reason) for e in csp_approved)
            csp_approved = []

        # Correlation / sector concentration cap (opt-in): veto approved CSPs that would push one
        # sector over its share of the account — so a many-name book can't become one correlated bet.
        if self.settings.risk.max_pct_per_sector and csp_approved:
            kept, capped = apply_sector_cap(
                csp_approved, open_positions, self.settings.risk, account_value)
            if capped:
                log.info("Sector cap vetoed %d CSP entr(ies) for concentration.", len(capped))
                for cand, reason in capped:
                    skips.append({"symbol": cand.underlying, "reason": reason})
                csp_rejected.extend(capped)
            csp_approved = kept

        # CC pass — covered calls on shares held (strike floored at cost basis), per-ticker criteria.
        # Assignment clock (opt-in per ticker): shares under water for >= N days since assignment may
        # be written against BELOW basis, inside the configured OTM band above spot, so capital
        # turns over instead of sitting (measured: ~35 days vs ~100 with the basis floor).
        from .holdings import below_basis_allowed
        cc_cands: list[EntryCandidate] = []
        journal_rows = self.trade_journal.recent(500) if self.trade_journal is not None else []
        self.last_cc_clock = {}
        for h in holdings_tradable:
            if h.quantity < 100:
                continue
            crit = cfg.criteria_for(h.symbol, cfg.cc_criteria)
            chain = await chain_for(h.symbol)
            if h.symbol not in context_by_underlying:
                context_by_underlying[h.symbol] = await self._context_for(h.symbol, chain, crit)
            px = getattr(context_by_underlying.get(h.symbol), "price", None)
            allowed, days = below_basis_allowed(h, crit, journal_rows, utcnow(), px)
            self.last_cc_clock[h.symbol] = {"days_held": days, "below_basis_allowed": allowed,
                                            "under_water": (px is not None and px < h.average_cost),
                                            "clock_days": crit.cc_below_basis_after_days}
            if allowed and px:
                lo, hi = crit.cc_otm_band
                cc_cands.extend(screen_candidates(
                    h.symbol, chain, crit, option_type="call", strike_floor=px * (1 + lo),
                    strike_ceiling=px * (1 + hi), ignore_delta=True,
                ))
            else:
                cc_cands.extend(screen_candidates(
                    h.symbol, chain, crit, option_type="call", strike_floor=h.average_cost,
                ))
        cc_cands.sort(key=lambda x: x.theta_efficiency, reverse=True)
        self.last_cc_candidates = cc_cands
        self.last_context = context_by_underlying
        self.last_skips = skips
        cc_result = self.sizer.evaluate_covered_calls(
            cc_cands, holdings=holdings_tradable, open_positions=open_positions, exclude=reserve,
        )
        cc_approved = cc_result.approved
        self.last_scan_at = utcnow()
        self.last_csp_approved = list(csp_approved)
        self.last_cc_approved = list(cc_approved)
        self.last_quotes = quote_by_occ
        feed_problem = self._feed_problem(chain_cache, quote_by_occ)
        if feed_problem:
            return self._skip_scan(feed_problem)
        if self._quotes_stale(quote_by_occ):
            return self._skip_scan("stale quote")
        # A held name that was not on this scan is quoted for the exit check.
        # A stale quote there is the same gate: nothing goes out, and prior
        # alerts are not reconciled.
        if await self._stale_held_quote(open_positions, quote_by_occ, chain_cache):
            return self._skip_scan("stale quote")

        # Persist the full scan disposition (approved + reasoned rejections) — the negative-example
        # dataset for refining entry logic. Best-effort: a logging failure must not break the scan.
        self._record_candidates(csp_approved, csp_rejected, cc_approved,
                                list(cc_result.rejected))

        # Seed IV history (ATM IV per scanned symbol, once/day) for later IV-Rank computation.
        if self.trade_journal is not None:
            today = utcnow().date()
            for sym, ch in chain_cache.items():
                iv = self._atm_iv(ch)
                if iv is not None:
                    self.trade_journal.record_iv(sym, today, iv)

        self.audit.record(
            AuditEventType.POLL,
            {"scan": True, "watchlist": len(cfg.watchlist), "holdings": len(holdings),
             "csp_candidates": len(csp_cands), "csp_approved": len(csp_approved),
             "cc_candidates": len(cc_cands), "cc_approved": len(cc_approved),
             "skipped": skips, "buying_power": buying_power},
            source="scanner",
        )
        log.info("Scan: CSP %d cand/%d approved; CC %d cand/%d approved.",
                 len(csp_cands), len(csp_approved), len(cc_cands), len(cc_approved))

        self.expire_stale_entry_approvals()  # sweep any approval requests that timed out

        # The health gate already returned. Reconcile the positions this scan
        # already read, then grade and consider exits, then emit new plays.
        now = utcnow()
        notes: list[str] = []
        self._reconcile_entries(open_positions, now)
        await self._grade_scorecard(now, notes)
        exit_inserted, exit_confirmed = await self._exit_alerts(
            open_positions, quote_by_occ, chain_cache, notes, now,
        )

        inserted, confirmed = await self._emit_alerts(csp_approved, quote_by_occ, "CSP")
        more_inserted, more_confirmed = await self._emit_alerts(cc_approved, quote_by_occ, "CC")
        self.alerts.record_run(
            status="ok", reason="; ".join(notes) or None,
            alerts_sent=confirmed + more_confirmed + exit_confirmed,
            scanned_at=self.last_scan_at,
        )
        return inserted + more_inserted + exit_inserted

    def _account_broker(self):
        """Broker the scan reads for buying power, open options, and share holdings.

        ``market_data: robinhood`` uses that session and never the paper book.
        A missing session is returned as None so the health gate can fail closed.
        """
        if self.settings.market_data == "robinhood":
            return self.read_broker
        return self.read_broker if self.read_broker is not None else self.broker

    def _broker_down(self) -> bool:
        """True when the account session is missing, disconnected, or unreadable.

        A ``capabilities()`` exception fails closed. A book with no
        ``capabilities`` method (the golden fixture) is not treated as down.
        """
        broker = self._account_broker()
        if broker is None:
            return True
        if getattr(broker, "_connected", None) is False:
            return True
        caps = getattr(broker, "capabilities", None)
        if callable(caps):
            try:
                caps()
            except Exception:  # noqa: BLE001 — an unreadable session must not scan
                log.warning("broker capabilities() failed; skipping the scan")
                return True
        return broker_degraded(self.settings, broker)

    def _feed_problem(self, chain_cache: dict, quotes: dict) -> str | None:
        """Why this scan's chains are unusable, or None when the feed looks healthy.

        Nothing fetched (empty watchlist, no covered shares) is not a failure.
        An empty chain, or a chain that produced no quotes, is.
        """
        if not chain_cache:
            return None
        if any(len(chain) == 0 for chain in chain_cache.values()):
            return "empty chain"
        if not quotes:
            return "empty quote map"
        return None

    def _quotes_stale(self, quotes: dict) -> bool:
        """True when any quote on this scan is older than max_quote_age_seconds."""
        if not quotes:
            return False
        max_age = self.settings.max_quote_age_seconds
        now = utcnow()
        return any(q.is_stale(max_age, now) for q in quotes.values())

    async def _stale_held_quote(self, positions, quote_by_occ, chain_cache) -> bool:
        """True when a held short's quote, fetched for the exit check, is too old.

        Quotes already on this scan were checked with the rest. A name read only
        because it is held uses that same rule.
        """
        now = utcnow()
        max_age = self.settings.max_quote_age_seconds
        for position in positions or []:
            if not _is_short_open(position):
                continue
            occ = getattr(position, "occ_symbol", None)
            if not occ or occ in quote_by_occ:
                continue
            try:
                quote, fresh = await self._quote_for_held(position, quote_by_occ, chain_cache)
            except Exception:  # noqa: BLE001 — the exit pass notes a bad read
                continue
            if quote is not None and fresh and quote.is_stale(max_age, now):
                return True
        return False

    def _skip_scan(self, reason: str) -> int:
        """Send nothing and record a skipped scan_runs row."""
        self.last_scan_at = utcnow()
        self.alerts.record_run(
            status="skipped", reason=reason, alerts_sent=0, scanned_at=self.last_scan_at,
        )
        log.info("Scan skipped: %s", reason)
        return 0

    def _reconcile_entries(self, positions, now) -> None:
        """Match prior CSP/CC alerts to the positions this scan already read.

        Does not read the broker again. A new alert with no match goes stale.
        A stale alert that matches before expiry is taken and flagged late.
        After the expiration date, anything not already taken is expired.
        Exit alerts are left alone: a held short is not proof the exit was taken.
        """
        today = now.date()
        for alert in self.alerts.open_entries():
            try:
                exp = date.fromisoformat(str(alert["expiration"])[:10])
            except (TypeError, ValueError):
                log.info("alert %s left alone: bad expiry", alert.get("dedup_key"))
                continue
            if today > exp:
                self.alerts.mark_expired(alert["id"])
                log.info("alert %s expired", alert.get("dedup_key"))
                continue
            match = _entry_match(alert, positions)
            if match is not None:
                late = alert["status"] == "stale"
                partial = int(match.quantity) < int(alert["contracts"])
                self.alerts.mark_taken(
                    alert["id"], fill_price=_broker_average(match),
                    taken_at=now.isoformat(), late=late, partial=partial,
                )
                log.info("alert %s taken", alert.get("dedup_key"))
            elif alert["status"] == "new":
                self.alerts.mark_stale(alert["id"], now.isoformat())
                log.info("alert %s stale", alert.get("dedup_key"))

    async def _grade_scorecard(self, now, notes: list[str]) -> None:
        """Grade each expired entry once. Taken uses the fill; the rest use the mid."""
        today = now.date()
        spots: dict[str, float | None] = {}
        for alert in self.alerts.ungraded_entries():
            try:
                exp = date.fromisoformat(str(alert["expiration"])[:10])
            except (TypeError, ValueError):
                continue
            if today <= exp:
                continue
            sym = alert["underlying"]
            if sym not in spots:
                spots[sym] = await self._spot(sym, notes)
            spot = spots[sym]
            if spot is None:
                continue
            taken = alert["status"] == "taken"
            if taken:
                credit = alert.get("fill_price")
                result = "realized"
                if credit is None:
                    notes.append(f"scorecard skipped: {alert['occ_symbol']} missing fill")
                    log.info("scorecard skipped: %s missing fill", alert["occ_symbol"])
                    continue
            else:
                credit = alert.get("premium")
                result = "hypothetical"
                if credit is None:
                    notes.append(f"scorecard skipped: {alert['occ_symbol']} missing mid")
                    log.info("scorecard skipped: %s missing mid", alert["occ_symbol"])
                    continue
            pnl = play_pnl(
                kind=alert["kind"], strike=float(alert["strike"]), spot=float(spot),
                credit=float(credit), contracts=int(alert["contracts"]),
            )
            self.alerts.grade(
                alert["id"], taken=taken, result=result, pnl=pnl, graded_at=now.isoformat(),
            )

    async def _spot(self, symbol: str, notes: list[str]) -> float | None:
        try:
            price = await self.market_data.get_underlying_price(symbol)
        except Exception as exc:  # noqa: BLE001 — a price miss must not break the scan
            log.info("scorecard skipped: %s missing price (%s)", symbol, type(exc).__name__)
            notes.append(f"scorecard skipped: {symbol} missing price")
            return None
        if price is None:
            log.info("scorecard skipped: %s missing price", symbol)
            notes.append(f"scorecard skipped: {symbol} missing price")
            return None
        return float(price)

    async def _exit_alerts(
        self, positions, quote_by_occ, chain_cache, notes: list[str], now,
    ) -> tuple[int, int]:
        """One exit alert per held short, through the same insert and webhook path."""
        inserted = 0
        confirmed = 0
        touched: set[str] = set()
        for position in positions or []:
            if not _is_short_open(position):
                continue
            occ = getattr(position, "occ_symbol", None)
            if not occ:
                continue
            dedup_key = f"exit:{position.underlying}:{occ}"
            if dedup_key in touched:
                continue
            try:
                decision = await self._exit_decision(
                    position, quote_by_occ, chain_cache, notes, now,
                )
            except Exception as exc:  # noqa: BLE001 — one bad position must not break the scan
                log.info("exit skipped: %s (%s)", occ, type(exc).__name__)
                notes.append(f"exit skipped: {occ} missing quote")
                continue
            if decision is None:
                continue
            n_ins, n_ok = await self._emit_play(**decision)
            touched.add(dedup_key)
            inserted += n_ins
            confirmed += n_ok
        for row in self.alerts.unsent("exit"):
            if row["dedup_key"] in touched:
                continue
            n_ins, n_ok = await self._emit_play(
                dedup_key=row["dedup_key"], kind="exit", underlying=row["underlying"],
                occ=row["occ_symbol"], strike=row["strike"], expiration=row["expiration"],
                contracts=row["contracts"], premium=row.get("premium"),
                bid=row.get("bid"), ask=row.get("ask"), mid=row.get("premium"),
            )
            inserted += n_ins
            confirmed += n_ok
        return inserted, confirmed

    async def _exit_decision(self, position, quote_by_occ, chain_cache, notes, now):
        """The exit play to emit, or None.

        80% of the credit does not need an open time. The 50% clause does, and
        a missing open time is noted. A missing quote is noted and skipped.
        """
        occ = position.occ_symbol
        quote, _fresh = await self._quote_for_held(position, quote_by_occ, chain_cache)
        if quote is None or quote.midpoint is None:
            notes.append(f"exit skipped: {occ} missing quote")
            log.info("exit skipped: %s missing quote", occ)
            return None
        credit = self._exit_credit(position)
        profit = profit_fraction(credit, quote.midpoint)
        if profit is None:
            notes.append(f"exit skipped: {occ} missing credit")
            log.info("exit skipped: %s missing credit", occ)
            return None
        opened = self._opened_at(position)
        if opened is None:
            notes.append(f"exit skipped: {occ} missing open time")
            log.info("exit skipped: %s missing open time", occ)
            if not clears_anytime(profit):
                return None
        else:
            expiry = expiry_at(position.expiration, opened)
            if not exit_due(profit, as_utc(now), as_utc(opened), expiry):
                return None
        return dict(
            dedup_key=f"exit:{position.underlying}:{occ}", kind="exit",
            underlying=position.underlying, occ=occ, strike=position.strike,
            expiration=position.expiration, contracts=int(position.quantity),
            premium=quote.midpoint, bid=quote.bid, ask=quote.ask, mid=quote.midpoint,
        )

    async def _quote_for_held(self, position, quote_by_occ, chain_cache):
        """Quote from this scan's chain. Fetches the underlying only if it was not scanned."""
        occ = position.occ_symbol
        cached = quote_by_occ.get(occ)
        if cached is not None:
            return cached, False
        symbol = position.underlying
        if symbol not in chain_cache:
            chain = await self.market_data.get_chain(symbol)
            chain_cache[symbol] = list(chain or [])
            for contract in chain_cache[symbol]:
                if contract.occ_symbol:
                    quote_by_occ[contract.occ_symbol] = contract
        return quote_by_occ.get(occ), True

    def _opened_at(self, position):
        opened = getattr(position, "opened_at", None)
        if isinstance(opened, datetime):
            return opened
        row = self.alerts.taken_entry_for_occ(position.occ_symbol)
        if row is None:
            return None
        return parse_time(row.get("taken_at"))

    def _exit_credit(self, position) -> float | None:
        """Fill from the taken alert, or the broker average when opened outside the app."""
        row = self.alerts.taken_entry_for_occ(position.occ_symbol)
        if row is not None and row.get("fill_price") is not None:
            return float(row["fill_price"])
        return _broker_average(position)

    async def _emit_alerts(self, approved, quote_by_occ, kind: str) -> tuple[int, int]:
        """Insert a new play once, and deliver it.

        Returns ``(inserted, confirmed)``. A row that is already ``sent`` is
        left alone. A ``pending`` or ``failed`` row is posted again. ``confirmed``
        counts only webhooks ``_deliver`` accepted.
        """
        inserted = 0
        confirmed = 0
        for entry in approved:
            c = entry.candidate
            quote = quote_by_occ.get(c.occ_symbol)
            if quote is None:
                continue
            n_ins, n_ok = await self._emit_play(
                dedup_key=f"{kind}:{c.underlying}:{c.occ_symbol}",
                kind=kind, underlying=c.underlying, occ=c.occ_symbol,
                strike=c.strike, expiration=c.expiration, contracts=entry.contracts,
                premium=c.premium, bid=quote.bid, ask=quote.ask, mid=c.premium,
            )
            inserted += n_ins
            confirmed += n_ok
        return inserted, confirmed

    async def _emit_play(
        self, *, dedup_key, kind, underlying, occ, strike, expiration, contracts,
        premium, bid, ask, mid,
    ) -> tuple[int, int]:
        """Insert the play if it is new, and post it unless it was already sent."""
        existing = self.alerts.get_by_dedup(dedup_key)
        if existing is not None and existing.get("delivery_status") == "sent":
            return 0, 0
        inserted = 0
        if existing is None:
            exp = expiration.isoformat() if hasattr(expiration, "isoformat") else str(expiration)[:10]
            alert_id = self.alerts.insert_if_new({
                "dedup_key": dedup_key,
                "kind": kind,
                "underlying": underlying,
                "occ_symbol": occ,
                "strike": strike,
                "expiration": exp,
                "contracts": contracts,
                "premium": premium,
                "bid": bid,
                "ask": ask,
                "created_at": utcnow().isoformat(),
            })
            if alert_id is None:
                return 0, 0
            inserted = 1
        else:
            alert_id = existing["id"]
        if self.killswitch.is_paused():
            # Hold the row. pending stays pending; failed stays failed.
            # Neither is posted, so a paused scan cannot burn the one send.
            return inserted, 0
        ok = await self._send_alert(
            alert_id, kind, underlying=underlying, occ=occ, strike=strike,
            expiration=expiration, contracts=contracts, bid=bid, ask=ask, mid=mid,
        )
        if ok is True:
            self.alerts.set_delivery_status(alert_id, "sent")
            return inserted, 1
        if ok is False:
            self.alerts.set_delivery_status(alert_id, "failed")
        return inserted, 0

    async def _send_alert(
        self, alert_id: str, kind: str, *, underlying, occ, strike, expiration,
        contracts: int, bid, ask, mid,
    ):
        """Hand the play to the webhook transport. Never raises into the scan.

        True means the POST was accepted, False means it failed, None means
        there was nothing to send.
        """
        if self.trade_alerts is None:
            return None
        try:
            if isinstance(expiration, str):
                expiration = date.fromisoformat(expiration[:10])
            from ..notify.trade_alerts import build_scan_alert_payload
            return await self.trade_alerts.deliver_scan_alert(build_scan_alert_payload(
                alert_id=alert_id, kind=kind, underlying=underlying,
                occ=occ, strike=float(strike),
                expiration=expiration, contracts=int(contracts),
                bid=bid, ask=ask, mid=mid,
            ))
        except Exception:  # noqa: BLE001 — a webhook failure must not break the scan
            log.warning("scan alert dispatch failed")
            return False

    async def _submit(self, approved, quote_by_occ, rule_name: str, tag: str,
                      review: bool = False, account_value: float = 0.0) -> int:
        """Persist (dedup'd) + auto-execute a list of approved entries. Returns count submitted.

        When ``review`` and AI is enabled, each entry is analyzed just before submission: the
        verdict is stored (advisory) and, in veto mode, a 'skip' suppresses the entry. The AI never
        widens risk — it only annotates or vetoes what the screen + sizer already approved.

        Weekly premium throttle (CSP only, opt-in): once this week's collected premium reaches
        weekly_premium_target_pct of account value, further entries are PARKED for one-tap approval
        instead of auto-firing. Soft, not a hard cap.
        """
        today = utcnow().date().isoformat()
        target_pct = self.settings.entry.weekly_premium_target_pct if review else 0.0
        target_dollars = account_value * target_pct if target_pct > 0 else 0.0
        collected = (self.entry_decisions.premium_collected_since(self._week_start_iso())
                     if target_dollars > 0 else 0.0)
        reviewed = 0
        n = 0
        for entry in approved:
            c = entry.candidate
            # Resolve the broker option id for live execution (paper returns None and is fine).
            option_id = c.option_id or await self.broker.resolve_option_id(c.occ_symbol)
            key_parts = [c.underlying, c.expiration.isoformat(), str(c.strike)]
            if tag:
                key_parts.append(tag)
            key_parts.append(today)
            decision = EntryDecision(
                underlying=c.underlying,
                occ_symbol=c.occ_symbol,
                option_id=option_id,
                strike=c.strike,
                expiration=c.expiration,
                contracts=entry.contracts,
                premium=c.premium,
                rule_name=rule_name,
                reason=(f"{tag or 'CSP'} Δ={c.delta:.2f} dte={c.dte} "
                        f"ann={c.annualized_ror:.0f}% {entry.contracts}x @ ~{c.premium:.2f}"),
                dedup_key=":".join(key_parts),
            )
            if not self.entry_decisions.insert_if_new(decision):
                continue  # already decided this contract today
            self.audit.record(
                AuditEventType.DECISION,
                {"open": True, "kind": rule_name, "occ": decision.occ_symbol,
                 "contracts": decision.contracts, "reason": decision.reason},
                source="scanner", decision_id=decision.id,
            )
            quote = quote_by_occ.get(c.occ_symbol)
            if quote is None:
                self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
                continue

            # AI trade review (advisory, entries only) — annotate, and in veto mode suppress a skip.
            if (review and self.ai_reviewer is not None and self.settings.ai.enabled
                    and reviewed < self.settings.ai.max_candidates_per_scan):
                reviewed += 1
                if await self._ai_veto(decision, c):
                    continue  # veto mode + 'skip' -> do not execute

            # Weekly premium throttle: over target -> hold for approval instead of auto-firing.
            if target_dollars > 0 and collected >= target_dollars:
                await self._park_for_approval(decision, c, collected, target_dollars)
                continue

            order = await self.executor.execute_open(decision, quote)
            n += 1
            # Journal real fills only (the labeled-dataset row; observe-only).
            if order is not None and order.status == OrderStatus.FILLED:
                collected += c.premium * decision.contracts * 100  # count premium just collected
                if self.trade_journal is not None:
                    await self._journal_fill(decision, c, quote, tag, order)
        return n

    def _week_start_iso(self) -> str:
        """ISO timestamp for the start of the current week (Monday 00:00 UTC) — the throttle window."""
        now = utcnow()
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return monday.isoformat()

    async def _park_for_approval(self, decision, candidate, collected: float,
                                 target: float) -> None:
        """Hold an over-budget entry as AWAITING_APPROVAL and push a one-tap approve/reject alert."""
        self.entry_decisions.set_status(decision.id, DecisionStatus.AWAITING_APPROVAL)
        base = self.settings.public_base_url
        # Per-decision token so the link authorizes only THIS trade and can't be replayed from a
        # leaked decision id (executing a real order must not be unauthenticated).
        from ..config import entry_action_token
        tok = entry_action_token(decision.id) or ""
        q = f"?t={tok}"
        actions = [
            {"action": "http", "label": "Approve",
             "url": f"{base}/control/approve-entry/{decision.id}{q}", "method": "POST"},
            {"action": "http", "label": "Reject",
             "url": f"{base}/control/reject-entry/{decision.id}{q}", "method": "POST"},
        ]
        est = candidate.premium * decision.contracts * 100
        mins = self.settings.approval_timeout_seconds // 60
        title = f"Approve entry? {candidate.underlying} {candidate.strike:g}P"
        msg = (f"Weekly premium target reached (${collected:.0f} / ${target:.0f}) — this one's held "
               f"for your OK.\nSell {decision.contracts}x {decision.occ_symbol} for ~${est:.0f} "
               f"(Δ={candidate.delta:.2f}, {candidate.dte}d, {candidate.annualized_ror:.0f}% ann).\n"
               f"Approve within {mins} min (mode={self.settings.mode}).")
        notifier = getattr(self.executor, "notifier", None)
        if notifier is not None:
            await notifier.send(title, msg, priority="high", actions=actions)
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "executed": False, "awaiting_approval": True,
             "reason": "weekly premium target reached", "occ": decision.occ_symbol},
            source="scanner", decision_id=decision.id,
        )

    async def _fresh_quote(self, underlying: str, occ: str):
        try:
            chain = await self.market_data.get_chain(underlying)
            return next((c for c in chain if c.occ_symbol == occ), None)
        except Exception:  # noqa: BLE001 — caller treats None as "no quote"
            return None

    async def approve_parked_entry(self, decision_id: str) -> dict:
        """One-tap approve for a throttled entry: refetch a fresh quote and execute it."""
        d = self.entry_decisions.get(decision_id)
        if d is None:
            return {"ok": False, "status": "not_found"}
        if d.status == DecisionStatus.DONE:
            return {"ok": True, "status": "already_done"}
        if d.status != DecisionStatus.AWAITING_APPROVAL:
            return {"ok": False, "status": "not_pending", "detail": d.status.value}
        if utcnow() > d.created_at + timedelta(seconds=self.settings.approval_timeout_seconds):
            self.entry_decisions.set_status(decision_id, DecisionStatus.EXPIRED)
            return {"ok": False, "status": "expired"}
        self.entry_decisions.set_status(decision_id, DecisionStatus.APPROVED)
        quote = await self._fresh_quote(d.underlying, d.occ_symbol)
        if quote is None:
            self.entry_decisions.set_status(decision_id, DecisionStatus.FAILED)
            return {"ok": False, "status": "no_quote"}
        order = await self.executor.execute_open(d, quote)
        ok = order is not None and order.status == OrderStatus.FILLED
        return {"ok": ok, "status": "executed" if ok else "not_filled"}

    async def reject_parked_entry(self, decision_id: str) -> dict:
        d = self.entry_decisions.get(decision_id)
        if d is None:
            return {"ok": False, "status": "not_found"}
        if d.status != DecisionStatus.AWAITING_APPROVAL:
            return {"ok": False, "status": "not_pending", "detail": d.status.value}
        self.entry_decisions.set_status(decision_id, DecisionStatus.REJECTED)
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "executed": False, "rejected": True, "occ": d.occ_symbol},
            source="scanner", decision_id=decision_id,
        )
        return {"ok": True, "status": "rejected"}

    def expire_stale_entry_approvals(self) -> int:
        """Mark any AWAITING_APPROVAL entry past its window EXPIRED. Returns how many."""
        now = utcnow()
        window = timedelta(seconds=self.settings.approval_timeout_seconds)
        n = 0
        for d in self.entry_decisions.list_by_status(DecisionStatus.AWAITING_APPROVAL):
            if now > d.created_at + window:
                self.entry_decisions.set_status(d.id, DecisionStatus.EXPIRED)
                n += 1
        return n

    async def _refresh_news(self, watchlist: list[str]) -> None:
        """Pull recent headlines for the whole watchlist once per scan and store them (idempotent).
        Fail-open: no provider/store or any error is swallowed so news never breaks a scan."""
        if self.news_provider is None or self.news is None:
            return
        try:
            items = await self.news_provider.fetch(
                list(watchlist), limit=50)
            if items:
                self.news.add_many(items)
        except Exception as exc:  # noqa: BLE001 — advisory; never break the scan
            log.warning("News refresh failed: %s", exc)

    def _recent_news(self, underlying: str) -> list[dict]:
        """Recent headlines for a name within the configured freshness window (advisory context)."""
        if self.news is None:
            return []
        cfg = self.settings.news
        return self.news.recent_for(
            underlying, max_age_seconds=cfg.max_age_hours * 3600,
            limit=cfg.max_items_per_symbol)

    def _enrich_ctx_from_tv(self, underlying: str, ctx: UnderlyingContext) -> None:
        """Overlay TradingView-sourced features (ADX, Bollinger %B) onto the built context so they
        are logged with every decision and available to the underlying gates. Fail-open: no store /
        no fresh snapshot / non-numeric value leaves the field None (the gates then simply skip)."""
        if self.tv_indicators is None:
            return
        snap = self.tv_indicators.get_latest(
            underlying, self.settings.ai.tv_indicator_max_age_seconds)
        payload = snap.get("payload", {}) if snap else {}
        for field in ("adx", "bb_percent_b"):
            v = payload.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                setattr(ctx, field, float(v))
        # TradingView setup flags (real-time layer): numerics only (0/1 for booleans; JSON true/false
        # is rejected), freshness keyed on the PAYLOAD bar time -- every alert bumps the merged
        # snapshot's received_at, so stale keys linger there -- then merged (union) with the bot's read.
        try:
            tv_setups, age = parse_tv_setups(payload, now_ms=int(utcnow().timestamp() * 1000))
            if tv_setups:
                ctx.tv_setups, ctx.tv_bar_age_seconds = tv_setups, age
                self._merge_tv_setups(underlying, ctx, tv_setups)
        except Exception as exc:  # noqa: BLE001 -- advisory overlay; never break a scan
            log.warning("TV setup overlay failed for %s: %s", underlying, exc)

    def _merge_tv_setups(self, symbol: str, ctx: UnderlyingContext, tv: dict) -> None:
        """Union fresh TradingView setup flags into the context (and the cached read the Setups
        panel serves). TV can add/upgrade labels and set the live *_attempt flags; it never removes
        what the bot detected."""
        cached = self.last_setups.get(symbol)
        live = dict((cached or {}).get("live") or {})
        setups, live, sources = merge_tv(ctx.setups, live, tv,
                                         breakout_vol_ratio=self.settings.entry.setups.breakout_vol_ratio)
        ctx.setups = setups
        ctx.primary_setup, ctx.setup_bias, ctx.setup_score = primary_setup(setups), setup_bias(setups), setup_score(setups)
        ctx.live_breakout_attempt = live.get("breakout_attempt")
        ctx.live_breakdown_attempt = live.get("breakdown_attempt")
        ctx.setup_sources = sources or None
        tv_block = {"present": True, "bar_age_seconds": ctx.tv_bar_age_seconds, "flags": tv,
                    "sources": sources}
        if cached is None:
            cached = {"flags": {}, "features": {}, "fired_now": [], "partial_bar": None}
            self.last_setups[symbol] = cached
        cached.update({"setups": setups, "live": live, "bias": ctx.setup_bias,
                       "score": ctx.setup_score, "primary": ctx.primary_setup, "tv": tv_block})

    def _refresh_risk_profile(self, symbol: str, bars: list[dict]) -> None:
        """Once per day per name: the strike-survival risk profile from the bars already fetched
        (~1y), so every watchlist name -- including a fresh add -- is profiled on its first scan.
        Advisory; failures leave the previous profile in place."""
        cfg = self.settings.entry.setups
        today = utcnow().date().isoformat()
        cur = self.last_risk_profile.get(symbol)
        if cur is not None and cur.get("date") == today:
            return
        try:
            from .risk_profile import ticker_risk_profile
            prof = ticker_risk_profile(bars, base_cushion=cfg.profile_base_cushion,
                                       horizon=cfg.profile_horizon, target_itm=cfg.target_itm_rate)
            prof["date"], prof["symbol"] = today, symbol
            self.last_risk_profile[symbol] = prof
        except Exception as exc:  # noqa: BLE001 -- advisory
            log.warning("risk profile failed for %s: %s", symbol, exc)

    def _tv_levels(self, underlying: str) -> tuple[float | None, float | None]:
        """Fresh TradingView support/resistance for a name (None when absent, stale, or invalid)."""
        if self.tv_indicators is None:
            return None, None
        snap = self.tv_indicators.get_latest(
            underlying, self.settings.ai.tv_indicator_max_age_seconds)
        pay = snap.get("payload", {}) if snap else {}
        out: list[float | None] = []
        for k in ("support", "resistance"):
            v = pay.get(k)
            ok = (isinstance(v, (int, float)) and not isinstance(v, bool)
                  and math.isfinite(v) and v > 0)
            out.append(float(v) if ok else None)
        return out[0], out[1]

    def _overlay_setups(self, symbol: str, ctx: UnderlyingContext, bars: list[dict]) -> None:
        """Run the deterministic technical-setup detectors on the bars already fetched and overlay
        the read onto the context: it is journaled with every entry, feeds the opt-in setup gates
        and the prefer_setups tilt, and reaches the AI reviewer. Advisory -- any failure leaves the
        fields None and never breaks a scan. Runs on completed bars; the live partial bar only
        populates the *_attempt flags."""
        cfg = self.settings.entry.setups
        if not cfg.enabled or not bars:
            return
        try:
            partial = bool(is_market_hours())
            sup, res = self._tv_levels(symbol)
            read = detect_setups(bars, cfg, support=sup, resistance=res, last_bar_partial=partial)
            if read is None:
                return
            f = read.features
            ctx.setups, ctx.primary_setup, ctx.setup_bias = list(read.setups), read.primary, read.bias
            ctx.setup_score, ctx.setups_fired_now = read.score, list(read.fired_now)
            ctx.bb_width_pct, ctx.vol_ratio_20 = f.get("bb_width_pct"), f.get("vol_ratio_20")
            ctx.bb_squeeze = read.flags.get("bb_squeeze")
            ctx.ttm_squeeze = read.flags.get("ttm_squeeze")
            ctx.donchian_high_20, ctx.donchian_low_20 = f.get("donchian_high_20"), f.get("donchian_low_20")
            ctx.support_ref, ctx.support_source = f.get("support_ref"), f.get("support_source")
            ctx.dist_to_support_pct = f.get("dist_to_support_pct")
            ctx.resistance_ref = f.get("resistance_ref")
            ctx.dist_to_resistance_pct = f.get("dist_to_resistance_pct")
            ctx.live_breakout_attempt = read.live.get("breakout_attempt")
            ctx.live_breakdown_attempt = read.live.get("breakdown_attempt")
            d = read.as_dict()
            d["partial_bar"] = partial
            self.last_setups[symbol] = d
            self._refresh_risk_profile(symbol, bars)
            # Setup-accuracy tracker: log each label that fired on the completed bar (deduped per
            # bar) and fill in forward outcomes for earlier fires once the bars exist.
            if self.setup_events is not None:
                from .setup_tracker import record_fires, resolve_for_symbol
                now = utcnow()
                # labels on the bar BEFORE the last completed one -> episode-start stamping
                completed = bars[:-1] if (partial and len(bars) > 1) else bars
                prev_labels = None
                if len(completed) > MIN_BARS:
                    prev_flags, _ = detect_flags(completed[:-1], cfg, support=sup, resistance=res)
                    prev_labels = set(compose(prev_flags))
                record_fires(self.setup_events, symbol, read, bars, now, partial=partial,
                             prev_labels=prev_labels)
                resolve_for_symbol(self.setup_events, symbol, bars, now)
        except Exception as exc:  # noqa: BLE001 -- advisory; never break the scan
            log.warning("setup detection failed for %s: %s", symbol, exc)

    def _support_ceiling(self, underlying: str, crit) -> float | None:
        """Strike ceiling from the latest TradingView support level, for the strike-below-support
        gate. Returns None (no gate) when the gate is off or no fresh/valid support exists — the
        gate fails open so a missing alert never freezes entries. Applies support_buffer_pct as a
        margin *below* support when set.
        """
        if not getattr(crit, "require_strike_below_support", False):
            return None
        if self.tv_indicators is None:
            return None
        snap = self.tv_indicators.get_latest(
            underlying, self.settings.ai.tv_indicator_max_age_seconds)
        support = snap.get("payload", {}).get("support") if snap else None
        if not isinstance(support, (int, float)) or isinstance(support, bool):
            return None
        if not math.isfinite(support) or support <= 0:
            return None
        return support * (1.0 - max(0.0, getattr(crit, "support_buffer_pct", 0.0) or 0.0))

    async def _ai_veto(self, decision, candidate) -> bool:
        """Run the AI review for one candidate, store the verdict (advisory), and return True only
        when veto mode is on AND the model says 'skip'. Fail-open: a None verdict never suppresses."""
        ctx = self.last_context.get(candidate.underlying)
        regime = self.last_regime
        stock_dd = ctx.drawdown_20d if ctx is not None else None
        move_class = (classify_move(stock_dd, regime, self.settings.macro)
                      if regime is not None else "unknown")
        tv = None
        if self.tv_indicators is not None:
            tv = self.tv_indicators.get_latest(
                candidate.underlying, self.settings.ai.tv_indicator_max_age_seconds)
        portfolio = {"held_names": [h.symbol for h in self.last_holdings]}
        news = self._recent_news(candidate.underlying)

        verdict = await self.ai_reviewer.review(
            candidate=candidate, ctx=ctx, regime=regime, move_class=move_class,
            tv=tv, portfolio=portfolio, news=news,
        )
        if verdict is None:
            return False  # fail-open — trade proceeds under the rules

        if self.ai_reviews is not None:
            self.ai_reviews.insert(
                occ_symbol=candidate.occ_symbol, underlying=candidate.underlying,
                verdict=verdict, decision_id=decision.id,
                regime_label=(regime.label if regime is not None else None),
                move_class=move_class, model=self.settings.ai.model,
            )
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "ai_review": verdict.as_dict(), "occ": candidate.occ_symbol,
             "move_class": move_class},
            source="scanner", decision_id=decision.id,
        )
        if self.settings.ai.mode == "veto" and verdict.recommendation == "skip":
            self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
            log.info("AI veto: skipping %s — %s", candidate.occ_symbol, verdict.rationale[:80])
            return True
        return False

    def _record_candidates(self, csp_approved, csp_rejected, cc_approved, cc_rejected) -> None:
        """Persist one scan's full candidate disposition (approved + reasoned rejections)."""
        if self.entry_candidates is None:
            return
        import uuid
        scan_id = uuid.uuid4().hex
        rows: list[dict] = []

        def row(c, kind: str, approved: bool, reason: str, contracts=None) -> dict:
            return {
                "underlying": c.underlying, "occ_symbol": c.occ_symbol, "kind": kind,
                "strike": c.strike, "expiration": c.expiration.isoformat(), "dte": c.dte,
                "delta": c.delta, "iv": c.iv, "premium": c.premium,
                "annualized_ror": c.annualized_ror, "open_interest": c.open_interest,
                "volume": c.volume, "score": c.score, "approved": approved,
                "contracts": contracts, "reason": reason,
            }

        for e in csp_approved:
            rows.append(row(e.candidate, "CSP", True, "approved", e.contracts))
        for c, reason in csp_rejected:
            rows.append(row(c, "CSP", False, reason))
        for e in cc_approved:
            rows.append(row(e.candidate, "CC", True, "approved", e.contracts))
        for c, reason in cc_rejected:
            rows.append(row(c, "CC", False, reason))

        try:
            self.entry_candidates.record_scan(scan_id, rows, scanned_at=self.last_scan_at)
        except Exception as exc:  # noqa: BLE001 — candidate logging must never break the scan
            log.warning("entry-candidate logging failed: %s", exc)

    async def _journal_fill(self, decision: EntryDecision, candidate, quote, tag: str, order) -> None:
        ctx = self.last_context.get(candidate.underlying)
        context = {k: v for k, v in ctx.as_dict().items() if k != "symbol"} if ctx else {}
        # Per-option greeks at entry (extensible context; feeds the refinement dataset).
        context["theta"] = candidate.theta
        context["gamma"] = candidate.gamma
        context["theta_efficiency"] = candidate.theta_efficiency
        # IV / realized-vol ratio at entry — the variance-risk-premium signal. >1 means implied vol
        # exceeds the stock's actual movement, i.e. you're being paid MORE than the realized risk
        # (the edge premium sellers harvest). Both inputs are already captured; persist the ratio so
        # the analytics can test whether trades sold into a rich IV/RV actually pay off.
        rv = ctx.realized_vol if ctx else None
        context["iv_rv_ratio"] = (round(candidate.iv / rv, 3)
                                  if (candidate.iv and rv and rv > 0) else None)
        # Persist the MARKET REGIME at entry (previously computed then discarded). Premium-selling is
        # highly regime-dependent, so this is the metadata most likely to reveal what conditions the
        # strategy actually works in — the substrate for finding an edge.
        reg = self.last_regime
        if reg is not None:
            context["mkt_regime"] = reg.label
            context["mkt_risk_off"] = reg.risk_off
            context["mkt_spy_vol"] = reg.spy_realized_vol
            context["mkt_spy_drawdown_20d"] = reg.spy_drawdown_20d
            context["mkt_spy_above_sma200"] = reg.spy_above_sma200
            context["mkt_vix"] = reg.vix
            context["mkt_vix_state"] = reg.vix_state
        underlying_price = ctx.price if (ctx and ctx.price is not None) else None
        if underlying_price is None:
            try:
                underlying_price = await self.market_data.get_underlying_price(candidate.underlying)
            except Exception:  # noqa: BLE001 — price is best-effort context
                underlying_price = None
        snap = pricing_kwargs(order)
        self.trade_journal.insert(TradeJournalEntry(
            occ_symbol=candidate.occ_symbol,
            underlying=candidate.underlying,
            kind="CC" if tag == "CC" else "CSP",
            contracts=decision.contracts,
            strike=candidate.strike,
            dte=candidate.dte,
            delta=candidate.delta,
            iv=candidate.iv,
            premium=candidate.premium,
            spread_pct=quote.spread_pct,
            open_interest=candidate.open_interest,
            volume=candidate.volume,
            annualized_ror=candidate.annualized_ror,
            underlying_price=underlying_price,
            context=context,
            entry_decision_id=decision.id,
            order_type=snap["order_type"],
            limit_price=snap["limit_price"],
            bid=snap["bid"],
            ask=snap["ask"],
            mid=snap["mid"],
            time_in_force=snap["time_in_force"],
        ))

    @staticmethod
    def _atm_iv(chain: list[OptionContractQuote]) -> float | None:
        """IV of the contract nearest 0.50 |delta| (≈ at-the-money) — the IV-Rank seed."""
        best_iv: float | None = None
        best_dist: float | None = None
        for c in chain:
            if c.iv is None or c.delta is None:
                continue
            dist = abs(abs(c.delta) - 0.5)
            if best_dist is None or dist < best_dist:
                best_dist, best_iv = dist, c.iv
        return best_iv

    async def _context_for(self, symbol, chain, criteria) -> UnderlyingContext:
        """Build the technicals + IV-Rank context for one underlying (best-effort)."""
        try:
            # ~400 calendar days ≈ 285 trading bars — enough to compute SMA200 (+ warm-up). The
            # old default (260 cal ≈ 186 trading) fell short, leaving sma200/above_sma200 null and
            # the trend gate dark. Names younger than ~200 sessions still yield None (fail-open).
            bars = await self.market_data.get_underlying_bars(symbol, 400)
        except Exception:  # noqa: BLE001 — technicals are best-effort; never break the scan
            bars = []
        iv_hist = (
            [iv for _d, iv in self.trade_journal.iv_history(symbol)]
            if self.trade_journal is not None else []
        )
        ctx = build_context(symbol, bars, self._atm_iv(chain), iv_hist, criteria)
        await self._overlay_quality(symbol, ctx, bars)
        self._overlay_setups(symbol, ctx, bars)
        return ctx

    async def _overlay_quality(self, symbol: str, ctx: UnderlyingContext, bars: list[dict]) -> None:
        """Overlay the company quality/growth score onto the context (best-effort, fail-open).

        Reuses the bars already fetched for technicals (no extra market-data call) and the
        company-data provider (cached per name per day). Any failure leaves quality_score None —
        a name with no company data is treated as neutral, never penalized."""
        try:
            profile = await self.company_data.profile(symbol)
        except Exception as exc:  # noqa: BLE001 — quality is advisory; never break the scan
            log.warning("company-data lookup failed for %s: %s", symbol, exc)
            return
        if profile is None:
            return
        try:
            bd = quality_breakdown(profile, bars, ctx.above_sma200)
            ctx.quality_score = bd["score"]
            # Informational snapshot for the dashboard's Company Quality panel (NOT a trade input):
            # the score, its sub-scores, and the raw metrics behind them ("is this name profitable?").
            self.last_quality[symbol.upper()] = {
                "score": bd["score"],
                "sector": profile.sector,
                "market_cap": profile.market_cap,
                "gross_margin": profile.gross_margin,
                "net_margin": profile.net_margin,
                "revenue_growth": profile.revenue_growth,
                "fcf_margin": profile.fcf_margin,
                "gross_profitability": profile.gross_profitability,
                "insider_net_buys_90d": profile.insider_net_buys_90d,
                "subscores": {k: bd.get(k) for k in
                              ("profitability", "cash", "growth", "momentum", "insider_bonus")},
            }
        except Exception as exc:  # noqa: BLE001 — scoring must never break the scan
            log.warning("quality score failed for %s: %s", symbol, exc)

    def stop(self) -> None:
        self._stop.set()
