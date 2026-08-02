# Aispend — AI Toolchain Cost Observability MCP Server

Every AI coding tool tracks its own cost in isolation. `aispend` gives you one
place that sees all of it: a reverse proxy sits in front of the Anthropic API
and captures every request at the source, and an MCP server lets you ask
Claude Code plain-language questions about your own spend — "how much did I
spend this week and where," "what were my most expensive requests," "flag
requests that probably didn't need Opus."

See [docs/SPEC.md](docs/SPEC.md) for the design and the reasoning behind it.

**v1 scope:** Anthropic only, and only clients that support redirecting API
calls via `ANTHROPIC_BASE_URL` (the Claude Code CLI, or any script hitting the
Anthropic API directly with a user-supplied key). This does not, and cannot,
cover claude.ai (web/desktop) — it has no user-configurable base URL.

## Architecture

Three layers, each with one job: the proxy moves bytes, capture turns a
response into a priced row, and the MCP server answers questions about those
rows. Nothing crosses those boundaries except metadata.

```mermaid
graph TD
    subgraph Clients [Client Layer]
        CC[Claude Code CLI]
        SC[Scripts hitting the Anthropic API]
    end

    subgraph ProxyLayer ["Proxy Layer (Starlette + httpx)"]
        PX(Reverse proxy)
        CAP(Capture)
        PRICE{{"pricing.py rate table"}}
    end

    subgraph StorageLayer ["Storage Layer"]
        PG[("Postgres: requests")]
    end

    subgraph McpLayer ["MCP Layer (stdio)"]
        MCP(aispend MCP server)
        TOOLS["get_spend_summary / get_expensive_requests<br/>get_efficiency_flags / check_budget"]
    end

    CC -->|ANTHROPIC_BASE_URL| PX
    SC -->|ANTHROPIC_BASE_URL| PX
    PX -->|"forwarded verbatim"| API[api.anthropic.com]
    API -.->|"streamed back untouched"| PX
    PX -->|"response body, off the hot path"| CAP
    CAP -->|"model + usage only"| PRICE
    PRICE -->|"one row per request"| PG
    CAP -.->|"unpriceable model or DB down"| SKIP[/"Row dropped, client response unaffected"/]
    PG --> MCP
    MCP --> TOOLS
    YOU["You, asking Claude Code"] <--> MCP
```

The proxy is a pure passthrough — it never mutates the request or response
Anthropic sees. Capture happens off to the side and is best-effort: if
Postgres is down or a model is unpriceable, the client's response is still
returned untouched and only the metrics row is lost.

## Features

- **Zero-friction capture.** One environment variable (`ANTHROPIC_BASE_URL`)
  and every request from that client is metered. No SDK wrapper, no callback
  to register, no per-tool integration.
- **Metadata only, by construction.** Capture reads `model` and `usage` and
  nothing else — prompt and response text never reach the pricing code, let
  alone the database. The schema has no column that could hold it.
