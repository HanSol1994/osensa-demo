"""In-memory restaurant state.

The assignment permits state that does not survive a restart, so this is a plain
in-process store. It sits behind the ``Store`` protocol purely as a seam: swapping
in aiosqlite or Redis means writing one new class, not touching the kitchen.

Deliberate design choice: **every method here is synchronous.** asyncio runs one
task at a time and only switches at an ``await``, so a method containing no
``await`` cannot be interleaved with another task. That makes read-modify-write
sequences atomic for free, and the whole service needs no locks. The trade-off is
recorded in the README: a genuinely persistent store would have to be async, and
would then need explicit locking or optimistic concurrency to regain this
property.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .config import Settings
from .models import Order, OrderStatus, TableState, utcnow


@dataclass
class _TableRecord:
    table_id: int
    version: int = 0
    #: Insertion-ordered so the UI renders oldest order first.
    orders: OrderedDict[str, Order] = field(default_factory=OrderedDict)


class Store(Protocol):
    """The persistence seam. See module docstring."""

    def has_table(self, table_id: int) -> bool: ...
    def is_duplicate(self, client_order_id: str) -> bool: ...
    def remember_client_order(self, client_order_id: str) -> None: ...
    def cooking_count(self, table_id: int) -> int: ...
    def total_cooking_count(self) -> int: ...
    def add_order(self, table_id: int, order: Order) -> TableState: ...
    def finish_order(
        self, table_id: int, order_id: str, status: OrderStatus, at: datetime
    ) -> TableState | None: ...
    def snapshot(self, table_id: int) -> TableState: ...
    def all_snapshots(self) -> list[TableState]: ...
    def reset(self) -> None: ...


class InMemoryStore:
    """Concrete ``Store``, holding everything in dictionaries."""

    def __init__(
        self,
        table_ids: tuple[int, ...],
        *,
        dedupe_cache_size: int = 1000,
        max_served_history: int = 10,
        epoch: str | None = None,
    ) -> None:
        # A fresh epoch per store instance, i.e. per kitchen process. This is what
        # tells clients that version numbers have restarted from zero rather than
        # gone backwards. See TableState.epoch.
        self._epoch = epoch if epoch is not None else uuid.uuid4().hex
        self._tables: dict[int, _TableRecord] = {
            table_id: _TableRecord(table_id=table_id) for table_id in table_ids
        }
        # Bounded LRU of client order ids we have already acted on. Bounded so a
        # long-running or hostile client cannot grow it without limit.
        self._seen_client_orders: OrderedDict[str, None] = OrderedDict()
        self._dedupe_cache_size = dedupe_cache_size
        self._max_served_history = max_served_history

    # --- queries -----------------------------------------------------------

    def has_table(self, table_id: int) -> bool:
        return table_id in self._tables

    def is_duplicate(self, client_order_id: str) -> bool:
        return client_order_id in self._seen_client_orders

    def cooking_count(self, table_id: int) -> int:
        record = self._tables.get(table_id)
        if record is None:
            return 0
        return sum(
            1 for order in record.orders.values() if order.status is OrderStatus.COOKING
        )

    def total_cooking_count(self) -> int:
        return sum(self.cooking_count(table_id) for table_id in self._tables)

    def snapshot(self, table_id: int) -> TableState:
        record = self._tables[table_id]
        return TableState(
            table_id=record.table_id,
            epoch=self._epoch,
            version=record.version,
            updated_at=utcnow(),
            # Copy the models so a later mutation cannot alter a snapshot that has
            # already been handed out for publishing.
            orders=[order.model_copy() for order in record.orders.values()],
        )

    def all_snapshots(self) -> list[TableState]:
        return [self.snapshot(table_id) for table_id in sorted(self._tables)]

    # --- mutations ---------------------------------------------------------

    def remember_client_order(self, client_order_id: str) -> None:
        self._seen_client_orders[client_order_id] = None
        self._seen_client_orders.move_to_end(client_order_id)
        while len(self._seen_client_orders) > self._dedupe_cache_size:
            self._seen_client_orders.popitem(last=False)

    def reset(self) -> None:
        """Clear everything and begin a new generation.

        A new ``epoch`` is issued rather than trying to keep versions climbing.
        Resetting is exactly the situation the epoch exists for: versions restart
        at zero and clients accept the state because the generation changed. The
        alternative — preserving version counters across a wipe — would work but
        would mean two mechanisms for one idea.

        The dedupe cache is cleared too, so replaying an earlier order after a
        reset behaves like a genuinely new order rather than being silently
        swallowed as a duplicate.
        """
        self._epoch = uuid.uuid4().hex
        self._seen_client_orders.clear()
        for record in self._tables.values():
            record.orders.clear()
            record.version = 0

    def add_order(self, table_id: int, order: Order) -> TableState:
        record = self._tables[table_id]
        record.orders[order.order_id] = order
        record.version += 1
        self._trim_served_history(record)
        return self.snapshot(table_id)

    def finish_order(
        self, table_id: int, order_id: str, status: OrderStatus, at: datetime
    ) -> TableState | None:
        """Move an order out of COOKING. Returns None if it is already gone.

        Returning None rather than raising matters: a cooking task completing
        after its table was trimmed or the order removed is a benign race, not an
        error worth crashing a task over.
        """
        record = self._tables.get(table_id)
        if record is None:
            return None
        order = record.orders.get(order_id)
        if order is None or order.status is not OrderStatus.COOKING:
            return None
        order.status = status
        order.ready_at = at
        record.version += 1
        self._trim_served_history(record)
        return self.snapshot(table_id)

    def _trim_served_history(self, record: _TableRecord) -> None:
        """Cap finished orders per table so the retained payload stays bounded.

        Only finished orders are eligible for eviction; anything still COOKING is
        live state and is never dropped.
        """
        finished = [
            order_id
            for order_id, order in record.orders.items()
            if order.status is not OrderStatus.COOKING
        ]
        for order_id in finished[: max(0, len(finished) - self._max_served_history)]:
            del record.orders[order_id]


def create_store(settings: Settings) -> Store:
    """Select a Store implementation from configuration.

    The factory is the seam described in the module docstring: swapping in
    persistence means adding one case here and one class, with no changes to the
    kitchen. See docs/FUTURE-WORK.md §4 for the consequences that a persistent
    store would carry (async methods, explicit locking, timer recovery).
    """
    match settings.store_backend:
        case "memory":
            return InMemoryStore(
                settings.table_ids, dedupe_cache_size=settings.dedupe_cache_size
            )
        case unknown:  # pragma: no cover - unreachable while the Literal has one member
            msg = f"Unsupported store backend: {unknown!r}"
            raise ValueError(msg)
