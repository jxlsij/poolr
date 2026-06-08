# Poolr Project Audit

Date: 2026-06-08

Recommendation: REQUEST CHANGES

Architectural status: BLOCK

Critical vulnerabilities: none found.

Current remediation status: 1 of 6 high-severity blockers addressed in the current branch.

Validation evidence:

- Git worktree was clean before audit.
- `.venv/bin/python -m compileall api bot tests main.py` passed.
- `.venv/bin/python -m pytest -q` passed with `122 passed, 676 warnings in 5.47s` after allowing localhost socket bind for `aiohttp TestServer`.
- Initial sandboxed pytest run failed only because tests could not bind `127.0.0.1:0`.
- Official Telegram documentation was checked for Mini App `auth_date` freshness and Stars payment support requirements.
- Freshness remediation landed in `bot/security.py` with regression coverage in `tests/test_security.py`, `tests/test_api_webapp.py`, and `tests/test_users.py`.

## High Severity

### 1. Mini App initData Freshness Check

Evidence:

- `bot/security.py:56`
- `bot/security.py:117`
- `api/webapp.py:621`
- `api/webapp.py:626`
- `tests/test_security.py:21`

Status:

Fixed in the current branch.

Risk before fix:

`validate_webapp_init_data` verifies the HMAC and parses/logs `auth_date`, but does not reject stale, missing, non-integer, or future timestamps. A captured valid Mini App `initData` can be replayed indefinitely against state-changing API endpoints such as `/api/bet`, `/api/deposit`, and `/api/withdraw`. If the captured user is an admin, `/api/admin/overview` is also exposed.

Telegram documentation explicitly says `auth_date` can be checked to prevent use of outdated data:
https://core.telegram.org/bots/webapps

Fix implemented:

Require `auth_date`, parse it as Unix seconds, enforce a configurable max age, reject large future skew, and add regression tests for stale/missing/non-integer/future values.

### 2. Banned Users Can Still Bet

Evidence:

- `bot/betting.py:442`
- `bot/fraud.py:155`
- `bot/models.py:71`

Risk:

`User.is_banned` exists, and the project documentation claims banned-user bet gating, but stake validation checks only market status, deadline, creator, option index, and minimum stake. Banned users can still receive invoices and place paid bets.

Fix:

Add a banned-user check to stake invoice validation, pre-checkout validation, and final `place_bet` validation. Cover both Telegram callback/FSM and Mini App paths.

### 3. Resolved Payouts Become Withdrawable Before Dispute Window Ends

Evidence:

- `bot/resolution.py:334`
- `bot/withdrawals.py:341`
- `bot/fraud.py:286`

Risk:

Creator resolution immediately credits winners' withdrawable balances. Users can reserve those funds for manual payout before the dispute window closes. If arbitration later changes the outcome, existing payouts cause redistribution to be skipped, so balances cannot be corrected cleanly.

Fix:

Keep resolution payouts pending until the dispute window closes, or introduce ledger entries that can be reversed/clawed back during arbitration. Block withdrawals for disputed or still-disputable payout funds.

### 4. Inline Queries Create Persistent Active Markets Before Selection

Evidence:

- `bot/handlers/markets.py:209`
- `bot/handlers/markets.py:243`

Risk:

Every non-empty inline query creates a real active market before the user chooses the inline result. Typing in inline mode can spam persistent markets, pollute `/api/markets`, and trigger notification/expiry work for markets that were never posted.

Fix:

Persist only on `chosen_inline_result`, or create a draft status excluded from feeds/schedulers and expire unchosen drafts.

### 5. DB Transactions Are Mixed With Telegram Side Effects Before Commit

Evidence:

- `bot/middleware/database.py:30`
- `bot/database.py:37`
- `bot/handlers/markets.py:340`
- `bot/handlers/markets.py:350`
- `bot/handlers/markets.py:357`
- `bot/betting.py:286`
- `bot/betting.py:301`
- `bot/betting.py:310`
- `bot/betting.py:324`

Risk:

