# Aispend — AI Toolchain Cost Observability MCP Server

Every AI coding tool tracks its own cost in isolation. `aispend` gives you one
place that sees all of it: a reverse proxy sits in front of the Anthropic API
and captures every request at the source, and an MCP server lets you ask
Claude Code plain-language questions about your own spend — "how much did I
spend this week and where," "what were my most expensive requests," "flag
requests that probably didn't need Opus."

See [docs/SPEC.md](docs/SPEC.md) for the full design.

**v1 scope:** Anthropic only, and only clients that support redirecting API
calls via `ANTHROPIC_BASE_URL` (the Claude Code CLI, or any script hitting the
Anthropic API directly with a user-supplied key). This does not, and cannot,
cover claude.ai (web/desktop) — it has no user-configurable base URL.

## Setup

Requires Docker, Python 3.11+, and [`uv`](https://docs.astral.sh/uv/).

```bash
cp .env.example .env        # fill in ANTHROPIC_API_KEY if you want to test with curl directly
docker compose up -d        # starts Postgres
uv sync                     # installs dependencies
```

If port `5555` (the host port Postgres is published on) is already taken on
your machine, change the `ports` mapping in `docker-compose.yml` and the port
in `DATABASE_URL` to match.

## Running

```bash
uv run uvicorn aispend.proxy.app:app --port 8787
```

Point any Anthropic-compatible client at the proxy:

```bash
$env:ANTHROPIC_BASE_URL="http://localhost:8787"
```

The MCP server is normally launched by Claude Code's MCP config, not run
directly. Add it as a stdio MCP server pointing at:

```
uv run python -m aispend.mcp_server.server
```

## Tools

| Tool | What it answers |
|---|---|
| `get_spend_summary` | Total spend in a window, optionally grouped by model, source tool, or day |
| `get_expensive_requests` | The N most expensive requests |
| `get_efficiency_flags` | Advisory report of requests that probably didn't need an Opus-tier model |
| `check_budget` | Whether spend in a window is over or under a given threshold |

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
  of Anthropic's published $/token rates. There is no pricing API — when
  Anthropic changes prices, or ships a new model, `PRICING_PER_MTOK` needs a
  manual update, or cost calculations for that model will be wrong (unknown
  models raise loudly rather than silently mis-price).
- **Postgres only**, no SQLite fallback — `docker compose up -d` is the setup
  bar for local development.
- **`source_tool` is always `null` in v1.** Only the Claude Code CLI is in
  scope, so there's nothing to distinguish yet; this is where a v2 provider
  integration would tag requests by client.
