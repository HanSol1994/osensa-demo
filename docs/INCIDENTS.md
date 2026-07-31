# Problems Hit While Building This

Every non-trivial problem encountered, with the symptom, how it was diagnosed, the
actual cause, and what it changed. Kept because the interesting part of a project is
rarely the code that worked first time.

Grouped by theme. The ones marked **★** are the ones worth volunteering unprompted —
they involve a wrong first hypothesis, which is usually the more interesting story.

---

## A. Correctness bugs in this codebase

### A1 ★ The version guard rejected a restarted backend forever

**Symptom.** Restart the kitchen with a browser open and the page keeps showing
orders that no longer exist. Permanently — it never recovers.

**Diagnosis.** The client discards any table state whose `version` is not greater
than the version it already holds, to survive duplicate and out-of-order delivery.
Kitchen state is in-memory, so a restart resets every `version` to zero. A browser
holding version 5 therefore rejects the restarted kitchen's 0, 1, 2 … and can only
recover once the new process climbs past 6.

**Cause.** Comparing sequence numbers across two different generations of state, as
if they were from one continuous sequence.

**Fix.** Added an `epoch` — a random id per kitchen generation. Versions are compared
*only within an epoch*; a different epoch is accepted unconditionally. Same idea as a
fencing token or a Raft term.

**Why it matters.** Found by restarting the backend while watching the UI, not by
reading the code. A monotonic counter is only monotonic within the lifetime of
whatever holds it.

### A2 ★ The countdown always read one second too high

**Symptom.** A 9-second cook displayed "10s left". Consistently, never off by two.

**Diagnosis.** The consistency was the clue — a race would be intermittent. Two
things combined: the shared clock store ticks once per second, so its value can be
nearly a second stale at the moment an order arrives; and `remainingSeconds` used
`Math.ceil`, which rounds any fraction up. Stale-by-0.8s plus `ceil` equals exactly
one second too many, every time.

**Fix.** `Math.floor`, which absorbs the sub-second staleness instead of amplifying
it, and matches how a countdown reads: "9s left" means at least 9 seconds remain.
Covered by a regression test that feeds a deliberately stale clock value.

### A3 A control-character regex would have rejected every food name with a space

**Symptom.** None — caught before it ran.

**Cause.** The character class was written as a literal range from space to hyphen
rather than the C0 control range, so it matched space, `!`, `"`, `#` and more.

**Fix.** Explicit `\u0000-\u001F\u007F`, plus a test asserting that
`"Green curry with rice"` is accepted. That test exists specifically to catch this
class of mistake returning.

### A4 Invisible control bytes ended up inside source files

**Symptom.** An edit failed to match text that looked identical on screen.

**Diagnosis.** Dumped the line's character codes: the file contained raw `0x00`,
`0x1F` and `0x7F` bytes rather than escape sequences.

**Fix.** Replaced with `\u` escapes, and added
[`scripts/check-control-bytes.mjs`](../scripts/check-control-bytes.mjs), which fails on
any raw control byte in a tracked file and runs in CI. Test data that needs control
characters now builds them with `String.fromCharCode`, so the source stays plain ASCII.

**Lesson.** "Looks right" is not a check when the characters are invisible — it has to be
a machine check, not a review convention. Proven by the fact that this very document
later shipped with a raw NUL byte in the paragraph above, which the scanner caught.

### A5 Two log fields on one line disagreed about the same instant

**Symptom.** A state snapshot read `cookingNow=4` while its own `summary` already
showed one dish served.

**Cause.** Two separate errors. The count was emitted before the cooking task was
registered, so it trailed by one; and it came from the asyncio task set, which still
contains a task while that task's own coroutine is running.

**Fix.** Count from the same store snapshot that produces the summary. Two numbers
describing one moment should come from one source.

---

## B. asyncio and MQTT semantics

### B1 Windows cannot run the MQTT client on the default event loop

**Symptom.** `NotImplementedError` from `add_reader` / `add_writer`; the client never
connects.

