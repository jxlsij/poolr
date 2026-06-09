CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255) NOT NULL,
    balance_credits INTEGER NOT NULL DEFAULT 0 CHECK (balance_credits >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_banned BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS deposits (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    stars_amount INTEGER NOT NULL CHECK (stars_amount > 0),
    charge_id VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'refunded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_deposits_charge_id UNIQUE (charge_id)
);

CREATE INDEX IF NOT EXISTS ix_deposits_user_status_created
    ON deposits(user_id, status, created_at);

CREATE TABLE IF NOT EXISTS markets (
    id SERIAL PRIMARY KEY,
    creator_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    chat_id BIGINT NOT NULL,
    message_id BIGINT,
    inline_message_id VARCHAR(255),
    question TEXT NOT NULL,
    options JSONB NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    min_bet INTEGER NOT NULL CHECK (min_bet > 0),
    status VARCHAR(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'closed', 'resolved', 'cancelled', 'disputed')),
    winning_option INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_markets_chat_status_deadline
    ON markets(chat_id, status, deadline);

CREATE INDEX IF NOT EXISTS ix_markets_status_deadline
    ON markets(status, deadline);

CREATE TABLE IF NOT EXISTS bets (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    option_index INTEGER NOT NULL CHECK (option_index >= 0),
    credits_amount INTEGER NOT NULL CHECK (credits_amount > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_bets_user_market UNIQUE (user_id, market_id)
);

CREATE INDEX IF NOT EXISTS ix_bets_market_option
    ON bets(market_id, option_index);

CREATE TABLE IF NOT EXISTS payouts (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    credits_won INTEGER NOT NULL CHECK (credits_won >= 0),
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_payouts_market_user
    ON payouts(market_id, user_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    credits_amount INTEGER NOT NULL CHECK (credits_amount > 0),
    charge_ids_used JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_withdrawals_user_status_created
    ON withdrawals(user_id, status, created_at);

CREATE TABLE IF NOT EXISTS disputes (
    id SERIAL PRIMARY KEY,
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    raised_by BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_disputes_market_status_created
    ON disputes(market_id, status, created_at);
