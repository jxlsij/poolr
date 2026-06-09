# Agents Guide

This file is the working memory for future agents on this repository. Update it
after every meaningful architecture, deployment, environment, or module change.

## Current State

- Product: Telegram bot plus Mini App for group prediction markets.
- Stack: Python 3.11+, aiogram 3.x, aiohttp, SQLAlchemy async, asyncpg,
  PostgreSQL/Supabase, Pillow for generated market card PNGs, Hugging Face
  Spaces Docker, Cloudflare Worker proxy.
- Source plan: `prediction_market_mvp_plan.md`. Treat it as the historical
  baseline; the product/payment direction in this `agents.md` supersedes older
  "credits deposit then withdraw" wording in the plan.
- Implemented modules:
  - Module 1: infrastructure, config, webhook setup, DB engine factory,
    Docker/Hugging Face scaffolding, logging, exception handling.
  - Module 2: webhook secret validation, Telegram Mini App `initData`
    validation, admin check, admin middleware, logging, exception handling.
  - Module 3: SQLAlchemy ORM models, session helpers, PostgreSQL SQL migration,
    CRUD helpers for users/deposits/markets/bets, and database-layer logging
    plus exception normalization.
  - Module 4: implicit user identity foundation with `ensure_user`,
    Mini App initData identity helper, aiogram DB session middleware, app-level
    DB wiring, `/start` silent user upsert, domain exceptions, persistence error
    wrapping, and safe operational logging.
  - Module 5: direct Stars stake intake foundation with compact invoice payloads,
    native `currency="XTR"` invoice sending, pre-checkout validation, successful
    payment handling, idempotent `charge_id` persistence, internal Stars balance
    credit/debit helpers, payments router, provider/API error wrapping, safe
    fallback notifications, logging, and tests.
  - Module 6: group market creation flow with `/bet`, normal mention shortcut
    for chats where the bot is present, and Telegram Inline Mode support for
    `@pooolr_bot question` in chats where the bot is not present. Includes
    aiogram FSM states, question/options/deadline/min-stake validation, market
    row creation, market card publishing/updating helpers, `message_id`
    persistence, lazy inline preview generation that does not write rows during
    typing, inline answer-count/deadline preview choices, custom inline syntax
    `question | option 1, option 2 | 2h`, `chosen_inline_result` market
    creation, `inline_message_id` persistence, bet buttons for Module 7,
    logging, exception wrapping, and tests.
  - Module 7: betting engine and market card updates. Includes bet callback
    parsing, stake amount FSM prompt, market-specific Stars invoice payloads,
    stake pre-checkout validation, successful stake payment handling,
    idempotent `charge_id` persistence, internal Stars credit-then-debit stake
    recording, creator/minimum/deadline/option/balance validation,
    probability/payout estimate helpers, normal and inline market card edits,
    betting router wiring, logging, exception wrapping, and tests.
  - Module 8: Stars resolution and payout distribution. Includes creator-only
    `resolve:{market_id}:{option_index}` callbacks, deadline enforcement,
    resolution keyboards/notifications, proportional winner payout calculation
    with configurable `PLATFORM_FEE_PCT`, `Payout` row creation, withdrawable
    Stars accrual through the existing internal balance column, market status
    and winning option persistence, resolved/cancelled card updates, group
    result posts, 24-hour grace auto-cancel stake refunds, operation-level
    logging, provider/persistence/unexpected failure wrapping, and tests.
  - Module 9: manual TON-equivalent withdrawal requests. Includes `/withdraw`
    FSM/direct-args flow, TON wallet validation, withdrawable Stars reservation,
    `Withdrawal` request persistence, admin notifications, admin paid/reject
    callbacks, TON transaction hash capture, payout paid/rejected audit fields,
    rejection refunds, user notifications, operation-level logging, exception
    wrapping, message/callback fallback responses for provider/persistence
    failures, migration `0003_manual_ton_withdrawals.sql`, and tests. This
    does not use `refundStarPayment` as a normal withdrawal rail.
  - Module 10: anti-fraud checks and dispute/arbitration foundation. Includes
    banned-user/market-state bet gating, lightweight suspicious-pattern scoring,
    24-hour post-resolution dispute window, `dispute:{market_id}` callbacks,
    market freezing with `MarketStatus.DISPUTED`, admin dispute notifications,
    admin resolve/reject callbacks, arbitration payout redistribution through
    the Module 8 resolution service, duplicate-payout guard, `markets.resolved_at`
    and `disputes.resolution_note` persistence, operation-level logging,
    exception wrapping, safe callback-answer fallbacks, invalid/unauthorized
    callback logging, all-admin-notification-failure logging, migration
    `0004_disputes_and_resolved_at.sql`, and tests.
  - Module 11: notifications and scheduler foundation. Includes persistent
    `notification_logs` idempotency, an asyncio background expiry worker started
    on aiohttp startup and cancelled on cleanup, deadline-approaching group
    reminders, deadline-reached market card updates without persisting `CLOSED`
    status, creator resolution prompts, 24-hour unresolved auto-cancel scanning
    with stake refunds, best-effort direct bet confirmations, best-effort payout
    notifications after creator/admin resolution, provider/persistence error
    wrapping, per-market expiry-scan rollback after item failures, tolerant
    closed-market handling when creator DMs fail but the card update succeeds,
    optional APScheduler-compatible `schedule_market_jobs`, migration
    `0005_notification_logs.sql`, and tests.
  - Module 12: Mini App backend API foundation. Includes aiohttp `/api/*`
    routes with Telegram Mini App `initData` auth, implicit Mini App user
    ensure, session-scoped transactions, profile/stats endpoint, market detail
    and chat active-market list endpoints, personal bets and withdrawals
    endpoints, admin-safe overview endpoint, direct Stars invoice initiation
    for market stakes and deposits, manual TON-equivalent withdrawal request
    creation, JSON serializers, structured API errors, request-duration and
    operation-level logging, explicit validation/persistence/provider exception
    normalization, provider-failure fallbacks, and tests.
  - Module 13: admin operations foundation. Includes `/admin_stats`,
    `/admin_disputes`, and `/broadcast` admin-only commands, platform stats
    aggregation, open dispute listing, broadcast FSM with per-user delivery
    logging, Telegram Stars transaction fetch wrapper, provider/validation
    error types, admin access checks, and tests.
  - Module 14: deployment and monitoring hardening. Includes structured JSON or
    pretty logging setup, optional log-file sink, `/health` JSON endpoint with
    database probe and bot status, root `OK` health compatibility for Hugging
    Face, payment anomaly scanning helpers, typed aiohttp app keys for
    monitoring state, operation logging, and tests.
  - User-facing `/start` route: sends `bot/assets/start_message.png` with
    emoji-free Markdown caption, adds an explicit inline `Open Mini App` Web App
    button, re-applies the per-chat native Telegram Bot Menu Web App `Open`
    button as a fallback when `/start` is used, and silently ensures the
    Telegram user when a database session is available. Startup still configures
    the global native Telegram Bot Menu Web App `Open` button, and market card
    keyboards now use the resolved webhook-derived `open_url` fallback when
    `MINI_APP_URL` is absent.
  - Mini App frontend shell: a vanilla HTML/CSS/JS app under `frontend/`
    served at `/app`, with Telegram WebApp bootstrap, first-entry onboarding
    disclaimers, live markets/activity/wallet views, Stars bet and deposit
    actions, manual payout request UI, generated Telegram-style PNG sticker
    assets, iPhone-mini-safe onboarding layout, and a `/api/markets` feed for
    the main screen. The public Mini App feed only lists public/app-origin
    markets stored with `markets.chat_id = 0`; group and private-chat markets
    remain reachable through their message button/direct `market_id` link but
    do not appear in the global feed.

