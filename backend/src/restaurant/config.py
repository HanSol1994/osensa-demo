"""Configuration, entirely environment-driven.

Nothing about the deployment target is baked into the code: the same image runs
against a local broker over plain WebSockets and against a managed cloud broker
over TLS, decided by env vars alone. That is what keeps the hosting choice
reversible.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESTAURANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Broker connection -------------------------------------------------
    broker_host: str = "localhost"
    broker_port: int = 9001
    broker_transport: Literal["websockets", "tcp"] = "websockets"
    broker_ws_path: str = "/mqtt"
    broker_use_tls: bool = False
    broker_username: str | None = None
    broker_password: str | None = None

    #: MQTT client identifier for the kitchen. Fixed rather than random so that a
    #: reconnecting kitchen resumes its own session instead of leaking sessions on
    #: the broker.
    client_id: str = "restaurant-kitchen"

    keepalive_seconds: int = 30

    #: Seconds to wait before retrying a dropped broker connection. The service
    #: must survive the broker restarting without needing a restart itself.
    reconnect_delay_seconds: float = 3.0

    # --- Pluggable implementations -----------------------------------------
    #: Selects a Transport implementation via transport.create_transport. Only
    #: MQTT exists today; the assignment forbids REST. The seam is what makes a
    #: second transport additive rather than invasive.
    messaging_backend: Literal["mqtt"] = "mqtt"

    #: Selects a Store implementation via store.create_store. In-memory is what
    #: the assignment permits; see docs/FUTURE-WORK.md §4 for what persistence
    #: would force to change.
    store_backend: Literal["memory"] = "memory"

    # --- Restaurant --------------------------------------------------------
    table_count: int = Field(default=4, ge=1, le=64)

    #: Whole seconds. Integers rather than floats because the cook time is shown
    #: to people ("of 23s"), and a rendered 28.42885786840063 is noise, not
    #: precision. Rounding at the display layer instead would mean the number in
    #: the UI and the number in the log could disagree.
    cook_min_seconds: int = Field(default=10, ge=0)
    cook_max_seconds: int = Field(default=30, ge=0)

    #: Whether every state change also logs a full snapshot of the restaurant.
    #:
    #: On for demos: it makes the server log a self-contained narrative that can be
    #: shown alongside the UI. Something to turn off at scale, where snapshotting
    #: all state on every change is quadratic noise.
    log_state_snapshots: bool = True

    #: Whether the `restaurant/kitchen/reset` command is honoured.
    #:
    #: Enabled by default because this is a demo: being able to clear the board
    #: and start over is the difference between a smooth walkthrough and
    #: restarting processes mid-conversation. It is destructive and unauthenticated
    #: though, so it is a config flag rather than an unconditional feature, and the
    #: broker ACL should not grant it to the public web client in a real
    #: deployment. See docs/SECURITY.md.
    enable_reset: bool = True

    # --- Limits (see EVENT-CONTRACT.md §5) ---------------------------------
    max_orders_per_table: int = Field(default=5, ge=1)
    max_orders_global: int = Field(default=100, ge=1)
    dedupe_cache_size: int = Field(default=1000, ge=1)

    # --- Observability -----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    @model_validator(mode="after")
    def _check_cook_range(self) -> Settings:
        if self.cook_min_seconds > self.cook_max_seconds:
            raise ValueError(
                "RESTAURANT_COOK_MIN_SECONDS must not exceed "
                "RESTAURANT_COOK_MAX_SECONDS"
            )
        return self

    @property
    def table_ids(self) -> tuple[int, ...]:
        """Tables are numbered 1..N because customers do not count from zero."""
        return tuple(range(1, self.table_count + 1))
