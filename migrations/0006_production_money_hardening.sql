CREATE TABLE IF NOT EXISTS ledger_entries (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(telegram_id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount <> 0),
    currency VARCHAR(16) NOT NULL DEFAULT 'XTR',
    entry_type VARCHAR(32) NOT NULL,
    source_table VARCHAR(64),
    source_id VARCHAR(255),
    idempotency_key VARCHAR(255) UNIQUE,
    entry_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ledger_entries_user_created
    ON ledger_entries(user_id, created_at);

CREATE INDEX IF NOT EXISTS ix_ledger_entries_source
    ON ledger_entries(source_table, source_id);

ALTER TABLE payouts
    ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'held';

ALTER TABLE payouts
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE payouts
    ADD COLUMN IF NOT EXISTS released_at TIMESTAMP WITH TIME ZONE;

CREATE INDEX IF NOT EXISTS ix_payouts_status_available
    ON payouts(status, available_at);
