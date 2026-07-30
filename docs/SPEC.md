# Spec: AI Toolchain Cost Observability MCP Server (aispend)

## Objective

Every AI coding tool (Cursor, Claude Code, raw API scripts) tracks its own cost in isolation. There is no single place showing total AI spend across a developer's whole toolchain, and no way to tell *why* a request was expensive.

`aispend` is a two-part system:
1. A **reverse proxy** that sits in front of the Anthropic API. Any tool that respects `ANTHROPIC_BASE_URL` routes its traffic through it, so every request is captured at the source — no reverse-engineering of per-tool log formats.
2. An **MCP server** that queries the captured data, so spend can be inspected from inside Claude Code (or any MCP client) with plain-language questions: "how much did I spend this week and where," "what were my most expensive requests," "flag requests that probably didn't need Opus."

**User:** Primarily the builder (real Anthropic API usage across Claude Code + scripts) — secondarily framed as something an early-stage startup could point their whole team's API traffic through for cost attribution.

**Success looks like:** Point `ANTHROPIC_BASE_URL` at the proxy, use Claude Code normally for a day, then ask Claude Code itself (via the MCP tools) "how much did I spend today and on what" and get a correct, real answer.

**v1 scope (this spec):** Anthropic only, and specifically only clients that support redirecting API calls via `ANTHROPIC_BASE_URL` — the Claude Code CLI, and any raw script/agent hitting the Anthropic API directly with a user-supplied key. This explicitly **excludes claude.ai (web/desktop chat)**, which talks to Anthropic's backend with no user-configurable base URL — that's a permanent architectural wall for this approach, not a v1-vs-v2 scoping choice.

**v2 candidates:** Cursor, OpenAI Codex, Gemini CLI. Each requires its own feasibility spike before being added — same class of risk already flagged for Cursor (does the tool actually honor a base-URL override?), plus each provider has a different request/response wire format, so `capture.py` needs a provider-specific parser per addition, not just a new forwarding URL.

## Tech Stack

- Python 3.11+
- Proxy: Starlette + httpx (async, supports SSE streaming passthrough)
- MCP server: official Python MCP SDK, stdio transport
- Storage: PostgreSQL (via `docker-compose.yml` for local dev), `psycopg` for access — no ORM
- Packaging: `pyproject.toml`, dependency management via `uv`
- Lint/format: `ruff`
- Tests: `pytest`

## Commands

```
Start Postgres:  docker compose up -d
Install deps:    uv sync
Run proxy:       uv run uvicorn aispend.proxy.app:app --port 8787
Run MCP server:  uv run python -m aispend.mcp_server.server   # launched by Claude Code's MCP config in practice
Test:            uv run pytest
Lint:            uv run ruff check . --fix
Format:          uv run ruff format .
```

## Project Structure

```
aispend/
├── proxy/
│   ├── app.py           → Starlette app: forwards requests to api.anthropic.com, streams SSE through unmodified
│   ├── capture.py       → Extracts model/token usage from the response, writes a spend row
│   └── pricing.py       → Hardcoded $/token table per Anthropic model (documented as needing manual updates)
├── mcp_server/
│   ├── server.py        → MCP entrypoint, stdio transport, registers tools
│   └── tools/
│       ├── spend_summary.py       → get_spend_summary(since, until, group_by)
│       ├── expensive_requests.py  → get_expensive_requests(limit)
│       ├── efficiency_flags.py    → get_efficiency_flags() — heuristic "cheaper model likely fine" report
│       └── budget_check.py        → check_budget(threshold) — on-demand, not a background alert
├── storage/
│   ├── db.py             → connection pool, query functions
│   └── schema.sql        → requests table DDL
tests/
│   ├── test_capture.py       → response parsing, streaming and non-streaming
│   ├── test_pricing.py       → cost math, cache rates, dated price changes
│   ├── test_storage.py       → query functions against a test Postgres schema
│   ├── test_efficiency_flags.py
│   ├── test_proxy.py         → header handling and passthrough, upstream mocked
│   └── test_integration.py   → proxy → capture → pricing → Postgres, end to end
docker-compose.yml         → Postgres service for local dev
.env.example                → ANTHROPIC_API_KEY, DATABASE_URL, PROXY_PORT
pyproject.toml
docs/
│   ├── SPEC.md            → this file
│   └── INTERVIEW.md       → architecture walkthrough and decision rationale
README.md
```

