ALTER TABLE withdrawals
    ADD COLUMN IF NOT EXISTS ton_wallet_address VARCHAR(255);

ALTER TABLE withdrawals
    ADD COLUMN IF NOT EXISTS ton_tx_hash VARCHAR(255);

ALTER TABLE withdrawals
    ADD COLUMN IF NOT EXISTS admin_id BIGINT;

ALTER TABLE withdrawals
    ADD COLUMN IF NOT EXISTS admin_note TEXT;

ALTER TABLE withdrawals
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
