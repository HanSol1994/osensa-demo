"""End-to-end over a real broker.

Everything else stubs the transport. This starts an actual MQTT broker with a
WebSocket listener inside the test process, runs the real MqttTransport against it,
and drives it with a separate real MQTT client — so the pieces the unit tests
deliberately mock (serialisation, topic routing, retention, QoS) are exercised for
real, with no external services and therefore no CI dependencies.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiomqtt
import pytest
from tools.dev_broker import running_broker_on_free_port

from restaurant.config import Settings
from restaurant.kitchen import Kitchen
from restaurant.store import InMemoryStore
from restaurant.transport import MqttTransport
from tests.helpers import wait_until


@pytest.fixture
async def broker() -> AsyncIterator[int]:
    """A real broker per test, on whatever port is free. See the helper's docstring
    for why finding that port needs a retry rather than a check."""
    async with running_broker_on_free_port() as (_, port):
        yield port


def settings_for(port: int, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "broker_host": "127.0.0.1",
        "broker_port": port,
        "broker_transport": "websockets",
        "broker_ws_path": "/mqtt",
        "broker_use_tls": False,
        "table_count": 3,
        "cook_min_seconds": 0,
        "cook_max_seconds": 0,
        "log_state_snapshots": False,
        "client_id": f"kitchen-test-{port}",
    }
    base.update(overrides)
    return Settings(**base)


async def wait_for(predicate: Callable[[], bool]) -> None:
    """Real broker round trips are slower than the in-process tests, so this uses a
    longer bound than the unit-test default."""
    await wait_until(predicate, timeout=10.0, interval=0.05)


class Harness:
    """A running kitchen plus a real client to poke it with."""

    def __init__(self, client: aiomqtt.Client, store: InMemoryStore) -> None:
        self.client = client
        self.store = store
        self.states: dict[int, dict[str, Any]] = {}
        self.status: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

    async def consume(self) -> None:
        async for message in self.client.messages:
            topic = str(message.topic)
            body = json.loads(bytes(message.payload))
            if topic.endswith("/state"):
                self.states[body["tableId"]] = body
            elif topic == "restaurant/kitchen/status":
                self.status.append(body)
            elif topic.endswith("/err"):
                self.errors.append(body)

    async def order(self, table_id: int, food: str, client_order_id: str) -> None:
        await self.client.publish(
            f"restaurant/table/{table_id}/order",
            json.dumps(
                {
                    "clientOrderId": client_order_id,
                    "clientId": "probe",
                    "tableId": table_id,
                    "foodName": food,
                    "sentAt": "2026-01-01T00:00:00Z",
                }
            ).encode(),
            qos=1,
        )

    def served(self, table_id: int) -> list[str]:
        table = self.states.get(table_id, {})
        return [
            o["foodName"] for o in table.get("orders", []) if o["status"] == "SERVED"
        ]


@pytest.fixture
async def harness(broker: int) -> AsyncIterator[Harness]:
    port = broker
    settings = settings_for(port)
    store = InMemoryStore(settings.table_ids)
    transport = MqttTransport(settings)
    kitchen = Kitchen(store, transport.publisher, settings, delay_provider=lambda: 0)
    stop = asyncio.Event()

    runner = asyncio.create_task(
        transport.run(
            on_connected=kitchen.announce_online,
            on_message=kitchen.handle_message,
            on_stopping=kitchen.shutdown,
            stop=stop,
        )
    )

    async with aiomqtt.Client(
        hostname="127.0.0.1",
        port=port,
        transport="websockets",
        websocket_path="/mqtt",
        identifier=f"probe-{port}",
    ) as client:
        harness = Harness(client, store)
        consumer = asyncio.create_task(harness.consume())
        await client.subscribe("restaurant/table/+/state", qos=1)
        await client.subscribe("restaurant/kitchen/status", qos=1)
        await client.subscribe("restaurant/client/probe/err", qos=1)

        # Wait for the kitchen's baseline publish so tests start from a known state.
        await wait_for(lambda: len(harness.states) == 3)

        try:
            yield harness
        finally:
            consumer.cancel()
            stop.set()
            await asyncio.wait_for(runner, timeout=10)


class TestEndToEnd:
    async def test_a_late_subscriber_receives_retained_state(
        self, harness: Harness
    ) -> None:
        # This client subscribed AFTER the kitchen published. It still knows every
        # table — which is exactly the multi-window requirement, and it comes from
        # retained messages rather than any application-level replay.
        assert sorted(harness.states) == [1, 2, 3]
        assert any(s["status"] == "ONLINE" for s in harness.status)

    async def test_an_order_round_trips(self, harness: Harness) -> None:
        await harness.order(1, "Ramen", "c1")
        await wait_for(lambda: harness.served(1) == ["Ramen"])

    async def test_concurrent_orders_all_complete(self, harness: Harness) -> None:
        for table_id in (1, 2, 3):
            await harness.order(table_id, f"Dish {table_id}", f"c{table_id}")
        await wait_for(lambda: all(harness.served(t) for t in (1, 2, 3)))

    async def test_a_duplicate_publish_yields_one_dish(self, harness: Harness) -> None:
        await harness.order(2, "Tacos", "same-id")
        await wait_for(lambda: harness.served(2) == ["Tacos"])
        await harness.order(2, "Tacos", "same-id")
        await asyncio.sleep(0.5)
        assert harness.served(2) == ["Tacos"]

    async def test_an_invalid_order_returns_a_rejection(self, harness: Harness) -> None:
        await harness.order(1, "   ", "bad")
        await wait_for(lambda: harness.errors != [])
        assert harness.errors[0]["reason"] == "VALIDATION_FAILED"

    async def test_an_unknown_table_is_rejected(self, harness: Harness) -> None:
        await harness.order(99, "Ghost", "ghost")
        await wait_for(
            lambda: any(e["reason"] == "UNKNOWN_TABLE" for e in harness.errors)
        )
        assert 99 not in harness.states

    async def test_malformed_json_does_not_stop_the_consumer(
        self, harness: Harness
    ) -> None:
        await harness.client.publish("restaurant/table/1/order", b"not json", qos=1)
        # The consumer must still be alive afterwards; that is the whole point of
        # handle_message never raising.
        await harness.order(1, "Recovered", "after-garbage")
        await wait_for(lambda: "Recovered" in harness.served(1))

    async def test_reset_clears_every_table_and_bumps_the_epoch(
        self, harness: Harness
    ) -> None:
        await harness.order(1, "Ramen", "r1")
        await wait_for(lambda: harness.served(1) == ["Ramen"])
        epoch_before = harness.states[1]["epoch"]

        await harness.client.publish(
            "restaurant/kitchen/reset", b'{"requestedBy":"probe"}', qos=1
        )

        await wait_for(lambda: harness.states[1]["orders"] == [])
        # Versions restart at zero, so a new epoch is what makes the reset
        # acceptable to a client instead of looking like stale state.
        assert harness.states[1]["version"] == 0
        assert harness.states[1]["epoch"] != epoch_before
