"""Process wiring and lifecycle.

This module does three things and nothing else: build the object graph from
configuration, translate OS signals into a stop event, and choose an event loop
implementation. All connection handling lives in `transport.py`, all domain logic
in `kitchen.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable
from types import FrameType

from .config import Settings
from .kitchen import Kitchen
from .logging_config import configure_logging
from .store import create_store
from .transport import create_transport

logger = logging.getLogger(__name__)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to the stop event.

    ``loop.add_signal_handler`` is the correct asyncio mechanism but is not
    implemented on Windows' Proactor loop, so this falls back to the blocking
    ``signal.signal`` API there. ``call_soon_threadsafe`` is required in the
    fallback because that handler runs outside the event loop.
    """
    loop = asyncio.get_running_loop()
    signals: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)

    for sig in signals:
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError):

            def _handler(
                signum: int,
                frame: FrameType | None,
                _loop: asyncio.AbstractEventLoop = loop,
            ) -> None:
                _loop.call_soon_threadsafe(stop.set)

            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _handler)


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)

    store = create_store(settings)
    transport = create_transport(settings)
    # One kitchen for the life of the process: table state and in-flight dishes
    # must survive a reconnect, so neither is tied to a connection.
    kitchen = Kitchen(store, transport.publisher, settings)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    logger.info(
        "kitchen_starting",
        extra={
            "tables": list(settings.table_ids),
            "cookSeconds": [settings.cook_min_seconds, settings.cook_max_seconds],
            "messagingBackend": settings.messaging_backend,
            "storeBackend": settings.store_backend,
        },
    )

    await transport.run(
        # Republishes every table's retained state, which both bootstraps a cold
        # start and repairs anything lost during a disconnect.
        on_connected=kitchen.announce_online,
        on_message=kitchen.handle_message,
        on_stopping=kitchen.shutdown,
        stop=stop,
    )

    logger.info("kitchen_stopped")


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Pick an event loop implementation that paho can actually use.

    paho's asyncio integration registers the socket via ``loop.add_reader()``,
    which is a selector-loop API. Windows defaults to the Proactor loop, where
    ``add_reader`` raises NotImplementedError and the client can never connect.
    Linux — the actual deployment target — uses a selector loop already, so this
    only matters for local development on Windows.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def main() -> None:
    try:
        # asyncio.Runner rather than asyncio.run() so the loop implementation can
        # be chosen without touching the deprecated event-loop policy API.
        with asyncio.Runner(loop_factory=_loop_factory()) as runner:
            runner.run(run())
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C outside the handler
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
