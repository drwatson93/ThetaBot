"""The current option book is one broker read, filtered, and then discarded.

Nothing here reads or writes the positions table. A contract is shown only when
the broker still returns it and its expiration date is today or later in
America/New_York. A contract expiring today stays until the end of that day.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import NoReturn

from ..domain.enums import RuleType
from ..domain.models import Position
from ..errors import is_transient_connect_error
from ..marketdata.quote import OptionQuote
from ..notify.trade_alerts import sanitize_error, scrub_error_text
from ..rules.engine import RulesEngine, build_rules

log = logging.getLogger("agentic.live_positions")

_OPEN = frozenset({"OPEN", "CLOSING"})
_NS = uuid.UUID("8c1e4b2a-6d0f-4a7e-9c3b-1f5a8e2d7b04")

BROKER_UNAVAILABLE = "No broker is connected."
BROKER_READ_FAILED = "Could not read positions from the broker."

# One retry, long enough for a dropped TLS handshake and short enough to skip the cycle.
_RETRY_DELAY_SECONDS = 1.5
_MAX_DETAIL_CHARS = 300


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


def _leaf_type_name(rest: str) -> str:
    name: list[str] = []
    for ch in rest:
        if ch.isalnum() or ch == "_":
            name.append(ch)
        else:
            break
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return ""
    return "".join(name)


def _cap_detail(text: str) -> str:
    """Cap length without cutting off the trailing leaf type (``-> ConnectError``)."""
    text = text.strip()
    limit = _MAX_DETAIL_CHARS
    if len(text) <= limit:
        return text
    arrow = text.rfind(" -> ")
    leaf = ""
    if arrow != -1:
        name = _leaf_type_name(text[arrow + 4:])
        if name:
            leaf = " -> " + name
    if leaf and len(leaf) < limit:
        return text[: limit - len(leaf)].rstrip() + leaf
    return text[:limit]


def _sanitize_message(text: str) -> str:
    """Run one exception message through ``sanitize_error`` without adding a second type name."""
    if not text:
        return ""
    cleaned = sanitize_error(RuntimeError(text))
    prefix = "RuntimeError: "
    if cleaned.startswith(prefix):
        return cleaned[len(prefix):].strip()
    if cleaned == "RuntimeError":
        return ""
    return cleaned.strip()


def _raw_message(exc: BaseException) -> str:
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 — a broken __str__ must not hide the type
        return ""
    return text.strip()


def _exception_part(exc: BaseException) -> str:
    """``TypeName: message``, or just the type name when the message is empty.

    ExceptionGroups contribute the type only. Their ``str()`` is the TaskGroup
    wrapper (``unhandled errors in a TaskGroup``) and hides the leaf.
    """
    name = type(exc).__name__
    if isinstance(exc, BaseExceptionGroup):
        return name
    msg = _sanitize_message(_raw_message(exc))
    if not msg:
        return name
    return f"{name}: {msg}"


def _next_related(exc: BaseException) -> BaseException | None:
    """Explicit cause, else context. ``from None`` sets ``__suppress_context__`` and stops the walk.

    Cause and context are not both followed, so the same ConnectError is not listed twice.
    """
    cause = exc.__cause__
    if cause is not None:
        return cause
    if getattr(exc, "__suppress_context__", False):
        return None
    context = exc.__context__
    if context is None or context is cause:
        return None
    return context


def _iter_nodes(exc: BaseException, seen: set[int], depth: int = 0):
    if id(exc) in seen or depth > 12:
        return
    seen.add(id(exc))
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_nodes(sub, seen, depth + 1)
    nxt = _next_related(exc)
    if nxt is not None:
        yield from _iter_nodes(nxt, seen, depth + 1)


def _primary_nodes(exc: BaseException) -> list[BaseException]:
    """Outer groups, then the first leaf, then that leaf's cause/context chain."""
    nodes: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    guard = 0
    while cur is not None and id(cur) not in seen and guard < 12:
        guard += 1
        seen.add(id(cur))
        nodes.append(cur)
        if isinstance(cur, BaseExceptionGroup) and cur.exceptions:
            cur = cur.exceptions[0]
            continue
        cur = _next_related(cur)
    return nodes


