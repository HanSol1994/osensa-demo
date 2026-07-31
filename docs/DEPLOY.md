# Deployment

Three pieces need homes. The broker is the one people underestimate, so it comes
first.

| Piece | Where | Why |
| --- | --- | --- |
| MQTT broker | HiveMQ Cloud (free tier) | Gives `wss://` with a valid certificate, auth, and per-credential topic permissions. Getting TLS right on a self-hosted broker is the single most time-expensive part of this otherwise. |
| Kitchen (Python) | Fly.io | Needs a long-running process, not a serverless function. Render's free web tier sleeps; Fly keeps a small machine up. |
| Frontend (Svelte) | GitHub Pages | Static build output, and the repository is already here. No extra account and no API tokens — the workflow authenticates with the built-in `GITHUB_TOKEN` via OIDC, which removes three secrets compared with an external host. |

Self-hosting the broker instead is fully supported — see
[deploy/mosquitto.conf](../deploy/mosquitto.conf),
[deploy/mosquitto.acl](../deploy/mosquitto.acl) and
[deploy/restaurant-kitchen.service](../deploy/restaurant-kitchen.service).

---

## 1. Broker — HiveMQ Cloud

Create a free cluster, then create **two separate credentials** under Access
Management. Two, not one: the web client's password is compiled into the public
JavaScript bundle and is therefore readable by anyone, so it must not be an
identity that can publish state.

| Credential | Permissions |
| --- | --- |
| `kitchen` | Publish `restaurant/table/+/state`, `restaurant/kitchen/status`, `restaurant/client/+/err` · Subscribe `restaurant/table/+/order`, `restaurant/kitchen/reset` |
| `web-client` | Publish `restaurant/table/+/order` · Subscribe `restaurant/table/+/state`, `restaurant/kitchen/status`, `restaurant/client/+/err` |

The load-bearing rule: **`web-client` must not be able to publish to
`.../state`.** Otherwise any visitor can publish a retained "your food is ready"
for any table and every browser will believe it, with the backend never involved.
See [SECURITY.md](SECURITY.md) §2.

Ports: **8884** is WebSockets-over-TLS and is what both halves of this system use.
8883 is plain MQTT over TLS — usable by Python but not by a browser, so using it
for the backend would make the two sides inconsistent for no benefit.

**Optional, demo only:** to make the "Reset all" button work on the hosted demo,
`web-client` also needs publish on `restaurant/kitchen/reset`. That is a
destructive command exposed to anyone who loads the page. Grant it for a
walkthrough, and say so out loud rather than leaving it undocumented.

---

## 2. Kitchen — Fly.io

```bash
cd backend
fly launch --no-deploy --copy-config --name osensa-demo
```

Set the broker identity as secrets so none of it lands in git:

```bash
fly secrets set \
  RESTAURANT_BROKER_HOST=<cluster-id>.s1.eu.hivemq.cloud \
  RESTAURANT_BROKER_USERNAME=kitchen \
  RESTAURANT_BROKER_PASSWORD=<the kitchen password>
```

Everything non-secret is already in [fly.toml](../backend/fly.toml).

```bash
fly deploy
fly logs
```

Success looks like one line:

```json
{"event": "broker_connected", "host": "...", "port": 8884, "transport": "websockets", "tls": true}
```

Then `restaurant_state` snapshots follow every transition.

### Notes

- **No ports are exposed, deliberately.** The kitchen dials out to the broker and
  listens on nothing, so there is no `[http_service]` and Fly's HTTP health checks
  do not apply. Liveness is visible in the logs and, to clients, through the
  retained kitchen status topic backed by the MQTT Last Will.
- **Exactly one machine.** Two kitchens on one broker would both consume the order
  topic and cook every order twice; the in-memory store gives them nothing to
  coordinate through. Scaling out needs MQTT shared subscriptions plus the
  persistence work in [FUTURE-WORK.md](FUTURE-WORK.md) §5, which covers the four
  distinct blockers and the two viable designs.
- `[code:135] Not authorized` in the logs means the credential is wrong, not the
  network. Check the username for typos first.

---

## 3. Frontend — GitHub Pages

One repository setting, done once:

**Settings → Pages → Build and deployment → Source: _GitHub Actions_.**

That is all. Do not pick "Deploy from a branch" — the workflow uploads an artifact
and deploys it, so there is no `gh-pages` branch to point at.

The site lands at `https://<user>.github.io/osensa-demo/`.

### The subpath matters

Pages serves the project from `/osensa-demo/`, not the domain root, so
[vite.config.ts](../frontend/vite.config.ts) sets `base` accordingly. Without it the
HTML loads but every asset request 404s and you get a blank page — the single most
common GitHub Pages mistake.

