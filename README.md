# Restaurant Orders

An event-driven restaurant simulation. Customers order at tables, the kitchen cooks
for a random 10–30 seconds, and the food appears on the right table the moment it
is ready — across every open browser window.

**Frontend:** Svelte 5 + TypeScript · **Backend:** Python + asyncio ·
**Transport:** MQTT over WebSockets only, no REST anywhere.

## Live demo

| | |
| --- | --- |
| Frontend | <https://hansol1994.github.io/osensa-demo/> |
| Kitchen | Fly.io (`osensa-demo`), connects outbound only — no public URL by design |
| Broker | HiveMQ Cloud, `wss` on 8884 |

Worth trying: open it in **two browser windows**. Order in one and it appears in the
other, because neither window talks to the other — they both render broker state.

If the header shows **kitchen offline**, the backend is not running; the UI hides the
tables deliberately, because their last known state is no longer live. Everything runs
locally with no accounts — see below.

---

## Running it locally

**Prerequisites:** Python 3.11+ and Node 20+. No Docker, no broker install, no
cloud account.

Every default in the code is the local configuration, so running locally needs no
config files at all — if `.env` files exist they *override* these defaults to point
somewhere else.

### One-time setup

macOS / Linux:

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Windows:

```bash
cd backend && python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"
```

Then:

```bash
cd frontend && npm install
```

### Three terminals

| | macOS / Linux | Windows |
| --- | --- | --- |
| **1. Broker** | `cd backend && .venv/bin/python -m tools.dev_broker` | `cd backend && .venv\Scripts\python -m tools.dev_broker` |
| **2. Kitchen** | `cd backend && .venv/bin/python -m restaurant` | `cd backend && .venv\Scripts\python -m restaurant` |
| **3. Frontend** | `cd frontend && npm run dev` | `cd frontend && npm run dev` |

Open <http://localhost:5173>.

> Windows note: use backslashes. `cmd.exe` treats `/` as a switch separator and
> will not resolve `.venv/Scripts/python`. PowerShell accepts either.

> **Why a Python broker?** The browser speaks MQTT over WebSockets, so *some*
> broker with a WebSocket listener has to exist. Mosquitto in Docker is the usual
> answer, but that makes Docker a prerequisite for running anything. `amqtt` is
> pip-installable, and the same helper starts a real broker inside pytest so the
> integration tests need no external services.

### Or with Docker

```bash
docker compose up --build
```

Then run the frontend with `npm run dev` as above — it stays outside Docker because
its whole development value is Vite's hot reload.

This path runs **real Mosquitto**. See [deploy/](deploy/) for the authenticated,
ACL-enforced configuration; compose uses an anonymous dev config so it works with
no setup step.

### Pointing at a hosted broker

Copy `.env.example` to `.env` (backend) and `.env.local` (frontend) and fill in the
broker details. Removing those files returns you to the all-local defaults. Full
instructions in [docs/DEPLOY.md](docs/DEPLOY.md).

---

## Testing

80 backend tests and 25 frontend tests. Substitute `.venv\Scripts\` for
`.venv/bin/` on Windows.

```bash
cd backend && .venv/bin/python -m pytest
```

```bash
cd frontend && npm test
```

The backend suite includes integration tests that start a **real MQTT broker
in-process**, so serialisation, topic routing, retention and QoS are exercised for
real — and CI needs no external services.

Lint, types, and the Svelte checker:

```bash
cd backend && .venv/bin/python -m ruff check src tools tests && .venv/bin/python -m mypy
```

```bash
cd frontend && npm run check
```

`mypy --strict` passes on the whole backend; `svelte-check` reports zero errors and
zero warnings.

---

## How it works

```
┌────────────────┐                  ┌──────────┐                  ┌─────────────────┐
│  Svelte client │ ─── ORDER ─────► │  Broker  │ ◄─── ORDER ───── │ Python asyncio  │
│   (browser)    │ ◄── FOOD ─────── │          │ ──── FOOD ─────► │    "kitchen"    │
└────────────────┘                  └──────────┘                  └─────────────────┘
        ▲
        └─ N browser windows subscribe to the same topics, so every window renders
           identical state without ever talking to another window.
