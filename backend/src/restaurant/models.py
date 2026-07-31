"""Wire format for every MQTT message in the system.

These models are the executable form of docs/EVENT-CONTRACT.md. Inbound payloads
are parsed strictly (unknown fields are an error) so that a malformed or hostile
message is rejected at the edge rather than halfway through the kitchen.

Python uses snake_case, JSON on the wire uses camelCase; the alias generator
bridges the two so neither side has to compromise its own conventions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

MAX_FOOD_NAME_LENGTH = 80
MAX_ID_LENGTH = 64


def utcnow() -> datetime:
    """Timezone-aware now. Naive datetimes are a bug waiting to happen."""
    return datetime.now(UTC)


class OrderStatus(StrEnum):
    COOKING = "COOKING"
    SERVED = "SERVED"
    FAILED = "FAILED"


class KitchenState(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class RejectionReason(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNKNOWN_TABLE = "UNKNOWN_TABLE"
    TABLE_AT_CAPACITY = "TABLE_AT_CAPACITY"
    KITCHEN_AT_CAPACITY = "KITCHEN_AT_CAPACITY"


class _CamelModel(BaseModel):
    """Base for messages we publish."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def to_json(self) -> bytes:
        """Serialise for publishing, using the camelCase wire names."""
        return self.model_dump_json(by_alias=True).encode("utf-8")


class _StrictCamelModel(_CamelModel):
    """Base for messages we receive.

    ``extra="forbid"`` makes the parser fail closed: a payload carrying fields we
    do not recognise is rejected rather than silently half-applied.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


def _reject_control_characters(value: str, field: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


class OrderPlaced(_StrictCamelModel):
    """Inbound: a customer pressed ORDER.

    ``client_order_id`` is an idempotency key. MQTT QoS 1 is *at least once*, so
    the same publish can arrive twice (a lost PUBACK makes the client retransmit).
    Deduplicating on this key is what stops one button press becoming two dishes.
    """

    client_order_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    client_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    table_id: int = Field(ge=0)
    food_name: str
    sent_at: datetime

    @field_validator("food_name")
    @classmethod
    def _clean_food_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("foodName must not be blank")
        if len(cleaned) > MAX_FOOD_NAME_LENGTH:
            raise ValueError(
                f"foodName must be at most {MAX_FOOD_NAME_LENGTH} characters"
            )
        return _reject_control_characters(cleaned, "foodName")

    @field_validator("client_order_id", "client_id")
    @classmethod
    def _clean_identifier(cls, value: str) -> str:
        return _reject_control_characters(value.strip(), "identifier")


class Order(_CamelModel):
    """One dish, with the lifecycle the frontend renders."""

    order_id: str
    food_name: str
    status: OrderStatus
    placed_at: datetime

    #: The random cook time the kitchen assigned to this order, in whole seconds.
    #:
    #: Published so the UI can show a countdown *and* state the assigned duration
    #: outright. To be unambiguous: this is **display data only**. The real timer
    #: is the backend's ``asyncio.sleep``, and an order becomes SERVED only when
    #: the kitchen says so. A client that ignored or forged this field could not
    #: make a dish arrive sooner or later.
    cook_seconds: int = Field(ge=0)

    #: ``placed_at + cook_seconds``, computed server-side so every client agrees
    #: rather than each doing its own arithmetic.
    expected_ready_at: datetime

    ready_at: datetime | None = None


class TableState(_CamelModel):
    """Outbound, retained: the authoritative state of a single table.

    Published in full on every change rather than as a delta. A delta protocol
    needs gap detection and resynchronisation; a full snapshot is idempotent and
    the payload stays bounded because orders per table are capped.
    """

    table_id: int

    #: Identifies the kitchen *generation* that produced this state.
    #:
    #: State is in-memory, so a kitchen restart resets `version` to zero. Without
    #: this field a client holding version 5 would reject the restarted kitchen's
    #: version 0 as stale and display dead orders forever. A client compares
    #: versions only within the same epoch; a changed epoch is always accepted.
    epoch: str

    version: int
    updated_at: datetime
    orders: list[Order] = Field(default_factory=list)


class OrderRejected(_CamelModel):
    """Outbound: why a specific order was refused, sent only to its author."""

    client_order_id: str | None
    table_id: int | None
    reason: RejectionReason
    message: str


class KitchenStatus(_CamelModel):
    """Outbound, retained: also registered as the client's Last Will.

    If the backend dies without saying goodbye, the broker publishes OFFLINE on
    its behalf, so the UI can tell "nothing cooking" apart from "kitchen gone".
    """

    status: KitchenState
    since: datetime
