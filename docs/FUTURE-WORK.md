# Future Work

Ordered roughly by value per unit of effort. Anything already handled in this
version is deliberately excluded — see [SECURITY.md](SECURITY.md) for the split
between handled, accepted, and outstanding.

## 1. Auth gateway and real client identity

**Problem.** Every browser shares one broker credential (unavoidable for a public
page), all clients are anonymous, and a client id is a self-asserted string. There
is no way to tell a real customer from a script, and no way to attribute an order
to anyone.

**Approach.** A small service in front of the broker:

1. The browser authenticates (or gets an anonymous but *signed* session).
2. The service mints a short-lived JWT scoped to that session's own topics.
3. The broker verifies the JWT and derives ACLs from its claims — HiveMQ and EMQX
   support this directly; Mosquitto needs an auth plugin.

**What this unlocks, in order:**

- Per-session credentials instead of one shared password. Revocable, expiring.
- **Per-identity rate limiting**, which today is impossible: current limits are
  resource-based precisely because identity cannot be trusted.
- Knowing *which* client is connected — needed for "table 4 is Hanieh's order",
  order history, or staff-versus-customer views.
- A real answer to bot detection, since identity would no longer be client-chosen.

**Note on ordering:** this must come *before* per-client abuse controls, not after.
Any limit keyed on a client-supplied id is bypassed by changing the id.

## 2. Connection abuse controls

Repeated open/close of a browser tab creates a new session each time (a refresh
does not — see SECURITY.md §5). Sessions do not accumulate, because the client
connects with `clean: true`, but the *rate* of connections is uncapped.

- Per-IP connection rate limiting at a reverse proxy or the managed broker.
- `max_connections` on the broker listener as a blunt ceiling.
- Alert on connection churn, which is the signal that distinguishes abuse from
  load.

Deliberately **not** application-level: an app that has already accepted a
connection has already paid most of the cost. This belongs at the edge.

## 3. Delivery guarantees for outbound state

Today, a publish attempted while the kitchen is disconnected is **dropped**, and
correctness is restored by the full-state resync on reconnect (see
`ReconnectingPublisher`). That is sound but coarse: a client sees stale state for
the duration of the outage.

Options, cheapest first:

- **Outbound queue with backpressure** — buffer publishes during an outage, bounded
  in size, dropping oldest since only the newest state per table matters. Because
  state is a full snapshot rather than a delta, coalescing is trivially correct:
  keep only the latest per topic.
- **MQTT 5 session expiry with a persistent session** so the broker holds queued
  messages for the kitchen.
- **Retry with exponential backoff and jitter** on the connection itself. Currently
  the reconnect delay is fixed (`reconnect_delay_seconds`), which synchronises
  reconnect storms if many instances restart together. Jitter is a ten-line change
  and worth doing before any multi-instance deployment.

## 4. Persistence

State is in-memory by design (the assignment permits it). `Store` is a Protocol
with `InMemoryStore` behind a factory, so adding persistence means writing one
class and changing one config value.

The real work is not the class, it is what persistence forces:

- The Protocol becomes `async`, which removes the free atomicity the synchronous
  store currently enjoys (see the `store.py` module docstring). Read-modify-write
  sequences then need explicit locking or optimistic concurrency on `version`.
- `epoch` semantics change: with durable state, versions no longer reset on
  restart, so the epoch mechanism could be retired — or kept, since it also covers
  a wiped database.
- Cooking timers must be recovered on startup: an order left `COOKING` by a crash
  needs either resumption or transition to `FAILED`.

## 5. Running more than one kitchen instance

Today the system requires **exactly one** kitchen. That is enforced operationally
(`fly scale count 1`) rather than by the design, and it is not a theoretical
concern: running two instances was tried by accident during deployment and produced
an immediate, total outage. Worth documenting properly, because the failure is
instructive and the fix is not a config flag.

### What breaks, and why

Four independent blockers, in the order they bite:

**1. Duplicate MQTT client identifier.** Every instance connects as
`restaurant-kitchen`. A broker evicts an existing session when a duplicate id
connects, so two instances kick each other off every few seconds indefinitely.
Nothing works at all. This one is trivially fixed — derive the id from the hostname
or machine id — but fixing it only reveals the next three.

**2. Standard MQTT subscriptions are broadcast, not load-balanced.** Both instances
subscribe to `restaurant/table/+/order`, so both receive *every* order and cook it
twice. The customer gets two dishes, and the idempotency key does not help because
each instance keeps its dedupe cache in its own memory.

**3. State is per-process.** Each instance has its own store, its own `epoch`, and
its own `version` counters. Both publish to the same retained
`restaurant/table/N/state` topics with conflicting epochs, so clients accept
whichever arrived last and the UI thrashes between two versions of reality.

