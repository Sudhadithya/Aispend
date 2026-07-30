-- Metadata only. No prompt or response content is ever stored here.
--
-- init_schema() replays this file on every startup, so it must stay
-- idempotent: new columns are added with ADD COLUMN IF NOT EXISTS rather than
-- edited into the CREATE TABLE above, which would only apply to fresh databases.

CREATE TABLE IF NOT EXISTS requests (
    id BIGSERIAL PRIMARY KEY,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd NUMERIC(12, 6) NOT NULL,
    latency_ms INTEGER NOT NULL,
    source_tool TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cached prompt tokens bill at their own rates, so they are counted
-- separately rather than folded into input_tokens (which the API reports as
-- the uncached remainder only). Split by TTL because the 5-minute and 1-hour
-- cache-write rates differ.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests (created_at);
