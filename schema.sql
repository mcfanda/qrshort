CREATE TABLE IF NOT EXISTS short_links (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    nickname TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL,
    clicks BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_clicked_at TIMESTAMPTZ
);

ALTER TABLE short_links
ADD COLUMN IF NOT EXISTS nickname TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS short_links_created_at_idx ON short_links (created_at DESC);
