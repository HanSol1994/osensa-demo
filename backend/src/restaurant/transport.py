"""Messaging transport: the seam between the kitchen and the wire.

The ``Kitchen`` never imports MQTT. It depends on a ``Publisher`` for outbound
messages and is *driven* by a ``Transport`` for inbound ones, so the entire domain
is transport-agnostic and testable with no broker.

``create_transport`` is the factory. Adding a second transport (see
docs/FUTURE-WORK.md §5) means writing one class and adding one case here; no
domain code changes. One honest caveat recorded there: a request/response
transport such as REST is a poor fit for the server-initiated FOOD event, so the
abstraction makes substitution *possible*, not automatically *appropriate*.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import aiomqtt

from . import topics
from .config import Settings
from .kitchen import Publisher
from .models import KitchenState, KitchenStatus, utcnow

logger = logging.getLogger(__name__)

#: Called for every inbound message. Must not raise.
OnMessage = Callable[[str, bytes], Awaitable[None]]
#: Called after each successful (re)connection and subscription.
OnConnected = Callable[[], Awaitable[None]]
#: Called once, while still connected, when a shutdown has been requested.
OnStopping = Callable[[], Awaitable[None]]


class Transport(Protocol):
    """A bidirectional event transport with its own connection lifecycle."""

    @property
    def publisher(self) -> Publisher:
        """Outbound side, valid for the transport's whole lifetime."""
        ...

    async def run(
        self,
        *,
        on_connected: OnConnected,
        on_message: OnMessage,
        on_stopping: OnStopping,
        stop: asyncio.Event,
    ) -> None:
        """Serve until ``stop`` is set, reconnecting as needed."""
        ...


class ReconnectingPublisher:
    """A ``Publisher`` whose underlying client is swapped on each reconnect.

    This exists so cooking tasks survive a broker outage: they hold a reference to
    this object, not to a client, so a dish 8 seconds into a 25-second cook when
    the connection dropped is still served on the new connection.

    A publish attempted while disconnected is logged and dropped rather than
    queued. That is safe *specifically because* table state is retained and
    republished in full on every reconnect, so the lost message is corrected
    moments later by the resync. A bounded outbound queue is listed as future work
    for the case where that window matters.
    """

    def __init__(self) -> None:
        self._client: aiomqtt.Client | None = None

    def attach(self, client: aiomqtt.Client) -> None:
        self._client = client

    def detach(self) -> None:
        self._client = None

    async def publish(
        self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
    ) -> None:
        client = self._client
        if client is None:
            logger.warning("publish_dropped_disconnected", extra={"topic": topic})
            return
        try:
            await client.publish(topic, payload, qos=qos, retain=retain)
        except aiomqtt.MqttError as exc:
            # Not fatal: see class docstring on resync.
            logger.warning("publish_failed", extra={"topic": topic, "error": str(exc)})


class MqttTransport:
    """MQTT over WebSockets (or TCP), with supervised reconnection."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._publisher = ReconnectingPublisher()

    @property
    def publisher(self) -> Publisher:
        return self._publisher

    def _build_client(self) -> aiomqtt.Client:
        """Construct the client, including its Last Will.

        The Last Will is handed to the broker at CONNECT time. If this process is
        killed or its network disappears, the *broker* publishes OFFLINE on the
        status topic for us — the one message we cannot send ourselves.
        """
        settings = self._settings
        will = aiomqtt.Will(
            topic=topics.KITCHEN_STATUS,
            payload=KitchenStatus(
                status=KitchenState.OFFLINE, since=utcnow()
            ).to_json(),
            qos=1,
            retain=True,
        )
        return aiomqtt.Client(
            hostname=settings.broker_host,
            port=settings.broker_port,
            transport=settings.broker_transport,
            websocket_path=(
                settings.broker_ws_path
                if settings.broker_transport == "websockets"
                else None
            ),
            username=settings.broker_username,
            password=settings.broker_password,
            identifier=settings.client_id,
            keepalive=settings.keepalive_seconds,
            will=will,
            # Defaults verify the certificate; nothing here disables that.
            tls_params=aiomqtt.TLSParameters() if settings.broker_use_tls else None,
        )

    async def run(
        self,
        *,
        on_connected: OnConnected,
        on_message: OnMessage,
        on_stopping: OnStopping,
        stop: asyncio.Event,
    ) -> None:
        settings = self._settings
        while not stop.is_set():
            try:
                await self._session(
                    on_connected=on_connected,
                    on_message=on_message,
                    on_stopping=on_stopping,
                    stop=stop,
                )
            except aiomqtt.MqttError as exc:
                if stop.is_set():
                    break
                logger.warning(
                    "broker_connection_lost",
                    extra={
                        "error": str(exc),
                        "retryIn": settings.reconnect_delay_seconds,
                    },
                )
                # Wait on the stop event rather than sleeping blindly, so Ctrl-C
                # during a reconnect backoff is honoured immediately.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.reconnect_delay_seconds
                    )

    async def _session(
        self,
        *,
        on_connected: OnConnected,
        on_message: OnMessage,
        on_stopping: OnStopping,
        stop: asyncio.Event,
    ) -> None:
        """One broker connection, from CONNECT to disconnect."""
        settings = self._settings
        async with self._build_client() as client:
            self._publisher.attach(client)
            logger.info(
                "broker_connected",
                extra={
                    "host": settings.broker_host,
                    "port": settings.broker_port,
                    "transport": settings.broker_transport,
                    "tls": settings.broker_use_tls,
                },
            )
            try:
                for topic_filter in topics.INBOUND_SUBSCRIPTIONS:
                    await client.subscribe(topic_filter, qos=1)
                await on_connected()

                consume_task = asyncio.create_task(
                    self._consume(client, on_message), name="consume"
                )
                stop_task = asyncio.create_task(stop.wait(), name="stop-signal")
                done, pending = await asyncio.wait(
                    {consume_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                if stop.is_set():
                    # Ordered deliberately: say goodbye while the connection is
                    # still open, so the message actually reaches the broker
                    # instead of relying on the Last Will.
                    await on_stopping()
                if consume_task in done:
                    # Surfaces MqttError so the supervisor can reconnect.
                    consume_task.result()
            finally:
                self._publisher.detach()

    @staticmethod
    async def _consume(client: aiomqtt.Client, on_message: OnMessage) -> None:
        """Feed every inbound message to the handler.

        ``on_message`` is awaited, but it only validates and publishes — the long
        cook is spawned as its own task inside the kitchen. So this loop is never
        blocked by cooking and orders genuinely overlap.
        """
        async for message in client.messages:
            await on_message(str(message.topic), bytes(message.payload or b""))


def create_transport(settings: Settings) -> Transport:
    """Select a transport implementation from configuration."""
    match settings.messaging_backend:
        case "mqtt":
            return MqttTransport(settings)
        case unknown:  # pragma: no cover - unreachable while the Literal has one member
            msg = f"Unsupported messaging backend: {unknown!r}"
            raise ValueError(msg)
