ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS inline_message_id VARCHAR(255);
