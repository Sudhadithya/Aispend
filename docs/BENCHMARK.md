# Aispend — proxy latency, measured

A proxy lives or dies on the latency it adds. This measures that against a stub
upstream, so the numbers cost nothing in API spend and don't move with
Anthropic's load.

Read in order: §1 is what the proxy costs today, §2 is what it cost before and
what was wrong with it, §3 is what got fixed and what deliberately didn't, and
§4 is the guardrail — the most important thing in this document, because it is
the one that came from breaking something.

Harness: [`bench/`](../bench). Reproduce with §6.

---

## 1. Where it stands

**Added latency vs. talking to the stub directly, non-streaming:**

| concurrency | direct p50 | proxied p50 | **added p50** | **added p95** | **added p99** | proxy rps |
|---|---|---|---|---|---|---|
| 1 | 0.81ms | 10.46ms | **+9.6ms** | **+12.6ms** | **+15.3ms** | 93 |
| 4 | 2.43ms | 42.29ms | **+39.9ms** | **+49.0ms** | **+58.7ms** | 92 |
| 16 | 9.72ms | 280.24ms | **+270.5ms** | **+703.4ms** | **+1208.2ms** | 45 |
| 50 | 30.14ms | 790.23ms | **+760.1ms** | **+2750.2ms** | **+3905.0ms** | 38 |

1200 samples per phase, 6 interleaved rounds. Windows 11 / Python 3.14 /
Postgres 16 in Docker Desktop.

**The number that generalises is the first row: ~10ms added at p50, ~13ms at
p95, with one request in flight.** Everything below that line is queueing, not
service time — the proxy saturates at **~90 requests/sec**, and past
concurrency 4 the "added latency" is just the queue draining.

For what this tool actually is — one developer, a coding agent, a handful of
concurrent requests against an API call that takes seconds — 10ms is
negligible. Against a real 2-second Anthropic call it is **0.5%**. The
saturation point only matters if this ever goes in front of a team.

### Where the 10ms goes

Each layer isolated against the same stub, concurrency 1:

| layer | p50 | share |
|---|---|---|
| stub alone, raw socket | 0.81ms | — |
| **proxy's httpx call to upstream** — no Starlette, no capture | **5.96ms** | **~53%** |
| Postgres `INSERT` + commit | 5.05ms | now off the request path (§3.1) |
| full proxy | 10.46ms | 100% |

**The dominant cost is httpx** — the client the proxy uses to reach upstream.
Not pricing, not the database, not SSE parsing. Roughly half of every
millisecond the proxy adds is spent inside the HTTP client library, and it also
sets the throughput ceiling: httpx alone, nothing else in the path, tops out at
~160 rps at concurrency 1 and degrades to ~94 rps at concurrency 50.

---

## 2. What it looked like before, and what was wrong

Five problems, in descending order of how much they actually cost.

| # | Issue | Evidence | Status |
|---|---|---|---|
| 1 | Pricing ran **inline** on the request path — every response waited on a Postgres commit | 5.05ms of a 13.3ms p50 | **Fixed** (§3.1) |
| 2 | **httpx is the throughput ceiling** — ~53% of added latency, caps at ~90 rps | isolated measurement, §1 | **Not fixed** (§3.5) |
| 3 | Connection pool was **4 connections wide**, unintentionally | writes cap ~780/sec | **Fixed** (§3.2) |
| 4 | Proxy turned transient upstream failures into **client-visible 502s** | 1 in ~2400 at conc 50 | **Fixed** (§3.3) |
| 5 | **`pytest` truncated whatever `DATABASE_URL` pointed at** | destroyed live data | **Fixed** (§4) |

Issue 5 is not a performance problem and it is the one worth reading.

---

## 3. What was done, and what wasn't

### 3.1 Fixed — pricing was on the request path

**Before.** `_capture_safely` was `await`ed before the response returned. Every
single request paid for a Postgres commit before its bytes went back to the
client. The proxy's own architecture doc already claimed capture "never
blocks"; that claim was false.

**After.** `_capture_off_path` schedules capture as a task. The response goes
out as soon as the upstream body is in hand. Tasks are held in a module-level
set (asyncio keeps only a weak reference to a running task — without a strong
one, a capture can be garbage-collected mid-flight and the row silently never
lands) and drained on shutdown.

**Measured**, both variants running simultaneously on separate ports, interleaved:

| concurrency | inline p50 | off-path p50 | ratio | p95 ratio | verdict |
|---|---|---|---|---|---|
| 1 | 13.27ms | **8.51ms** | **0.64x** | **0.60x** | −4.8ms p50, −8.1ms p95 |
| 4 | 36.61ms | 34.58ms | 0.94x | 0.88x | marginal |
| 16 | 205.75ms | 220.82ms | 1.07x | 0.95x | noise |
| 50 | 700.35ms | 727.62ms | 1.04x | 1.00x | no effect |

