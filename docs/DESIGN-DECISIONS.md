# Design Decisions and Trade-offs

The full reasoning behind each choice. The README carries a condensed version; this is
the long form, including the options that were rejected and why.

---

## Retained messages instead of a snapshot endpoint

The broker stores the last message on a topic and delivers it to anyone who subscribes
*later*. Publishing each table's state as a retained message is the entire answer to
"open a second window and it must already know what is cooking" — no snapshot endpoint,
no replay protocol, no window-to-window sync.

**Trade-off:** retained state can outlive the process that produced it. A stale retained
message describing a dish nobody is cooking is why `epoch` exists, below.

## `version` and `epoch` on every table

QoS 1 is *at-least-once*, so the same message can arrive twice; and on reconnect the
retained message and a live update arrive by two paths with no ordering guarantee
between them. So each table carries a monotonic `version`, and the client discards
anything not strictly newer.

`epoch` bounds that comparison. State is in-memory, so restarting the kitchen resets
versions to zero — and a client holding version 5 would reject the restarted kitchen's
version 0 as stale **forever**, displaying dead orders permanently. Same epoch →
compare versions; different epoch → accept unconditionally. Found by restarting the
backend with a browser open, not by reading the code.

## Idempotency key on every order

The browser generates a `clientOrderId`. QoS 1 means a lost acknowledgement causes a
legitimate retransmission, and without a dedupe key one button press buys two dishes.
The kitchen keeps a bounded LRU of keys it has acted on.

**Rejected alternative:** QoS 2. It costs an extra round trip per message, and an
idempotency key is needed anyway once retries exist at any layer.

## A synchronous store, and therefore no locks

Every method on the state store is `def`, not `async def`. asyncio only switches tasks
at an `await`, so a function containing none cannot be interleaved — which makes
check-then-commit sequences atomic for free. Two orders arriving in the same tick
cannot both pass a capacity check only one should pass, with **zero** locks.

**Trade-off:** this is exactly what persistence would cost. A real database makes those
methods async, reintroduces interleaving, and you would need explicit locking or
optimistic concurrency on `version` to get the property back. In-memory state is what
the assignment permits, and `Store` is a Protocol behind a factory, so adding
persistence is one new class — but the consequences are architectural, not mechanical.
See [FUTURE-WORK.md](FUTURE-WORK.md) §4.

## Full snapshots per table, not deltas

Every change republishes that table's entire state. A delta protocol needs gap
detection and resynchronisation; a full snapshot is idempotent, and the payload stays
bounded because orders per table are capped.

## Cooking is spawned, never awaited

`asyncio.create_task`, not `await`. Awaiting the cook inside the consumer loop would
serialise the restaurant — table 2 could not order until table 1 was fed. This is the
difference between satisfying the concurrency requirement and only appearing to: four
orders placed within 6 ms complete in ~27 s (the longest single dish), not the 91 s sum.

Tasks are held in a set, because asyncio keeps only a weak reference to a running task
and one whose sole reference is a local variable can be garbage collected mid-`await`.

## The backend owns the timer; the countdown is display only

`cookSeconds` and `expectedReadyAt` are published so the UI can render a countdown and
state the assigned duration. The authoritative timer is the backend's `asyncio.sleep` on
that same value, and an order becomes SERVED only when the kitchen publishes it. A
client that ignored or forged those fields could not make a dish arrive sooner or later.

The value is drawn at *acceptance* rather than when the cooking task starts, so it is
already present in the first state message and the countdown begins correct.

## Publishes during an outage are dropped, not queued

The kitchen survives its broker connection: one `Kitchen` instance for the process
lifetime with the client swapped underneath, so a dish 8 seconds into a 25-second cook
is still served on the new connection. A publish attempted while disconnected is logged
and dropped — safe *specifically because* state is retained and republished in full on
reconnect, so the loss self-heals seconds later.

**Trade-off:** clients see stale state for the duration of the outage. A bounded
outbound queue would shorten that window; coalescing would be trivially correct since
only the newest state per topic matters.

## Both sides validate, and the browser's validation counts for nothing

The client checks food names for length and control characters purely so the customer
gets instant feedback. The backend re-validates everything with pydantic, because
anything enforced only in a browser is bypassed by publishing to the broker directly.

## Broker credentials in the frontend are public, and that is stated plainly

Anything Vite compiles into the bundle is readable by every visitor. Security rests on
the ACL restricting that identity to publishing orders and reading state — never on
secrecy. Critically it cannot publish to `.../state`, or any visitor could forge a
retained "your food is ready" without the backend being involved. See
[SECURITY.md](SECURITY.md).

## Tables are discovered, not configured

The frontend has no table count. The kitchen publishes a retained state message per
table it owns and the client renders whatever it hears about — one source of truth, no
config to drift.

## Tables are hidden when the kitchen is offline

The retained state is still the last known truth, so it is not deleted from the store —
but rendering it would be a lie, because anything shown as COOKING has no process behind
it and will never be served. Ordering is disabled for the same reason.

## Types are duplicated by hand

`frontend/src/lib/types.ts` and `backend/src/restaurant/models.py` describe the same wire
format and can drift. Generating both from one JSON Schema is the right answer with more
time. The runtime validators stay regardless: TypeScript types vanish at runtime, and
inbound network data is untrusted either way.

## MQTT 3.1.1

Widest broker compatibility; nothing here needs an MQTT 5 feature. Horizontal scaling
would need MQTT 5 shared subscriptions — see [FUTURE-WORK.md](FUTURE-WORK.md) §5.

## A second transport is a seam, not a plan

`Transport` (backend) and `MessagingClient` (frontend) are protocols selected by a
factory, so adding a transport is additive on both halves. But REST is a poor fit for
the server-initiated FOOD event, and [FUTURE-WORK.md](FUTURE-WORK.md) §6 works through
exactly why rather than pretending the abstraction settles it.

---

## Two things a reviewer might flag as odd

- **A monkeypatch in `tools/dev_broker.py`.** `amqtt` starts its WebSocket listener via
  `websockets.serve()`, which defaults to `ping_interval=20` — a WebSocket-level Ping
  that paho's minimal WebSocket support does not answer, tearing the connection down
  every 20 seconds exactly. Disabling it loses nothing because MQTT has its own PINGREQ
  keepalive. Scoped to the dev broker; Mosquitto and HiveMQ do not behave this way.
- **A Windows-specific event loop.** paho registers its socket with `loop.add_reader()`,
  a selector-loop API that Windows' default Proactor loop does not implement, so the
  client can never connect. Linux — the deployment target — already uses a selector loop.

Both are recorded with full diagnosis in [INCIDENTS.md](INCIDENTS.md).
