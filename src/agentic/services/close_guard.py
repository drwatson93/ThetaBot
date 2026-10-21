"""One buy-to-close in flight or filled per position.

The monitor can evaluate several exit rules for the same position in one tick, and
other tasks (signals, approval, rolls) can call the executor at the same time. Each
of those used to persist its own ``client_order_id`` and submit its own order.

Two layers stop that:

* An in-process ``threading.Lock`` keyed by position id makes the check-and-set
  atomic across tasks and threads. It is not held across ``await``.
* A ``close_claims`` row plus the partial unique index on live buy-to-close orders
  (pending, submitted, partial, filled) still blocks a second close after a restart.
  A rejected or cancelled close deletes the claim so the position can be retried.
  Contracts already filled on that cancelled row stay reserved; the retry submits
  only the remainder, under a new client order id.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from ..domain.enums import PositionStatus
from ..domain.models import Order
from ..store.orders import LIVE_CLOSE_STATUSES, OrderStore
from ..store.positions import PositionStore


class CloseGuard:
    def __init__(self, orders: OrderStore):
        self.orders = orders
        self._registry = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._busy: set[str] = set()

    def _lock_for(self, position_id: str) -> threading.Lock:
        with self._registry:
            lock = self._locks.get(position_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[position_id] = lock
            return lock

    def _set_busy(self, position_id: str, busy: bool) -> None:
        with self._registry:
            if busy:
                self._busy.add(position_id)
            else:
                self._busy.discard(position_id)

    def _is_busy(self, position_id: str) -> bool:
        with self._registry:
            return position_id in self._busy

    @contextmanager
    def claim(self, order: Order):
        """Check-and-set the close for ``order.position_id``.

        Yields ``(result, owned_order)`` from ``OrderStore.begin_live_close``.
        Do not await inside the ``with`` block: the position lock is held and
        this runs on the event-loop thread.
        """
        lock = self._lock_for(order.position_id)
        with lock:
            result, owned = self.orders.begin_live_close(
                order, busy=self._is_busy(order.position_id)
            )
            if result in ("inserted", "resume"):
                self._set_busy(order.position_id, True)
            yield result, owned

    def release(
        self, position_id: str, order: Order | None, positions: PositionStore
    ) -> None:
        """Drop the in-process owner flag.

        A pending, submitted, partial, or filled order keeps the persisted claim.
        Anything else (rejected, cancelled, or no order) clears it and, if the
        position was left ``CLOSING``, puts it back to ``OPEN`` so a retry can run.
        Clearing the claim does not zero ``filled_qty`` on a partial cancel.
        """
        with self._lock_for(position_id):
            self._set_busy(position_id, False)
            if order is not None and order.status in LIVE_CLOSE_STATUSES:
                self.orders.upsert_close_claim(order)
                return
            self.orders.clear_close_claim(position_id)
            current = positions.get(position_id)
            if current is not None and current.status == PositionStatus.CLOSING:
                positions.set_status(position_id, PositionStatus.OPEN)

    def note_order(self, order: Order) -> None:
        """Point the persisted claim at a replacement order (reprice)."""
        with self._lock_for(order.position_id):
            self.orders.upsert_close_claim(order)