**4. Cooking timers live inside the process that accepted the order.** The timer is
an `asyncio.sleep` in one instance. If that instance dies mid-cook, the order stays
`COOKING` forever — no other instance knows it exists, and the retained state
advertises a dish that will never arrive. Today a whole-process restart clears this
because state is in memory; with shared state it becomes a permanent inconsistency.

### First, decide what scaling is *for*

This matters more than the mechanism. The kitchen's "work" is
`await asyncio.sleep(10..30)` — it consumes almost no CPU, and a single small
instance handles far more orders than a restaurant simulation will ever produce.

So **horizontal scaling here buys availability, not throughput.** Naming that goal
changes the answer, because the cheap design solves availability and the expensive
one solves throughput nobody needs.

### Design A — active/passive with a leader lock (recommended)

One instance works; the others idle until it dies.

- All instances connect (with unique client ids) but only the leader subscribes to
  the order topic.
- Leadership is a lease in Redis or Postgres — `SET key NX PX 10000` refreshed every
  few seconds. Lose the lease, unsubscribe and stop cooking.
- A standby that acquires the lease subscribes, then republishes retained state.

This sidesteps blockers 2, 3 and 4 entirely: there is still only ever one writer,
one epoch, and one set of timers. The trade-off is a failover gap of roughly the
lease TTL, during which no orders are accepted — but the browser already handles
that correctly, because the kitchen's Last Will marks it offline and the UI hides the
tables until it returns.

Cost: a lock backend and maybe 80 lines. No change to the wire contract.

### Design B — active/active partitioned by table

If throughput ever genuinely matters:

- **MQTT 5 shared subscriptions** — subscribe to
  `$share/kitchens/restaurant/table/+/order` so the broker delivers each order to
  exactly one member of the group. Note this requires moving the backend from MQTT
  3.1.1 to MQTT 5 (the browser can stay on 3.1.1), and broker support is uneven —
  HiveMQ and EMQX implement it; verify before relying on it.
- **Partition by `tableId`**, via consistent hashing over the instance set. This is
  the load-bearing idea: if a table is always handled by the same instance, then that
  instance is the only writer for that table's retained topic, the only one checking
  its capacity limit, and the only one holding its timers. Blockers 2, 3 and 4 all
  reduce to "one owner per table" rather than needing distributed coordination.
- Without partitioning, shared subscriptions alone are not enough: two orders for
  the same table could be processed concurrently on different instances and both pass
  a capacity check that only one should.

### Regardless of design, these become necessary

- **Shared, async `Store`.** The `Store` protocol is the seam, but see §4 — the
  synchronous store's free atomicity is exactly what is lost. `_try_accept` currently
  does capacity check, dedupe check, and insert as one uninterruptible step. Across
  instances that has to become a single atomic operation: a Redis Lua script, or one
  SQL statement, or a `SERIALIZABLE` transaction.
- **Atomic version counters.** `version` must increment monotonically per table
  across all instances — a Redis `INCR` or a database sequence, not `+= 1` in memory.
- **`epoch` moves into the store.** It currently identifies a *process generation*.
  With shared state it must identify a *state generation*, otherwise every instance
  announces a different epoch and clients treat each other's updates as a fresh
  kitchen. This is a contract-semantics change, not just an implementation one.
- **Shared dedupe.** The idempotency key must be checked in shared state, atomically
  with the insert — `INSERT ... ON CONFLICT DO NOTHING` and inspect the row count, or
  `SET key NX`. An in-process LRU is worse than useless once a redelivery can land on
  a different instance.
- **Durable timers with recovery.** Persist `expectedReadyAt` (already in the
  contract) and add a sweeper that claims overdue orders whose owner's lease has
  expired — `SELECT ... FOR UPDATE SKIP LOCKED` is the standard shape. This is the
  pleasing part: because `expectedReadyAt` is already published, *any* instance can
  determine that an order is due, so completion stops depending on which process
  accepted it.
- **Observability of ownership.** Log which instance owns which tables, and alert on
  reconnect rate. A duplicate-id eviction loop is invisible in aggregate metrics but
  obvious in a per-instance reconnect count — that is how the accidental two-instance
  outage was actually diagnosed.

### Suggested order

1. Unique client id per instance. Small, and removes the catastrophic failure mode
   even if nothing else changes.
2. Shared store with atomic version and dedupe (§4 first — this depends on it).
3. Move `epoch` into the store.
4. Durable timers plus the overdue sweeper.
5. Leader lock (Design A). Stop here unless throughput is measured to be a problem.
6. Only then: MQTT 5, shared subscriptions, and table partitioning (Design B).

---

## 6. A second transport (REST, SSE) alongside MQTT

The assignment forbids REST, so MQTT is the only transport implemented. The code is
structured so this is additive rather than invasive: `Transport` is a Protocol,
`MqttTransport` implements it, and `create_transport` selects one from config. The
`Kitchen` depends only on a `Publisher`, and has no MQTT imports at all.

