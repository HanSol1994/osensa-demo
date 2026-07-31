"""Shared test doubles and utilities.

Kept out of conftest.py deliberately: pytest loads conftest through its own
mechanism, so importing it as a normal module would create a second copy and
duplicate the fixtures defined there.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any


class FakePublisher:
    """Records publishes instead of sending them.

    This is the seam that makes the whole domain testable without a broker: the
    Kitchen only ever needs something with a `publish` coroutine.
    """

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def publish(
        self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
    ) -> None:
        self.messages.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )

    def on(self, topic: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["topic"] == topic]

    def decoded(self, topic: str) -> list[dict[str, Any]]:
        return [json.loads(m["payload"]) for m in self.on(topic)]

    def latest(self, topic: str) -> dict[str, Any] | None:
        decoded = self.decoded(topic)
        return decoded[-1] if decoded else None

    def clear(self) -> None:
        self.messages.clear()


def order_payload(
    *,
    table_id: int = 1,
    food: str = "Ramen",
    client_order_id: str = "coid-1",
    client_id: str = "client-1",
    extra: dict[str, Any] | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "clientOrderId": client_order_id,
        "clientId": client_id,
        "tableId": table_id,
        "foodName": food,
        "sentAt": "2026-01-01T00:00:00Z",
    }
    if extra:
        body.update(extra)
    return json.dumps(body).encode()


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.01
) -> None:
    """Poll until a condition holds, rather than sleeping a guessed duration.

    A guessed sleep is either flaky or slow; this is neither. A genuine hang fails
    with a clear assertion instead of stalling the suite.
    """
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(interval)
    except TimeoutError as exc:
        msg = f"condition was not met within {timeout}s"
        raise AssertionError(msg) from exc
