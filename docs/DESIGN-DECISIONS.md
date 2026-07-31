# Design Decisions and Trade-offs

The full reasoning behind each choice. The README carries a condensed version; this is
the long form.

Every decision below states what it **cost**, because a decision without a stated cost
is usually one that was not really made. Where an alternative was considered and
rejected, that is named too.

---

## Retained messages instead of a snapshot endpoint

The broker stores the last message on a topic and delivers it to anyone who subscribes
*later*. Publishing each table's state as a retained message is the entire answer to
"open a second window and it must already know what is cooking" — no snapshot endpoint,
no replay protocol, no window-to-window sync.

**Trade-off:** retained state outlives the process that wrote it. The broker will happily
serve a snapshot describing a dish nobody is cooking, which is what forced `epoch` below.
It also means the broker holds application state, so clearing a table is a publish rather
than a delete.

## `version` and `epoch` on every table

QoS 1 is *at-least-once*, so the same message can arrive twice; and on reconnect the
retained message and a live update arrive by two paths with no ordering guarantee between
them. So each table carries a monotonic `version`, and the client discards anything not
strictly newer.

`epoch` bounds that comparison. State is in-memory, so restarting the kitchen resets
versions to zero — and a client holding version 5 would reject the restarted kitchen's
version 0 as stale **forever**, displaying dead orders permanently. Same epoch → compare
versions; different epoch → accept unconditionally. Found by restarting the backend with a
browser open, not by reading the code.

**Trade-off:** two extra fields on every message, and a rule every client must implement
correctly. A client that ignores `epoch` works fine until the backend restarts and then
breaks silently — the failure is invisible in normal operation, which is the worst kind.

## Idempotency key on every order

The browser generates a `clientOrderId`. QoS 1 means a lost acknowledgement causes a
legitimate retransmission, and without a dedupe key one button press buys two dishes. The
kitchen keeps a bounded LRU of keys it has acted on.

**Rejected alternative:** QoS 2. It costs an extra round trip per message, and an
idempotency key is needed anyway once retries exist at any layer, so QoS 2 would have paid
for a guarantee the application still cannot rely on alone.

**Trade-off:** the dedupe window is bounded (1000 keys). A retransmission arriving after
1000 intervening orders would be treated as new. Acceptable here; a persistent store would
remove the limit.

## A synchronous store, and therefore no locks

Every method on the state store is `def`, not `async def`. asyncio only switches tasks at
an `await`, so a function containing none cannot be interleaved — which makes
check-then-commit sequences atomic for free. Two orders arriving in the same tick cannot
both pass a capacity check only one should pass, with **zero** locks.

**Trade-off:** this is exactly what persistence would cost. A real database makes those
methods async, reintroduces interleaving, and you would need explicit locking or optimistic
concurrency on `version` to get the property back. `Store` is a Protocol behind a factory,
so adding persistence is one new class — but the consequences are architectural, not
mechanical. See [FUTURE-WORK.md](FUTURE-WORK.md) §4.

## Full snapshots per table, not deltas

Every change republishes that table's entire state. A delta protocol needs gap detection
and resynchronisation; a full snapshot is idempotent and a late joiner needs no special
path.

**Trade-off:** payload size grows with orders per table, and every change resends orders
that did not change. Only tolerable because orders per table are capped at 5 and finished
orders are trimmed to 10 — without those bounds this choice would not hold.

## Cooking is spawned, never awaited

`asyncio.create_task`, not `await`. Awaiting the cook inside the consumer loop would
serialise the restaurant — table 2 could not order until table 1 was fed. This is the
difference between satisfying the concurrency requirement and only appearing to: four
orders placed within 6 ms complete in ~27 s (the longest single dish), not the 91 s sum.

Tasks are held in a set, because asyncio keeps only a weak reference to a running task and
one whose sole reference is a local variable can be garbage collected mid-`await`.

**Trade-off:** no natural backpressure. Nothing slows the consumer down when the kitchen is
busy, so the explicit caps (5 per table, 100 global) are the *only* thing bounding
concurrent work. And because timers live in the process, a crash loses every in-flight
dish — see [FUTURE-WORK.md](FUTURE-WORK.md) §5 on durable timers.

## The backend owns the timer; the countdown is display only

`cookSeconds` and `expectedReadyAt` are published so the UI can render a countdown and
state the assigned duration. The authoritative timer is the backend's `asyncio.sleep` on
that same value, and an order becomes SERVED only when the kitchen publishes it. A client
that ignored or forged those fields could not make a dish arrive sooner or later.

The value is drawn at *acceptance* rather than when the cooking task starts, so it is
already present in the first state message and the countdown begins correct.

**Trade-off:** the countdown is a prediction, so it can reach zero before the dish is
served — if the machine is paused, throttled, or the event loop is busy. The UI shows
"finishing…" at that point rather than continuing to count or claiming a missed deadline,
which is honest but means the number is not a promise.