Adding an HTTP transport means a new `Transport` implementation plus a factory
case. Nothing in the domain logic changes.

### Why REST is a poor fit here — in detail

Not "REST is bad." REST is a poor fit for *this* problem, for six specific
reasons.

**1. The direction is wrong.** REST is client-initiated request/response. ORDER
fits it perfectly — the customer acts, the server answers. FOOD does not: the
kitchen decides, 10–30 seconds later, with nobody asking. HTTP gives the server no
way to speak first. Every workaround (polling, long-polling, SSE, WebSockets) is
either wasteful or is no longer REST.

**2. Polling forces a choice between staleness and waste.** The requirement is
"food should show up as soon as it is ready." With polling you pick an interval and
lose either way:

| Poll interval | Worst-case staleness | Requests per client per minute |
| --- | --- | --- |
| 1s | 1s | 60 |
| 5s | 5s | 12 |
| 30s | 30s (longer than a cook) | 2 |

MQTT has no such dial. It pushes, so latency is network-bound and cost is
proportional to *changes*, not to *time × clients*.

**3. The cost gap is concrete.** One order, 20-second cook, three browser windows
open:

- **Polling at 1s:** 3 clients × 20s = **60 HTTP requests**, each with headers,
  cookies and a TLS record, and 58 of them return "nothing changed."
- **MQTT:** 2 publishes (order accepted, order served), fanned out by the broker to
  3 subscribers = **6 small frames** over connections that already exist.

**4. Fan-out is the broker's job, and REST has no broker.** In MQTT the server
publishes once and the broker delivers to whoever subscribed; the kitchen does not
know or care how many windows are open. With REST the server has no idea who is
interested, so every client asks independently and load scales with
`clients × poll rate` instead of with events.

**5. The late-joiner problem needs a second code path.** Retained messages give a
new window the current state automatically — same topic, same payload shape, same
reducer. REST needs a bespoke `GET /tables` *plus* the live channel, and then you
must reason about how a snapshot interleaves with live updates. That is exactly the
version/epoch problem from §3.2 of the contract, except now two different code
paths deliver state and both have to agree.

**6. Modelling events as resources reinvents a queue, badly.** The usual escape is
`GET /events?since={cursor}`, which means implementing cursors, retention, and
acknowledgement — a message queue with extra steps, over a protocol that was not
designed for it, without the QoS semantics MQTT already specifies.

**Where REST would genuinely be the better choice:** anything with a definite
answer and a client-initiated question — authentication, fetching a menu, admin
actions, config. It caches with plain HTTP semantics, debugs with `curl`, needs no
persistent connection, and every proxy and observability tool already understands
it. If the feature were "show today's menu," REST would win outright. It is the
*server-initiated, multi-subscriber, state-change-stream* shape that makes it the
wrong tool here — which is presumably why the assignment forbids it.

## 7. Generate types from one schema

`frontend/src/lib/types.ts` and `backend/src/restaurant/models.py` describe the
same wire format, maintained by hand. They can drift, and only a runtime failure
would reveal it.

Emit JSON Schema from the pydantic models at build time and generate the
TypeScript from that, with a CI check that the committed output is current. The
runtime validators in `types.ts` stay regardless — generated types still vanish at
runtime, and inbound data is still untrusted.

## 8. Observability

Structured JSON logs exist. Missing:

- **Metrics** — orders accepted/rejected by reason, cook duration histogram,
  in-flight count, reconnect count. Reconnect rate is the leading indicator for
  every failure seen while building this.
- **Trace correlation** — carry `clientOrderId` through every log line for one
  order so a single order's path is greppable end to end.
- **Health endpoint** — the kitchen has no way to report readiness, which most
  deployment platforms want.

## 9. UI improvements

- Show remaining time rather than elapsed. Needs `expectedReadyAt` in the
  contract; deliberately not added, because the kitchen makes no promise about
  completion time and a countdown that overruns is worse than an honest stopwatch.
- Optimistic pending state between clicking ORDER and the state message arriving.
  Currently the dish appears only once the kitchen confirms — correct, but it looks
  unresponsive on a slow link.
- Accessibility pass: announce state changes via a live region so a screen reader
  hears "Ramen served".
- Clear served dishes ("plate cleared") — needs a new inbound command and an ACL
  entry for it.

## 10. Testing gaps

- Property-based tests over the reducer (`applyTableState`) for arbitrary
  version/epoch orderings — the invariant is small and total, which suits
  Hypothesis well.
- A chaos test that kills the broker mid-cook and asserts state converges after
  reconnect. This is where the epoch bug would have been caught automatically.
- Load test establishing the real ceiling on concurrent orders, replacing the
  currently-guessed limit of 100.