**Cause.** paho registers its socket with `loop.add_reader()`, a selector-loop API.
Windows defaults to the Proactor loop, which does not implement it.

**Fix.** `asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)` on `win32`. Linux —
the deployment target — already uses a selector loop, so this is a development-only
concern. Chose `Runner` over the event-loop policy API because policies are deprecated.

### B2 ★ The connection died every 20.0 seconds, exactly

**Symptom.** Connect, work, drop after precisely 20 seconds, reconnect, repeat.

**Diagnosis.** The precision ruled out a network fault. Only the paho-based backend
was affected — the browser's mqtt.js client on the same broker stayed connected — so
the problem was client-specific rather than broker-wide. Reading the broker's source
showed it starting its listener via `websockets.serve()`, whose default is
`ping_interval=20`.

**Cause.** The WebSocket *server* sends a protocol-level Ping every 20 seconds and
expects a Pong. paho's WebSocket support is minimal and does not answer them.

**Fix.** Disabled WebSocket-level pings on the development broker only. MQTT has its
own liveness mechanism in PINGREQ/PINGRESP, so nothing is lost. Deliberately scoped to
the dev broker, because Mosquitto and HiveMQ do not behave this way — patching
production code for a dev-broker quirk would have been the wrong fix.

### B3 The backend received its own published messages

**Symptom.** Warnings about unroutable topics naming the backend's own outbound
topics, including one that could not match its subscription under any wildcard rule.

**Diagnosis.** That impossible match was the clue: `restaurant/kitchen/status` has
three segments and cannot match `restaurant/table/+/order`. So the delivery could not
be explained by this app's subscriptions. Reading the broker source found it iterating
the broker-*global* subscription map on connect and replaying retained messages for
filters belonging to **other** sessions.

**Cause.** A bug in the development broker, not in this code.

**Fix.** Nothing functional — the consumer already dropped unknown topics rather than
crashing, which is exactly why a broker bug degraded into a log line. Reclassified
recognised own-topics to `DEBUG` so the log stops implying an anomaly.

### B4 ★ Two backend instances took the whole system down

**Symptom.** After deploying, connect/disconnect every ~3 seconds forever, with
`Disconnected during message iteration`.

**Diagnosis.** The deploy output said `[1/2]` and `[2/2]` — two machines. Both connect
with the same MQTT client identifier, and a broker evicts an existing session when a
duplicate id connects. So each instance kicked the other off, forever.

**Fix.** `fly scale count 1`, and documented that the `[[vm]]` block controls machine
*size*, not count.

**Why it matters.** This is the failure mode the architecture had already been
documented as vulnerable to — one kitchen, because the in-memory store gives two
nothing to coordinate through. Seeing it happen for real produced
[FUTURE-WORK.md](FUTURE-WORK.md) §5, which works through what genuine horizontal
scaling would require.

### B5 `contextlib.suppress(Exception)` did not suppress the exception

**Symptom.** Every test failed at teardown with `CancelledError`, and pytest reported
`previous item was not torn down properly` on *unrelated* tests.

**Cause.** `asyncio.CancelledError` derives from `BaseException`, not `Exception`, so
`suppress(Exception)` does not catch it. The broker's `shutdown()` leaks one.

**Fix.** Named it explicitly, with a comment on why suppressing cancellation is
defensible in teardown-only tooling and would not be elsewhere.

---

## C. Testing

### C1 ★ Ports could not be reserved by checking them first

**Symptom.** Integration tests passed one at a time and failed in a suite; a fixed
port failed after the first test, and the usual "bind a probe socket to find a free
port" trick failed *immediately*.

**Cause.** Two layers. The broker does not release its listening socket synchronously
on shutdown, so a fixed port is still held. And asyncio deliberately does not set
`SO_REUSEADDR` on Windows — it permits socket hijacking there — so a port a probe
socket just released cannot be rebound.

**Fix.** Pick a random high port and retry on `OSError`. Port availability genuinely
cannot be checked without taking the port, so retrying is the only honest approach.

### C2 A validation test asserted the wrong thing

