# Poolr Production MVP Audit

Date: 2026-06-09

Remediation update: 2026-06-10

Status after remediation: **CLOSED for repository-level audit items**, with
remaining launch dependency on external legal/compliance review and real
Telegram payment-cycle validation.

Implemented remediation:

- Added dispute-safe payout holds: resolution creates held payouts, records
  ledger hold entries, and releases winnings only after the 24-hour dispute
  window through the notification worker.
- Added append-only `ledger_entries` model/migration for balance adjustments,
  payout holds, and platform fee entries.
- Added `/paysupport`, beta Terms, Privacy, support runbook, and economics
  scaffold under `docs/`.
- Added product Stars limits for stake/deposit/withdrawal/market minimums and
  validates them in bot, API, and invoice paths.
- Added banned-user bet gating in invoice validation, pre-checkout, API bet
  initiation, and final bet placement.
- Set dispute window to 24 hours and made arbitration redistribute held payouts
  before release.
- Added stricter TON wallet validation for 48-character friendly addresses or
  raw `workchain:hex` addresses.
- Added `/admin_withdrawals` durable payout queue and `/admin_anomalies`.
- Added schema migration tracking and migration `0006_production_money_hardening.sql`.
- Batched Mini App market list summaries to avoid per-market pool/my-bet/count
  queries.
- Returned configured platform fee metadata to the Mini App and removed the
  hardcoded frontend `0.92` estimate.

Verification after remediation:

```bash
.venv/bin/python -m compileall api bot tests main.py
.venv/bin/python -m pytest -q
```

Result: compileall passed; pytest passed with `139 passed, 721 warnings in
4.44s`. Warnings are existing `pytest-asyncio` deprecations on Python 3.14.

Mode: OMX-style repository audit using `oh-my-codex:analyze` and `oh-my-codex:code-review` guidance. This is not a legal opinion.

Recommendation: **REQUEST CHANGES before real-money/public production launch**.

Architectural status: **BLOCK for public paid MVP**, **WATCH for closed beta with tight limits and manual operations**.

## Executive Summary

Poolr is beyond a toy prototype: it has real Telegram Stars invoices, idempotent charge IDs, Mini App `initData` backend validation, async PostgreSQL wiring, market creation/betting/resolution/dispute/withdrawal flows, health checks, and a vanilla Mini App frontend.

The blockers are not mostly syntax or missing modules. They are production-money gaps:

- payout/dispute lifecycle can make winnings withdrawable before disputes are safe;
- accounting is mutable-balance based, not an auditable ledger;
- manual payout operations are operationally fragile;
- product/legal/support/economics gates are still explicitly unresolved;
- several limits and privacy boundaries are not productized.

Official Telegram docs checked during this audit:

- Mini Apps docs require backend validation of `Telegram.WebApp.initData` and warn not to trust `initDataUnsafe`: https://core.telegram.org/bots/webapps
- Stars payment docs confirm digital goods use Stars and refunds are via `refundStarPayment`: https://core.telegram.org/bots/payments-stars
- Bot API docs state Stars invoices use `provider_token=""` and `currency="XTR"`: https://core.telegram.org/bots/api

## Current Strengths

- `bot/security.py:46` validates Mini App `initData` HMAC and freshness via `auth_date`.
- `bot/payments.py:66` and `bot/betting.py:222` send Stars invoices with `provider_token=""` and `currency="XTR"`.
- `bot/models.py:96` has unique `deposits.charge_id`, and `bot/betting.py:589` / `bot/payments.py:369` recheck charge IDs for idempotency.
- `api/webapp.py:622` requires Telegram Mini App auth for `/api/*`.
- `bot/handlers/markets.py:263` now creates inline markets only on `chosen_inline_result`, not every inline query.
- `bot/monitoring.py:72` provides a DB-aware `/health`.
- Test suite is broad for the current foundation.

## Must Fix Before Public Paid MVP

### P0. Dispute-safe payout lifecycle is not production-safe

Evidence:

