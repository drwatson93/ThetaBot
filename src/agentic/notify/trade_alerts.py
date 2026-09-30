"""Outbound instant trade alerts (webhook POST on fill).

Fires after ThetaBot records an open/close fill. Never on the trading path:
background thread, ~3s timeout, one retry, all failures swallowed/logged.
The webhook URL (TRADE_ALERT_URL) and optional sender-key header value
(TRADE_ALERT_HEADER_VALUE) are never logged, printed, or returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import urllib.request
from datetime import date, datetime
from typing import Any

from ..config import Settings, get_secret, is_usable_secret
from ..domain.enums import AuditEventType, OptionType, PositionStatus
from ..domain.models import EntryDecision, Order, Position, utcnow
from ..store.audit import AuditStore
from ..store.db import Database

log = logging.getLogger("agentic.notify.trade_alerts")

VALID_MODES = ("instant", "regular", "off")
DEFAULT_MODE = "instant"
HTTP_TIMEOUT_S = 3.0
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
_SIDES = {
    "SELL_TO_OPEN": "STO",
    "BUY_TO_CLOSE": "BTC",
    "SELL_TO_CLOSE": "STC",
    "BUY_TO_OPEN": "BTO",
}

# OCC: <root><YYMMDD><C|P><strike*1000, 8 digits> — trailing 15 chars are fixed.
def _parse_occ(occ: str) -> tuple[str, date, str, float] | None:
    if not occ or len(occ) < 16:
        return None
    try:
        tail = occ[-15:]
        root = occ[:-15]
        yy, mm, dd = int(tail[0:2]), int(tail[2:4]), int(tail[4:6])
        cp = tail[6].upper()
        strike = int(tail[7:15]) / 1000.0
        if cp not in ("C", "P") or not root:
            return None
        return root, date(2000 + yy, mm, dd), cp, strike
    except (ValueError, IndexError):
        return None


def webhook_configured() -> bool:
    """True when TRADE_ALERT_URL is set to a real value. Never returns the URL."""
    return is_usable_secret(get_secret("TRADE_ALERT_URL"))


def _webhook_url() -> str | None:
    raw = get_secret("TRADE_ALERT_URL")
    if not is_usable_secret(raw):
        return None
    return raw.strip()


def _header_secret() -> str | None:
    raw = get_secret("TRADE_ALERT_HEADER_VALUE")
    if not is_usable_secret(raw):
        return None
    return raw.strip()


def _auth_header() -> tuple[str, str] | None:
    """(name, value) when both env vars are set; otherwise None. Never log the value."""
    name_raw = get_secret("TRADE_ALERT_HEADER_NAME")
    value = _header_secret()
    if not is_usable_secret(name_raw) or value is None:
        return None
    return name_raw.strip(), value


def auth_header_configured() -> bool:
    """True when both sender-key header env vars are set. Never returns the value."""
    return _auth_header() is not None


def sanitize_error(exc: BaseException, url: str | None = None) -> str:
    """Exception text with any URL (including the webhook) and header value stripped."""
    detail = str(exc) or ""
    if url:
        detail = detail.replace(url, "[redacted]")
    raw_header = get_secret("TRADE_ALERT_HEADER_VALUE")
    if is_usable_secret(raw_header):
        for piece in {raw_header, raw_header.strip()}:
            if piece:
                detail = detail.replace(piece, "[redacted]")
    detail = _URL_RE.sub("[redacted]", detail).strip()
    msg = type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
    return msg[:400]


def _action(side: str | None) -> str:
    key = (side or "").upper()
    return _SIDES.get(key, key or "STO")


def _fmt_strike(strike: float) -> str:
    if float(strike).is_integer():
        return str(int(strike))
    return f"{strike:g}"


def _fmt_limit(px: float) -> str:
    return f"{px:.2f}"


def _money_int(n: float) -> int:
    return int(round(n))


def _fmt_signed_dollars(n: float) -> str:
    mag = abs(_money_int(n))
    return f"+${mag}" if n >= 0 else f"-${mag}"


def _right(*, option_type: OptionType | str | None = None, occ: str = "") -> str:
    parsed = _parse_occ(occ)
    if parsed:
        return parsed[2]
    if option_type is not None:
        raw = getattr(option_type, "value", option_type)
        s = str(raw).upper()
        if s.startswith("C"):
            return "C"
        if s.startswith("P"):
            return "P"
    return "P"


def _expiry_mmdd(exp: date | None, occ: str = "") -> str:
    if exp is not None:
        return exp.strftime("%m/%d")
    parsed = _parse_occ(occ)
    if parsed:
        return parsed[1].strftime("%m/%d")
    return ""


def _ticker(underlying: str | None, occ: str = "") -> str:
    if underlying:
        return str(underlying).upper()
    parsed = _parse_occ(occ)
    return parsed[0].upper() if parsed else ""


def _contracts(order: Order) -> int:
    qty = order.filled_qty or 0
    return qty if qty > 0 else int(order.quantity or 0)


def _iso(ts: datetime | None = None) -> str:
    t = ts or utcnow()
    if t.tzinfo is None:
        from datetime import timezone
        t = t.replace(tzinfo=timezone.utc)
    return t.isoformat()


def format_open_line(
    *,
    action: str,
    contracts: int,
    ticker: str,
    strike: float,
    right: str,
    expiry: str,
    limit_price: float,
    premium: float,
) -> str:
    return (
        f"{action} {contracts} {ticker} {_fmt_strike(strike)}{right} {expiry} "
        f"@{_fmt_limit(limit_price)} (${_money_int(premium)})"
    )


def format_close_line(
    *,
    action: str,
    contracts: int,
    ticker: str,
    strike: float,
    right: str,
    expiry: str,
    limit_price: float,
    realized_pnl: float,
) -> str:
    return (
        f"{action} {contracts} {ticker} {_fmt_strike(strike)}{right} {expiry} "
        f"@{_fmt_limit(limit_price)} ({_fmt_signed_dollars(realized_pnl)})"
    )


def build_open_payload(decision: EntryDecision, order: Order, *, mode: str) -> dict[str, Any]:
    contracts = _contracts(order)
    limit = float(order.limit_price)
    premium = round(limit * contracts * 100, 2)
    ticker = _ticker(decision.underlying, decision.occ_symbol or order.occ_symbol)
    right = _right(occ=decision.occ_symbol or order.occ_symbol)
    expiry = _expiry_mmdd(decision.expiration, decision.occ_symbol or order.occ_symbol)
    action = _action(order.side)
    strike = float(decision.strike)
    return {
        "event": "trade_open",
        "trade_id": order.id,
        "summary": format_open_line(
            action=action, contracts=contracts, ticker=ticker, strike=strike,
            right=right, expiry=expiry, limit_price=limit, premium=premium,
        ),
        "ticker": ticker,
        "action": action,
        "contracts": contracts,
        "strike": strike,
        "right": right,
        "expiry": expiry,
        "limit_price": limit,
        "premium": premium,
        "realized_pnl": None,
        "order_type": order.order_type,
        "bid": order.bid,
        "ask": order.ask,
        "filled_at": _iso(order.last_status_at or utcnow()),
        "mode": mode,
    }


def build_close_payload(position: Position, order: Order, *, mode: str) -> dict[str, Any]:
    from dataclasses import replace

    from ..services.stats import position_pnl

    contracts = _contracts(order)
    limit = float(order.limit_price)
    ticker = _ticker(position.underlying, position.occ_symbol or order.occ_symbol)
    right = _right(option_type=position.option_type, occ=position.occ_symbol or order.occ_symbol)
    expiry = _expiry_mmdd(position.expiration, position.occ_symbol or order.occ_symbol)
    action = _action(order.side)
    strike = float(position.strike)
    closed = replace(position, status=PositionStatus.CLOSED)
    info = position_pnl(closed, order)
    pnl = info.get("realized_pnl")
    if pnl is None:
        pnl = 0.0
    return {
        "event": "trade_close",
        "trade_id": order.id,
        "summary": format_close_line(
            action=action, contracts=contracts, ticker=ticker, strike=strike,
            right=right, expiry=expiry, limit_price=limit, realized_pnl=float(pnl),
        ),
        "ticker": ticker,
        "action": action,
        "contracts": contracts,
        "strike": strike,
        "right": right,
        "expiry": expiry,
        "limit_price": limit,
        "premium": None,
        "realized_pnl": pnl,
        "order_type": order.order_type,
        "bid": order.bid,
        "ask": order.ask,
        "filled_at": _iso(order.last_status_at or utcnow()),
        "mode": mode,
    }


def build_test_payload(mode: str) -> dict[str, Any]:
    now = utcnow()
    return {
        "event": "test",
        "trade_id": "test",
        "summary": "TEST ThetaBot trade alert",
        "ticker": "TEST",
        "action": "TEST",
        "contracts": 0,
        "strike": None,
        "right": None,
        "expiry": None,
        "limit_price": None,
        "premium": None,
        "realized_pnl": None,
        "order_type": None,
        "bid": None,
        "ask": None,
        "filled_at": _iso(now),
        "mode": mode,
    }


def _http_post(url: str, payload: dict[str, Any], timeout: float = HTTP_TIMEOUT_S) -> None:
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ThetaBot/trade-alert",
        },
        method="POST",
    )
    extra = _auth_header()
    if extra:
        # Set after construction so the configured name is sent verbatim
        # (Request.add_header would .capitalize() it).
        req.headers[extra[0]] = extra[1]
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None)
        if status is not None and int(status) >= 400:
            raise OSError(f"HTTP {status}")


def _deliver(url: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    """POST once, then one retry. Returns (ok, sanitized_error). Never raises."""
    last_err: str | None = None
    for _attempt in range(2):
        try:
            _http_post(url, payload)
            return True, None
        except Exception as exc:  # noqa: BLE001 — swallow; caller records status
            last_err = sanitize_error(exc, url)
    return False, last_err


class TradeAlerts:
    """Persisted alerts mode + fire-and-forget webhook delivery."""

    def __init__(
        self,
        db: Database,
        audit: AuditStore,
        settings: Settings | None = None,
        *,
        background: bool = True,
    ):
        self.db = db
        self.audit = audit
        self.settings = settings
        self._background = background

    def mode(self) -> str:
        row = self.db.conn.execute(
            "SELECT alerts_mode FROM control WHERE id = 1"
        ).fetchone()
        raw = (row["alerts_mode"] if row else None) or DEFAULT_MODE
        return raw if raw in VALID_MODES else DEFAULT_MODE

    def webhook_configured(self) -> bool:
        return webhook_configured()

    def should_send(self) -> bool:
        return self.mode() == "instant" and self.webhook_configured()

    def status(self) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT alerts_mode, alerts_last_sent_at, alerts_last_status, alerts_last_error "
            "FROM control WHERE id = 1"
        ).fetchone()
        return {
            "mode": (row["alerts_mode"] if row and row["alerts_mode"] in VALID_MODES
                     else DEFAULT_MODE),
            "webhook_configured": self.webhook_configured(),
            "auth_header_configured": auth_header_configured(),
            "last_sent_at": row["alerts_last_sent_at"] if row else None,
            "last_status": row["alerts_last_status"] if row else None,
            "last_error": row["alerts_last_error"] if row else None,
        }

    def set_mode(self, mode: str, *, source: str = "control") -> str:
        wanted = (mode or "").strip().lower()
        if wanted not in VALID_MODES:
            raise ValueError("alerts mode must be instant, regular, or off")
        previous = self.mode()
        self.db.conn.execute(
            "UPDATE control SET alerts_mode = ? WHERE id = 1",
            (wanted,),
        )
        self.db.conn.commit()
        self.audit.record(
            AuditEventType.ALERTS,
            {"mode": wanted, "previous": previous},
            source=source,
        )
        log.info("alerts mode %s -> %s", previous, wanted)
        return wanted

    def notify_fill(self, payload: dict[str, Any]) -> None:
        """Fire-and-forget. Never raises into the caller (trading path)."""
        try:
            if not self.should_send():
                return
            url = _webhook_url()
            if not url:
                return
            loop = self._running_loop()
            if self._background:
                threading.Thread(
                    target=self._deliver_bg, args=(url, payload, loop),
                    daemon=True, name="trade-alert",
                ).start()
            else:
                ok, err = _deliver(url, payload)
                self._finish_delivery(payload, ok, err)
        except Exception:  # noqa: BLE001 — never break trading
            log.warning("trade alert dispatch failed")

    async def send_test(self) -> dict[str, Any]:
        """Owner-initiated test POST. HTTP runs in a worker thread; DB stays on the loop."""
        try:
            url = _webhook_url()
            if not url:
                return {"ok": False, "error": "webhook not configured"}
            mode = self.settings.mode if self.settings is not None else "paper"
            payload = build_test_payload(mode)
            ok, err = await asyncio.to_thread(_deliver, url, payload)
            self._finish_delivery(payload, ok, err)
            return {"ok": ok, "error": err}
        except Exception as exc:  # noqa: BLE001 — never raise into the control handler
            err = sanitize_error(exc)
            log.warning("trade alert failed event=test err=%s", err)
            return {"ok": False, "error": err}

    def _running_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _deliver_bg(
        self,
        url: str,
        payload: dict[str, Any],
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """HTTP only. Never touches the shared sqlite connection."""
        try:
            ok, err = _deliver(url, payload)
            self._log_result(payload, ok, err)
            self._schedule_record(loop, ok, err)
        except Exception as exc:  # noqa: BLE001
            err = sanitize_error(exc, url)
            log.warning("trade alert failed: %s", err)
            self._schedule_record(loop, False, err)

    def _schedule_record(
        self,
        loop: asyncio.AbstractEventLoop | None,
        ok: bool,
        error: str | None,
    ) -> None:
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._record_delivery, ok, error)
        except Exception:  # noqa: BLE001 — never persist from this thread as a fallback
            log.warning("trade alert status persist schedule failed")

    def _finish_delivery(
        self, payload: dict[str, Any], ok: bool, error: str | None
    ) -> None:
        self._log_result(payload, ok, error)
        self._record_delivery(ok, error)

    def _log_result(self, payload: dict[str, Any], ok: bool, error: str | None) -> None:
        event = payload.get("event")
        trade_id = payload.get("trade_id")
        if ok:
            log.info("trade alert sent event=%s trade_id=%s", event, trade_id)
        else:
            log.warning("trade alert failed event=%s trade_id=%s err=%s",
                        event, trade_id, error)

    def _record_delivery(self, ok: bool, error: str | None) -> None:
        """Must run on the asyncio loop thread. Does not touch control.updated_at."""
        try:
            self.db.conn.execute(
                "UPDATE control SET alerts_last_sent_at = ?, alerts_last_status = ?, "
                "alerts_last_error = ? WHERE id = 1",
                (utcnow().isoformat(), "ok" if ok else "error",
                 None if ok else error),
            )
            self.db.conn.commit()
        except Exception:  # noqa: BLE001 — status is observational only
            log.warning("trade alert status persist failed")


def alerts_from_deps(deps: Any) -> TradeAlerts:
    existing = getattr(deps, "trade_alerts", None)
    if existing is not None:
        return existing
    return TradeAlerts(
        deps.killswitch.db, deps.audit, getattr(deps, "settings", None),
    )
