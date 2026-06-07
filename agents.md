# Agents Guide

This file is the working memory for future agents on this repository. Update it
after every meaningful architecture, deployment, environment, or module change.

## Current State

- Product: Telegram bot plus Mini App for group prediction markets.
- Stack: Python 3.11+, aiogram 3.x, aiohttp, SQLAlchemy async, asyncpg,
  PostgreSQL/Supabase, Hugging Face Spaces Docker, Cloudflare Worker proxy.
- Source plan: `prediction_market_mvp_plan.md`.
- Implemented modules:
  - Module 1: infrastructure, config, webhook setup, DB engine factory,
    Docker/Hugging Face scaffolding, logging, exception handling.
  - Module 2: webhook secret validation, Telegram Mini App `initData`
    validation, admin check, admin middleware, logging, exception handling.
  - User-facing `/start` route: sends `bot/assets/start_message.png` with
    Markdown caption and an Open button.

## Important Repos And Deploys

- GitHub: `https://github.com/jxlsij/poolr`
- Hugging Face Space: `https://huggingface.co/spaces/amiasayedau/poolr`
- Public Space URL: `https://amiasayedau-poolr.hf.space`
- Cloudflare Worker is used as Telegram API proxy because the Hugging Face free
  runtime can block direct outbound calls to `api.telegram.org`.

## Project Layout

- `main.py`: aiohttp app entrypoint, health-check, aiogram webhook mounting,
  webhook registration, custom Telegram API endpoint support.
- `bot/config.py`: `Config`, `.env`/environment loading, config validation,
  redacted config logging.
- `bot/infrastructure.py`: `setup_webhook`, `create_db_pool`, async PostgreSQL
  URL normalization, infrastructure errors.
- `bot/security.py`: Module 2 security functions and `AdminMiddleware`.
- `bot/handlers/start.py`: `/start` handler and Open button composition.
- `bot/assets/start_message.png`: image sent by `/start`.
- `api/`: reserved for Mini App backend endpoints.
- `frontend/`: reserved for React/Tailwind Mini App.
- `migrations/`: reserved for Alembic migrations.
- `tests/`: focused tests for config, infrastructure, and security helpers.
- `deploy guides/`: user-provided Hugging Face/Cloudflare deployment guide.

## Environment Variables

Required in production:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `DATABASE_URL` or `DB_URL`: PostgreSQL/Supabase connection string.
- `WEBHOOK_URL`: public webhook URL, currently the HF Space URL.
- `WEBHOOK_SECRET`: Telegram webhook secret token.
- `TELEGRAM_API_URL`: Cloudflare Worker API base in guide format, e.g.
  `https://xxx.workers.dev/bot{0}/{1}`. The code normalizes this for aiogram.

Optional:

- `MINI_APP_URL`: Open button URL for `/start`. Falls back to `WEBHOOK_URL`.
- `PLATFORM_FEE_PCT`: defaults to `0.08`.
- `ADMIN_IDS`: comma/space-separated Telegram user IDs.
- `LOG_LEVEL`: defaults to `INFO`.
- `PORT`: defaults to `7860`.

Never commit real secrets or real `.env` files.

## Telegram Stars Rules

- The bot cannot send Stars directly. Withdrawals must use
  `refundStarPayment` against original Telegram payment `charge_id` values.
- Store every deposit `charge_id` permanently.
- Use `currency="XTR"` for Stars invoices.
- `answerPreCheckoutQuery` must happen within 10 seconds.
- On `successful_payment`, save `telegram_payment_charge_id` before crediting
  user balance.

## Security Rules

- Telegram webhook auth is a constant-time comparison of
  `X-Telegram-Bot-Api-Secret-Token` with `WEBHOOK_SECRET`.
- Telegram Mini App auth uses HMAC-SHA256:
  - secret key: HMAC-SHA256 of bot token with key `WebAppData`;
  - data check string: sorted initData fields excluding `hash`.
- `validate_webapp_init_data` returns parsed data or `None`; it must not leak
  bot tokens, hashes, or raw initData into logs.
- `AdminMiddleware` should be attached only to admin-only routers/handlers.

## Architecture Conventions

- Keep `main.py` small: app creation, wiring, health-check, deployment glue.
- Add bot handlers under `bot/handlers/`.
- Keep business logic out of handlers; future modules should use service
  modules for users, deposits, markets, bets, payouts, withdrawals, disputes,
  and notifications.
- Keep reusable validation/security in dedicated modules such as
  `bot/security.py`.
- Keep static bot assets under `bot/assets/`, not in the repository root.
- If a new module changes architecture, environment variables, deployment,
  folder layout, or user-facing behavior, update this `agents.md` in the same
  change.

## Implementation Order

Continue following the dependency chain from the plan:

1. Module 3: database schema, ORM models, migrations, CRUD layer.
2. Module 4: user commands/profile flow, building on current `/start`.
3. Module 5: Stars payment intake and credit accounting.
4. Module 6: market creation flow and market card publishing.
5. Module 7: betting engine and market card updates.
6. Module 8: resolution and payout distribution.
7. Module 9: withdrawals via FIFO `charge_id` refunds.
8. Modules 10-14: anti-fraud, disputes, notifications, API, admin, deployment,
   monitoring.

Do not build later modules on temporary storage. Module 3 is the next major
foundation.

## Testing And Verification

- Current local Python does not have `pytest` installed; `python3 -m pytest -q`
  fails with `No module named pytest`.
- Manual checks used so far:
  - `python3 -m compileall bot tests main.py`
  - targeted smoke scripts for config, infrastructure, security, and `/start`.
- `requirements-dev.txt` includes `pytest`; use a virtualenv to run the full
  test suite.
- After deployment changes, verify:
  - HF Space health-check returns `200 OK`;
  - logs include `Telegram webhook set successfully`;
  - logs include `Webhook setup result: True`;
  - secrets are redacted in logs.

## Open Questions

- Mini App frontend is not built yet; set `MINI_APP_URL` when it exists.
- Rounding rules for payout distribution are not finalized.
- Partial refund behavior for Stars deposits still needs real Telegram API
  validation.
- Anti-fraud thresholds are not specified yet.