- `bot/resolution.py:253` distributes payouts during creator resolution.
- `bot/resolution.py:334` creates `Payout` rows and `bot/resolution.py:340` immediately credits user withdrawable balance.
- `bot/fraud.py:38` sets `DISPUTE_WINDOW = timedelta(hours=2)`.
- `bot/fraud.py:286` skips redistribution if payouts already exist.
- `bot/withdrawals.py:317` lets users reserve withdrawable balance for payout.

Risk:

Winners can request manual payout before the dispute window is safely closed. If arbitration later changes the outcome, existing payouts are not redistributed, and there is no clean clawback or reversal mechanism.

Fix:

Introduce payout states or ledger holds. Keep resolved winnings unavailable for withdrawal until the dispute window closes, or make arbitration able to reverse prior payout ledger entries. Align the dispute window with product policy before launch.

### P0. Internal money model is not an auditable ledger

Evidence:

- `bot/models.py:71` stores mutable `users.balance_credits`.
- Balance updates are persisted only as changed balances; reasons are passed to functions but not stored as immutable accounting records.
- `bot/models.py:93`, `bot/models.py:164`, `bot/models.py:194`, and `bot/models.py:220` store deposits/bets/payouts/withdrawals, but not a unified journal.
- `bot/resolution.py:306` calculates platform fee, but there is no fee ledger/account row.
- `bot/withdrawals.py:350` creates withdrawals with `charge_ids_used=[]`.

Risk:

You cannot reliably prove reserves, fee income, payout liabilities, refunds, manual payout status, or dispute reversals. For money-like production behavior, mutable balance plus side tables is not enough.

Fix:

Add append-only `ledger_entries`: idempotency key, user/account, amount, currency, entry type, source table/id, metadata, created_at. Treat `balance_credits` as a projection, or reconcile it from the ledger. Persist platform fees and payout reserves explicitly.

### P0. Product/legal/support gates are still open

Evidence:

- `agents.md:166` says `/paysupport` and clear Terms are required before real payments.
- `agents.md:175` says the fee/reserve/payout economics are unresolved.
- `agents.md:387` lists production hardening, terms/payment support copy, economic simulation, legal/compliance review, and real 1-Star validation as next work.
- `agents.md:499` says legal/compliance posture must be reviewed before broad public launch.
- No Terms, Privacy, `/paysupport`, refund/support runbook, or jurisdiction policy exists in repo.

Risk:

Prediction markets plus Stars/TON-equivalent payouts are legally and operationally sensitive. A public paid launch without Terms, support, refund/dispute policy, age/jurisdiction restrictions, and economic modeling is not production-ready.

Fix:

Add Terms, Privacy, `/paysupport`, support/refund runbook, payout SLA, jurisdiction/age policy, admin operations guide, and an economic simulator or spreadsheet before taking real public money.

### P1. Withdrawal notification path can make payout requests disappear on provider failure

Evidence:

- `api/webapp.py:443` creates a withdrawal inside `api_context`.
- `api/webapp.py:450` calls `notify_admins_for_withdrawal` before leaving the same `session_scope`.
- `api/webapp.py:608` maps `WithdrawalProviderError` to API provider failure inside the context.
- `bot/database.py:39` commits only after the handler returns; `bot/database.py:41` rolls back on exceptions.

Risk:

If Telegram admin notification fails in a way that raises, the withdrawal reservation can roll back and the user sees a failed payout request. The current `notify_admins_for_withdrawal` mostly logs per-admin failures, but the transaction boundary is still wrong for production operations.

Fix:

Commit the withdrawal request first, then send admin notifications best-effort, or persist an outbox job. Return success once the reservation is committed and expose unnotified pending withdrawals in an admin queue.

### P1. Telegram side effects are mixed with DB transactions

Evidence:

- `bot/database.py:32` commits aiogram sessions after handlers return.
- `bot/handlers/markets.py:398` creates market, `bot/handlers/markets.py:408` publishes Telegram card, then `bot/handlers/markets.py:415` stores message id before outer commit.
- `bot/betting.py:286` records payment, `bot/betting.py:301` places bet, then `bot/betting.py:310` updates the market card before outer commit.
- `bot/resolution.py:182` persists resolution, then `bot/resolution.py:213` publishes results before handler commit.