**Symptom.** Control-character tests failed for `\n`, `\r` and `\x1F`.

**Diagnosis.** Not a code bug. `strip()` runs before the control-character check, and
Python treats those three as whitespace, so a *trailing* one is removed rather than
rejected. Interior ones — the log-injection risk — are still rejected.

**Fix.** Split into two tests: interior characters are rejected, trailing whitespace is
stripped. The behaviour was right; the test encoded a wrong assumption.

### C3 Shared test helpers in `conftest.py` could not be imported

**Cause.** pytest loads `conftest.py` through its own mechanism, so importing it as a
normal module creates a second copy and duplicates its fixtures.

**Fix.** Moved shared doubles to `tests/helpers.py` and made `tests` a package.

---

## D. Deployment

### D1 ★ `Connection refused` was a missing setting, not a network problem

**Symptom.** The deployed backend logged `[Errno 111] Connection refused` on a loop.

**Diagnosis.** The error was *refused*, not *rejected* — wrong credentials produce an
MQTT-level `Not authorized`, which proves you reached a broker. `ECONNREFUSED` means
nothing was listening at the address. `RESTAURANT_BROKER_HOST` defaults to
`localhost`, and there is no broker inside the container.

**Cause.** Two Fly apps existed; the secrets were set on the one that had never been
deployed, where they sat in `Staged` state. The running app had none.

**Lesson.** A default that is convenient for local development becomes a confusing
error in production. The default is still right, but the failure now appears in the
troubleshooting guide.

### D2 The launch tool configured the app as a web service

**Symptom.** `is your app listening on 0.0.0.0:8080?` plus a reboot loop.

**Cause.** `fly launch` adds an `http_service` block by default. This backend is a
pure MQTT client that dials out and listens on nothing.

**Why it was the more dangerous of the two.** The failing health check caused a
visible reboot loop — loud, easy to notice. But the same block also sets
`auto_stop_machines` with `min_machines_running = 0`, which stops the machine whenever
no HTTP traffic arrives. For a worker that never receives HTTP, that is always: a
silently stopped service. Loud bugs are safer than quiet ones.

### D3 The launch tool then re-added the block via a pull request

**Symptom.** `fly.toml` contained `<<<<<<<` conflict markers, so it was not valid TOML
at all.

**Cause.** `fly launch` opened a PR against the repository re-adding its generated
config; merging it collided with the commit that removed the block.

**Fix.** Resolved in favour of no `http_service`, and left a comment in the file
recording that this has already happened once.

### D4 ★ Three different causes produced the same five-minute symptom

**Symptom.** The machine kept stopping after roughly five minutes. Adding a payment
method appeared not to help.

**Diagnosis, and the wrong turn.** The first stops carried
`Trial machine stopping` — a Fly trial limit. After the card was added it stopped
again at the same interval, and the natural conclusion was that the card had not taken
effect. That was wrong. The machine event log showed `SOURCE = proxy` with a `cordon`
immediately before, and **no** trial message: this was `auto_stop_machines` from D2,
not the trial cap. The card had worked.

**The real cause underneath.** The corrected `fly.toml` had never actually been
deployed. Three attempts had each failed for a *different* reason — one targeted the
wrong app, one was cancelled mid-deploy, one hit a stale token — so the machine kept
its original configuration the whole time. Inspecting the machine's own config
(`fly machines list --json`) rather than the local file showed
`internal_port=8080, autostop=true` still present.

**Lesson, and the one worth saying out loud.** Editing infrastructure config is not
the same as applying it, so verify against the deployed object rather than the file.
And when a symptom recurs after a fix, re-read the evidence instead of assuming the
same cause: the event `SOURCE` field had already changed from `runner` to `proxy`, and
going back to it was what settled the question.

### D5 CI cancelled its own deploy half-way through

**Symptom.** `Acquiring lease for …` followed by `Error: The operation was canceled`.

**Cause.** The workflow set `cancel-in-progress: true` for every ref, so each new push
killed the previous run — including one part-way through updating a machine, leaving
the deploy half-applied and the lease held.