At one request in flight, taking the commit off the path removes **39% of the
proxy's added latency** — almost exactly the 5.05ms an `INSERT` + commit costs.
The win vanishes under load because the event loop is httpx-bound: moving work
off the request path doesn't reduce it, it only reorders it.

**Kept** because concurrency 1–4 *is* the operating point of one developer's
toolchain, and because the architecture notes already described capture as
never blocking the response — this is what makes that true.

### 3.2 Fixed — the pool was 4 connections wide

**Before.** `ConnectionPool(url, open=True)` takes psycopg's default
`min_size=4`, and `max_size` defaults to `min_size`. So `max_size=4` — a
ceiling nobody chose. Measured: writes cap at **~780/sec**, and at 32
concurrent writers per-write p50 rises from 5ms to **41ms** queueing for a slot.

**After.** `min_size=4, max_size=16`, both env-tunable.

**What is not claimed:** this does not show up in end-to-end latency, and no
measurement here says it does. The proxy saturates at ~90 rps, far below where
4 connections bind. It removes an accidental ceiling — which matters more now
that captures run off-path and can pile up.

### 3.3 Fixed — the proxy turned transient failures into 502s

**Before.** One request in ~2400 at concurrency 50 got a 502 from the proxy,
against a stub that never failed once. A transient connection error, surfaced
to the client. Rare, but it means the proxy was less reliable than the API it
fronts, which defeats the point of being transparent.

**After.** One retry, on `ConnectError` and `RemoteProtocolError` only —
failures where the request **provably never reached Anthropic**, so a replay
cannot double-send and therefore cannot double-charge. A `ReadError`
mid-response is deliberately *not* retried: the request may already have been
processed and billed. There is a test pinning that distinction in both
directions.

**What is not claimed:** a 4000-request soak at concurrency 50 on the fixed
build saw zero errors. That is *consistent with* the retry working. Against a
~1-in-2400 baseline it is not proof, and shouldn't be quoted as such. It has
also only been tested against mocks, never a real API key.

### 3.4 Tried and rejected on evidence — widening httpx keepalive

httpx defaults to `max_connections=100` but `max_keepalive_connections=20`, so
above 20 concurrent requests a proxy reconnects instead of reusing. Raising
keepalive to match looks like free throughput, and one isolated measurement
agreed: concurrency-1 throughput 161 → 228 rps.

The interleaved A/B disagreed, keepalive 20 vs 100:

| concurrency | p50 | p95 | p99 |
|---|---|---|---|
| 1 | 0.99x | 1.01x | 1.00x |
| 16 | **1.14x** | **1.12x** | **1.28x** |
| 50 | **1.10x** | **1.17x** | **1.23x** |

Neutral at concurrency 1 and **10–28% slower** under load — holding many idle
sockets costs more than recycling them. Reverted to httpx's default, now
explicit with the measurement recorded beside it so nobody optimises it again.

That 161 → 228 figure was drift, and it nearly shipped a regression. The knob
stays env-tunable so the range remains testable.

### 3.5 Not done — replacing httpx

The ceiling in §1 is httpx: ~53% of added latency and the entire ~90 rps cap.
Everything reachable by configuration was tried, and the one knob that looked
promising made things worse (§3.4).

Actually fixing it means replacing the client on the upstream path — a rewrite,
not a tuning change. **The ceiling is understood and documented, not fixed.**
That is the honest state.

### 3.6 Not done — time-to-first-token

The stub emits its whole SSE body at once, so the streaming numbers below
measure time-to-*last*-byte:

| concurrency | direct p50 | proxied p50 | added p50 | added p95 |
|---|---|---|---|---|
| 1 | 0.83ms | 11.49ms | +10.7ms | +14.4ms |
| 16 | 9.76ms | 168.96ms | +159.2ms | +988.8ms |

Same shape as non-streaming — forwarding overhead dominates, capture placement
isn't what drives it. But for a streaming agent the number a *user feels* is
time-to-first-token, and this harness does not measure it. Doing that honestly
needs a stub that paces its chunks.

### 3.7 Left in, flagged

`INLINE_CAPTURE` is benchmark scaffolding sitting in a hot path. It is kept so
the §3.1 A/B stays reproducible — without it nobody can re-derive the number —
but it is a branch in the request path that exists for measurement, not for
users. Worth removing once the result is trusted.

---

## 4. The guardrail — the important learning

**`pytest` deleted live data during this work.** Not a near miss.

### What happened

All six test modules do this:

```python
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://...@localhost:5555/aispend")

@pytest.fixture
def conn():
    ...
    connection.execute("TRUNCATE requests RESTART IDENTITY")
```

