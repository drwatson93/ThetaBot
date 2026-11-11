"""The current option book is one broker read, filtered, and then discarded.

Nothing here reads or writes the positions table. A contract is shown only when
the broker still returns it and its expiration date is today or later in
America/New_York. A contract expiring today stays until the end of that day.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone

from ..domain.enums import RuleType
from ..domain.models import Position
from ..marketdata.quote import OptionQuote
from ..rules.engine import RulesEngine, build_rules

log = logging.getLogger("agentic.live_positions")

_OPEN = frozenset({"OPEN", "CLOSING"})
_NS = uuid.UUID("8c1e4b2a-6d0f-4a7e-9c3b-1f5a8e2d7b04")

BROKER_UNAVAILABLE = "No broker is connected."
BROKER_READ_FAILED = "Could not read positions from the broker."


class LiveBookError(Exception):
    """The broker read did not produce a book. Callers must not use stored rows."""


def ny_today(now: datetime | None = None) -> date:
    """Today's date in America/New_York."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 — tzdata missing; UTC is the only remaining clock
        log.warning("America/New_York unavailable; expiration check used UTC")
        return now.astimezone(timezone.utc).date()


def status_value(position) -> str:
    status = getattr(position, "status", None)
    return str(getattr(status, "value", status) or "")


def is_open_status(position) -> bool:
    return status_value(position) in _OPEN


def still_held(position, today: date | None = None) -> bool:
    """Open, with quantity, and not past expiration in America/New_York."""
    if not is_open_status(position):
        return False
    try:
        qty = int(position.quantity)
    except (TypeError, ValueError):
        return False
    if qty <= 0:
        return False
    exp = getattr(position, "expiration", None)
    if isinstance(exp, datetime):
        exp = exp.date()
    if not isinstance(exp, date):
        return False
    today = ny_today() if today is None else today
    return exp >= today


def select_live(positions, today: date | None = None) -> list:
    """Drop expired, flat, and non-open contracts. Does not copy or store them."""
    today = ny_today() if today is None else today
    return [p for p in (positions or []) if still_held(p, today)]


def stored_history(positions) -> list:
    """Rows that are not an open book. Live views never treat OPEN rows as current."""
    return [p for p in (positions or []) if not is_open_status(p)]


def stable_position_id(occ_symbol: str) -> str:
    """Same id for the same contract on every read, with nothing written down."""
    return uuid.uuid5(_NS, str(occ_symbol or "")).hex


def snapshot(position: Position) -> Position:
    """A copy the monitor can price without mutating the broker's object or a table."""
    return replace(position, id=stable_position_id(position.occ_symbol))


def _read_blocked(broker) -> str | None:
    """Why this session cannot return a book, before ``get_open_positions`` is called.

    A disconnected Robinhood session and a session with no ``list_positions``
    tool both return ``[]``. That is a failed read, not an empty book. A paper
    book has neither flag and is read normally.
    """
    if getattr(broker, "_connected", None) is False:
        return "failed"
    roles = getattr(broker, "_roles", None)
    if isinstance(roles, dict) and not roles.get("list_positions"):
        return "failed"
    return None


async def fetch_broker_positions(broker, account_number: str | None = None) -> list:
    """Read option positions from the broker. Raises LiveBookError on failure.

    A book with no account resolver is called with no argument, or with
    ``account_number`` when the caller already has one. A Robinhood session
    resolves the account first. A blank account, a disconnected session, and a
    missing ``list_positions`` tool are failed reads. An account that resolves
    and returns no rows is an empty book. This never consults the positions table.
    """
    if broker is None:
        raise LiveBookError("unavailable")
    blocked = _read_blocked(broker)
    if blocked:
        raise LiveBookError(blocked)
    ensure = getattr(broker, "ensure_account_number", None)
    try:
        if ensure is None:
            if account_number:
                raw = await broker.get_open_positions(account_number)
            else:
                raw = await broker.get_open_positions()
        else:
            acct = await ensure(account_number)
            if not str(acct or "").strip():
                raise LiveBookError("failed")
            raw = await broker.get_open_positions(acct)
    except LiveBookError:
        raise
    except Exception as exc:
        log.warning("live position read failed: %s", type(exc).__name__)
        raise LiveBookError("failed") from exc
    if raw is None:
        raise LiveBookError("failed")
    return list(raw)


async def load_live_positions(broker) -> tuple[list[Position], str | None]:
    """``(positions, error)``. On failure the list is empty and ``error`` is set.

    Stored rows are never substituted for a failed or missing read.
    """
    try:
        raw = await fetch_broker_positions(broker)
    except LiveBookError as exc:
        if str(exc) == "unavailable":
            return [], BROKER_UNAVAILABLE
        return [], BROKER_READ_FAILED
    except Exception:
        log.warning("live position read failed", exc_info=True)
        return [], BROKER_READ_FAILED
    return [snapshot(p) for p in select_live(raw)], None


async def attach_quote(position: Position, market_data) -> OptionQuote | None:
    """Fill bid/ask/mark on this copy from a quote. A quote miss leaves the broker marks."""
    quote = None
    getter = getattr(market_data, "get_quote", None) if market_data is not None else None
    if getter is not None:
        try:
            quote = await getter(position)
        except Exception:  # noqa: BLE001 — one quote must not hide the rest of the book
            log.warning("quote read failed for %s", position.occ_symbol)
            quote = None
    if quote is not None:
        if quote.bid is not None:
            position.current_bid = quote.bid
        if quote.ask is not None:
            position.current_ask = quote.ask
        if quote.mark is not None:
            position.current_mark = quote.mark
        if quote.delta is not None:
            position.delta = quote.delta
        if quote.iv is not None:
            position.iv = quote.iv
        return quote
    mark = position.current_mark
    bid = position.current_bid
    ask = position.current_ask
    if mark is None and bid is None and ask is None:
        return None
    if mark is None and bid is not None and ask is not None:
        mark = round((bid + ask) / 2, 4)
        position.current_mark = mark
    return OptionQuote(
        occ_symbol=position.occ_symbol,
        bid=bid,
        ask=ask,
        mark=mark,
        delta=position.delta,
        iv=position.iv,
    )


def fresh_rule_label(position: Position, rules, quote: OptionQuote | None) -> str | None:
    """Name of the exit rule that matches this contract right now, or None.

    Taken from the current config. Historical decision rows are not consulted,
    so a removed label such as ``dte-close`` cannot be attached.
    """
    for decision in RulesEngine(build_rules(list(rules or []))).evaluate(position, quote):
        name = decision.rule_name or ""
        if name == "dte-close" or decision.rule_type == RuleType.DTE:
            continue
        return name
    return None