## Code Style

```python
# storage/db.py
def insert_request(
    conn,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    source_tool: str | None,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> None:
    """Metadata only — never pass prompt/response content into this function.

    `input_tokens` is the uncached remainder the API reports; the cache
    counters are stored alongside it rather than summed into it.
    """
```

- Type hints everywhere (`from __future__ import annotations` not needed at 3.11+)
- Keyword-only args for anything with 3+ parameters (as above)
- No classes where a function suffices — this codebase should stay small and function-first
- Docstrings only where behavior isn't obvious from the signature (e.g., the privacy note above)

## Testing Strategy

- `pytest`, tests live in `tests/`, mirroring the source tree
- **Unit level:** `pricing.py` cost calculations, `capture.py` parsing of Anthropic response shapes (streaming and non-streaming), `efficiency_flags.py` heuristic logic, storage query functions against a test Postgres schema
- **Proxy level:** upstream is an `httpx.MockTransport`, so header handling, passthrough fidelity, and the capture/skip decision are testable without the network or a live key
- **Automated integration:** `test_integration.py` drives a request through the real proxy app into real Postgres (upstream still mocked) and asserts the stored row and its cost. This is the seam where the two halves of the system meet, and where a failure is silent rather than loud — the proxy keeps serving traffic while capture writes nothing — so it is worth covering automatically rather than by checklist
- **Manual (not automated):** pointing Claude Code at the running proxy and confirming a real session is captured. Stays a README checklist since it needs a live Anthropic key and a running Claude Code session
- No coverage percentage target — this is a portfolio project, prioritize tests that catch real breakage (streaming parsing, cost math) over blanket coverage

## Boundaries

- **Always:** run `pytest` before considering a task done; capture metadata only (model, token counts, cost, latency, timestamp, source tool) — never log prompt or response bodies, under any refactor; keep the proxy a pure passthrough (never mutate the request/response Anthropic sees)
- **Ask first:** adding new dependencies beyond what's listed in Tech Stack; changing the `requests` table schema once data exists; adding Cursor or any other provider/tool integration (that's v2); changing what gets captured/logged (privacy-sensitive by design)
- **Never:** commit `.env` or any API key; store prompt/response content; add active request-blocking/enforcement (explicitly cut from v1 during ideation — advisory only); force-push; skip a failing test to unblock a commit

## Success Criteria

- [ ] Setting `ANTHROPIC_BASE_URL=http://localhost:8787` and running Claude Code CLI works with no functional difference from talking to Anthropic directly (streaming responses render normally, no added latency a user would notice)
- [ ] Every request through the proxy produces exactly one row in the `requests` table with correct model, input/output tokens, and computed cost
- [ ] `get_spend_summary` returns a total that matches a manual `SUM(cost_usd)` over the same window
- [ ] `get_expensive_requests` returns the top N requests by cost, correctly ordered
- [ ] `get_efficiency_flags` flags at least the obvious case (a short, simple prompt sent to Opus) using a documented, explainable rule — and is presented as advisory, not a validated savings guarantee
- [ ] `check_budget` correctly reports over/under a given threshold for a given window
- [ ] All four MCP tools are invocable from within a live Claude Code session and return legible results
- [ ] No prompt or response content appears anywhere in the database, at rest, or in logs

## Resolved Questions

- Pricing table staleness: README documents `pricing.py` as a hardcoded snapshot needing manual updates. An unpriceable model fails closed (no row written) and logs an error naming the model ID, rather than guessing a rate and recording a wrong number.
- Local setup: Postgres only, no SQLite fallback.
- `source_tool`: left `null` in v1 (single client in scope; no meaningful "spend by tool" split yet).
- Cached tokens: stored as three additional columns (`cache_read_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens`) rather than summed into `input_tokens`. They bill at different rates (0.1x / 1.25x / 2x base input), so they cannot share a column with uncached input without losing the ability to price them — and keeping them separate leaves the door open to reporting a cache-hit rate.
- Schema evolution: `schema.sql` is replayed on every startup, so it must stay idempotent. New columns are added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` rather than edited into the `CREATE TABLE`, which would only affect fresh databases.
- Mid-stream disconnects: not recorded. A truncated SSE body has no final `message_delta`, so its token counts would understate the request; a missing row is preferable to a wrong one in a tool whose only job is to be numerically correct.