## Product Direction

- The product should feel zero-registration. Users should be able to mention
  the bot in a group, create a market, and let others participate without first
  visiting the bot or creating an account.
- User rows are still required technically. Create or update them implicitly via
  `ensure_user` on every meaningful interaction: `/start`, Mini App open,
  group market creation, callback button click, payment, bet, payout request, or
  admin action.
- Do not add a required `/register` flow.
- Do not require `/start` before a user can create a market or place a bet.
- Keep personal profile, wallet, history, withdrawal requests, and stats in the
  Mini App. A bot-side `/me` command is optional and currently not part of the
  preferred MVP.
- The group bot should focus on the shared market surface: create market,
  publish market cards, accept Yes/No actions, update odds/pool state, and show
  resolution.

## Payment And Payout Direction

- The preferred UX is Stars-first and direct-stake: users should not see
  "Poolr credits" as the primary purchase. A group user should be able to press
  a market button, choose an amount, pay a native Telegram Stars invoice, and be
  in the market.
- Internally, keep a ledger denominated in Stars. The ledger can track stakes,
  fees, winnings, reserves, refunds, and manual payout state, but the user-facing
  copy should say Stars rather than credits unless a legal/compliance reason
  requires different wording.
- Telegram Bot API can accept Stars and refund a specific prior Stars payment,
  but it does not provide a clean bot-to-user "send Stars" payout. Do not design
  withdrawal around arbitrary direct Star transfers.