Handlers receive one DB session and commit only after the handler returns. Several flows flush DB state, call Telegram APIs, and only commit later. Telegram can show a market, bet, payout, or withdrawal request that later rolls back. This is especially risky around payments because users trust Telegram-side confirmations.

Fix:

Separate committed state from provider side effects. Preferred approach: persist outbox/event rows inside the transaction, commit, then publish cards/notifications/invoices from a worker or delivery step. MVP hardening: explicitly commit before non-critical Telegram updates in payment, market creation, resolution, dispute, and withdrawal paths.

### 6. Internal Money Model Is Not a Production Ledger

Evidence:

- `bot/models.py:71`
- `bot/crud.py:128`
- `bot/crud.py:144`
- `bot/crud.py:150`
- `bot/models.py:93`
- `bot/models.py:164`
- `bot/models.py:194`
- `bot/models.py:220`
- `bot/resolution.py:253`
- `bot/resolution.py:355`
- `bot/withdrawals.py:350`
- `bot/withdrawals.py:355`

Risk:

The only user balance is mutable `users.balance_credits`; balance reasons are logged but not persisted as accounting records. Deposits, bets, payouts, and withdrawals exist, but platform fees are not persisted as account movements. Manual withdrawals store `charge_ids_used=[]`.

This makes reconciliation, refunds, fee audit, dispute repair, and reserve accounting hard to prove.

Fix:

Add append-only `ledger_entries` with idempotency key, account/user, amount, currency, entry type, source table/id, metadata, and created_at. Treat `balance_credits` as a cached projection or reconcile it from ledger entries.

## Medium Severity

### 1. Dispute Window Is 2 Hours Instead Of 24 Hours

Evidence:

- `bot/fraud.py:38`
- `bot/fraud.py:567`
- `agents.md:65`

Risk:

The project documentation says 24-hour post-resolution disputes, but code uses `DISPUTE_WINDOW = timedelta(hours=2)`. Legitimate disputes are cut off 22 hours early.

Fix:

Change dispute window to 24 hours and update tests for inside/outside the window.

### 2. Stars Amount Inputs Have No Upper Bound

Evidence:

- `api/webapp.py:771`
- `bot/betting.py:577`
- `bot/withdrawals.py:642`
- `migrations/0001_module3_database_layer.sql:14`

Risk:

Stake, deposit, withdrawal, and minimum-bet inputs accept positive integers without a product-level maximum, while DB columns are `INTEGER`. Very large values can fail provider calls, overflow persistence, or create abuse paths.

Fix:

Define shared product constants for max stake/deposit/withdrawal/min-bet, validate at all entry points and pre-checkout, and add DB check constraints if needed.

### 3. Withdrawals Can Be Created With No Admins Configured

Evidence:

- `bot/withdrawals.py:565`
- `api/webapp.py:460`

Risk:

If `ADMIN_IDS` is empty, withdrawal requests still reserve user balances and return success, but no admin receives or processes the payout. Funds can become operationally stuck.

Fix:

Reject withdrawal creation when no payout admins are configured, or fail startup in production when withdrawals are enabled without admins.

### 4. TON Wallet Validation Is Too Loose

Evidence:

- `bot/withdrawals.py:41`
- `bot/withdrawals.py:654`

Risk:

TON wallet validation is only a character/length regex, so invalid addresses can enter the admin payout queue and be manually paid incorrectly.

Fix:

Use a TON address parser/validator with checksum and workchain handling, or at minimum stricter bounceable/base64url validation plus tests.

### 5. Migration Strategy Is Fragile For Production Databases

Evidence:

- `bot/database.py:85`
- `bot/database.py:92`
- `bot/database.py:108`
- `migrations/0001_module3_database_layer.sql:1`
- `migrations/0003_manual_ton_withdrawals.sql:1`

Risk:

Startup runs all sorted `*.sql` files every time, with no migration history table. SQL is split by raw semicolon. This is only safe for trivial idempotent DDL and will break for future non-idempotent migrations, backfills, function definitions, or constraint changes.

Fix:

Adopt Alembic or add a minimal `schema_migrations` table with transactional version tracking. Avoid raw semicolon splitting for anything beyond simple DDL.

