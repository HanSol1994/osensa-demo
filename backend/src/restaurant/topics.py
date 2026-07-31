"""Topic construction and parsing.

Topic strings are the API surface of an MQTT system, so they live in one module
instead of being scattered as literals. Parsing is defensive: a topic is
attacker-controlled input, and `int(...)` on an arbitrary segment raises.
"""

from __future__ import annotations

PREFIX = "restaurant"

#: Backend subscribes here. `+` matches exactly one topic level, so new tables
#: need no subscription change.
ORDER_SUBSCRIPTION = f"{PREFIX}/table/+/order"

KITCHEN_STATUS = f"{PREFIX}/kitchen/status"

#: Inbound command: clear all state and start over. A demo affordance, not a
#: customer action — see Settings.enable_reset and docs/SECURITY.md.
KITCHEN_RESET = f"{PREFIX}/kitchen/reset"

#: Every filter the kitchen subscribes to. Kept here so the transport does not
#: need to know what the application cares about.
INBOUND_SUBSCRIPTIONS: tuple[str, ...] = (ORDER_SUBSCRIPTION, KITCHEN_RESET)


def order_topic(table_id: int) -> str:
    return f"{PREFIX}/table/{table_id}/order"


def table_state_topic(table_id: int) -> str:
    return f"{PREFIX}/table/{table_id}/state"


def client_error_topic(client_id: str) -> str:
    return f"{PREFIX}/client/{client_id}/err"


def is_own_outbound_topic(topic: str) -> bool:
    """True if this is a topic *we* publish rather than consume.

    Needed because a broker may deliver our own published messages back to us.
    amqtt in particular replays retained messages for every topic filter known to
    the broker — including filters belonging to *other* sessions — whenever any
    client connects (see amqtt/broker.py, "publish retained messages to a
    pre-existing session's subscriptions"). Once a browser has subscribed to the
    state topics, the kitchen starts receiving them on connect too.

    Recognising them keeps the log honest: this is expected broker chatter, not
    the unexpected traffic that a warning should be reserved for.
    """
    parts = topic.split("/")
    if parts[0] != PREFIX:
        return False
    if len(parts) == 4 and parts[1] == "table" and parts[3] == "state":
        return True
    if len(parts) == 4 and parts[1] == "client" and parts[3] == "err":
        return True
    return topic == KITCHEN_STATUS


def parse_table_id_from_order_topic(topic: str) -> int | None:
    """Extract the table id from `restaurant/table/{id}/order`.

    Returns None for anything that does not match exactly, rather than raising,
    because the caller's job is to log and drop unparseable traffic — not to
    crash the consumer loop over it.
    """
    parts = topic.split("/")
    if len(parts) != 4:
        return None
    if parts[0] != PREFIX or parts[1] != "table" or parts[3] != "order":
        return None
    if not parts[2].isdigit():
        return None
    return int(parts[2])
