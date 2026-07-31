# Security Analysis

This is a simulation, not a production ordering system, so the point of this
document is to be explicit about which threats are *handled*, which are
*deliberately accepted*, and which would need real work. Pretending a browser
application has secrets it cannot have would be worse than saying so plainly.

## 1. The browser bundle is public — the central constraint

Everything shipped to the browser is readable by anyone who opens devtools,
including the broker username and password in `VITE_MQTT_*`. There is no way to
give a public web page a secret.

The consequence: **an attacker can always connect to the broker with the web
client's own credentials.** Security therefore cannot rest on secrecy; it must
rest on that identity being permitted to do very little. Everything in §2 follows
from accepting this rather than wishing it away.

The real fix, out of scope here, is in [FUTURE-WORK.md](FUTURE-WORK.md): a small
auth service mints a short-lived per-session token, and the broker verifies it.
The browser then never holds a long-lived shared credential.

## 2. Topic authorization is the most important control

Without ACLs, any client that can connect can do all of the following, and none of
them involve the backend at all:

| Attack | Topic abused | Effect |
| --- | --- | --- |
| Forge "food is ready" | publish `restaurant/table/1/state` | Retained lie; every browser shows a dish that was never cooked, and it persists |
| Wipe the restaurant | publish `restaurant/kitchen/reset` | Cancels every in-flight order and clears all tables, for everyone |
| Erase a table | publish empty retained to `.../state` | Clears the retained snapshot for all future clients |
| Fake the kitchen being alive | publish `restaurant/kitchen/status` | UI reports ONLINE while the kitchen is down |
| Read someone's rejections | subscribe `restaurant/client/+/err` | Side channel into another session |
| Read everything | subscribe `restaurant/#` | Full traffic visibility |

The mitigation is least privilege at the broker. The web client identity gets
**publish on order topics only**, and **subscribe scoped to its own inbox**:

```conf
# deploy/mosquitto.acl
pattern write  restaurant/table/+/order
pattern read   restaurant/table/+/state
pattern read   restaurant/kitchen/status
# %c expands to the connecting client's own MQTT client id, so one client cannot
# subscribe to another client's error inbox.
pattern read   restaurant/client/%c/err
# Note the absence of `restaurant/kitchen/reset`. See below.
```

### The reset command, explicitly

`restaurant/kitchen/reset` is destructive, affects every connected client, and is
authenticated by nothing. It exists because being able to clear the board makes a
demo dramatically easier to walk through, which is a real requirement for this
submission — but it is not a customer action.

Two controls, deliberately independent:

- **`RESTAURANT_ENABLE_RESET`** (default true) lets the kitchen refuse the command
  outright. Turning it off is the safe posture for anything public-facing.
- **The broker ACL** should not grant the public web client publish rights on the
  topic. That is the stronger control, because it stops the message ever reaching
  the kitchen rather than relying on the kitchen to decline.

The payload carries `requestedBy` for the log line only. It is never used to
authorise anything — a self-asserted client id is not an identity (see §5).

The kitchen connects as a **separate** identity, and it is the only one permitted
to publish state. This is why the state topics being retained is safe: only one
principal can write them.

`%c` matters. Scoping the inbox by pattern rather than granting
`restaurant/client/+/err` is the difference between a private channel and a
broadcast one.

## 3. Transport confidentiality

`ws://` is plaintext: credentials and every order are readable by anyone on the
path. Production uses `wss://` only, with normal certificate verification left
**on** — `aiomqtt.TLSParameters()` defaults to verifying, and nothing in this
codebase disables it. Local development uses `ws://` against a loopback-bound
broker, which never leaves the machine.

## 4. Untrusted input

Both sides validate independently, because a check that exists only in the browser
is not a check — an attacker publishes straight to the broker and skips the UI
entirely. The browser validation in `validateFoodName` is a UX affordance; the
pydantic models are the enforcement.

- **Length** — `foodName` is capped at 80 characters, bounding payload size and
  the retained snapshot.
- **Control characters** — rejected. Otherwise a newline in a food name could
  forge a fake line in the structured logs (log injection), and stray control
  bytes corrupt terminal output.