- Refunds are for support, disputes, failed delivery, or chargeback handling.
  Do not treat `refundStarPayment` as the main withdrawal rail unless a later
  real Telegram API test proves a safe, compliant pattern.
- MVP payout preference: Stars-in, manual TON-equivalent payouts in beta.
  Winners accrue a withdrawable balance in Stars units, then request payout in
  the Mini App by providing or connecting a TON wallet. Admins review, pay
  manually, record the TON transaction hash, and mark the request paid.
- Communicate manual payouts transparently during beta. Do not imply instant
  Telegram Stars cashout.
- Add `/paysupport` and clear Terms before going live with real payments.
- Consider a future TON-only or TON-advanced mode for higher-friction serious
  markets, but do not make TON wallet connection the default casual group flow
  unless the product direction changes again.

## Economy Direction

- The economics are unresolved and must be modeled before production money
  launch. The app needs a sustainable fee, reserve, and payout policy that keeps
  Poolr profitable while remaining understandable to users.
- Likely revenue levers: platform fee on losing pool or gross winnings,
  withdrawal fee/spread, minimum withdrawal, payout batching, promotional free
  markets, and creator/group revenue share.
- Required modeling inputs: Telegram Stars purchase/withdrawal economics,
  TON conversion assumptions, expected average stake, market fill rate, payout
  frequency, fraud/refund rate, chargeback/dispute reserve, infrastructure cost,
  and support/admin time.
- Do not hard-code final fee math yet beyond the current configurable
  `PLATFORM_FEE_PCT`. Future work should include an explicit economic simulator
  or spreadsheet before broad launch.

## Important Repos And Deploys

- GitHub: `https://github.com/jxlsij/poolr`
- Hugging Face Space: `https://huggingface.co/spaces/amiasayedau/poolr`
- Public Space URL: `https://amiasayedau-poolr.hf.space`
- Cloudflare Worker is used as Telegram API proxy because the Hugging Face free
  runtime can block direct outbound calls to `api.telegram.org`.
- Telegram Inline Mode must be enabled manually in BotFather with `/setinline`
  before `@pooolr_bot question` works in chats where the bot is not present.
  Suggested placeholder: `Ask a prediction question`.
- Optional but recommended: enable `/setinlinefeedback` in BotFather so Telegram
  sends `chosen_inline_result` updates; the code stores `inline_message_id` when
  those updates arrive. Inline markets are created only from
  `chosen_inline_result`, not from every `inline_query` while the user is still
  typing.

## Project Layout

- `main.py`: aiohttp app entrypoint, health-check, Mini App API mounting,
  static Mini App frontend mounting at `/app`, aiogram webhook mounting,
  webhook registration, native Telegram Bot Menu Web App button setup, custom
  Telegram API endpoint support.
- `api/webapp.py`: Module 12 Mini App backend API. Registers `/api/profile`,
  `/api/markets`, `/api/market/{market_id}`, `/api/chat/{chat_id}/markets`,
  `/api/bets`, `/api/withdrawals`, `/api/admin/overview`, `/api/bet`,
  `/api/deposit`, and `/api/withdraw`; validates Mini App `initData`, ensures
  users implicitly, serializes markets/bets/withdrawals, sends Stars invoices,
  and creates manual TON-equivalent payout requests. It logs request durations
  and operation lifecycle, returns stable JSON error codes, and separates
  validation, persistence, and Telegram provider failures.
- `bot/config.py`: `Config`, `.env`/environment loading, config validation,
  redacted config logging.
- `bot/admin.py`: Module 13 admin operations. Provides admin-only stats,
  dispute listing, broadcast flow, platform stats aggregation, and Stars
  transaction fetch wrapper.
- `bot/monitoring.py`: Module 14 deployment/monitoring helpers. Provides
  structured logging setup, `/health` JSON response logic, and payment anomaly
  scanning.