```

The full message contract — topics, payloads, QoS, retention, state machine — is
in **[docs/EVENT-CONTRACT.md](docs/EVENT-CONTRACT.md)**. In an event-driven system
that document *is* the architecture; both sides just implement it.

Other docs:

| Document | What it covers |
| --- | --- |
| [EVENT-CONTRACT.md](docs/EVENT-CONTRACT.md) | Topics, payloads, QoS, retention, state machine — the architecture |
| [SECURITY.md](docs/SECURITY.md) | Threat model, broker ACLs, what is deliberately accepted |
| [FUTURE-WORK.md](docs/FUTURE-WORK.md) | What to build next, including what horizontal scaling would require |
| [DEPLOY.md](docs/DEPLOY.md) | Hosting, CI/CD, and a troubleshooting guide per failure mode |
| [INCIDENTS.md](docs/INCIDENTS.md) | Every non-trivial problem hit while building this, and how it was diagnosed |

---

## Key design decisions and trade-offs

### Retained messages instead of a snapshot endpoint

The broker stores the last message on a topic and delivers it to anyone who
subscribes *later*. Publishing each table's state as a retained message is the
entire answer to "open a second window and it must already know what is cooking" —
no snapshot endpoint, no replay protocol, no window-to-window sync.

**Trade-off:** retained state can outlive the process that produced it. A stale
retained message describing a dish nobody is cooking is why `epoch` exists, below.

### `version` and `epoch` on every table

QoS 1 is *at-least-once*, so the same message can arrive twice; and on reconnect the
retained message and a live update arrive by two paths with no ordering guarantee
between them. So each table carries a monotonic `version`, and the client discards
anything not strictly newer.

`epoch` bounds that comparison. State is in-memory, so restarting the kitchen resets
versions to zero — and a client holding version 5 would reject the restarted
kitchen's version 0 as stale **forever**, displaying dead orders permanently. Same
epoch → compare versions; different epoch → accept unconditionally. Found by
restarting the backend with a browser open, not by reading the code.

### Idempotency key on every order

The browser generates a `clientOrderId`. QoS 1 means a lost acknowledgement causes
a legitimate retransmission, and without a dedupe key one button press buys two
dishes. The kitchen keeps a bounded LRU of keys it has acted on.

### A synchronous store, and therefore no locks

Every method on the state store is `def`, not `async def`. asyncio only switches
tasks at an `await`, so a function containing none cannot be interleaved — which
makes check-then-commit sequences atomic for free. Two orders arriving in the same
tick cannot both pass a capacity check only one should pass, with **zero** locks.

**Trade-off:** this is exactly what persistence would cost. A real database makes
those methods async, reintroduces interleaving, and you would need explicit locking
or optimistic concurrency on `version` to get the property back. In-memory state is
what the assignment permits, and `Store` is a Protocol behind a factory, so adding
persistence is one new class — but the consequences are architectural, not
mechanical. See [FUTURE-WORK.md](docs/FUTURE-WORK.md) §4.

### Full snapshots per table, not deltas

Every change republishes that table's entire state. A delta protocol needs gap
detection and resynchronisation; a full snapshot is idempotent, and the payload
stays bounded because orders per table are capped.

### Cooking is spawned, never awaited

`asyncio.create_task`, not `await`. Awaiting the cook inside the consumer loop would
serialise the restaurant — table 2 could not order until table 1 was fed. This is
the difference between satisfying the concurrency requirement and only appearing to:
four orders placed within 6 ms complete in ~27 s (the longest single dish), not the
91 s sum.

Tasks are held in a set, because asyncio keeps only a weak reference to a running
task and one whose sole reference is a local variable can be garbage collected
mid-`await`.

### The backend owns the timer; the countdown is display only

`cookSeconds` and `expectedReadyAt` are published so the UI can render a countdown
and state the assigned duration. The authoritative timer is the backend's
`asyncio.sleep` on that same value, and an order becomes SERVED only when the
kitchen publishes it. A client that ignored or forged those fields could not make a
dish arrive sooner or later.

### Publishes during an outage are dropped, not queued

The kitchen survives its broker connection: one `Kitchen` instance for the process
lifetime with the client swapped underneath, so a dish 8 seconds into a 25-second
cook is still served on the new connection. A publish attempted while disconnected
is logged and dropped — safe *specifically because* state is retained and
republished in full on reconnect, so the loss self-heals seconds later.

**Trade-off:** clients see stale state for the duration of the outage. A bounded
outbound queue would shorten that window; coalescing would be trivially correct
since only the newest state per topic matters.

### Both sides validate, and the browser's validation counts for nothing

The client checks food names for length and control characters purely so the
customer gets instant feedback. The backend re-validates everything with pydantic,
because anything enforced only in a browser is bypassed by publishing to the broker
directly.

### Broker credentials in the frontend are public, and that is stated plainly

Anything Vite compiles into the bundle is readable by every visitor. Security rests
on the ACL restricting that identity to publishing orders and reading state — never
on secrecy. Critically it cannot publish to `.../state`, or any visitor could forge
a retained "your food is ready" without the backend being involved. See
[SECURITY.md](docs/SECURITY.md).

### Tables are discovered, not configured

The frontend has no table count. The kitchen publishes a retained state message per
table it owns and the client renders whatever it hears about — one source of truth,
no config to drift.

### Types are duplicated by hand

`frontend/src/lib/types.ts` and `backend/src/restaurant/models.py` describe the same
wire format and can drift. Generating both from one JSON Schema is the right answer
with more time. The runtime validators stay regardless: TypeScript types vanish at
runtime, and inbound network data is untrusted either way.

### MQTT 3.1.1

Widest broker compatibility; nothing here needs an MQTT 5 feature.

### Two things a reviewer might flag as odd

- **A `tools/dev_broker.py` monkeypatch.** `amqtt` starts its WebSocket listener via
  `websockets.serve()`, which defaults to `ping_interval=20` — a WebSocket-level Ping
  that paho's minimal WebSocket support does not answer, tearing the connection down
  every 20 seconds exactly. Disabling it loses nothing because MQTT has its own
  PINGREQ keepalive. Scoped to the dev broker; Mosquitto and HiveMQ do not do this.
- **A Windows-specific event loop.** paho registers its socket with
  `loop.add_reader()`, a selector-loop API that Windows' default Proactor loop does
  not implement, so the client can never connect. Linux — the deployment target —
  already uses a selector loop.

---

## What I would add with more time

Ordered by value; detail and reasoning in
[docs/FUTURE-WORK.md](docs/FUTURE-WORK.md).

1. **An auth gateway.** Every browser currently shares one broker credential and a
   client id is a self-asserted string. Short-lived per-session tokens would enable
   real identity, revocation, and per-identity rate limiting — which is impossible
   today, and why the current limits are deliberately resource-based instead.
2. **Connection abuse controls** at the edge: per-IP rate limiting and connection
   ceilings. Application code cannot fix this; by the time a connection is accepted
   the cost is paid.
3. **Backpressure on outbound state** — a bounded, coalescing queue to close the
   staleness window during a broker outage, plus jitter on the reconnect delay,
   which is currently fixed and would synchronise reconnect storms.
4. **Generated types** from a shared JSON Schema, with a CI check that the committed
   output is current.
5. **Metrics** — orders accepted/rejected by reason, cook duration histogram,
   reconnect count. Reconnect rate was the leading indicator for every failure hit
   while building this.
6. **A chaos test** that kills the broker mid-cook and asserts state converges. This
   is where the `epoch` bug would have been caught automatically instead of by hand.

---

## Project layout

```
backend/
  src/restaurant/
    models.py      wire format (pydantic), the contract in code
    topics.py      topic construction and parsing
    kitchen.py     domain logic: validation, limits, concurrency, cooking
    store.py       in-memory state behind a Protocol + factory
    transport.py   Transport protocol, MQTT implementation, factory
    app.py         object graph, signals, event loop choice
    config.py      environment-driven settings
  tools/dev_broker.py   pure-Python broker for dev and tests
  tests/
frontend/
  src/lib/
    types.ts       wire format + runtime validators
    state.ts       pure reducers and timing maths (fully unit tested)
    stores.ts      Svelte stores fed by MQTT
    dispatch.ts    inbound message routing
    messaging.ts   MessagingClient interface + factory
    mqttClient.ts  the MQTT implementation
    clock.ts       one shared ticking clock
deploy/            mosquitto.conf, ACL, systemd unit
docs/              EVENT-CONTRACT, SECURITY, FUTURE-WORK, DEPLOY
```

`Kitchen` imports no MQTT and no transport. `App.svelte` imports no mqtt.js. Both
sides go through a factory, so adding a transport is additive on both halves.