**Fix.** Cancellation is now conditional on the ref: superseded branch and
pull-request runs are cancelled, `main` runs queue. Job-level concurrency cannot fix
this, because workflow-level cancellation kills the whole run regardless.

### D6 Deploy jobs were skipped while every check passed

**Cause.** `workflow_dispatch` was added as a trigger, but the deploy conditions
tested `github.event_name == 'push'`. Manual runs therefore ran the tests and silently
skipped both deploys.

**Fix.** Accept `push` or `workflow_dispatch`, while still requiring
`refs/heads/main` so a manually dispatched feature branch cannot ship.

### D7 A deploy token scoped to a deleted app

**Symptom.** `Error: unauthorized` from flyctl in CI, after the credentials had been
proven working locally.

**Cause.** The token had been created while `fly.toml` named a different app — an app
since destroyed.

### D8 Pages deployment failed before it started

**Symptom.** `Get Pages site failed … Not Found`.

**Cause.** GitHub Pages had never been enabled on the repository.

**Fix.** `enablement: true` on `configure-pages`, so the workflow provisions Pages
itself rather than depending on a manual settings change. A fresh clone now deploys
with no setup step.

---

## E. Environment and tooling

### E1 Switching Node versions removed Node from PATH

`nvm use` on Windows needs administrator rights to swap its symlink. It reported
success, failed silently, and left no `node` on PATH at all. Worked around by invoking
the binary by absolute path; the real fix is an elevated shell.

### E2 The project scaffolder silently produced the wrong template

`npm create vite -- --template svelte-ts` generated a **vanilla-ts** project — no
Svelte in `package.json` at all. Noticed because the file listing contained
`counter.ts`. Wired Svelte up by hand, which was better anyway: it made every config
choice explicit rather than inherited.

### E3 `cmd.exe` cannot run `.venv/Scripts/python`

Forward slashes are switch separators in `cmd`, so the path fails to resolve;
PowerShell accepts either. The README now gives both forms in a table instead of a
footnote, because it is the first thing a reviewer would hit.

### E4 A static-site host served the app from a subpath

GitHub Pages serves at `/<repo>/`, not the domain root, so Vite's `base` must match or
the HTML loads and every asset 404s. Keyed on Vite's `mode` rather than its `command`,
because `vite preview` runs with command `serve` but mode `production` — keying on
`command` would break preview, which is precisely where a base-path mistake should be
caught.

### E5 Reading a UTF-8 file with the wrong encoding corrupted it

A PowerShell read/replace/write round-trip decoded UTF-8 as ANSI and turned every em
dash into mojibake across a documentation file. Restored from git and redone with an
explicitly UTF-8-aware tool. Cheap to fix because it was committed; the lesson is that
text transformations need an explicit encoding, not a default.

### E6 A push used the wrong identity

The first push was rejected with a permission error naming an unrelated account: the
machine's cached credential belonged to a different GitHub account. Commit *authorship*
and push *authentication* are separate — the commits were already correctly attributed.
Fixed by qualifying the remote URL with the intended username, which left the other
account's cached credential untouched.

---

## What this list is actually evidence of

Three things worth drawing out if asked:

1. **Error messages were read precisely.** `refused` vs `rejected`, `runner` vs
   `proxy`, CONNACK 135 vs `ECONNREFUSED` — each pair looks like "it will not connect"
   and points at a different layer. Most of the deployment time went into telling them
   apart, and every one is now in the troubleshooting guide with the command that
   distinguishes it.

2. **The application degraded rather than broke.** A broker bug (B3), a duplicate
   session (B4), a wrong host (D1) and a stopped machine (D4) all produced bounded,
   visible, self-healing behaviour: log lines and retries, not crashes or lost data.
   That was the point of the supervised reconnect loop, the never-raising consumer, and
   the retained-state resync.

3. **Two hypotheses were wrong and got corrected by evidence** — the frozen timers
   (a hot-reload artifact, not a reactivity bug) and the five-minute stops (D4). Both
   were resolved by measuring rather than reasoning further.
