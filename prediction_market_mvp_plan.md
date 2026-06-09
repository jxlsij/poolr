# 🎯 Prediction Market Bot — Пошаговый план MVP

> **Стек:** Python 3.11+ · aiogram 3.x · React + Tailwind · PostgreSQL (Supabase) · Railway · Vercel  
> **Срок:** 3 недели · **Валюта:** Telegram Stars-first, внутренний ledger в Stars, beta-вывод эквивалента в TON вручную
> **Критически важно:** бот не может отправлять Stars напрямую; `refundStarPayment` использовать для support/disputes, а не как обычный withdrawal

---

## Product Direction Update — 2026-06-07

Этот план остается базой по модулям, но старую модель "пользователь покупает
внутренние кредиты, потом выводит через refund" больше не считать целевой.
Актуальное направление:

- zero-registration UX: пользователь может создать рынок или поставить в группе
  без обязательного `/start` и без `/register`;
- Module 4 трактовать как User Identity: `ensure_user`, implicit upsert,
  профиль/кошелек/история в Mini App, bot-side `/me` не обязателен;
- Module 5 трактовать как direct Stars stake intake: invoice на конкретную
  ставку, без user-facing "Poolr credits";
- Module 8 начисляет выигрыш во внутренний withdrawable balance,
  номинированный в Stars;
- Module 9 трактовать как Mini App withdrawal request + admin-reviewed manual
  TON-equivalent payout с записью transaction hash;
- экономику, fee model, reserve, minimum withdrawal, payout batching, TON
  conversion/spread и fraud/dispute reserve нужно просчитать до production
  money launch.

Подробный source of truth для будущих агентов: `agents.md`.

---

## Содержание