## Publishes during an outage are dropped, not queued

The kitchen survives its broker connection: one `Kitchen` instance for the process lifetime
with the client swapped underneath, so a dish 8 seconds into a 25-second cook is still
served on the new connection. A publish attempted while disconnected is logged and dropped
— safe *specifically because* state is retained and republished in full on reconnect, so
the loss self-heals seconds later.

**Trade-off:** clients see stale state for the duration of the outage, and a dish that
completes during it appears to finish late. A bounded outbound queue would shorten the
window, and coalescing would be trivially correct since only the newest state per topic
matters. Not built because the resync already restores correctness — only timeliness is
lost.

## Both sides validate, and the browser's validation counts for nothing

The client checks food names for length and control characters purely so the customer gets
instant feedback. The backend re-validates everything with pydantic, because anything
enforced only in a browser is bypassed by publishing to the broker directly.

**Trade-off:** the same rules exist in two languages and can drift. The limit (80
characters) and the control-character rule are duplicated by hand, so a change needs both
sides edited. Generating them from one schema is the fix — see below.

## Broker credentials in the frontend are public, and that is stated plainly

Anything Vite compiles into the bundle is readable by every visitor. Security rests on the
ACL restricting that identity to publishing orders and reading state — never on secrecy.
Critically it cannot publish to `.../state`, or any visitor could forge a retained "your
food is ready" without the backend being involved. See [SECURITY.md](SECURITY.md).

**Trade-off:** one shared credential for every visitor means no per-user revocation and no
per-identity rate limiting, and rotating the password requires a rebuild and redeploy
rather than a config change. The proper fix is short-lived per-session tokens —
[FUTURE-WORK.md](FUTURE-WORK.md) §1.

## Tables are discovered, not configured

The frontend has no table count. The kitchen publishes a retained state message per table
it owns and the client renders whatever it hears about — one source of truth, no config to
drift.

**Trade-off:** nothing renders until the kitchen has published. A cold start with no
kitchen running shows an empty page, so the UI needs explicit copy explaining that state
rather than being able to draw four empty tables from its own configuration.

## Tables are hidden when the kitchen is offline

The retained state is still the last known truth, so it is not deleted from the store — but
rendering it would be a lie, because anything shown as COOKING has no process behind it and
will never be served. Ordering is disabled for the same reason.

**Trade-off:** someone opening the hosted demo while the backend is asleep sees no tables
and could reasonably conclude the app is broken. Mitigated with explanatory copy, but
showing stale data with a warning banner would have looked more alive. Chose honesty over
appearing to work.

## Types are duplicated by hand

`frontend/src/lib/types.ts` and `backend/src/restaurant/models.py` describe the same wire
format.

**Trade-off:** they can drift, and nothing catches it at build time — only a runtime
validation failure would reveal it. Generating both from one JSON Schema is the right
answer with more time. The runtime validators stay regardless: TypeScript types vanish at
runtime, and inbound network data is untrusted either way.

## MQTT 3.1.1 rather than MQTT 5

Widest broker compatibility, and nothing in this application needs an MQTT 5 feature.

**Trade-off:** no shared subscriptions, which are the mechanism horizontal scaling would
need. Choosing 3.1.1 means a protocol version bump is a prerequisite for running more than
one backend instance — see [FUTURE-WORK.md](FUTURE-WORK.md) §5.

## A second transport is a seam, not a plan

`Transport` (backend) and `MessagingClient` (frontend) are protocols selected by a factory,
so adding a transport is additive on both halves.

**Trade-off:** this is speculative generality, and worth admitting as such — there is
exactly one implementation on each side, so the indirection currently buys nothing at
runtime. It is justified by keeping domain logic free of transport imports, which is
independently useful for testing: `Kitchen` is tested against a fake publisher precisely
because that seam exists. But REST is also a poor fit for the server-initiated FOOD event,
so the abstraction does not make the substitution *appropriate* —
[FUTURE-WORK.md](FUTURE-WORK.md) §6 works through why rather than pretending otherwise.

---

## Two things a reviewer might flag as odd

- **A monkeypatch in `tools/dev_broker.py`.** `amqtt` starts its WebSocket listener via
  `websockets.serve()`, which defaults to `ping_interval=20` — a WebSocket-level Ping that
  paho's minimal WebSocket support does not answer, tearing the connection down every 20
  seconds exactly. Disabling it loses nothing because MQTT has its own PINGREQ keepalive.
  Scoped to the dev broker; Mosquitto and HiveMQ do not behave this way.
- **A Windows-specific event loop.** paho registers its socket with `loop.add_reader()`, a
  selector-loop API that Windows' default Proactor loop does not implement, so the client
  can never connect. Linux — the deployment target — already uses a selector loop.

Both are recorded with full diagnosis in [INCIDENTS.md](INCIDENTS.md).
