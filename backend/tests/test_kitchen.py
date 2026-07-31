"""Kitchen behaviour: validation, limits, concurrency, cooking, reset.

All of it runs with no broker and no waiting, because the kitchen depends on a
Publisher protocol and an injectable delay provider.
"""

from __future__ import annotations

import asyncio

import pytest

from restaurant.config import Settings
from restaurant.kitchen import Kitchen
from restaurant.store import InMemoryStore
from restaurant.topics import (
    KITCHEN_RESET,
    KITCHEN_STATUS,
    client_error_topic,
    order_topic,
    table_state_topic,
)
from tests.helpers import FakePublisher, order_payload, wait_until


def cooking_names(publisher: FakePublisher, table_id: int) -> list[str]:
    latest = publisher.latest(table_state_topic(table_id))
    if latest is None:
        return []
    return [o["foodName"] for o in latest["orders"] if o["status"] == "COOKING"]


def served_names(publisher: FakePublisher, table_id: int) -> list[str]:
    latest = publisher.latest(table_state_topic(table_id))
    if latest is None:
        return []
    return [o["foodName"] for o in latest["orders"] if o["status"] == "SERVED"]


class TestLifecycle:
    async def test_announce_publishes_every_table_retained_plus_status(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        await kitchen.announce_online()

        for table_id in (1, 2, 3, 4):
            messages = publisher.on(table_state_topic(table_id))
            assert messages, f"table {table_id} was not published"
            # Retained is what lets a late-joining browser learn the state.
            assert messages[-1]["retain"] is True

        status = publisher.latest(KITCHEN_STATUS)
        assert status is not None
        assert status["status"] == "ONLINE"

    async def test_shutdown_announces_offline(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        await kitchen.shutdown()
        status = publisher.latest(KITCHEN_STATUS)
        assert status is not None
        assert status["status"] == "OFFLINE"


class TestHappyPath:
    async def test_an_order_is_accepted_then_served(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        await kitchen.handle_message(order_topic(1), order_payload(food="Ramen"))

        assert cooking_names(publisher, 1) == ["Ramen"]
        await wait_until(lambda: served_names(publisher, 1) == ["Ramen"])

    async def test_the_assigned_cook_time_is_published(
        self, store: InMemoryStore, publisher: FakePublisher, settings: Settings
    ) -> None:
        # The countdown needs cookSeconds in the FIRST state message, so the value
        # is drawn at acceptance rather than when the cooking task starts.
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 7)
        await kitchen.handle_message(order_topic(1), order_payload())

        state = publisher.latest(table_state_topic(1))
        assert state is not None
        assert state["orders"][0]["cookSeconds"] == 7
        assert state["orders"][0]["expectedReadyAt"] > state["orders"][0]["placedAt"]

    async def test_state_is_published_retained(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        await kitchen.handle_message(order_topic(1), order_payload())
        assert publisher.on(table_state_topic(1))[-1]["retain"] is True

    async def test_version_advances_across_publishes(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        await kitchen.handle_message(order_topic(1), order_payload())
        await wait_until(lambda: len(publisher.decoded(table_state_topic(1))) >= 2)
        versions = [s["version"] for s in publisher.decoded(table_state_topic(1))]
        assert versions == sorted(versions)
        assert versions[-1] > versions[0]


class TestConcurrency:
    async def test_orders_on_different_tables_overlap(
        self, store: InMemoryStore, publisher: FakePublisher, settings: Settings
    ) -> None:
        # A long cook time with no waiting: if the consumer awaited the cook, the
        # second order could not be accepted at all.
        never = 3600
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: never)

        for table_id in (1, 2, 3, 4):
            await kitchen.handle_message(
                order_topic(table_id),
                order_payload(table_id=table_id, client_order_id=f"c{table_id}"),
            )

        assert kitchen.in_flight == 4
        assert store.total_cooking_count() == 4
        await kitchen.shutdown()

    async def test_a_slow_dish_does_not_block_a_fast_one(
        self, store: InMemoryStore, publisher: FakePublisher, settings: Settings
    ) -> None:
        delays = iter([3600, 0])
        kitchen = Kitchen(
            store, publisher, settings, delay_provider=lambda: next(delays)
        )

        await kitchen.handle_message(
            order_topic(1), order_payload(client_order_id="slow")
        )
        await kitchen.handle_message(
            order_topic(2), order_payload(table_id=2, client_order_id="fast")
        )

        # Table 2 is served while table 1 is still cooking.
        await wait_until(lambda: len(served_names(publisher, 2)) == 1)
        assert cooking_names(publisher, 1) != []
        await kitchen.shutdown()


class TestIdempotency:
    async def test_a_duplicate_client_order_id_produces_one_dish(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        # QoS 1 is at-least-once, so this is a legitimate redelivery.
        payload = order_payload(client_order_id="same")
        await kitchen.handle_message(order_topic(1), payload)
        await kitchen.handle_message(order_topic(1), payload)

        state = publisher.latest(table_state_topic(1))
        assert state is not None
        assert len(state["orders"]) == 1

    async def test_a_duplicate_is_not_reported_as_an_error(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        payload = order_payload(client_order_id="same", client_id="cid")
        await kitchen.handle_message(order_topic(1), payload)
        publisher.clear()
        await kitchen.handle_message(order_topic(1), payload)
        assert publisher.on(client_error_topic("cid")) == []


class TestRejections:
    async def test_unknown_table_is_rejected_to_the_sender(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        await kitchen.handle_message(
            order_topic(99), order_payload(table_id=99, client_id="cid")
        )
        rejections = publisher.decoded(client_error_topic("cid"))
        assert [r["reason"] for r in rejections] == ["UNKNOWN_TABLE"]

    async def test_payload_topic_mismatch_is_rejected(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        # tableId appears in both places; trusting either blindly would be wrong.
        await kitchen.handle_message(
            order_topic(1), order_payload(table_id=2, client_id="cid")
        )
        rejections = publisher.decoded(client_error_topic("cid"))
        assert [r["reason"] for r in rejections] == ["VALIDATION_FAILED"]

    async def test_invalid_payload_still_reaches_the_sender(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        # The reply address is salvaged from the raw JSON before strict validation,
        # so a validation failure is reported rather than vanishing into the logs.
        await kitchen.handle_message(
            order_topic(1), order_payload(food="   ", client_id="cid")
        )
        rejections = publisher.decoded(client_error_topic("cid"))
        assert [r["reason"] for r in rejections] == ["VALIDATION_FAILED"]

    async def test_table_capacity_is_enforced(
        self, kitchen: Kitchen, publisher: FakePublisher, settings: Settings
    ) -> None:
        never = 3600
        kitchen._delay_provider = lambda: never

        for i in range(settings.max_orders_per_table):
            await kitchen.handle_message(
                order_topic(1), order_payload(client_order_id=f"ok{i}", client_id="cid")
            )
        await kitchen.handle_message(
            order_topic(1), order_payload(client_order_id="over", client_id="cid")
        )

        reasons = [r["reason"] for r in publisher.decoded(client_error_topic("cid"))]
        assert reasons == ["TABLE_AT_CAPACITY"]
        await kitchen.shutdown()

    async def test_global_capacity_is_enforced(
        self, store: InMemoryStore, publisher: FakePublisher
    ) -> None:
        settings = Settings(
            table_count=4,
            cook_min_seconds=0,
            cook_max_seconds=0,
            max_orders_per_table=10,
            max_orders_global=2,
            log_state_snapshots=False,
        )
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 3600)

        for i in range(3):
            await kitchen.handle_message(
                order_topic(1), order_payload(client_order_id=f"c{i}", client_id="cid")
            )

        reasons = [r["reason"] for r in publisher.decoded(client_error_topic("cid"))]
        assert reasons == ["KITCHEN_AT_CAPACITY"]
        await kitchen.shutdown()


class TestMalformedInput:
    @pytest.mark.parametrize(
        "payload",
        [b"", b"not json", b"[]", b'"a string"', b"null", b"{", b"\xff\xfe"],
    )
    async def test_never_raises_on_garbage(
        self, kitchen: Kitchen, payload: bytes
    ) -> None:
        # The consumer loop must survive anything a client can send.
        await kitchen.handle_message(order_topic(1), payload)

    async def test_garbage_creates_no_order(
        self, kitchen: Kitchen, store: InMemoryStore
    ) -> None:
        await kitchen.handle_message(order_topic(1), b"not json")
        assert store.total_cooking_count() == 0

    async def test_undeliverable_rejection_is_dropped_not_raised(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        # No clientId means nobody to apologise to.
        await kitchen.handle_message(order_topic(1), b"{}")
        assert publisher.messages == []

    async def test_messages_on_our_own_outbound_topics_are_ignored(
        self, kitchen: Kitchen, store: InMemoryStore
    ) -> None:
        # Some brokers echo retained publishes back to the publisher.
        await kitchen.handle_message(table_state_topic(1), b"{}")
        await kitchen.handle_message(KITCHEN_STATUS, b"{}")
        assert store.total_cooking_count() == 0


class TestReset:
    async def test_reset_cancels_cooking_and_clears_tables(
        self, store: InMemoryStore, publisher: FakePublisher, settings: Settings
    ) -> None:
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 3600)
        await kitchen.handle_message(order_topic(1), order_payload())
        assert store.total_cooking_count() == 1
        epoch_before = store.snapshot(1).epoch

        await kitchen.handle_message(KITCHEN_RESET, b'{"requestedBy":"cid"}')

        assert store.total_cooking_count() == 0
        assert kitchen.in_flight == 0
        assert store.snapshot(1).epoch != epoch_before
        assert publisher.latest(table_state_topic(1))["orders"] == []

    async def test_reset_can_be_disabled(
        self, store: InMemoryStore, publisher: FakePublisher
    ) -> None:
        settings = Settings(
            table_count=4,
            cook_min_seconds=0,
            cook_max_seconds=0,
            enable_reset=False,
            log_state_snapshots=False,
        )
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 3600)
        await kitchen.handle_message(order_topic(1), order_payload())

        await kitchen.handle_message(KITCHEN_RESET, b"{}")

        assert store.total_cooking_count() == 1
        await kitchen.shutdown()

    async def test_reset_survives_a_malformed_payload(
        self, kitchen: Kitchen, store: InMemoryStore
    ) -> None:
        # The payload is logged, never trusted, so it cannot break the command.
        await kitchen.handle_message(KITCHEN_RESET, b"not json")
        assert store.total_cooking_count() == 0


class TestCookTaskHygiene:
    async def test_finished_tasks_are_released(
        self, kitchen: Kitchen, publisher: FakePublisher
    ) -> None:
        # The task set must not grow without bound; the done-callback discards.
        await kitchen.handle_message(order_topic(1), order_payload())
        await wait_until(lambda: kitchen.in_flight == 0)

    async def test_shutdown_cancels_in_flight_cooking(
        self, store: InMemoryStore, publisher: FakePublisher, settings: Settings
    ) -> None:
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 3600)
        await kitchen.handle_message(order_topic(1), order_payload())
        assert kitchen.in_flight == 1

        await kitchen.shutdown()
        assert kitchen.in_flight == 0

    async def test_a_failing_publish_does_not_kill_the_kitchen(
        self, store: InMemoryStore, settings: Settings
    ) -> None:
        class BrokenPublisher:
            def __init__(self) -> None:
                self.calls = 0

            async def publish(
                self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
            ) -> None:
                self.calls += 1
                raise RuntimeError("broker exploded")

        publisher = BrokenPublisher()
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 0)

        # Must not propagate: handle_message is the consumer loop's boundary.
        await kitchen.handle_message(order_topic(1), order_payload())
        await asyncio.sleep(0.05)
        assert publisher.calls >= 1


class TestStateSnapshotLogging:
    async def test_snapshot_logging_is_configurable(
        self,
        store: InMemoryStore,
        publisher: FakePublisher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        settings = Settings(
            table_count=2,
            cook_min_seconds=0,
            cook_max_seconds=0,
            log_state_snapshots=True,
        )
        kitchen = Kitchen(store, publisher, settings, delay_provider=lambda: 0)
        with caplog.at_level("INFO"):
            kitchen.log_state_snapshot("test")
        assert any(r.message == "restaurant_state" for r in caplog.records)

    async def test_snapshot_logging_can_be_turned_off(
        self, kitchen: Kitchen, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            kitchen.log_state_snapshot("test")
        assert not any(r.message == "restaurant_state" for r in caplog.records)