- **Cache-aware costing.** All four token counters are priced at their own
  rates and stored separately, so cached spend is neither over-billed nor
  invisible. See [How cost is calculated](#how-cost-is-calculated).
- **Temporal pricing.** `calculate_cost` takes an `as_of` timestamp, so a
  request made under introductory pricing stays priced that way after the
  rates change.
- **Capture never breaks a request.** Capture errors are logged, never raised
  into the response. When something is wrong — no database, an unknown model,
  a stream that ended early — the row is dropped rather than written with
  numbers that would be wrong.
- **Idempotent schema.** `init_schema()` is safe to re-run against an existing
  database: columns are added with `ADD COLUMN IF NOT EXISTS`, so applying it
  to a database created by an earlier version migrates it in place.

## Getting Started

Requires Docker, Python 3.11+, and [`uv`](https://docs.astral.sh/uv/).

1. Copy the environment template, then export `DATABASE_URL`. Nothing in the
   app reads `.env` — the variable has to be in the environment of whatever
   you run, or every capture will fail:
   ```bash
   cp .env.example .env    # ANTHROPIC_API_KEY is only needed to test with curl directly
   export DATABASE_URL=postgresql://aispend:aispend@localhost:5555/aispend
   ```
   PowerShell: `$env:DATABASE_URL = "postgresql://aispend:aispend@localhost:5555/aispend"`
2. Start Postgres and install dependencies:
   ```bash
   docker compose up -d
   uv sync
   ```
   If port `5555` (the host port Postgres is published on) is already taken,
   change the `ports` mapping in `docker-compose.yml` and the port in
   `DATABASE_URL` to match.
3. Create the `requests` table. Nothing does this automatically, and the
   command is idempotent, so re-run it after pulling schema changes:
   ```bash
   uv run python -c "
   from aispend.storage import db
   with db.get_connection() as conn:
       db.init_schema(conn)
   db.close_pool()
   "
   ```
   The `close_pool()` matters: left to the garbage collector, psycopg_pool's
   finalizer tries to join its worker threads after the interpreter has begun
   shutting down, and prints a `PythonFinalizationError` traceback on exit.
   The schema is still created — but the noise looks like a failure.
4. Run the proxy:
   ```bash
   uv run uvicorn aispend.proxy.app:app --port 8787
   ```
5. Point any Anthropic-compatible client at it:
   ```bash
   export ANTHROPIC_BASE_URL=http://localhost:8787
   ```
   PowerShell: `$env:ANTHROPIC_BASE_URL = "http://localhost:8787"`
6. Smoke-test the whole pipeline with one cheap request, using the payload in
   [`tests/sample_request.json`](tests/sample_request.json):
   ```bash
   curl -X POST http://localhost:8787/v1/messages \
     -H "x-api-key: $ANTHROPIC_API_KEY" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     --data "@tests/sample_request.json"
   ```
   PowerShell (`curl.exe` doesn't parse single-quoted JSON the way a Unix
   shell does, so use `--data @file` rather than an inline `-d '...'`):
   ```powershell
   curl.exe -X POST http://localhost:8787/v1/messages `
     -H "x-api-key: $env:ANTHROPIC_API_KEY" `
     -H "anthropic-version: 2023-06-01" `
     -H "content-type: application/json" `
     --data "@tests/sample_request.json"
   ```
   You should get a short reply from Claude, a `POST /v1/messages` line in the
   proxy's log, and one new row in `requests`:
   ```bash
   docker exec <container-id> psql -U aispend -d aispend -c \
     "select model, input_tokens, output_tokens, cost_usd, latency_ms from requests order by id desc limit 5;"
   ```

Because capture is best-effort, a missing `DATABASE_URL` or missing table
doesn't produce an error at the client — the proxy works and records nothing.
If `get_spend_summary` comes back empty after a real session, check the proxy
log for `Failed to capture spend`.

The MCP server is normally launched by Claude Code's MCP config rather than
run by hand. It needs `DATABASE_URL` in its own environment and needs to start
in the project directory, so pass both explicitly:

```json
{
  "mcpServers": {
    "aispend": {
      "command": "uv",
      "args": ["run", "python", "-m", "aispend.mcp_server.server"],
      "cwd": "/path/to/Cost Observability MCP Server",
      "env": { "DATABASE_URL": "postgresql://aispend:aispend@localhost:5555/aispend" }
    }
  }
}
```

## Tools

| Tool | What it answers |
|---|---|
| `get_spend_summary` | Total spend in a window, optionally grouped by model, source tool, or day |
| `get_expensive_requests` | The N most expensive requests, with the token and cache counts that explain why |
| `get_efficiency_flags` | Advisory report of requests that probably didn't need an Opus-tier model |
| `check_budget` | Whether spend in a window is over or under a given threshold |

`get_efficiency_flags` is a heuristic, not a savings guarantee: it flags
Opus-tier requests whose *total* tokens — cached included — fall under 500. A
short question asked against a large cached system prompt is not a small
request and isn't flagged as one.

## How cost is calculated

Cost is computed at capture time from the token counts the API reports, using
the hardcoded rates in [aispend/proxy/pricing.py](aispend/proxy/pricing.py).
Two details drive most of the logic:

**Cached tokens bill separately.** `usage.input_tokens` in an Anthropic
response is only the *uncached remainder*. Cached prompt content is reported
in its own counters and priced off the base input rate:

| Counter | Rate |
|---|---|
| `input_tokens` (uncached) | 1x base input |
| `cache_read_input_tokens` | 0.1x base input |
| 5-minute cache writes | 1.25x base input |
| 1-hour cache writes | 2x base input |

Claude Code caches aggressively, so a session's cache reads routinely dwarf
its uncached input. Costing only `input_tokens` — as the first version of this
project did — understates real spend badly. All four counters are stored per
row so a cache-hit rate can be derived later.

**Pricing is temporal.** Claude Sonnet 5 is under introductory pricing
($2/$10 per MTok) until 2026-08-31, reverting to $3/$15 on 2026-09-01.
`calculate_cost` takes an `as_of` timestamp so a request is priced with the
rate in effect when it was made.

## Testing

Tests are `pytest`. The proxy and pricing suites mock their dependencies and
run anywhere; the storage, MCP, and integration suites need the Postgres
container up.

```bash
uv run pytest              # test
uv run ruff check . --fix  # lint
uv run ruff format .       # format
```

## Manual verification checklist

Not automated in CI — requires a live Anthropic key and a running Claude Code
session:

- [ ] `ANTHROPIC_BASE_URL=http://localhost:8787` + Claude Code CLI works with
      no functional difference from talking to Anthropic directly (streaming
      renders normally, no perceptible added latency)
- [ ] A real session produces one row per request in the `requests` table,
      with correct model, token counts, and cost
- [ ] All four MCP tools are invocable from within Claude Code and return
      legible results
- [ ] No prompt or response content appears in the database, at rest, or in
      logs, at any point

## Known limitations

- **Pricing goes stale.** `aispend/proxy/pricing.py` is a hardcoded snapshot
  of Anthropic's published rates. There is no pricing API — when Anthropic
  changes prices or ships a model, `PRICING_PER_MTOK` needs a manual update.
  An unpriceable model drops the row rather than guessing at a price: the
  request is served normally, and an error naming the missing model ID is
  logged. Watch for
  `Skipped spend capture` in the proxy log after a new model launches.
- **Only base token pricing is modelled.** Batch API (50% off), the fast-mode
  premium, `inference_geo` (1.1x), and per-use server-tool charges (web search
  is billed per search) are not accounted for. Spend for workloads using those
  will be off.
- **Streaming responses are buffered in memory** to be parsed after the stream
  completes. Bounded by response size, which is small for SSE, but it is a
  buffer that grows with the response rather than a streaming parser.
- **A client that disconnects mid-stream is not recorded.** The token counts
  in a truncated SSE body would understate the request, so the row is skipped
  rather than written wrong. Anthropic still bills for it.
- **Postgres only**, no SQLite fallback — `docker compose up -d` is the setup
  bar for local development.
- **`source_tool` is always `null` in v1.** Only the Claude Code CLI is in
  scope, so there's nothing to distinguish yet; this is where a v2 provider
  integration would tag requests by client.
