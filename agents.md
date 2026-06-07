# Agents Guide

This repository currently contains the product blueprint for a Telegram Stars
prediction-market MVP. Treat `prediction_market_mvp_plan.md` as the source of
truth until implementation files are added.

## Project Summary

- Product: Telegram bot plus Mini App for group prediction markets.
- Stack: Python 3.11+, aiogram 3.x, React, Tailwind, PostgreSQL via Supabase,
  Railway for the bot/API, Vercel for the Mini App.
- Currency model: Telegram Stars are converted to internal credits at 1:1.
- Timeline: 3-week MVP split into 14 modules.

## Repository State

- `prediction_market_mvp_plan.md` is the only functional project artifact at
  the time this guide was created.
- There is no application code, package metadata, test suite, or deployment
  config yet.
- When code is introduced, keep the planned top-level structure unless the
  user changes direction:
  - `bot/` for aiogram handlers, routers, middleware, and bot setup.
  - `api/` for Mini App backend endpoints and webhook handling.
  - `frontend/` for the React/Tailwind Telegram Mini App.
  - `migrations/` for Alembic database migrations.

## Critical Product Rules

- The bot cannot send Stars directly. Withdrawals must use
  `refundStarPayment` against original Telegram payment `charge_id` values.
- Store every deposit `charge_id` permanently. Withdrawal logic depends on
  these IDs.
- Handle `pre_checkout_query` quickly. Telegram requires
  `answerPreCheckoutQuery` within 10 seconds or the Stars charge will not
  complete.
- Mini App API endpoints must validate Telegram `initData`.
- Webhook requests must verify the configured Telegram secret token.
- Market creators cannot bet on their own markets.
- Market deadlines must stay within the planned range: 15 minutes to 7 days.
- Disputes can freeze markets and require admin arbitration.

## Implementation Order

Follow the dependency chain from the plan:

1. Infrastructure, config, webhook, database pool.
2. Authentication and security middleware.
3. Database schema, ORM models, and CRUD layer.
4. User commands and profile flow.
5. Stars payment intake and credit accounting.
6. Market creation flow and market card publishing.
7. Betting engine and market card updates.
8. Resolution and payout distribution.
9. Withdrawals via FIFO `charge_id` refunds.
10. Anti-fraud, disputes, notifications, API, admin, deployment, monitoring.

Do not build later modules on temporary storage. Module 3 is a prerequisite for
nearly everything else.

## Suggested Architecture

- Keep business logic separate from Telegram handlers. Handlers should parse
  events, call services, and format responses.
- Use explicit service boundaries for users, deposits, markets, bets, payouts,
  withdrawals, disputes, and notifications.
- Use database transactions for balance-changing operations:
  - deposits and crediting balance
  - placing bets and debiting balance
  - resolving markets and distributing payouts
  - withdrawals and recording refunded charge IDs
- Add an audit trail for every balance mutation. The plan already names
  reasons such as `bet_placed`, `payout_received`, and `withdrawal`.
- Prefer enums for statuses and validation errors:
  - market status
  - deposit status
  - withdrawal status
  - dispute status
  - bet validation errors
  - suspicion level

## Database Expectations

The initial schema should include:

- `users`: Telegram identity, username, credit balance, ban state, timestamps.
- `deposits`: user, Stars amount, `charge_id`, status, timestamps.
- `markets`: creator, chat, message ID, question, options JSON, deadline,
  minimum bet, status, timestamps.
- `bets`: user, market, option index, credit amount, timestamp.
- `payouts`: user, market, credits won, resolution timestamp.
- `withdrawals`: user, credit amount, used charge IDs JSON, status, timestamp.
- `disputes`: market, reporter, reason, status, timestamp.

When implementing withdrawals, select refundable deposits FIFO by `created_at`.

## Payment And Payout Notes

- Use `currency="XTR"` for Telegram Stars invoices.
- The invoice payload should identify the user and Stars amount.
- On successful payment, read `telegram_payment_charge_id`, create the deposit,
  and credit the user's balance in one durable flow.
- Payout calculation should return winners' stake plus their proportional share
  of the losing pool after platform fee.
- The planned default platform fee example is 8%, represented as `0.08`.

## Testing Priorities

Start tests where product risk is highest:

- Telegram Mini App `initData` validation.
- Webhook secret validation.
- Deposit confirmation and idempotency.
- Balance debit/credit transaction safety.
- Bet validation: closed market, creator self-bet, insufficient balance,
  below minimum, invalid option.
- Payout math, including rounding behavior.
- FIFO charge ID selection for withdrawals.
- Auto-cancel and refund of unresolved markets.
- Dispute freeze and admin arbitration.

Before production, run a real 1-Star payment test and verify that both
`charge_id` capture and `refundStarPayment` work end to end.

## Development Conventions

- Prefer typed Python code and dataclasses or Pydantic models for structured
  results such as `BetResult`, `ResolutionResult`, `WithdrawalResult`, and
  stats objects.
- Keep environment variables documented when they are introduced:
  `BOT_TOKEN`, `DB_URL` or `DATABASE_URL`, `WEBHOOK_URL`, `WEBHOOK_SECRET`,
  `PLATFORM_FEE_PCT`, and `ADMIN_IDS`.
- Do not commit real tokens, database URLs, webhook secrets, or admin IDs.
- Use structured JSON logs in production and readable logs locally.
- Add health checks that verify app, database, and bot reachability.
- Preserve user-facing Russian copy unless the user asks for a language change.

## Open Questions For Future Work

- Exact backend framework for webhook/API hosting is not fixed by existing code.
  The plan names `web.Request`, so aiohttp-style handlers are acceptable unless
  the project later chooses FastAPI or another framework.
- Rounding rules for payout distribution must be finalized before money-like
  flows are shipped.
- Partial refund behavior for deposits may require live Telegram API validation.
  Test this with a 1-Star transaction before relying on assumptions.
- Anti-fraud thresholds are not specified yet; implement conservative flags
  first and keep admin override paths clear.
