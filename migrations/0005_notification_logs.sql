CREATE TABLE IF NOT EXISTS notification_logs (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(64) NOT NULL,
    market_id INTEGER NOT NULL DEFAULT 0,
    user_id BIGINT NOT NULL DEFAULT 0,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_notification_logs_kind_market_user UNIQUE (kind, market_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_notification_logs_kind_sent
    ON notification_logs (kind, sent_at);
