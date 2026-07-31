"""A local MQTT broker with a WebSocket listener, for development and tests.

Why this exists: the browser speaks MQTT over WebSockets, so running this project
needs a broker with a websockets listener. Mosquitto in Docker is the usual
answer, but that makes Docker a prerequisite for running anything. amqtt is a
pure-Python broker, so `pip install -e .[dev]` is the whole setup, and the same
helper starts a real broker inside pytest for hermetic integration tests.

SECURITY: anonymous access is enabled here. This is a development convenience and
is NOT how the deployed system runs — see deploy/mosquitto.conf and
deploy/mosquitto.acl for the authenticated, ACL-restricted configuration, and the
trust model in docs/EVENT-CONTRACT.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import websockets
from amqtt.broker import Broker

DEFAULT_TCP_BIND = "127.0.0.1:1883"
DEFAULT_WS_BIND = "127.0.0.1:9001"

_pings_disabled = False


def disable_websocket_keepalive_pings() -> None:
    """Stop the WebSocket *server* from sending its own Ping control frames.

    amqtt starts its websockets listener with ``websockets.serve()``, which
    defaults to ``ping_interval=20``: every 20 seconds the server sends a
    WebSocket-level Ping and expects a Pong. paho-mqtt's WebSocket support is
    deliberately minimal and does not answer them, so the kitchen's connection was
    being torn down every 20 seconds on the dot and reconnecting forever.

    Disabling them loses nothing: MQTT has its own liveness mechanism in
    PINGREQ/PINGRESP (see ``keepalive_seconds``), which is what a real deployment
    relies on. Production brokers such as Mosquitto and HiveMQ do not exhibit this
    behaviour, so this patch is scoped to the development broker only.

    Idempotent, so importing this module from several tests is safe.
    """
    global _pings_disabled
    if _pings_disabled:
        return

    original_serve = websockets.serve

    def serve_without_pings(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("ping_interval", None)
        kwargs.setdefault("ping_timeout", None)
        return original_serve(*args, **kwargs)

    # amqtt resolves `websockets.serve` at call time and awaits the result, so
    # returning the original awaitable from a plain wrapper is enough.
    websockets.serve = serve_without_pings  # type: ignore[assignment]
    _pings_disabled = True


def broker_config(
    *, ws_bind: str = DEFAULT_WS_BIND, tcp_bind: str | None = DEFAULT_TCP_BIND
) -> dict[str, Any]:
    """Build an amqtt config. Binds to loopback so an anonymous broker is not
    exposed to the local network by accident.

    amqtt requires a listener literally named ``default`` and uses it as the base
    for the others, so the WebSocket listener takes that name — it is the one that
    always has to exist, because a browser cannot speak raw MQTT over TCP. The TCP
    listener is optional and exists for `mosquitto_sub`-style debugging.
    """
    listeners: dict[str, Any] = {"default": {"type": "ws", "bind": ws_bind}}
    if tcp_bind is not None:
        listeners["tcp"] = {"type": "tcp", "bind": tcp_bind}
    return {
        "listeners": listeners,
        # Only the auth plugin is loaded. Omitting BrokerSysPlugin keeps $SYS/#
        # traffic off the wire so logs show only application messages.
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {
                "allow_anonymous": True
            },
        },
    }


@contextlib.asynccontextmanager
async def running_broker(
    *, ws_bind: str = DEFAULT_WS_BIND, tcp_bind: str | None = DEFAULT_TCP_BIND
) -> AsyncIterator[Broker]:
    """Start a broker, yield it, and shut it down cleanly."""
    disable_websocket_keepalive_pings()
    broker = Broker(broker_config(ws_bind=ws_bind, tcp_bind=tcp_bind))
    await broker.start()
    try:
        yield broker
    finally:
        # amqtt's shutdown cancels its own internal tasks and lets the resulting
        # CancelledError escape. `suppress(Exception)` does not catch it, because
        # CancelledError derives from BaseException — so it has to be named
        # explicitly or it surfaces as a spurious teardown failure in every test.
        #
        # Suppressing cancellation is normally wrong; it is acceptable here only
        # because this is dev/test tooling whose sole remaining job is to release a
        # socket, and the alternative is a broker we cannot shut down cleanly.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await broker.shutdown()


@contextlib.asynccontextmanager
async def running_broker_on_free_port(
    *, attempts: int = 25
) -> AsyncIterator[tuple[Broker, int]]:
    """Start a broker on some free port and yield it along with the port.

    Used by the integration tests so each one gets its own broker without a fixed
    port. Retrying is the only honest approach: port availability cannot be checked
    without taking the port, and pre-flighting with a probe socket actively fails on
    Windows, where asyncio does not set ``SO_REUSEADDR`` (it would permit socket
    hijacking) so a just-released port cannot be immediately rebound.
    """
    last_error: OSError | None = None
    for _ in range(attempts):
        port = random.randint(20000, 60000)  # noqa: S311 - test port choice
        started = False
        try:
            async with running_broker(
                ws_bind=f"127.0.0.1:{port}", tcp_bind=None
            ) as broker:
                started = True
                yield broker, port
            return
        except OSError as exc:
            # Only a failure to *start* is worth retrying; an OSError raised by the
            # caller's own body must propagate.
            if started:
                raise
            last_error = exc
    msg = f"could not bind a broker after {attempts} attempts"
    raise OSError(msg) from last_error


async def serve_forever() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    async with running_broker():
        logging.getLogger("dev_broker").info(
            "dev broker listening: tcp=%s ws=%s (anonymous, ws pings disabled)",
            DEFAULT_TCP_BIND,
            DEFAULT_WS_BIND,
        )
        await asyncio.Event().wait()


def main() -> None:
    runner: Callable[[Awaitable[None]], None] = asyncio.run
    with contextlib.suppress(KeyboardInterrupt):
        runner(serve_forever())


if __name__ == "__main__":
    main()