- `bot/infrastructure.py`: `setup_webhook`, `create_db_pool`, async PostgreSQL
  URL normalization, infrastructure errors. Supabase pooler URLs with
  `sslmode=require` are normalized to asyncpg-compatible `ssl=require`, and the
  asyncpg statement cache is disabled for PgBouncer/transaction-pooler
  compatibility.
- `bot/models.py`: Module 3 SQLAlchemy ORM models and status enums.
- `bot/database.py`: async session factory/context helpers, transaction
  commit/rollback logging, and metadata table creation helper for local/test
  setup.
- `bot/crud.py`: Module 3 CRUD functions from the MVP plan, with domain
  exceptions and SQLAlchemy error wrapping.
- `bot/users.py`: Module 4 implicit Telegram user identity helpers, including
  `ensure_user`, `ensure_user_from_webapp_data`, validation errors, persistence
  error wrapping, and safe logs that do not include raw Mini App initData.
- `bot/middleware/database.py`: aiogram middleware that injects `db_session`
  into update handlers, logs update-session lifecycle, and commits/rolls back
  via `session_scope`.
- `bot/betting.py`: Module 7 betting service/router. Handles
  `bet:{market_id}:{option_index}` callbacks, collects stake amount, sends
  market-specific Stars invoices, validates stake payment payloads, records
  direct stakes into `deposits` plus `bets`, updates market cards, and exposes
  `place_bet`, `validate_bet_request`, `calculate_implied_probability`, and
  `estimate_payout`.
- `bot/resolution.py`: Module 8 resolution service/router. Handles creator
  resolution callbacks, builds resolution keyboards, validates deadline and
  creator ownership, distributes winner payouts in Stars units, records
  `Payout` rows, accrues withdrawable Stars internally, publishes resolved
  market cards/results, and can auto-cancel stale unresolved markets with stake
  refunds.
- `bot/withdrawals.py`: Module 9 manual TON-equivalent payout service/router.
  Handles `/withdraw`, TON wallet and tx hash validation, withdrawable Stars
  reservation, admin review callbacks, paid/rejected status transitions, TON tx
  hash recording, rejected-request refunds, and safe user/admin notifications.
- `bot/fraud.py`: Module 10 anti-fraud/dispute service/router. Handles
  bet-gating checks, suspicious-pattern scoring, user dispute callbacks,
  disputed-market freezing, admin dispute notifications, arbitration callbacks,
  rejected disputes, and safe logging/exception normalization.
- `bot/notifications.py`: Module 11 notification/scheduler service. Handles
  persistent notification idempotency, background expiry scans, deadline
  reminders, closed-market card updates, creator resolution prompts,
  unresolved-market auto-cancel triggering, bet confirmations, and payout
  notifications.
- `bot/payments.py`: Module 5 direct Stars stake intake helpers and router:
  `send_deposit_invoice`, `handle_pre_checkout_query`,
  `handle_successful_payment`, `credit_credits`, `debit_credits`, and compact
  payment payload parsing/building. It now dispatches market-stake payloads to
  Module 7 while preserving the older deposit payload helpers. It wraps
  Telegram provider/API failures, validates pre-checkout quickly, keeps payment
  persistence separate from best-effort user notifications, and avoids logging
  raw payload secrets.
  Function names still match the old plan, but user-facing behavior should
  remain Stars-first, not credits-first.
- `bot/handlers/markets.py`: Module 6 market creation FSM and market card
  helpers. `/bet` starts creation; inline question syntax `/bet Will Max be
  late?` skips directly to options. Mention syntax like `@pooolr_bot Will Max
  be late?` also starts creation when Telegram routes the mention to the bot.
  Telegram Inline Mode is implemented through lazy `inline_query` previews and
  `chosen_inline_result`: `@pooolr_bot Will Max be late?` shows a draft inline
  article while the user types, with selectable deadline presets
  `15m/45m/2h/1d/7d`, answer-count presets from 2-6 answers, and custom syntax
  `question | option 1, option 2, option 3 | 1d` for named options. It creates
  the market only after the user chooses a result, then edits the inline message
  with the selected options, 1 Star min stake, callback data
  `bet:{market_id}:{option_index}` for Module 7, and an `Open event` direct
  Mini App link like `https://t.me/pooolr_bot/poolr?startapp=market_{market_id}`
  so Telegram opens the Mini App natively instead of showing the raw `hf.space`
  URL prompt.