def _failure_nodes(exc: BaseException) -> list[BaseException]:
    """Primary chain, plus any other leaf type (the ConnectError hiding on a sibling)."""
    primary = _primary_nodes(exc)
    shown = {type(node).__name__ for node in primary}
    nodes = list(primary)
    extras = 0
    for node in _iter_nodes(exc, set()):
        if isinstance(node, BaseExceptionGroup):
            continue
        name = type(node).__name__
        if name in shown:
            continue
        shown.add(name)
        nodes.append(node)
        extras += 1
        if extras >= 4:
            break
    return nodes


def format_broker_failure(exc: BaseException) -> str:
    """Type, message, and cause for a broker-read failure. Secrets are removed.

    A group becomes ``ExceptionGroup -> ClosedResourceError -> ConnectError``.
    An empty message is dropped so the type name still shows. Capped at 300 characters.
    """
    if isinstance(exc, LiveBookError):
        # generic_colons off: the message already contains "TypeName: ..." markers.
        msg = scrub_error_text(str(exc).strip(), generic_colons=False)
        name = type(exc).__name__
        if msg:
            return _cap_detail(f"{name}: {msg}")
        cause = _next_related(exc)
        if cause is not None and cause is not exc and not isinstance(cause, LiveBookError):
            return _cap_detail(f"{name} -> {format_broker_failure(cause)}")
        return name
    parts = [_exception_part(node) for node in _failure_nodes(exc)]
    text = " -> ".join(part for part in parts if part)
    return _cap_detail(text or type(exc).__name__)


def _fail_read(exc: BaseException) -> NoReturn:
    text = format_broker_failure(exc)
    log.warning("live position read failed: %s", text)
    raise LiveBookError(text) from exc


def _read_blocked(broker) -> str | None:
    """Why this session cannot return a book, before ``get_open_positions`` is called.

    A disconnected Robinhood session and a session with no ``list_positions``
    tool both return ``[]``. That is a failed read, not an empty book. A paper
    book has neither flag and is read normally.
    """
    if getattr(broker, "_connected", None) is False:
        return "broker disconnected"
    roles = getattr(broker, "_roles", None)
    if isinstance(roles, dict) and not roles.get("list_positions"):
        return "list_positions tool missing"
    return None


async def _retry_pause(stop_event) -> None:
    """Wait before the one retry. A monitor that is already stopping does not sleep."""
    if stop_event is not None and stop_event.is_set():
        return
    await asyncio.sleep(_RETRY_DELAY_SECONDS)


async def _read_raw(broker, account_number: str | None):
    ensure = getattr(broker, "ensure_account_number", None)
    if ensure is None:
        if account_number:
            return await broker.get_open_positions(account_number)
        return await broker.get_open_positions()
    acct = await ensure(account_number)
    if not str(acct or "").strip():
        raise LiveBookError("blank account")
    return await broker.get_open_positions(acct)


async def fetch_broker_positions(
    broker, account_number: str | None = None, *, stop_event=None,
) -> list:
    """Read option positions from the broker. Raises LiveBookError on failure.

    A book with no account resolver is called with no argument, or with
    ``account_number`` when the caller already has one. A Robinhood session
    resolves the account first. A blank account, a disconnected session, and a
    missing ``list_positions`` tool are failed reads. An account that resolves
    and returns no rows is an empty book. This never consults the positions table.

    A connect failure (httpx/httpcore ``ConnectError`` or anyio ``ClosedResourceError``,
    including inside an ExceptionGroup, or an explicit ``__cause__``) is retried once.
    ``raise ValueError(...) from None`` while handling a connect error is not retried.
    CancelledError, KeyboardInterrupt, and a mixed BaseExceptionGroup propagate.
    When ``stop_event`` is already set the retry does not sleep. Stored rows are never
    substituted.
    """
    if broker is None:
        raise LiveBookError("unavailable")
    blocked = _read_blocked(broker)
    if blocked:
        raise LiveBookError(blocked)
    try:
        raw = await _read_raw(broker, account_number)
    except LiveBookError:
        raise
    except Exception as exc:
        # CancelledError, KeyboardInterrupt, and mixed BaseExceptionGroup are BaseExceptions.
        if not is_transient_connect_error(exc):
            _fail_read(exc)
        log.warning(
            "live position read failed: %s; retrying once",
            format_broker_failure(exc),
        )
        await _retry_pause(stop_event)
        try:
            raw = await _read_raw(broker, account_number)
        except LiveBookError:
            raise
        except Exception as retry_exc:
            _fail_read(retry_exc)
    if raw is None:
        raise LiveBookError("broker returned no payload")
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
    except Exception as exc:
        log.warning("live position read failed: %s", format_broker_failure(exc))
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