1. [Module 1 — Project Setup & Infrastructure](#module-1--project-setup--infrastructure)
2. [Module 2 — Authentication & Security](#module-2--authentication--security)
3. [Module 3 — Database Layer](#module-3--database-layer)
4. [Module 4 — User Management](#module-4--user-management)
5. [Module 5 — Payment & Credits System](#module-5--payment--credits-system)
6. [Module 6 — Market Core](#module-6--market-core)
7. [Module 7 — Betting Engine](#module-7--betting-engine)
8. [Module 8 — Resolution & Payouts](#module-8--resolution--payouts)
9. [Module 9 — Withdrawal System](#module-9--withdrawal-system)
10. [Module 10 — Anti-Fraud & Dispute Resolution](#module-10--anti-fraud--dispute-resolution)
11. [Module 11 — Notifications & Scheduler](#module-11--notifications--scheduler)
12. [Module 12 — Mini App Backend API](#module-12--mini-app-backend-api)
13. [Module 13 — Admin Panel](#module-13--admin-panel)
14. [Module 14 — Deployment & Monitoring](#module-14--deployment--monitoring)

---

## Module 1 — Project Setup & Infrastructure

**Цель:** инициализация репозитория, переменных окружения, регистрация бота.  
**Срок:** День 1

### Шаги
1. Создать структуру проекта (`/bot`, `/api`, `/frontend`, `/migrations`)
2. Зарегистрировать бота в [@BotFather](https://t.me/BotFather), включить Stars-платежи
3. Инициализировать Supabase-проект, получить `DATABASE_URL`
4. Настроить `.env` и `docker-compose.yml` для локальной разработки
5. Настроить вебхук вместо polling

### Функции

```python
def load_config(env_path: str = ".env") -> Config
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `env_path` | `str` | Путь к файлу `.env` |
| → | `Config` | Датакласс: `BOT_TOKEN`, `DB_URL`, `WEBHOOK_URL`, `WEBHOOK_SECRET`, `PLATFORM_FEE_PCT`, `ADMIN_IDS` |

```python
async def setup_webhook(
    bot: Bot,
    webhook_url: str,
    secret_token: str,
    allowed_updates: list[str]
) -> bool
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `bot` | `Bot` | aiogram Bot instance |
| `webhook_url` | `str` | Полный HTTPS-URL вебхука (напр. `https://app.railway.app/webhook`) |
| `secret_token` | `str` | Секрет для заголовка `X-Telegram-Bot-Api-Secret-Token` |
| `allowed_updates` | `list[str]` | Фильтр типов обновлений: `["message", "callback_query", "pre_checkout_query", ...]` |
| → | `bool` | True если вебхук успешно зарегистрирован |

```python
async def create_db_pool(db_url: str, pool_size: int = 10) -> AsyncEngine
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `db_url` | `str` | PostgreSQL connection string |
| `pool_size` | `int` | Размер пула соединений |
| → | `AsyncEngine` | SQLAlchemy async engine |

---

## Module 2 — Authentication & Security

**Цель:** безопасное хранение токенов, проверка вебхуков, контроль доступа.  
**Срок:** День 1–2

### Шаги
1. Реализовать HMAC-валидацию входящих вебхук-запросов от Telegram
2. Реализовать валидацию `initData` для Mini App (HMAC-SHA256)
3. Создать middleware для проверки прав администратора

### Функции

```python
def verify_webhook_request(
    raw_body: bytes,
    secret_token: str,
    signature_header: str
) -> bool
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `raw_body` | `bytes` | Тело входящего HTTP-запроса |
| `secret_token` | `str` | Секрет, заданный при регистрации вебхука |
| `signature_header` | `str` | Значение заголовка `X-Telegram-Bot-Api-Secret-Token` |
| → | `bool` | True если подпись валидна |

```python
def validate_webapp_init_data(
    init_data_raw: str,
    bot_token: str
) -> dict | None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `init_data_raw` | `str` | Строка `window.Telegram.WebApp.initData` |
| `bot_token` | `str` | Токен бота для HMAC-SHA256 проверки |
| → | `dict \| None` | Словарь с `user`, `auth_date`, `hash` или `None` если невалидно |

```python
def is_admin(user_id: int, admin_ids: list[int]) -> bool
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `user_id` | `int` | Telegram ID пользователя |
| `admin_ids` | `list[int]` | Список ID администраторов из конфига |
| → | `bool` | True если пользователь — администратор |

```python
class AdminMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable,
        event: Message | CallbackQuery,
        data: dict
    ) -> Any
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `handler` | `Callable` | Следующий обработчик |
| `event` | `Message \| CallbackQuery` | Входящее событие |
| `data` | `dict` | Контекст aiogram |

---

## Module 3 — Database Layer

**Цель:** схема БД, ORM-модели, базовые CRUD-операции.  
**Срок:** День 2–3

### Схема таблиц

```
users        → telegram_id, username, balance_credits, created_at, is_banned
deposits     → id, user_id, stars_amount, charge_id, status, created_at  ← хранить бессрочно!
markets      → id, creator_id, chat_id, message_id, question, options(json), deadline, min_bet, status, created_at
bets         → id, user_id, market_id, option_index, credits_amount, created_at
payouts      → id, user_id, market_id, credits_won, resolved_at
withdrawals  → id, user_id, credits_amount, charge_ids_used(json), status, created_at
disputes     → id, market_id, raised_by, reason, status, created_at
```

### Функции — `users`

```python
async def create_or_get_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str
) -> tuple[User, bool]
```
| → | `tuple[User, bool]` | User-объект и флаг `is_new` |

```python
async def get_user_by_id(session: AsyncSession, telegram_id: int) -> User | None
```

```python
async def update_user_balance(
    session: AsyncSession,
    telegram_id: int,
    delta: int,
    reason: str
) -> User
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `delta` | `int` | Положительный — начислить, отрицательный — списать |
| `reason` | `str` | Метка для аудит-лога (`"bet_placed"`, `"payout_received"`, `"withdrawal"`) |

### Функции — `deposits`

```python
async def create_deposit(
    session: AsyncSession,
    user_id: int,
    stars_amount: int,
    charge_id: str
) -> Deposit
```

```python
async def confirm_deposit(session: AsyncSession, charge_id: str) -> Deposit
```

```python
async def get_available_charge_ids(
    session: AsyncSession,
    user_id: int,
    credits_needed: int
) -> list[str]
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `credits_needed` | `int` | Сколько Stars нужно вернуть |
| → | `list[str]` | FIFO-список `charge_id` для покрытия суммы |

### Функции — `markets`

```python
async def create_market(
    session: AsyncSession,
    creator_id: int,
    chat_id: int,
    question: str,
    options: list[str],
    deadline: datetime,
    min_bet: int
) -> Market
```

```python
async def get_market(session: AsyncSession, market_id: int) -> Market | None
```

```python
async def get_active_markets_in_chat(
    session: AsyncSession,
    chat_id: int
) -> list[Market]
```

```python
async def get_markets_past_deadline(
    session: AsyncSession,
    grace_hours: int = 24
) -> list[Market]
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `grace_hours` | `int` | Сколько часов ждать после дедлайна перед авто-отменой |

```python
async def update_market_status(
    session: AsyncSession,
    market_id: int,
    status: MarketStatus,
    winning_option: int | None = None
) -> Market
```

### Функции — `bets`

```python
async def create_bet(
    session: AsyncSession,
    user_id: int,
    market_id: int,
    option_index: int,
    credits_amount: int
) -> Bet
```

```python
async def get_pool_by_option(
    session: AsyncSession,
    market_id: int
) -> dict[int, int]
```
| → | `dict[int, int]` | `{option_index: total_credits}` |

```python
async def get_user_bet_on_market(
    session: AsyncSession,
    user_id: int,
    market_id: int
) -> Bet | None
```

---

## Module 4 — User Management

**Цель:** команды `/start`, `/help`, `/me`, регистрация пользователя.  
**Срок:** День 2

### Функции

```python
async def handle_start(message: Message, session: AsyncSession) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `message` | `Message` | Сообщение с командой `/start` |
| `session` | `AsyncSession` | DB-сессия |
| Эффект | — | Регистрирует пользователя (если новый), отправляет приветственное сообщение с кнопками |

```python
async def handle_help(message: Message) -> None
```
| Эффект | — | Отправляет список команд: `/bet`, `/me`, `/deposit`, `/withdraw`, `/help` |

```python
async def handle_profile(message: Message, session: AsyncSession) -> None
```
| Эффект | — | Отправляет карточку: баланс кредитов, статистику ставок, кнопки «Пополнить» и «Вывести» |

```python
async def get_user_stats(session: AsyncSession, user_id: int) -> UserStats
```
| → | `UserStats` | Датакласс: `total_bets_count`, `total_credits_bet`, `total_won`, `markets_created`, `win_rate_pct` |

```python
def build_profile_text(user: User, stats: UserStats) -> str
```
| → | `str` | Готовый текст профиля в Markdown |

---

## Module 5 — Payment & Credits System

**Цель:** приём Stars, сохранение `charge_id`, зачисление кредитов.  
**Срок:** День 3–4  
**⚠️ Критически важно:** `answerPreCheckoutQuery` — в течение 10 секунд, иначе Stars не спишутся.

### Шаги
1. Реализовать `sendInvoice` для пополнения баланса
2. Обработать `pre_checkout_query` и `successful_payment`
3. Сохранить `charge_id` в таблице `deposits`
4. Зачислить кредиты пользователю

### Функции

```python
async def send_deposit_invoice(
    bot: Bot,
    user_id: int,
    stars_amount: int,
    description: str = "Пополнение баланса"
) -> Message
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `stars_amount` | `int` | Количество Stars (минимум 1) |
| Эффект | — | Вызывает `bot.send_invoice()` с `currency="XTR"` |

```python
async def handle_pre_checkout_query(
    query: PreCheckoutQuery,
    session: AsyncSession
) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `query` | `PreCheckoutQuery` | Входящий запрос перед списанием |
| Эффект | — | Валидирует, вызывает `answerPreCheckoutQuery(ok=True/False)` **в течение 10 сек** |

```python
async def handle_successful_payment(
    message: Message,
    session: AsyncSession
) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `message` | `Message` | Содержит `message.successful_payment` с `telegram_payment_charge_id` |
| Эффект | — | Сохраняет `charge_id` → создаёт `Deposit` → зачисляет кредиты |

```python
async def debit_credits(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str
) -> bool
```
| → | `bool` | False если недостаточно баланса |

```python
async def credit_credits(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str
) -> None
```

```python
def build_deposit_invoice_payload(user_id: int, stars_amount: int) -> str
```
| → | `str` | JSON-строка как `payload` инвойса для идентификации платежа |

---

## Module 6 — Market Core

**Цель:** FSM-диалог создания рынка, публикация карточки в групповой чат.  
**Срок:** День 4–6

### Шаги
1. Реализовать FSM-машину состояний для создания рынка (aiogram States)
2. Валидировать каждый шаг (вопрос, варианты, дедлайн, мин. ставка)
3. Публиковать карточку рынка в групповой чат
4. Обновлять карточку при каждой новой ставке

### FSM States
```python
class MarketCreationStates(StatesGroup):
    waiting_question   = State()  # ввод вопроса ≤ 200 символов
    waiting_options    = State()  # 2–6 вариантов
    waiting_deadline   = State()  # 15 мин – 7 дней
    waiting_min_bet    = State()  # ≥ 1 кредит
    confirm            = State()  # подтверждение перед публикацией
```

### Функции

```python
async def handle_bet_command(
    message: Message,
    state: FSMContext
) -> None
```
| Эффект | — | Запускает FSM, переходит в `waiting_question` |

```python
async def process_question_input(
    message: Message,
    state: FSMContext
) -> None
```
| Эффект | — | Валидирует длину (≤ 200 симв.), сохраняет в FSM-данных, запрашивает варианты |

```python
async def process_options_input(
    message: Message,
    state: FSMContext
) -> None
```
| Эффект | — | Парсит варианты (через запятую или по строкам), валидирует 2–6 штук |

```python
async def process_deadline_input(
    message: Message,
    state: FSMContext
) -> None
```
| Эффект | — | Парсит строку типа `"2h"`, `"3d"`, `"45m"` в `datetime`, валидирует диапазон |

```python
def parse_deadline_string(deadline_str: str) -> timedelta | None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `deadline_str` | `str` | Строка: `"15m"`, `"2h"`, `"1d"`, `"7d"` |
| → | `timedelta \| None` | None если формат неверен или вне диапазона |

```python
async def process_min_bet_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession
) -> None
```
| Эффект | — | Создаёт рынок в БД, очищает FSM, публикует карточку |

```python
def build_market_card_text(
    market: Market,
    pool_by_option: dict[int, int]
) -> str
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `pool_by_option` | `dict[int, int]` | `{option_index: total_credits}` |
| → | `str` | Текст карточки: вопрос, варианты, пул, % распределение, дедлайн |

```python
def build_market_keyboard(
    market_id: int,
    options: list[str],
    status: MarketStatus
) -> InlineKeyboardMarkup
```
| → | `InlineKeyboardMarkup` | Кнопки для ставки + «Открыть в приложении» |

```python
async def publish_market_card(
    bot: Bot,
    chat_id: int,
    market: Market,
    pool_by_option: dict[int, int]
) -> Message
```
| → | `Message` | Сохранить `message.message_id` в таблице `markets` для будущих правок |

```python
async def update_market_card(
    bot: Bot,
    chat_id: int,
    message_id: int,
    market: Market,
    pool_by_option: dict[int, int]
) -> None
```
| Эффект | — | Вызывает `editMessageText` + `editMessageReplyMarkup` |

---

## Module 7 — Betting Engine

**Цель:** приём ставок, валидация, дебет баланса, обновление карточки.  
**Срок:** День 5–7

### Шаги
1. Обработать нажатие кнопки ставки (callback)
2. Проверить: рынок активен, пользователь не создатель, баланс достаточен
3. Списать кредиты, записать ставку
4. Обновить карточку рынка в чате

### Функции

```python
async def handle_bet_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext
) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `callback.data` | `str` | Формат: `"bet:{market_id}:{option_index}"` |
| Эффект | — | Проверяет данные, запрашивает сумму ставки или открывает Mini App |

```python
async def place_bet(
    session: AsyncSession,
    user_id: int,
    market_id: int,
    option_index: int,
    credits_amount: int
) -> BetResult
```
| → | `BetResult` | Датакласс: `success`, `bet`, `new_balance`, `pool_by_option` |

```python
def validate_bet_request(
    user: User,
    market: Market,
    option_index: int,
    credits_amount: int
) -> BetValidationError | None
```
| → | `BetValidationError \| None` | Enum: `MARKET_CLOSED`, `CREATOR_CANNOT_BET`, `INSUFFICIENT_BALANCE`, `BELOW_MIN_BET`, `INVALID_OPTION` или `None` |

```python
def calculate_implied_probability(
    pool_by_option: dict[int, int]
) -> dict[int, float]
```
| → | `dict[int, float]` | `{option_index: probability_0_to_1}` на основе текущего распределения кредитов |

```python
def estimate_payout(
    bet_amount: int,
    option_index: int,
    pool_by_option: dict[int, int],
    platform_fee_pct: float
) -> int
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `platform_fee_pct` | `float` | Платформенная комиссия (напр. 0.08) |
| → | `int` | Примерный выигрыш если данный вариант победит |

---

## Module 8 — Resolution & Payouts

**Цель:** резолвинг создателем, автоматическое распределение кредитов, публикация итогов.  
**Срок:** День 7–9

### Шаги
1. После дедлайна уведомить создателя в личку
2. Создатель выбирает победивший вариант (InlineKeyboard)
3. Автоматически распределить кредиты (минус 8% платформенная комиссия)
4. Опубликовать итоги в групповой чат

### Функции

```python
async def notify_creator_for_resolution(
    bot: Bot,
    market: Market
) -> None
```
| Эффект | — | Отправляет личное сообщение создателю с кнопками для каждого варианта |

```python
def build_resolution_keyboard(
    market_id: int,
    options: list[str]
) -> InlineKeyboardMarkup
```
| → | `InlineKeyboardMarkup` | Кнопки: `"resolve:{market_id}:{i}"` для каждого варианта |

```python
async def handle_resolve_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot
) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `callback.data` | `str` | Формат: `"resolve:{market_id}:{winning_option_index}"` |
| Эффект | — | Проверяет что `callback.from_user.id == market.creator_id`, запускает резолвинг |

```python
async def resolve_market(
    session: AsyncSession,
    market_id: int,
    winning_option_index: int,
    resolved_by: int
) -> ResolutionResult
```
| → | `ResolutionResult` | Датакласс: `payouts`, `platform_fee_collected`, `total_participants` |

```python
async def distribute_payouts(
    session: AsyncSession,
    market: Market,
    winning_option_index: int,
    platform_fee_pct: float
) -> list[Payout]
```
| Алгоритм | — | 1) Суммировать проигравший пул; 2) Для каждого победителя: `payout = bet / winning_total * losing_total * (1 - fee)` + возврат ставки; 3) Записать Payout, зачислить кредиты |

```python
def calculate_winner_share(
    user_bet: int,
    winning_side_total: int,
    total_pool: int,
    platform_fee_pct: float
) -> int
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `winning_side_total` | `int` | Сумма ставок на победивший вариант |
| `total_pool` | `int` | Весь пул рынка |
| → | `int` | Кредиты для зачисления победителю (включая возврат ставки) |

```python
async def auto_cancel_market(
    session: AsyncSession,
    bot: Bot,
    market: Market
) -> None
```
| Эффект | — | Отмена рынка если создатель не резолвил за 24ч после дедлайна; возврат всех ставок |

```python
async def publish_resolution_results(
    bot: Bot,
    market: Market,
    payouts: list[Payout],
    pool_by_option: dict[int, int]
) -> None
```
| Эффект | — | Обновляет карточку рынка + публикует итоговый пост в групповой чат |

```python
def build_results_text(
    market: Market,
    winning_option_index: int,
    payouts: list[Payout],
    platform_fee: int
) -> str
```
| → | `str` | Текст с победившим вариантом, топ-3 победителями, суммой пула и комиссией |

---

## Module 9 — Withdrawal System

**Цель:** вывод кредитов обратно в Stars через `refundStarPayment`.  
**Срок:** День 9–11  
**Ограничение:** возможен только через `refundStarPayment` по оригинальным `charge_id`

### Шаги
1. Пользователь запрашивает вывод (команда или Mini App)
2. Подобрать `charge_id` в порядке FIFO, покрывающие сумму
3. Вызвать `refundStarPayment` для каждого `charge_id`
4. Обновить баланс

### Функции

```python
async def handle_withdraw_command(
    message: Message,
    session: AsyncSession,
    state: FSMContext
) -> None
```
| Эффект | — | Показывает текущий баланс, запрашивает сумму вывода |

```python
async def process_withdrawal_amount(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot
) -> None
```
| Эффект | — | Валидирует сумму, подбирает charge_ids, запускает вывод |

```python
async def execute_withdrawal(
    session: AsyncSession,
    bot: Bot,
    user_id: int,
    credits_amount: int
) -> WithdrawalResult
```
| → | `WithdrawalResult` | Датакласс: `refunded_stars`, `failed_charge_ids`, `remaining_balance` |

```python
def select_charge_ids_fifo(
    deposits: list[Deposit],
    credits_needed: int
) -> list[str]
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `deposits` | `list[Deposit]` | Список депозитов (отсортирован по `created_at ASC`) |
| `credits_needed` | `int` | Нужная сумма |
| → | `list[str]` | Список `charge_id` для покрытия суммы |

```python
async def refund_single_charge(
    bot: Bot,
    telegram_user_id: int,
    charge_id: str
) -> bool
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `charge_id` | `str` | `telegram_payment_charge_id` из оригинального платежа |
| Эффект | — | Вызывает `bot.refund_star_payment(user_id, charge_id)` |
| → | `bool` | True если рефанд прошёл успешно |

---

## Module 10 — Anti-Fraud & Dispute Resolution

**Цель:** защита от читерства, система оспаривания результатов.  
**Срок:** День 10–12

### Функции

```python
def can_user_bet(
    user_id: int,
    market: Market
) -> bool
```
| → | `bool` | False если `user_id == market.creator_id` (создатель не может ставить на свой рынок) |

```python
async def handle_dispute_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot
) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `callback.data` | `str` | Формат: `"dispute:{market_id}"` |
| Эффект | — | Создаёт `Dispute`, замораживает рынок если в течение 2ч после резолвинга |

```python
async def freeze_market_for_dispute(
    session: AsyncSession,
    bot: Bot,
    market_id: int,
    raised_by: int,
    reason: str
) -> None
```
| Эффект | — | Статус рынка → `FROZEN`, уведомление администратору |

```python
async def admin_arbitrate(
    session: AsyncSession,
    bot: Bot,
    market_id: int,
    winning_option_index: int,
    admin_id: int
) -> None
```
| Эффект | — | Ручное разрешение спора администратором с последующим distribution payouts |

```python
def detect_suspicious_patterns(
    bets: list[Bet],
    deposits: list[Deposit]
) -> SuspicionLevel
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `bets` | `list[Bet]` | Недавние ставки пользователя |
| `deposits` | `list[Deposit]` | История пополнений |
| → | `SuspicionLevel` | Enum: `CLEAN`, `SUSPICIOUS`, `HIGH_RISK` |

---

## Module 11 — Notifications & Scheduler

**Цель:** уведомления о дедлайнах, выигрышах, автоматические задачи.  
**Срок:** День 10–12

### Шаги
1. Подключить `APScheduler` (или `asyncio.create_task`) для фоновых задач
2. Планировать уведомления при создании рынка
3. Проверять просроченные рынки каждые 5 минут

### Функции

```python
async def schedule_market_jobs(
    scheduler: AsyncIOScheduler,
    market: Market
) -> None
```
| Эффект | — | Добавляет 3 задачи: «за 1ч до дедлайна», «дедлайн», «24ч без резолвинга» |

```python
async def notify_deadline_approaching(
    bot: Bot,
    market: Market
) -> None
```
| Эффект | — | Сообщение в групповой чат: «Осталось 1 час для ставок! Пул: X Stars» |

```python
async def notify_market_closed(
    bot: Bot,
    market: Market
) -> None
```
| Эффект | — | Обновляет карточку рынка (кнопки ставки → неактивны), уведомляет создателя |

```python
async def notify_bet_confirmed(
    bot: Bot,
    user_id: int,
    bet: Bet,
    market: Market,
    new_balance: int,
    estimated_payout: int
) -> None
```
| Эффект | — | Личное сообщение: «Ставка принята: X кредитов на "{вариант}". Потенциальный выигрыш: ~Y» |

```python
async def notify_payout_received(
    bot: Bot,
    user_id: int,
    payout: Payout,
    market: Market
) -> None
```
| Эффект | — | Личное сообщение: «🎉 Вы выиграли X кредитов! Рынок: "{вопрос}"» |

```python
async def run_expiry_check(
    session: AsyncSession,
    bot: Bot
) -> None
```
| Эффект | — | Cron-задача каждые 5 минут: находит просроченные рынки, запускает авто-отмену или уведомление создателя |

---

## Module 12 — Mini App Backend API

**Цель:** REST/WebApp API для React-фронтенда.  
**Срок:** День 13–15

### Все эндпоинты требуют валидации `initData` (Module 2)

```python
async def api_get_profile(
    request: web.Request,
    session: AsyncSession
) -> web.Response
```
| GET `/api/profile` | Возвращает `{user_id, balance, stats: UserStats}` |

```python
async def api_get_market(
    request: web.Request,
    session: AsyncSession
) -> web.Response
```
| GET `/api/market/{market_id}` | Возвращает `{question, options, pool_by_option, odds, status, deadline, my_bet}` |

```python
async def api_get_chat_markets(
    request: web.Request,
    session: AsyncSession
) -> web.Response
```
| GET `/api/chat/{chat_id}/markets` | Возвращает список активных рынков чата |

```python
async def api_place_bet(
    request: web.Request,
    session: AsyncSession,
    bot: Bot
) -> web.Response
```
| POST `/api/bet` | Body: `{market_id, option_index, credits_amount}` → `{success, new_balance, estimated_payout}` |

```python
async def api_get_deposit_link(
    request: web.Request,
    bot: Bot
) -> web.Response
```
| POST `/api/deposit` | Body: `{stars_amount}` → открывает Telegram-инвойс через `sendInvoice` |

```python
async def api_request_withdrawal(
    request: web.Request,
    session: AsyncSession,
    bot: Bot
) -> web.Response
```
| POST `/api/withdraw` | Body: `{credits_amount}` → `{success, stars_refunded, message}` |

---

## Module 13 — Admin Panel

**Цель:** мониторинг платформы, ручной арбитраж, аналитика.  
**Срок:** День 15–17

### Функции

```python
async def handle_admin_stats(
    message: Message,
    session: AsyncSession
) -> None
```
| Команда | `/admin_stats` (только для admin_ids) |
| Эффект | — | Отправляет дашборд: пользователи, оборот, выручка, активные споры |

```python
async def get_platform_stats(
    session: AsyncSession
) -> PlatformStats
```
| → | `PlatformStats` | Датакласс: `total_users`, `active_markets`, `total_volume_stars`, `platform_revenue_stars`, `pending_disputes`, `daily_new_users` |

```python
async def handle_admin_disputes(
    message: Message,
    session: AsyncSession
) -> None
```
| Команда | `/admin_disputes` |
| Эффект | — | Список рынков в статусе `FROZEN` с кнопками арбитража |

```python
async def fetch_star_transactions(
    bot: Bot,
    offset: int = 0,
    limit: int = 100
) -> list[StarTransaction]
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `offset` | `int` | Смещение для пагинации |
| `limit` | `int` | Размер страницы (макс. 100) |
| → | `list[StarTransaction]` | Из Telegram `getStarTransactions` |

```python
async def handle_broadcast(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot
) -> None
```
| Команда | `/broadcast` (только admin) |
| Эффект | — | FSM для ввода текста и рассылки всем пользователям |

---

## Module 14 — Deployment & Monitoring

**Цель:** деплой на Railway + Vercel, логирование, health-check.  
**Срок:** День 18–21

### Шаги
1. `Dockerfile` для бота (Python)
2. `vercel.json` для React Mini App
3. Health-check эндпоинт
4. Логирование ошибок (Sentry или structured logs)
5. Тестирование платёжного цикла на 1-Star транзакциях

### Функции

```python
async def setup_application(config: Config) -> Application
```
| → | `Application` | Сконфигурированный aiogram Application со всеми роутерами, middleware, пулом БД |

```python
def setup_logging(
    level: str = "INFO",
    format: str = "json",
    log_file: str | None = None
) -> None
```
| Параметр | Тип | Описание |
|----------|-----|----------|
| `format` | `str` | `"json"` для продакшена, `"pretty"` для локальной разработки |

```python
async def health_check(request: web.Request) -> web.Response
```
| GET `/health` | `{"status": "ok", "db": "connected", "bot": "running", "ts": "2026-06-07T..."}` |

```python
async def monitor_payment_anomalies(
    session: AsyncSession
) -> list[AnomalyReport]
```
| → | `list[AnomalyReport]` | Пользователи с подозрительными паттернами вывода/депозита |

```python
async def run_migrations(db_url: str) -> None
```
| Эффект | — | Применяет Alembic-миграции при старте приложения |

---

## Roadmap по неделям

| Неделя | Модули | Результат |
|--------|--------|-----------|
| **Неделя 1** (дни 1–7) | 1 → 2 → 3 → 4 → 5 | Бот принимает Stars, регистрирует пользователей, хранит charge_id |
| **Неделя 2** (дни 8–14) | 6 → 7 → 8 → 9 | Создание рынков, ставки, резолвинг, вывод Stars |
| **Неделя 3** (дни 15–21) | 10 → 11 → 12 → 13 → 14 | Mini App, антифрод, деплой, тесты на 1-Star транзакциях |

---

## Критические зависимости

```
Module 3 (DB) ──────────────────────────── все остальные модули
Module 2 (Auth) ──────────────────────────── Module 12 (Mini App API)
Module 5 (Payments) ──────────────────── Module 9 (Withdrawal)
Module 6 (Markets) ──────────────────────── Module 7, 8, 10
Module 8 (Resolution) ───────────────── Module 10 (Disputes)
```

---

> **Первый шаг после чтения:** создай бота в BotFather → включи Stars → напиши первый `sendInvoice` на 1 Star → убедись что `charge_id` возвращается и `refundStarPayment` работает. Это снимет все вопросы по реальному поведению API за 2–3 часа.