- **Unknown fields** — inbound models use `extra="forbid"`, so a payload carrying
  unexpected keys is rejected rather than partially applied.
- **Topic/payload agreement** — `tableId` appears in both the topic and the body;
  a mismatch is rejected rather than trusting either.
- **Malformed JSON** — dropped with a warning. The consumer loop must survive any
  bytes a client can send; `handle_order_message` cannot raise.

### XSS

Food names are attacker-controlled and rendered into the DOM. Svelte escapes
interpolations by default, so `{order.foodName}` becomes text, never markup. This
is only safe because the codebase **never uses `{@html}`** — that is the single
rule to keep. A Content-Security-Policy limiting `connect-src` to the broker
origin is listed as future work, to contain exfiltration if an XSS ever did land.

## 5. Denial of service and resource exhaustion

Every unbounded collection is a DoS vector, so each one has a cap:

| Resource | Bound | Why |
| --- | --- | --- |
| Orders cooking per table | 5 | One table cannot monopolise the kitchen |
| Orders cooking globally | 100 | Bounds concurrent asyncio tasks and memory |
| Finished orders kept per table | 10 | Retained payload stays small |
| Dedupe cache entries | 1000 (LRU) | Cannot grow without limit |
| `foodName` length | 80 chars | Bounds message size |

**These limits are deliberately resource-based, not identity-based.** That is what
makes them survive the client-churn attack below: an attacker who rotates client
ids on every request still hits the same global ceiling, because nothing is
counted per identity.

### Connection churn — "what if they open and close the browser repeatedly?"

Measured behaviour:

- **Refreshing (F5) does not create a new client.** The client id lives in
  `sessionStorage`, which survives a reload, so the same MQTT session is reused.
  Verified directly: the id was identical before and after a forced reload.
- **Closing and reopening a tab does create a new client id**, because
  `sessionStorage` is discarded with the tab. This is intentional — see §6.
- Those sessions do not accumulate. The client connects with `clean: true`, so the
  broker discards session state on disconnect rather than retaining subscriptions
  and queued messages.

What is *not* handled in this version, and belongs at the edge rather than in
application code:

- **Connection rate limiting per IP.** Nothing here stops a script opening
  thousands of connections. Mosquitto's `max_connections` caps the total, but
  per-IP throttling needs a reverse proxy or the cloud broker's own limits.
- **Cost per connect.** Each connection costs a TLS handshake, an auth check, and
  delivery of one retained message per table. Cheap individually, not free in
  aggregate.

### Distinguishing a real client from a bot

Worth being blunt: **this is not solvable with client ids.** A client id is a
string the client chooses; anyone can send any value. Rotating ids cannot be
detected by inspecting them.

Telling real clients from fake ones requires an *authenticated* identity issued by
something the client does not control — which is the auth gateway in
[FUTURE-WORK.md](FUTURE-WORK.md). Until then, the honest position is that this app
treats all clients as anonymous and untrusted, and defends its *resources* rather
than trying to judge *identities*.

## 6. Session identity choices

`sessionStorage` is used for the client id rather than `localStorage`,
specifically for security and correctness reasons:

- `localStorage` is shared across all tabs of an origin. Two windows would then
  present the **same MQTT client id**, and brokers disconnect the older client
  when a duplicate id connects — the two windows would fight, each kicking the
  other off in a loop.
- Ids come from `crypto.randomUUID()`, so they are unguessable. Sequential or
  predictable ids would let an attacker deliberately collide with a known id and
  repeatedly disconnect that user (a targeted DoS), or subscribe to their error
  inbox.

## 7. Supply chain

The frontend ships `mqtt.js` and its transitive dependencies into the bundle.
Versions are pinned via `package-lock.json`, and `npm audit` currently reports no
vulnerabilities. CI runs the audit so a new advisory surfaces as a build signal
rather than a surprise.

## 8. Accepted risks (explicitly out of scope)

| Risk | Why accepted |
| --- | --- |
| No user authentication | The assignment is an anonymous restaurant simulation |
| Any client may order for any table | There is no concept of table ownership |
| Broker credentials shared by all browsers | Unavoidable for a public page; mitigated by ACL |
| No audit trail of who ordered what | No identity to attribute orders to yet |
