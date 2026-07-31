"""The kitchen: consumes ORDER events, emits FOOD events.

This module contains no MQTT client code. It talks to a ``Publisher`` protocol,
which means the entire behaviour of the service — validation, capacity limits,
concurrency, timing — is testable without a broker, a network, or a real clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from pydantic import ValidationError

from . import topics
from .config import Settings
from .models import (
    KitchenState,
    KitchenStatus,
    Order,
    OrderPlaced,
    OrderRejected,
    OrderStatus,
    RejectionReason,
    TableState,
    utcnow,
)
from .store import Store

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    """The only thing the kitchen needs from a transport."""

    async def publish(
        self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
    ) -> None: ...


class _Rejection:
    """Internal result type: why an order was refused, and who to tell."""

    __slots__ = ("client_id", "client_order_id", "message", "reason", "table_id")

    def __init__(
        self,
        *,
        client_id: str | None,
        client_order_id: str | None,
        table_id: int | None,
        reason: RejectionReason,
        message: str,
    ) -> None:
        self.client_id = client_id
        self.client_order_id = client_order_id
        self.table_id = table_id
        self.reason = reason
        self.message = message


class Kitchen:
    def __init__(
        self,
        store: Store,
        publisher: Publisher,
        settings: Settings,
        *,
        delay_provider: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._settings = settings
        # Injectable so tests can make cooking instantaneous (return 0) instead of
        # sleeping for a real 10-30 seconds.
        self._delay_provider = delay_provider or self._random_cook_time
        # Strong references to in-flight cooking tasks. asyncio only holds a weak
        # reference to a running task, so a task whose only reference is a local
        # variable can be garbage collected mid-await and silently never finish.
        # This set is what keeps every dish alive until it is served.
        self._cook_tasks: set[asyncio.Task[None]] = set()

    def _random_cook_time(self) -> int:
        # randint is inclusive at both ends, so a 10-30 config really can produce
        # both 10 and 30.
        return random.randint(  # noqa: S311 - simulation timing, not cryptography
            self._settings.cook_min_seconds, self._settings.cook_max_seconds
        )

    @property
    def in_flight(self) -> int:
        return len(self._cook_tasks)

    # --- lifecycle ---------------------------------------------------------

    async def announce_online(self) -> None:
        """Publish the baseline retained state a fresh browser needs.

        Every table is published even when empty, so a client that connects to a
        kitchen that has never taken an order still receives a complete picture
        instead of rendering nothing until the first event.
        """
        for state in self._store.all_snapshots():
            await self._publish_table_state(state)
        await self._publish_status(KitchenState.ONLINE)
        self.log_state_snapshot("kitchen_online")

    async def shutdown(self) -> None:
        """Cancel in-flight cooking and announce OFFLINE."""
        await self._cancel_all_cooking()
        await self._publish_status(KitchenState.OFFLINE)

    # --- inbound -----------------------------------------------------------

    async def handle_message(self, topic: str, payload: bytes) -> None:
        """Entry point for one inbound message.

        This method never raises. The consumer loop must survive any payload a
        client can construct, so every failure path here ends in a log line and a
        dropped or rejected message.
        """
        try:
            if topic == topics.KITCHEN_RESET:
                await self._handle_reset()
                return
            await self._handle_order_message(topic, payload)
        except Exception:  # pragma: no cover - defensive backstop
            logger.exception(
                "unhandled_error_processing_message", extra={"topic": topic}
            )

    async def _handle_reset(self) -> None:
        """Cancel everything in progress and clear all state.

        Exists so a demo can be started over without restarting processes. Gated
        by configuration because it is a destructive command that any client
        permitted to publish it can invoke; see docs/SECURITY.md.
        """
        if not self._settings.enable_reset:
            logger.warning("reset_rejected_disabled")
            return
        cancelled = await self._cancel_all_cooking()
        self._store.reset()
        logger.info("kitchen_reset", extra={"cancelledOrders": cancelled})
        # Republish so every connected client — and the broker's retained slots —
        # reflect the cleared state immediately.
        for state in self._store.all_snapshots():
            await self._publish_table_state(state)
        self.log_state_snapshot("reset")

    async def _cancel_all_cooking(self) -> int:
        """Cancel in-flight cooking tasks and wait for them to finish unwinding.

        Iterates over a copy: cancelling a task fires its done-callback, which
        mutates ``self._cook_tasks``.
        """
        tasks = list(self._cook_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def _handle_order_message(self, topic: str, payload: bytes) -> None:
        table_id_from_topic = topics.parse_table_id_from_order_topic(topic)
        if table_id_from_topic is None:
            # We cannot identify a sender, so there is nobody to apologise to.
            if topics.is_own_outbound_topic(topic):
                # Expected broker echo, not an anomaly. See is_own_outbound_topic.
                logger.debug("ignored_own_outbound_topic", extra={"topic": topic})
            else:
                logger.warning("dropped_unroutable_topic", extra={"topic": topic})
            return

        parsed = self._parse_order(payload)
        if isinstance(parsed, _Rejection):
            await self._publish_rejection(parsed)
            return

        outcome = self._try_accept(parsed, table_id_from_topic)
        if isinstance(outcome, _Rejection):
            await self._publish_rejection(outcome)
            return
        if outcome is None:
            return  # duplicate delivery; already handled, nothing to say

        order, state = outcome
        await self._publish_table_state(state)

        # The crux of the concurrency requirement: the cook is *spawned*, not
        # awaited. Awaiting here would serialise the restaurant — table 2 could
        # not order until table 1's dish was served. Spawning returns control to
        # the consumer loop immediately, so orders overlap and each dish is timed
        # independently.
        task = asyncio.create_task(
            self._cook(table_id_from_topic, order), name=f"cook:{order.order_id}"
        )
        self._cook_tasks.add(task)
        task.add_done_callback(self._cook_tasks.discard)
        # Logged after the task is registered so `cookingNow` counts this dish
        # rather than trailing one behind.
        self.log_state_snapshot("order_accepted")

    def _parse_order(self, payload: bytes) -> OrderPlaced | _Rejection:
        """Decode and validate, salvaging a return address where possible."""
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("dropped_malformed_json", extra={"bytes": len(payload)})
            return _Rejection(
                client_id=None,
                client_order_id=None,
                table_id=None,
                reason=RejectionReason.VALIDATION_FAILED,
                message="Payload was not valid JSON.",
            )
        if not isinstance(raw, dict):
            return _Rejection(
                client_id=None,
                client_order_id=None,
                table_id=None,
                reason=RejectionReason.VALIDATION_FAILED,
                message="Payload must be a JSON object.",
            )

        # Best-effort: pull the reply address out of the raw dict before strict
        # validation, so that a validation failure can still be reported back to
        # the customer who caused it instead of vanishing into the logs.
        client_id = (
            raw.get("clientId") if isinstance(raw.get("clientId"), str) else None
        )
        client_order_id = (
            raw.get("clientOrderId")
            if isinstance(raw.get("clientOrderId"), str)
            else None
        )

        try:
            return OrderPlaced.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "rejected_invalid_order",
                extra={"errors": exc.error_count(), "clientId": client_id},
            )
            return _Rejection(
                client_id=client_id,
                client_order_id=client_order_id,
                table_id=None,
                reason=RejectionReason.VALIDATION_FAILED,
                message="Order failed validation.",
            )

    def _try_accept(
        self, order_cmd: OrderPlaced, table_id_from_topic: int
    ) -> tuple[Order, TableState] | _Rejection | None:
        """Decide the fate of a valid order and commit it. Returns None for a dupe.

        **This method contains no ``await`` on purpose.** Because asyncio only
        switches tasks at an await point, every check and the commit that follows
        run as one indivisible step. Two orders arriving in the same tick cannot
        both pass a capacity check that only one of them should pass, and no lock
        is needed to guarantee it.
        """
        if order_cmd.table_id != table_id_from_topic:
            return _Rejection(
                client_id=order_cmd.client_id,
                client_order_id=order_cmd.client_order_id,
                table_id=table_id_from_topic,
                reason=RejectionReason.VALIDATION_FAILED,
                message="tableId in the payload does not match the topic.",
            )

        if not self._store.has_table(order_cmd.table_id):
            return _Rejection(
                client_id=order_cmd.client_id,
                client_order_id=order_cmd.client_order_id,
                table_id=order_cmd.table_id,
                reason=RejectionReason.UNKNOWN_TABLE,
                message=f"Table {order_cmd.table_id} does not exist.",
            )

        if self._store.is_duplicate(order_cmd.client_order_id):
            logger.info(
                "ignored_duplicate_order",
                extra={"clientOrderId": order_cmd.client_order_id},
            )
            return None

        if self._store.total_cooking_count() >= self._settings.max_orders_global:
            return _Rejection(
                client_id=order_cmd.client_id,
                client_order_id=order_cmd.client_order_id,
                table_id=order_cmd.table_id,
                reason=RejectionReason.KITCHEN_AT_CAPACITY,
                message="The kitchen is at capacity, please try again shortly.",
            )

        if (
            self._store.cooking_count(order_cmd.table_id)
            >= self._settings.max_orders_per_table
        ):
            return _Rejection(
                client_id=order_cmd.client_id,
                client_order_id=order_cmd.client_order_id,
                table_id=order_cmd.table_id,
                reason=RejectionReason.TABLE_AT_CAPACITY,
                message=(
                    f"Table {order_cmd.table_id} already has "
                    f"{self._settings.max_orders_per_table} orders in progress."
                ),
            )

        # Commit. The dedupe key is recorded here, in the same atomic step as the
        # order itself, so a retransmission cannot slip through behind us.
        self._store.remember_client_order(order_cmd.client_order_id)
        # The cook time is drawn here, at acceptance, rather than inside the
        # cooking task. That way it is already present in the *first* state message
        # a client receives, so the countdown starts correct instead of appearing a
        # moment later. The cooking task then sleeps for exactly this value, so
        # there is one number, not two that could disagree.
        cook_seconds = max(0, self._delay_provider())
        placed_at = utcnow()
        order = Order(
            order_id=uuid.uuid4().hex,
            food_name=order_cmd.food_name,
            status=OrderStatus.COOKING,
            placed_at=placed_at,
            cook_seconds=cook_seconds,
            expected_ready_at=placed_at + timedelta(seconds=cook_seconds),
            ready_at=None,
        )
        state = self._store.add_order(order_cmd.table_id, order)
        logger.info(
            "accepted_order",
            extra={
                "orderId": order.order_id,
                "tableId": order_cmd.table_id,
                "clientOrderId": order_cmd.client_order_id,
            },
        )
        return order, state

    # --- cooking -----------------------------------------------------------

    async def _cook(self, table_id: int, order: Order) -> None:
        """One dish, one task, one independent timer.

        **This sleep is the authoritative timer.** The `cookSeconds` and
        `expectedReadyAt` fields published to clients are derived from the same
        value, purely so the UI can render a countdown. Nothing a client does
        affects when this completes.
        """
        logger.info(
            "cooking_started",
            extra={
                "orderId": order.order_id,
                "tableId": table_id,
                "cookSeconds": order.cook_seconds,
            },
        )
        try:
            await asyncio.sleep(order.cook_seconds)
            state = self._store.finish_order(
                table_id, order.order_id, OrderStatus.SERVED, utcnow()
            )
            if state is None:
                # The order vanished while cooking. Benign, and not worth an error.
                logger.info(
                    "order_gone_before_serving", extra={"orderId": order.order_id}
                )
                return
            await self._publish_table_state(state)
            logger.info(
                "order_served", extra={"orderId": order.order_id, "tableId": table_id}
            )
            self.log_state_snapshot("order_served")
        except asyncio.CancelledError:
            # Shutdown. Re-raise so the cancellation is honoured rather than
            # swallowed; a task that eats CancelledError cannot be shut down.
            logger.info("cooking_cancelled", extra={"orderId": order.order_id})
            raise
        except Exception:
            logger.exception(
                "cooking_failed", extra={"orderId": order.order_id, "tableId": table_id}
            )
            await self._mark_failed(table_id, order)

    async def _mark_failed(self, table_id: int, order: Order) -> None:
        """Surface a failed dish instead of letting it hang in COOKING forever."""
        try:
            state = self._store.finish_order(
                table_id, order.order_id, OrderStatus.FAILED, utcnow()
            )
            if state is not None:
                await self._publish_table_state(state)
        except Exception:  # pragma: no cover - last-resort guard
            logger.exception(
                "failed_to_report_failure", extra={"orderId": order.order_id}
            )

    # --- outbound ----------------------------------------------------------

    async def _publish_table_state(self, state: TableState) -> None:
        """Publish retained, so late-joining windows get the state for free."""
        await self._publisher.publish(
            topics.table_state_topic(state.table_id),
            state.to_json(),
            qos=1,
            retain=True,
        )

    def log_state_snapshot(self, reason: str) -> None:
        """Log the whole restaurant, not just the change.

        A per-event log tells you what happened; it does not tell you what the
        restaurant *looks like* without replaying every prior line in your head.
        Emitting a full snapshot on each transition makes the server log a
        self-contained narrative that can be read next to the UI and shown to
        somebody — which is its purpose here.

        Two renderings on one line: ``tables`` is structured for querying, and
        ``summary`` is a compact human-readable form. In text log mode the
        formatter appends both, so the summary reads like a status board.
        """
        if not self._settings.log_state_snapshots:
            return
        tables: dict[str, list[str]] = {}
        parts: list[str] = []
        for state in self._store.all_snapshots():
            entries = [
                f"{order.food_name}:{order.status.value}"
                + (
                    f"({order.cook_seconds}s)"
                    if order.status is OrderStatus.COOKING
                    else ""
                )
                for order in state.orders
            ]
            tables[str(state.table_id)] = entries
            parts.append(f"T{state.table_id}[{', '.join(entries) or '-'}]")
        logger.info(
            "restaurant_state",
            extra={
                "reason": reason,
                "summary": " ".join(parts),
                "tables": tables,
                # Counted from the same state that produced `summary`, not from
                # `in_flight`. A task is still in the task set while its own
                # coroutine runs, so the task count would read 4 on the line where
                # the summary already shows one dish served — two numbers on one
                # line disagreeing about the same instant.
                "cookingNow": self._store.total_cooking_count(),
            },
        )

    async def _publish_status(self, status: KitchenState) -> None:
        await self._publisher.publish(
            topics.KITCHEN_STATUS,
            KitchenStatus(status=status, since=utcnow()).to_json(),
            qos=1,
            retain=True,
        )

    async def _publish_rejection(self, rejection: _Rejection) -> None:
        """Deliver a rejection to its author's private inbox, if we know it."""
        if rejection.client_id is None:
            logger.warning(
                "rejection_undeliverable", extra={"reason": rejection.reason.value}
            )
            return
        message = OrderRejected(
            client_order_id=rejection.client_order_id,
            table_id=rejection.table_id,
            reason=rejection.reason,
            message=rejection.message,
        )
        await self._publisher.publish(
            topics.client_error_topic(rejection.client_id),
            message.to_json(),
            qos=1,
            retain=False,
        )