Running `pytest` with `DATABASE_URL` pointed at the dev database wiped it.

### Why the obvious lesson is the wrong one

The tempting conclusion is "be careful which database you point tests at." That
lesson is worthless, because it depends on a human being careful every single
time, and it would not have prevented this.

The real defect: **the destructive path was the default one.** Each module
defaulted `DATABASE_URL` to the *dev* database. Running `pytest` with no
environment set at all — the most innocent possible invocation, the one a new
contributor types first — truncated real data. There was no wrong action to
avoid. The safe-looking action was the destructive one.

### What was built

[`tests/conftest.py`](../tests/conftest.py) rewrites `DATABASE_URL`, before any
test module is imported, to a sibling database with `_test` appended:
`aispend` → `aispend_test`, created on first use.

The property this buys is worth stating precisely:

> Pointing `DATABASE_URL` at something precious is no longer **sufficient** to
> destroy it.

That is what separates a guardrail from a warning. A warning relies on someone
reading it; a guardrail removes the capability. Backstops:

- An explicit `AISPEND_TEST_DATABASE_URL` not ending in `_test` **aborts the
  run** with an explanation, rather than proceeding.
- The `_test` suffix is checked, never assumed.
- It derives from whatever `DATABASE_URL` is set to, so it needs no
  configuration locally or in CI — including CI's own `aispend` database.

**Verified both directions**, because an untested guardrail is a guess: a
sentinel row placed in the dev database survived a full 78-test run, and the
explicit non-test override was confirmed to abort.

### The generalisable rule

Any operation that can destroy data should be **hard to invoke by accident, not
merely documented as dangerous.** When a destructive operation and its safe
variant differ only by an environment variable someone has to remember, the
default must be the safe one — and the dangerous one should have to be asked
for explicitly, by name.

This is the same principle as §3.3, arrived at from the other direction: the
retry only replays requests that *provably* never landed, because "probably
safe to retry" is not good enough when the failure mode is charging someone
twice.

---

## 5. Methodology — two traps worth knowing

**This machine drifts more than the effects being measured.** Absolute p50s
moved ~40% between runs in this session from machine load alone — the
concurrency-1 figure landed at 10.5ms in one run and 7.2ms in another with no
relevant code change. A naive before/after showed off-path capture as *slower*
purely from variance.

Every improvement claimed in §3 comes from an A/B where **both variants ran
simultaneously on separate ports and the driver interleaved them**. Nothing is
claimed from comparing two numbers taken at different times, and nothing later
should be either.

**A driver slower than the server measures the driver.** The first version of
this benchmark used httpx and reported **122ms p50 against a stub that answers
in 0.6ms**. httpx costs ~4ms per request here — 8x the thing under test.
`bench.py` now speaks HTTP/1.1 over raw asyncio streams, putting its floor an
order of magnitude below the signal.

The proxy still uses httpx internally, and *that* cost is genuinely the
proxy's — which is exactly how §1 found the real bottleneck.

---

## 6. Reproducing

```bash
docker compose up -d

# stub upstream
.venv/Scripts/python.exe -m uvicorn bench.stub_upstream:app --port 8788 --log-level warning

# proxy, pointed at the stub
AISPEND_UPSTREAM_URL=http://127.0.0.1:8788 \
DATABASE_URL=postgresql://aispend:aispend@localhost:5555/aispend \
  .venv/Scripts/python.exe -m uvicorn aispend.proxy.app:app --port 8787 --log-level warning

# added latency vs. the stub
.venv/Scripts/python.exe bench/bench.py --concurrency 1 --requests 1200 --rounds 6
```

For an A/B, start a second proxy with the variant on another port and point
both targets at proxies — e.g. capture placement:

```bash
# other terminal: AISPEND_INLINE_CAPTURE=1 ... --port 8786
.venv/Scripts/python.exe bench/bench.py \
  --direct 127.0.0.1:8786 --proxy 127.0.0.1:8787 \
  --direct-name inline --proxy-name off-path --concurrency 1
```

Benchmark runs write thousands of rows to whatever `DATABASE_URL` names. That
is real data to the MCP tools — ~16k stub captures read as ~$159 of spend. Use
a throwaway database, or clear it afterwards.

---

## 7. What these numbers are not

- **Single machine, Windows, loopback.** Absolute values are hardware-bound;
  the ratios are what survive a move to other hardware.
- **No real network.** Against `api.anthropic.com` the upstream RTT is tens to
  hundreds of milliseconds, so the proxy's ~10ms is a small fraction of the
  total.
- **`asyncio.ProactorEventLoop`.** Windows' loop is not what this would run on
  in production. A Linux run would likely look better and should be the number
  quoted if one is ever taken.
- **Only the interleaved comparisons are trustworthy.** See §5.