- `bot/security.py`: Module 2 security functions and `AdminMiddleware`.
- `bot/handlers/start.py`: `/start` handler and start-message composition; the
  Open button is configured globally in `main.py` through Telegram Bot Menu.
- `bot/assets/start_message.png`: image sent by `/start`.
- `api/`: Mini App backend endpoints.
- `frontend/`: vanilla Mini App frontend shell and assets served at `/app`.
- `migrations/`: SQL migrations, currently
  `0001_module3_database_layer.sql` for the Module 3 schema and
  `0002_inline_mode_markets.sql` for `markets.inline_message_id`, and
  `0003_manual_ton_withdrawals.sql` for manual payout audit fields on
  `withdrawals`, and `0004_disputes_and_resolved_at.sql` for
  `markets.resolved_at` plus dispute resolution notes, and
  `0005_notification_logs.sql` for persistent notification idempotency.
- `tests/`: focused tests for config, infrastructure, security helpers, and
  Module 3 database CRUD.
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
- `MINI_APP_DIRECT_URL`: optional direct Mini App base URL for event buttons,
  e.g. `https://t.me/pooolr_bot/poolr`; defaults to `BOT_USERNAME` plus
  `MINI_APP_SHORT_NAME`.
- `BOT_USERNAME`: defaults to `pooolr_bot` for Mini App direct links.
- `MINI_APP_SHORT_NAME`: BotFather Mini App short name; defaults to `poolr`.
- `PLATFORM_FEE_PCT`: defaults to `0.08`. This is provisional; final fee,
  reserve, and withdrawal economics are not decided.
- `ADMIN_IDS`: comma/space-separated Telegram user IDs.
- `NOTIFICATION_CHECK_INTERVAL_SECONDS`: background expiry worker interval;
  defaults to `300`.
- `LOG_LEVEL`: defaults to `INFO`.
- `LOG_FORMAT`: `json` or `pretty`; defaults to `json`.
- `LOG_FILE`: optional path for an additional log-file sink.
- `PORT`: defaults to `7860`.

Never commit real secrets or real `.env` files.

## Telegram Stars Rules

- Use `currency="XTR"` for Stars invoices.
- Prefer invoice-per-stake over pre-funded credit deposits for the casual group
  flow.
- `answerPreCheckoutQuery` must happen within 10 seconds.
- On `successful_payment`, save `telegram_payment_charge_id` before recording
  the stake or crediting any internal balance.
- Store every incoming Stars payment `charge_id` permanently for support,
  dispute, reconciliation, and refund handling.
- The bot cannot send arbitrary Stars directly to users through the standard Bot
  API. Manual TON-equivalent payouts are the current beta payout direction.
- If a later Telegram API change enables direct bot-to-user Stars payouts, update
  this guide, the data model, the payout module, Terms, and tests before using
  it.

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

Modules 1-14 from the historical MVP plan now have implementation foundations.
Next work should focus on production hardening, real Mini App frontend, terms
and payment support copy, economic simulation, legal/compliance review, and
real 1-Star Telegram payment-cycle validation.

Do not build later modules on temporary storage. Module 3 is the persistent data
foundation; Module 4 is the implicit identity/session foundation; Module 5 is
the Stars payment intake foundation; Module 6 is the group market card
foundation; Module 7 is the direct betting/stake foundation; Module 8 is the
Stars resolution and withdrawable balance accrual foundation; Module 9 is the
manual TON-equivalent withdrawal request/admin review foundation; Module 10 is
the anti-fraud/dispute/arbitration foundation; Module 11 is the persistent
notification and expiry-worker foundation.

## Testing And Verification

- Current local Python does not have `pytest` installed; `python3 -m pytest -q`
  fails with `No module named pytest`.
- Module 3 test dependencies require `pytest-asyncio` and `aiosqlite` from
  `requirements-dev.txt`.
