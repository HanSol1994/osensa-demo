"""Shared fixtures.

The important one is ``kitchen``: it wires the real Kitchen against a fake
publisher and a zero-second delay provider, so the whole domain — validation,
limits, concurrency, cooking, reset — is exercised with no broker, no network and
no waiting.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from restaurant.config import Settings
from restaurant.kitchen import Kitchen
from restaurant.store import InMemoryStore
from tests.helpers import FakePublisher


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Force a selector event loop on Windows.

    paho registers its socket with ``loop.add_reader()``, which the Windows
    Proactor loop (the default) does not implement, so any test touching a real
    MQTT client fails there. Linux already uses a selector loop, so this is a
    local-development concern only.
    """
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def settings() -> Settings:
    # Zero cook time keeps tests instant. Limits are set low so capacity behaviour
    # can be exercised in a few calls rather than a hundred.
    return Settings(
        table_count=4,
        cook_min_seconds=0,
        cook_max_seconds=0,
        max_orders_per_table=3,
        max_orders_global=5,
        log_state_snapshots=False,
        enable_reset=True,
    )


@pytest.fixture
def store(settings: Settings) -> InMemoryStore:
    return InMemoryStore(settings.table_ids, dedupe_cache_size=10)


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def kitchen(
    store: InMemoryStore, publisher: FakePublisher, settings: Settings
) -> Kitchen:
    return Kitchen(store, publisher, settings, delay_provider=lambda: 0)