The `base` is keyed on Vite's `mode`, not its `command`, deliberately: `vite preview`
runs with command `serve` but mode `production`, and it serves already-built output
whose asset URLs already carry the prefix. Keying on `command` would make preview
serve from `/` while the HTML asks for `/osensa-demo/...` — breaking the one place
you would try to catch a base-path mistake before deploying.

Deploying to a root-served host instead means changing that `base` to `'/'`.

### Build-time configuration

The `VITE_*` values come from repository secrets (see §4) and are inlined by Vite at
build time, so they are **not** runtime secrets — they are published in the bundle.
That is expected, and is why `web-client` is deliberately near-powerless. Rotating
that password requires a rebuild, not just a config change.

---

## 4. Continuous deployment

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs lint, types and
tests for both halves on every push and pull request, and deploys both on a push
to `main` — but only if **all** checks pass.

Both deploy jobs depend on **both** check suites, not just their own. The two
halves share a wire contract, so shipping a backend whose payload shape the
frontend cannot parse is worse than shipping nothing. That coupling is real, so
the pipeline models it.

### Required repository secrets

Settings → Secrets and variables → Actions:

| Secret | Where to get it |
| --- | --- |
| `FLY_API_TOKEN` | `fly tokens create deploy -a osensa-demo` — scope it to the app you actually deploy, or the job fails on permissions |
| `VITE_MQTT_URL` | `wss://<cluster-id>.s1.eu.hivemq.cloud:8884/mqtt` |
| `VITE_MQTT_USERNAME` | the `web-client` credential |
| `VITE_MQTT_PASSWORD` | the `web-client` password |

Only four, because GitHub Pages needs no API token or account id. The deploy job
authenticates with the built-in `GITHUB_TOKEN` through OIDC, which is why it carries
`pages: write` and `id-token: write` **scoped to that job** rather than granted
workflow-wide.

The three `VITE_*` values are compiled into the public bundle and are therefore not
secret in any meaningful sense. They live in secrets to keep the cluster URL out of
a public repository, not to protect the password — see [SECURITY.md](SECURITY.md)
§1.

The workflow **fails fast if `VITE_MQTT_URL` is missing**. Without that check the
build would succeed and quietly ship a bundle pointing at `ws://localhost:9001`: a
completely broken deploy behind a green tick.

> **Set the Pages source to "GitHub Actions", not "Deploy from a branch".** The
> branch mode would serve repository files directly, bypassing the build entirely —
> so it would publish TypeScript and Svelte sources instead of a bundle.

The Pages job overrides the workflow-level `concurrency`, which cancels in-progress
runs. Cancelling a Pages deployment mid-update can leave the environment in a
half-applied state, so that job queues rather than cancels.

### One-time setup before the first automated deploy

The Fly app and its secrets must exist first; the workflow deploys, it does not
provision:

```bash
cd backend && fly launch --no-deploy --copy-config --name osensa-demo
```

For the frontend, set **Settings → Pages → Source** to _GitHub Actions_ once. There
is no project to create.

---

## 5. Verifying the deployment

Worth doing all four, in order — each checks something the previous one does not:

1. **Load the page.** Four tables appear with no orders. This proves retained state
   works: the page had no data of its own and learned everything from the broker.
2. **Place an order.** A countdown appears immediately and the dish is served when
   it reaches zero.
3. **Open a second window.** It shows the in-flight order it never placed. This is
   the multi-client requirement — the two windows never talk to each other, they
   both render broker state.
4. **Stop the kitchen** (`fly apps restart osensa-demo`). Within
   seconds every browser shows "kitchen offline" and hides its tables, then
   recovers on its own when the machine comes back. That is the Last Will and the
   reconnect resync working together, and it is the most convincing thing to
   demonstrate.

---

## 6. Troubleshooting

Four failure modes hit while deploying this, and how to tell them apart. All of them
look like "the kitchen will not connect", so the distinguishing detail matters.

**`[Errno 111] Connection refused`, repeating every 3 seconds**

`RESTAURANT_BROKER_HOST` is not reaching the process, so it falls back to its
default of `localhost` and dials a broker that does not exist inside the container.
Note it is *refused*, not *rejected*: the address is wrong, not the credentials.

```bash
fly secrets list -a osensa-demo
```

An empty list, or secrets marked `Staged` on a *different* app, is the cause. Fly
apps each have their own secrets; setting them on one app does nothing for another.

**`instance refused connection. is your app listening on 0.0.0.0:8080?`**