- Manual checks used so far:
  - `python3 -m compileall bot tests main.py`
  - targeted smoke scripts for config, infrastructure, security, and `/start`.
  - `.venv/bin/python -m compileall bot tests main.py`
  - `.venv/bin/python -m pytest -q` passed with 53 tests on 2026-06-07
    (pytest-asyncio deprecation warnings from Python 3.14).
  - `.venv/bin/python -m pytest -q` passed with 61 tests on 2026-06-07 after
    Module 7 betting engine implementation (same pytest-asyncio warnings).
  - `.venv/bin/python -m pytest -q` passed with 70 tests on 2026-06-07 after
    Module 8 Stars resolution implementation (same pytest-asyncio warnings).
  - `.venv/bin/python -m pytest -q` passed with 71 tests on 2026-06-07 after
    hardening Module 8 logging and exception wrapping (same warnings).
  - `.venv/bin/python -m pytest -q` passed with 80 tests on 2026-06-07 after
    Module 9 manual TON-equivalent withdrawal implementation (same warnings).
  - `.venv/bin/python -m pytest -q` passed with 81 tests on 2026-06-07 after
    hardening Module 9 logging and handler exception fallbacks (same warnings).
  - `.venv/bin/python -m compileall bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 87 tests on 2026-06-07 after
    Module 10 anti-fraud/dispute implementation (same pytest-asyncio warnings).
  - `.venv/bin/python -m compileall bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 89 tests on 2026-06-07 after
    hardening Module 10 callback logging and exception fallbacks (same warnings).
  - `.venv/bin/python -m compileall bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 95 tests on 2026-06-07 after
    Module 11 notifications/scheduler implementation (same pytest-asyncio
    warnings).
  - `.venv/bin/python -m compileall bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 96 tests on 2026-06-07 after
    hardening Module 11 logging, per-item rollback, and provider fallbacks
    (same warnings).
  - `.venv/bin/python -m compileall api bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 101 tests on 2026-06-08 after
    Module 12 Mini App backend API implementation (same pytest-asyncio
    warnings).
  - `.venv/bin/python -m compileall api bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 103 tests on 2026-06-08 after
    hardening Module 12 API logging and exception normalization (same
    pytest-asyncio warnings).
  - `.venv/bin/python -m compileall api bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 112 tests on 2026-06-08 after
    Module 13 admin operations and Module 14 deployment/monitoring hardening
    (same pytest-asyncio warnings).
  - `frontend/` Mini App shell implementation added on 2026-06-08; local
    compile/test/browser verification passed after generated PNG onboarding
    asset integration.
  - `.venv/bin/python -m compileall api bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 120 tests on 2026-06-08 after
    restoring robust Mini App entry buttons and tightening iPhone mini
    onboarding layout (same pytest-asyncio warnings). Browser checks passed for
    onboarding at 375x812, 375x680, and 375x620.
  - `.venv/bin/python -m compileall api bot tests main.py` and
    `.venv/bin/python -m pytest -q` passed with 134 tests on 2026-06-09 after
    limiting the Mini App market feed to public `chat_id = 0` markets while
    preserving direct message-button access to group/private markets (same
    pytest-asyncio warnings).
  - `.venv/bin/python -m compileall api bot tests main.py`,
    `.venv/bin/python -m pytest tests/test_markets.py -q`, and
    `.venv/bin/python -m pytest -q` passed with 136 tests on 2026-06-09 after
    changing inline mode to lazy market creation on `chosen_inline_result`
    instead of writing markets during `inline_query` typing (same
    pytest-asyncio warnings).
  - `.venv/bin/python -m compileall api bot tests main.py`,
    `.venv/bin/python -m pytest tests/test_markets.py -q`, and
    `.venv/bin/python -m pytest -q` passed with 139 tests on 2026-06-09 after
    adding inline market answer-count/deadline choices and custom
    `question | options | deadline` parsing while keeping creation lazy on
    `chosen_inline_result` (same pytest-asyncio warnings).
- `requirements-dev.txt` includes `pytest`; use a virtualenv to run the full
  test suite.
- After deployment changes, verify:
  - HF Space health-check returns `200 OK`;
  - logs include `Telegram webhook set successfully`;
  - logs include `Webhook setup result: True`;
  - secrets are redacted in logs.

## Open Questions

- Mini App frontend shell exists and is served at `/app`; production polish,
  full real-data UX validation, and richer admin/operator screens are still
  needed.
- The exact fee model, reserve policy, withdrawal minimum, payout batching
  cadence, and TON conversion/spread rules are not finalized.
- Rounding rules for payout distribution are not finalized.
- Manual payout operations now have bot-side admin review callbacks and admin
  stats/dispute commands, but still need a richer Mini App/admin-panel queue for
  production operations.
- Partial refund behavior for Stars payments still needs real Telegram API
  validation for disputes/support only, not as the default payout mechanism.
- Anti-fraud thresholds are provisional lightweight heuristics; tune with real
  usage data before production money launch.
- Legal/compliance posture for real-money-like prediction markets must be
  reviewed before broad public launch.