### 6. Mini App Market Feed Has N+1 Query Shape

Evidence:

- `api/webapp.py:191`
- `api/webapp.py:198`
- `api/webapp.py:501`
- `api/webapp.py:503`
- `api/webapp.py:504`
- `api/webapp.py:515`

Risk:

`/api/markets` loads markets, then each summary performs separate queries for pool, current user's bet, and all bets. Feed performance will degrade as market count grows.

Fix:

Batch aggregate pools, bet counts, and current-user bets with grouped queries or precomputed summaries.

### 7. Product And Compliance Gates Block Production Money Launch

Evidence:

- `agents.md:158`
- `agents.md:165`
- `agents.md:175`
- `agents.md:365`
- `agents.md:457`
- `bot/handlers/start.py:23`
- `README.md:11`

Risk:

The repo explicitly says economics, Terms, payment support copy, legal/compliance review, and real Telegram payment-cycle validation are still open. Telegram Stars docs say bots/mini apps must provide `/paysupport` and timely handling for payment issues:
https://core.telegram.org/bots/payments-stars

Fix:

Before real-money launch, add `/paysupport`, Terms, refund/support runbooks, a documented economic model, reserve/payout policy, real 1-Star cycle validation, and legal/compliance signoff.

## Low Severity

### 1. PLATFORM_FEE_PCT Allows Invalid Runtime Values

Evidence:

- `bot/config.py:124`

Risk:

Config accepts `PLATFORM_FEE_PCT >= 1`, but downstream payout/estimate code rejects it at runtime.

Fix:

Validate `0 <= PLATFORM_FEE_PCT < 1` during config loading.

### 2. Frontend Payout Estimate Hardcodes Fee

Evidence:

- `frontend/app.js:948`
- `frontend/app.js:956`
- `bot/betting.py:469`
- `bot/betting.py:488`

Risk:

Frontend hardcodes `0.92`, while backend uses configurable `PLATFORM_FEE_PCT`. User-visible payout estimates can drift from backend economics.

Fix:

Return fee metadata or server-computed estimates from the backend.

### 3. Tests Codify Risky Behavior

Evidence:

- `tests/test_security.py:21`
- `tests/test_fraud.py:135`

Risk:

Tests currently accept stale Mini App auth and a short dispute window, so passing tests can hide regressions against the intended security/spec contract.

Fix:

Replace with tests for auth expiry and 24-hour dispute eligibility.

## False-Positive Notes

- `frontend/app.js` uses `innerHTML`, but inspected dynamic market, wallet, activity, and toast fields are escaped with `escapeHtml` or `escapeAttr`; no concrete XSS path was found.
- Webhook secret enforcement is delegated to aiogram `SimpleRequestHandler(secret_token=...)`; the local `verify_webhook_request` helper is not the mounted enforcement path.
- Stars payment handling does preserve `telegram_payment_charge_id` through deposits and idempotent charge checks.

## Positive Observations

- Stars invoices use `currency="XTR"`.
- Direct stake invoices are implemented.
- Manual TON-equivalent payout requests reserve Stars and require admin-paid transaction hashes.
- Payment charge idempotency exists through unique `charge_id` and confirmed-deposit rechecks.
- The repository has useful module boundaries for an MVP: config, infrastructure, security, CRUD, payments, betting, resolution, withdrawals, fraud, notifications, monitoring, API, frontend, migrations, and tests.

## Recommended Fix Order

1. Add Mini App `auth_date` TTL validation and tests.
2. Add banned-user bet gating across invoice, pre-checkout, and final placement.
3. Fix dispute window to 24 hours.
4. Stop inline query from creating active persistent markets before selection.
5. Block withdrawals when no admins are configured.
6. Add upper bounds for all Stars amounts.
7. Decide and implement dispute-safe payout lifecycle.
8. Introduce an append-only ledger.
9. Move Telegram side effects after commit or through an outbox.
10. Add `/paysupport`, Terms, support/refund runbook, real Stars payment-cycle validation, and production compliance gates.