The app config contains an `http_service` block, which `fly launch` adds by default.
It breaks a worker twice over: Fly health-checks a port nothing listens on and
reboots the machine in a loop, and `auto_stop_machines` with
`min_machines_running = 0` stops the machine whenever no HTTP traffic arrives —
which for this app is always. Check with:

```bash
fly config show -a osensa-demo
```

Remove the block and redeploy. [fly.toml](../backend/fly.toml) deliberately has none.

**`[code:135] Not authorized`**

The credentials are wrong. This is an MQTT CONNACK code, so the network path is
fine and the broker actively refused the identity. Check the username for typos
before anything else.

**`Disconnected during message iteration`, connecting and dropping every ~3 seconds**

More than one instance is running. Every instance uses the same MQTT client
identifier, and a broker evicts an existing session when a duplicate id connects, so
two machines kick each other off indefinitely. The giveaway is `fly deploy` or
`fly secrets set` reporting `[1/2]` and `[2/2]`.

```bash
fly machines list -a osensa-demo
fly scale count 1 -a osensa-demo
```

Note the `[[vm]]` block in `fly.toml` does **not** control this — it sets machine
size, while the count is separate state on Fly's side, and Fly provisions two by
default. Confirm the fix by watching the broker rather than the logs: subscribe to
`restaurant/kitchen/status` and you should receive exactly one retained `ONLINE` and
then silence. Repeated status messages mean it is still flapping.

**The machine keeps stopping after about five minutes**

Two completely different causes produce the same interval, and they are told apart by
*who* stopped it:

```bash
fly machine status <id> -a osensa-demo    # look at the SOURCE column
```

- `SOURCE = runner` with a log line `Trial machine stopping` — a Fly trial account
  limit, unrelated to this app's configuration.
- `SOURCE = proxy`, preceded by `cordon` and with **no** trial log line — Fly's
  `auto_stop_machines`, i.e. the `http_service` block is still present in the
  **machine's** config. Verify with:

```bash
fly machines list -a osensa-demo --json
```

and look for `config.services`. Editing `fly.toml` is not enough — the machine keeps
its existing config until a deploy actually lands.

**`Error: unauthorized` from flyctl in CI**

The `FLY_API_TOKEN` is scoped to a different app than the one being deployed — easy
to hit if the token was created while `fly.toml` named another app, and guaranteed if
that app has since been destroyed. Regenerate it against the right app and update the
repository secret.

**`Error: The operation was canceled` part-way through a deploy**

A newer push cancelled the run. If it had already reached `Acquiring lease`, the
deploy is half-applied and the lease may still be held. This is why the workflow's
`cancel-in-progress` is conditional on the ref — `main` runs queue instead of
cancelling, because job-level concurrency cannot protect a job from workflow-level
cancellation.

**`Get Pages site failed … Not Found` from configure-pages**

GitHub Pages has never been enabled on the repository. `enablement: true` (already
set) turns it on through the API; failing that, set **Settings → Pages → Source** to
_GitHub Actions_ manually.

**Deploy jobs are skipped even though the checks pass**

The run was triggered by something the deploy condition excludes. A
`workflow_dispatch` run is not a `push`, so a condition testing only
`github.event_name == 'push'` silently skips both deploys while every check goes
green. Look at the run's trigger before suspecting the jobs themselves.

**Deploys go to an app you are not looking at**

`fly deploy` uses the `app` key in `fly.toml`, while `fly logs -a NAME` uses whatever
you type. If those disagree you will be reading a healthy app's logs while a
different one fails, or vice versa. Keep one app and let `fly.toml` name it.

---

## 7. Self-hosted alternative

Committed for the case where the broker must live next to the hardware rather than
in a cloud region:

- [deploy/mosquitto.conf](../deploy/mosquitto.conf) — authenticated, ACL-enforced,
  with both TCP and WebSocket listeners and bounded connection/message limits.
- [deploy/mosquitto.acl](../deploy/mosquitto.acl) — the topic authorization rules,
  including `pattern read restaurant/client/%c/err` so each client can read only
  its own inbox.
- [deploy/restaurant-kitchen.service](../deploy/restaurant-kitchen.service) — a
  hardened systemd unit. The process needs no capabilities, no writable paths and
  no listening sockets, so the unit grants none of them. Logs go to journald as
  JSON: `journalctl -u restaurant-kitchen -o cat | jq`.

The password file is generated, never committed:

```bash
mosquitto_passwd -c /mosquitto/config/mosquitto.passwd kitchen
mosquitto_passwd    /mosquitto/config/mosquitto.passwd web-client
```

Note `-c` truncates the file, so it belongs only on the first invocation.
