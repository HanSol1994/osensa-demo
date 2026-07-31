# Restaurant Orders

An event-driven restaurant simulation. Customers order at tables, the kitchen cooks for
a random 10–30 seconds, and the food appears on the right table the moment it is ready —
across every open browser window.

**Frontend:** Svelte 5 + TypeScript · **Backend:** Python + asyncio ·
**Transport:** MQTT over WebSockets only, no REST anywhere.

## Live demo

<https://hansol1994.github.io/osensa-demo/>

Open it in **two browser windows**: order in one and it appears in the other. Neither
window talks to the other — both render broker state. If the header says *kitchen
offline* the backend is not running, and the UI hides the tables deliberately because
their last known state is no longer live.

## Running it locally

**Prerequisites:** Python 3.11+ and Node 20+. No Docker, no broker install, no accounts —
every default in the code *is* the local configuration.

macOS / Linux:

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && cd ../frontend && npm install
```

Windows:

```bash
cd backend && python -m venv .venv && .venv\Scripts\pip install -e ".[dev]" && cd ..\frontend && npm install
```

Then three terminals:

| | macOS / Linux | Windows |
| --- | --- | --- |
| **1. Broker** | `cd backend && .venv/bin/python -m tools.dev_broker` | `cd backend && .venv\Scripts\python -m tools.dev_broker` |
| **2. Kitchen** | `cd backend && .venv/bin/python -m restaurant` | `cd backend && .venv\Scripts\python -m restaurant` |
| **3. Frontend** | `cd frontend && npm run dev` | `cd frontend && npm run dev` |

Open <http://localhost:5173>. On Windows use backslashes — `cmd.exe` reads `/` as a
switch separator.

`docker compose up --build` is an alternative that runs real Mosquitto; the frontend
still runs via `npm run dev`. The bundled Python broker exists so that neither Docker
nor a broker install is a prerequisite, and so the integration tests can start a real
broker in-process. To point at a hosted broker, copy the `.env.example` files — details
in [DEPLOY.md](docs/DEPLOY.md).

## Testing

```bash
cd backend && .venv/bin/python -m pytest && .venv/bin/python -m mypy && .venv/bin/python -m ruff check src tools tests
```

```bash
cd frontend && npm test && npm run check
```

80 backend tests, 25 frontend tests. `mypy --strict` and `svelte-check` both pass with
zero warnings. The backend suite starts a **real MQTT broker in-process**, so
serialisation, topic routing, retention and QoS are exercised for real and CI needs no
external services.

## How it works

```
┌────────────────┐                ┌──────────┐                ┌─────────────────┐
│  Svelte client │ ── ORDER ───►  │  Broker  │ ◄── ORDER ───  │ Python asyncio  │
│   (browser)    │ ◄─ FOOD ─────  │          │ ─── FOOD ───►  │    "kitchen"    │
└────────────────┘                └──────────┘                └─────────────────┘
```

N browser windows subscribe to the same topics, so every window renders identical state
without ever talking to another window. The message contract — topics, payloads, QoS,
retention, state machine — is in **[EVENT-CONTRACT.md](docs/EVENT-CONTRACT.md)**. In an
event-driven system that document *is* the architecture; both sides just implement it.

## Production readiness

Where each requirement is addressed, and the honest limit of it:

| | How | Detail |
| --- | --- | --- |
| **Well tested** | 105 tests. Pure reducers and timing maths unit tested; kitchen tested against a fake publisher with an injected clock so nothing sleeps; integration tests against a real broker | [tests](backend/tests) |
| **Handles edge cases** | Duplicate delivery, out-of-order state on reconnect, kitchen restart mid-cook, broker outage mid-cook, capacity limits, malformed and hostile payloads, browser clock skew | [DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) |
| **Takes care of errors** | The consumer never raises — any payload a client can send ends in a log line, not a crash. Rejections go to the sender's private topic with a machine-readable reason. Connection loss is supervised and retried; a failed dish becomes `FAILED` rather than hanging in `COOKING` | [kitchen.py](backend/src/restaurant/kitchen.py) |
| **Provides logging** | Structured JSON to stdout with `extra=` fields promoted to top-level keys, so logs are queryable rather than parsed. Every state transition also logs a full snapshot of the restaurant, which makes the log a self-contained narrative | [logging_config.py](backend/src/restaurant/logging_config.py) |
| **Is secure** | Topic ACLs are the primary control, not secrecy — the browser's credentials are public by construction. Both sides validate independently; every collection is bounded; limits are resource-based rather than identity-based, because a client id is self-asserted | [SECURITY.md](docs/SECURITY.md) |
| **Properly documented** | Contract, security model, design rationale, deployment runbook with per-failure-mode troubleshooting, and an incident log | table below |

**Not production ready, deliberately:** state is in-memory (the brief permits it), there
is no authentication, and exactly one backend instance may run. Each is explained rather
than left implicit — see [FUTURE-WORK.md](docs/FUTURE-WORK.md) §§1, 4, 5.

## Key design decisions and trade-offs

Condensed; full reasoning and rejected alternatives in
[DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md).

- **Retained messages instead of a snapshot endpoint.** The broker keeps the last message
  on a topic and gives it to whoever subscribes later, which is the whole answer to "a
  second window must already know what is cooking". *Trade-off:* retained state outlives
  the process that wrote it, which is why `epoch` below exists.

- **`version` + `epoch` per table.** QoS 1 can deliver twice, and on reconnect the
  retained message and a live update race by two paths. A monotonic `version` fixes
  ordering; `epoch` bounds it, because in-memory state resets versions on restart and a
  client holding version 5 would otherwise reject the restarted kitchen **forever**. Found
  by restarting the backend with a browser open.

- **An idempotency key on every order.** At-least-once delivery means a lost
  acknowledgement causes a legitimate retransmission — without a dedupe key, one click
  buys two dishes.

- **A synchronous store, and therefore no locks.** asyncio only switches at an `await`, so
  a method containing none cannot be interleaved: check-then-commit is atomic for free.
  *Trade-off:* this is precisely what persistence costs — async methods reintroduce
  interleaving and would need explicit locking or optimistic concurrency.

- **Cooking is spawned, never awaited.** Awaiting it would serialise the restaurant. Four
  orders placed within 6 ms complete in ~27 s (the longest dish), not the 91 s sum — the
  difference between meeting the concurrency requirement and appearing to.

- **The backend owns the timer.** `cookSeconds` and `expectedReadyAt` are published only so
  the UI can show a countdown; the authoritative timer is the backend's `asyncio.sleep`, and
  forging those fields cannot change when a dish arrives.

- **Publishes during an outage are dropped, not queued.** Safe *because* state is retained
  and republished in full on reconnect, so the loss self-heals. *Trade-off:* clients see
  stale state for the length of the outage.

- **Broker credentials in the frontend are public, and stated plainly.** Security rests on
  the ACL forbidding that identity from publishing state — otherwise any visitor could
  forge a retained "your food is ready" without the backend involved.

## What I would add with more time

Detail in [FUTURE-WORK.md](docs/FUTURE-WORK.md).

1. **An auth gateway.** Short-lived per-session tokens would give real identity,
   revocation, and per-identity rate limiting — impossible today, which is why current
   limits are resource-based.
2. **Connection abuse controls at the edge** — per-IP rate limiting and connection
   ceilings. By the time application code sees a connection, the cost is paid.
3. **Backpressure on outbound state** — a bounded coalescing queue, plus jitter on the
   reconnect delay, which is currently fixed and would synchronise reconnect storms.
4. **Generated types** from one JSON Schema, with a CI check that the output is current.
5. **Metrics** — accepted/rejected by reason, cook-duration histogram, reconnect count.
   Reconnect rate was the leading indicator for every failure hit while building this.
6. **A chaos test** that kills the broker mid-cook and asserts convergence. That is where
   the `epoch` bug would have been caught automatically.

## Documentation

| Document | Covers |
| --- | --- |
| [EVENT-CONTRACT.md](docs/EVENT-CONTRACT.md) | Topics, payloads, QoS, retention, state machine |
| [DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) | Full rationale and rejected alternatives |
| [SECURITY.md](docs/SECURITY.md) | Threat model, ACLs, accepted risks |
| [FUTURE-WORK.md](docs/FUTURE-WORK.md) | What to build next, including horizontal scaling |
| [DEPLOY.md](docs/DEPLOY.md) | Hosting, CI/CD, troubleshooting per failure mode |
| [INCIDENTS.md](docs/INCIDENTS.md) | Every non-trivial problem hit, and how it was diagnosed |

## Project layout

```
backend/src/restaurant/
  models.py     wire format (pydantic) — the contract in code
  topics.py     topic construction and parsing
  kitchen.py    domain logic: validation, limits, concurrency, cooking
  store.py      in-memory state behind a Protocol + factory
  transport.py  Transport protocol, MQTT implementation, factory
  app.py        object graph, signals, event loop choice
backend/tools/dev_broker.py    pure-Python broker for dev and tests
frontend/src/lib/
  types.ts      wire format + runtime validators
  state.ts      pure reducers and timing maths
  stores.ts     Svelte stores fed by MQTT
  messaging.ts  MessagingClient interface + factory
deploy/         mosquitto.conf, ACL, systemd unit
```

`Kitchen` imports no MQTT. `App.svelte` imports no mqtt.js. Both sides reach their
transport through a factory, so adding one is additive on both halves.