Risk:

Telegram can display a market, bet, result, or payout notification for state that later rolls back. Payment flows are especially sensitive because users trust Telegram confirmations.

Fix:

Use a transactional outbox or explicitly commit before non-critical Telegram updates. For MVP, prioritize payment success, market publish, resolution publish, withdrawal notifications, and dispute/admin notifications.

### P1. Banned-user gating is incomplete

Evidence:

- `bot/models.py:77` has `User.is_banned`.
- `bot/fraud.py:155` has `can_user_bet`, but it only checks creator/status and is not used in payment validation.
- `bot/betting.py:442` validates stake invoice requests without checking `user.is_banned`.

Risk:

Banned users can still receive invoices and place paid bets through bot or Mini App paths.

Fix:

Add `is_banned` checks to invoice validation, pre-checkout validation, and final `place_bet`. Add tests for callback/FSM, Mini App `/api/bet`, and successful payment handling.

### P1. No product-level upper bounds for money inputs

Evidence:

- `bot/betting.py:577` accepts any positive integer stake.
- `bot/withdrawals.py:642` accepts any positive integer withdrawal.
- `api/webapp.py:402` accepts any positive integer deposit.
- `bot/config.py:124` only checks `PLATFORM_FEE_PCT` is non-negative.
- `migrations/0001_module3_database_layer.sql:13` stores Stars amounts as `INTEGER`.

Risk:

Very large inputs can fail Telegram provider calls, overflow integer columns, create operational abuse, or break payout assumptions.

Fix:

Define shared limits: max stake, max deposit, max withdrawal, max market min bet, daily withdrawal limit, and maybe per-market total exposure. Validate in bot, API, pre-checkout, and DB constraints.

### P1. Privacy/access policy for direct market detail is unresolved

Evidence:

- `api/webapp.py:186` public feed limits `/api/markets` to `chat_id == 0`.
- `api/webapp.py:210` returns `/api/market/{market_id}` to any authenticated Mini App user if they know the id.
- `tests/test_api_webapp.py:213` explicitly asserts group/private markets remain directly accessible by `market_id`.

Risk:

This may be intentional for “Open event” links, but it means group/private market content is not access-controlled beyond unguessable-ish ids. That can expose group questions, pools, outcomes, and bet counts to any Telegram user with a link or guessed id.

Fix:

Decide the policy. If group markets are public-by-link, say so in UX/Terms. If not, add access control using Telegram chat membership validation, signed deep links, or share tokens.

## Should Fix Before Wider Beta

### P2. Dispute window conflicts with project documentation

Evidence:

- `bot/fraud.py:38` uses 2 hours.
- `agents.md:51` and `agents.md:64` describe a 24-hour post-resolution dispute window.
- `bot/fraud.py:562` enforces `resolved_at + DISPUTE_WINDOW`.

Risk:

Users and operators may expect 24 hours while code closes disputes after 2 hours.

Fix:

Pick one policy and update code, docs, tests, and user-facing copy.

### P2. TON address validation is too loose

Evidence:

- `bot/withdrawals.py:41` uses regex `^[A-Za-z0-9_:\-]{20,128}$`.
- `bot/withdrawals.py:654` accepts any string matching that regex.

Risk:

Invalid or wrong-network addresses can enter the manual payout queue and be paid incorrectly.

Fix:

Use a real TON address parser/checksum validator. If adding a dependency is deferred, add stricter bounceable/base64url validation and admin warning copy.

### P2. No durable admin payout queue UI

Evidence:

- `bot/withdrawals.py:559` only sends Telegram admin messages for payout requests.
- `bot/admin.py` has stats/disputes/broadcast commands, but no full withdrawal queue command or Mini App admin queue.
- `api/webapp.py:291` exposes only aggregate admin overview.

Risk:

If admin notifications are missed, deleted, or sent when admins are unavailable, pending payout operations are easy to lose.

Fix:

Add `/admin_withdrawals` and/or Mini App admin queue with pending withdrawals, wallet, amount, created_at, user, retry notification, mark paid/reject actions, and audit notes.

### P2. Migration system is fragile

Evidence:

- `bot/database.py:85` runs all sorted `*.sql` files at startup.
- `bot/database.py:99` splits SQL by raw semicolon.
- There is no `schema_migrations` table.

Risk:

Works for simple idempotent DDL, but production migrations with backfills, non-idempotent data changes, functions, or reordered constraints are risky.

Fix:

Adopt Alembic or add `schema_migrations` with transactional version tracking. Avoid raw semicolon splitting for complex SQL.

### P2. Feed/API has N+1 query shape

Evidence:

- `api/webapp.py:191` loads markets.
- `api/webapp.py:198` serializes each market one-by-one.
- `api/webapp.py:501`, `api/webapp.py:504`, and `api/webapp.py:505` query pool/my bet/all bets per market.

Risk:

Feed latency and DB load will grow quickly as active markets increase.

Fix:

Batch pool totals, bet counts, and current-user bets with grouped queries, or maintain market summary counters.

### P2. Frontend payout estimate can drift from backend economics

Evidence:

- `frontend/app.js:1374` estimates payout client-side.
- `frontend/app.js:1382` hardcodes `0.92`.
- Backend uses configurable `PLATFORM_FEE_PCT` via `api/webapp.py:362` and `bot/betting.py:469`.

Risk:

Users can see estimates that differ from actual backend fee policy.

Fix:

Return fee settings or server-computed estimates in market detail and bet preview.

### P2. `PLATFORM_FEE_PCT` validation is inconsistent

Evidence:

- `bot/config.py:124` accepts any non-negative float.
- `bot/resolution.py:609` and `bot/betting.py:478` reject values `>= 1` at runtime.

Risk:

Bad config can pass startup and fail later during betting/resolution.

Fix:

Validate `0 <= PLATFORM_FEE_PCT < 1` in config loading.

### P2. Monitoring exists but is not operationalized

Evidence:

- `bot/monitoring.py:98` defines `monitor_payment_anomalies`.
- No startup worker, admin command, alert sink, or scheduled job calls it.

Risk:

Fraud/payment anomaly logic does not actually alert operators in production.

Fix:

Schedule anomaly scans and route high severity reports to admins/log alerts. Add admin command for latest anomaly report.

## Acceptable For Closed Beta With Limits

These are not ideal, but can be acceptable if beta is small, invitation-only, and capped:

- Manual TON-equivalent payouts, as long as Terms and user copy are explicit.
- Polling notification worker, as long as only one app instance is active.
- Vanilla frontend without full admin panel, if admin bot commands cover operations.
- Public-by-link market detail, if this is clearly stated.

Beta guardrails:

- cap stakes/deposits/withdrawals very low;
- require configured admins before enabling withdrawals;
- freeze withdrawals until payout/dispute lifecycle is fixed;
- keep an operator reconciliation spreadsheet until ledger exists;
- test real 1-Star deposit/stake/refund/support cycles before accepting public traffic.

## Verification Run

Audit inspections were read-heavy and grounded in code references above. Fresh local verification after writing this report:

```bash
.venv/bin/python -m compileall api bot tests main.py
.venv/bin/python -m pytest -q
```

Result: compileall passed; pytest passed with `139 passed, 721 warnings in 4.49s`. Warnings are `pytest-asyncio` deprecations on Python 3.14.

## Recommended Fix Order

1. Freeze or delay withdrawals for newly resolved winnings until dispute-safe payout lifecycle is implemented.
2. Add append-only ledger and fee/reserve accounting.
3. Add Terms, Privacy, `/paysupport`, support/refund runbook, economics model, and legal/compliance decision.
4. Move payout/admin notification and Telegram publication side effects after commit or through an outbox.
5. Add banned-user checks in all betting paths.
6. Add amount/exposure limits and config validation.
7. Decide market privacy/access policy.
8. Add durable admin withdrawal queue.
9. Replace loose TON wallet regex.
10. Upgrade migrations and batch market feed queries.
