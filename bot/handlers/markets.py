from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import (
    DatabaseLayerError,
    create_market,
    get_pool_by_option,
    update_market_inline_message_id,
    update_market_message_id,
)
from bot.market_cards import (
    build_market_card_caption,
    build_market_card_image_url,
    resolve_public_base_url,
    send_market_card_photo,
    update_market_card_photo,
)
from bot.models import Market, MarketStatus
from bot.users import UserModuleError, ensure_user


logger = logging.getLogger(__name__)

MAX_QUESTION_LENGTH = 200
MIN_MARKET_DURATION = timedelta(minutes=15)
MAX_MARKET_DURATION = timedelta(days=7)
DEFAULT_OPTIONS = ["Yes", "No"]
INLINE_MARKET_CHAT_ID = 0
INLINE_DEFAULT_DURATION = timedelta(hours=2)
INLINE_DEFAULT_MIN_BET = 1


class MarketCreationError(RuntimeError):
    """Base error for Module 6 market creation operations."""


class MarketCreationValidationError(MarketCreationError, ValueError):
    """Raised when user-provided market creation input is invalid."""


class MarketCreationPersistenceError(MarketCreationError):
    """Raised when a valid market cannot be stored or published."""


class MarketCreationStates(StatesGroup):
    waiting_question = State()
    waiting_options = State()
    waiting_deadline = State()
    waiting_min_bet = State()
    confirm = State()


def create_markets_router(
    mini_app_url: str | None = None,
    public_base_url: str | None = None,
) -> Router:
    router = Router(name="markets")

    @router.message(Command("bet", ignore_mention=True))
    async def bet_command_handler(
        message: Message,
        state: FSMContext,
        command: CommandObject,
    ) -> None:
        await handle_bet_command(message, state, command)

    @router.message(StateFilter(None), F.text.regexp(r"^@\w+\s+.+"))
    async def mention_market_handler(message: Message, state: FSMContext) -> None:
        await handle_mention_market(message, state)

    @router.inline_query()
    async def inline_market_handler(
        query: InlineQuery,
        db_session: AsyncSession,
    ) -> None:
        await handle_inline_market_query(query, db_session, mini_app_url, public_base_url)

    @router.chosen_inline_result()
    async def chosen_inline_market_handler(
        result: ChosenInlineResult,
        db_session: AsyncSession,
    ) -> None:
        await handle_chosen_inline_market(result, db_session)

    @router.message(MarketCreationStates.waiting_question)
    async def question_input_handler(message: Message, state: FSMContext) -> None:
        await process_question_input(message, state)

    @router.message(MarketCreationStates.waiting_options)
    async def options_input_handler(message: Message, state: FSMContext) -> None:
        await process_options_input(message, state)

    @router.message(MarketCreationStates.waiting_deadline)
    async def deadline_input_handler(message: Message, state: FSMContext) -> None:
        await process_deadline_input(message, state)

    @router.message(MarketCreationStates.waiting_min_bet)
    async def min_bet_input_handler(
        message: Message,
        state: FSMContext,
        db_session: AsyncSession,
        bot: Bot,
    ) -> None:
        await process_min_bet_input(
            message=message,
            state=state,
            session=db_session,
            bot=bot,
            mini_app_url=mini_app_url,
        )

    return router


async def handle_bet_command(
    message: Message,
    state: FSMContext,
    command: CommandObject | None = None,
) -> None:
    question = (command.args or "").strip() if command else ""
    logger.info(
        "Starting market creation flow: chat_id=%s user_id=%s has_inline_question=%s",
        message.chat.id if message.chat else None,
        message.from_user.id if message.from_user else None,
        bool(question),
    )

    await state.clear()
    if question:
        try:
            _validate_question(question)
        except MarketCreationValidationError as exc:
            await message.answer(str(exc))
            return

        await state.update_data(question=question)
        await state.set_state(MarketCreationStates.waiting_options)
        await message.answer(
            "Send 2-6 options, separated by commas or new lines.\n"
            "Example: Yes, No"
        )
        return

    await state.set_state(MarketCreationStates.waiting_question)
    await message.answer("Send the market question, up to 200 characters.")


async def handle_mention_market(message: Message, state: FSMContext) -> None:
    text = _message_text(message)
    _mention, _separator, question = text.partition(" ")
    question = question.strip()
    logger.info(
        "Starting mention market creation flow: chat_id=%s user_id=%s",
        message.chat.id if message.chat else None,
        message.from_user.id if message.from_user else None,
    )

    await state.clear()
    try:
        _validate_question(question)
    except MarketCreationValidationError as exc:
        await message.answer(str(exc))
        return

    await state.update_data(question=question)
    await state.set_state(MarketCreationStates.waiting_options)
    await message.answer(
        "Send 2-6 options, separated by commas or new lines.\n"
        "Example: Yes, No"
    )


async def handle_inline_market_query(
    query: InlineQuery,
    session: AsyncSession,
    mini_app_url: str | None = None,
    public_base_url: str | None = None,
) -> None:
    question = query.query.strip()
    logger.info(
        "Handling inline market query: inline_query_id=%s user_id=%s length=%d",
        query.id,
        query.from_user.id,
        len(question),
    )

    if not question:
        await query.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            switch_pm_text="Type a question after @pooolr_bot",
            switch_pm_parameter="inline-help",
        )
        return

    try:
        question = _validate_question(question)
        user, _is_new = await ensure_user(session, query.from_user)
        deadline = datetime.now(timezone.utc) + INLINE_DEFAULT_DURATION
        market = await create_market(
            session=session,
            creator_id=user.telegram_id,
            chat_id=INLINE_MARKET_CHAT_ID,
            question=question,
            options=DEFAULT_OPTIONS,
            deadline=deadline,
            min_bet=INLINE_DEFAULT_MIN_BET,
        )
        pool_by_option = await get_pool_by_option(session, market.id)
        result = build_inline_market_result(
            market=market,
            pool_by_option=pool_by_option,
            mini_app_url=mini_app_url,
            public_base_url=public_base_url,
        )
    except (MarketCreationValidationError, UserModuleError, DatabaseLayerError) as exc:
        logger.exception(
            "Inline market creation failed: inline_query_id=%s user_id=%s",
            query.id,
            query.from_user.id,
        )
        await query.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            switch_pm_text="Could not create this market",
            switch_pm_parameter="inline-error",
        )
        raise MarketCreationPersistenceError("Inline market creation failed") from exc

    await query.answer(results=[result], cache_time=1, is_personal=True)
    logger.info(
        "Answered inline market query: inline_query_id=%s market_id=%d",
        query.id,
        market.id,
    )


async def handle_chosen_inline_market(
    result: ChosenInlineResult,
    session: AsyncSession,
) -> None:
    market_id = _parse_inline_result_market_id(result.result_id)
    if market_id is None or result.inline_message_id is None:
        logger.debug(
            "Ignoring chosen inline result without market id/message id: result_id=%s",
            result.result_id,
        )
        return

    try:
        await update_market_inline_message_id(session, market_id, result.inline_message_id)
    except DatabaseLayerError as exc:
        logger.exception("Could not persist inline message id: market_id=%d", market_id)
        raise MarketCreationPersistenceError("Could not persist inline message id") from exc


async def process_question_input(message: Message, state: FSMContext) -> None:
    question = _message_text(message)
    try:
        _validate_question(question)
    except MarketCreationValidationError as exc:
        await message.answer(str(exc))
        return

    await state.update_data(question=question)
    await state.set_state(MarketCreationStates.waiting_options)
    logger.debug("Market question accepted: chat_id=%s length=%d", message.chat.id, len(question))
    await message.answer(
        "Send 2-6 options, separated by commas or new lines.\n"
        "Example: Yes, No"
    )


async def process_options_input(message: Message, state: FSMContext) -> None:
    try:
        options = parse_options_string(_message_text(message))
    except MarketCreationValidationError as exc:
        await message.answer(str(exc))
        return

    await state.update_data(options=options)
    await state.set_state(MarketCreationStates.waiting_deadline)
    logger.debug("Market options accepted: chat_id=%s count=%d", message.chat.id, len(options))
    await message.answer("Send a deadline: 15m, 45m, 2h, 1d, up to 7d.")


async def process_deadline_input(message: Message, state: FSMContext) -> None:
    duration = parse_deadline_string(_message_text(message))
    if duration is None:
        await message.answer("Invalid deadline. Use 15m-7d, for example 45m, 2h, or 1d.")
        return

    deadline = datetime.now(timezone.utc) + duration
    await state.update_data(deadline=deadline.isoformat())
    await state.set_state(MarketCreationStates.waiting_min_bet)
    logger.debug(
        "Market deadline accepted: chat_id=%s duration_seconds=%d",
        message.chat.id,
        int(duration.total_seconds()),
    )
    await message.answer("Send the minimum stake in Stars, at least 1.")


async def process_min_bet_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    mini_app_url: str | None = None,
) -> None:
    try:
        min_bet = _parse_min_bet(_message_text(message))
    except MarketCreationValidationError as exc:
        await message.answer(str(exc))
        return

    if message.from_user is None:
        await message.answer("Could not identify the market creator.")
        return

    data = await state.get_data()
    try:
        question = _validate_question(str(data.get("question") or ""))
        options = _validate_options(data.get("options"))
        deadline = _parse_deadline_from_state(data.get("deadline"))
        user, _is_new = await ensure_user(session, message.from_user)
        market = await create_market(
            session=session,
            creator_id=user.telegram_id,
            chat_id=message.chat.id,
            question=question,
            options=options,
            deadline=deadline,
            min_bet=min_bet,
        )
        pool_by_option = await get_pool_by_option(session, market.id)
        market_message = await publish_market_card(
            bot=bot,
            chat_id=message.chat.id,
            market=market,
            pool_by_option=pool_by_option,
            mini_app_url=mini_app_url,
        )
        await update_market_message_id(session, market.id, market_message.message_id)
    except (MarketCreationValidationError, UserModuleError, DatabaseLayerError) as exc:
        logger.exception(
            "Market creation failed: chat_id=%s user_id=%s",
            message.chat.id,
            message.from_user.id,
        )
        await message.answer("Could not create this market. Please try again.")
        raise MarketCreationPersistenceError("Market creation failed") from exc
    except Exception as exc:
        logger.exception(
            "Unexpected market creation failure: chat_id=%s user_id=%s",
            message.chat.id,
            message.from_user.id,
        )
        await message.answer("Could not create this market. Please try again.")
        raise MarketCreationPersistenceError("Unexpected market creation failure") from exc

    await state.clear()
    logger.info(
        "Market created and published: market_id=%d chat_id=%d creator_id=%d message_id=%d",
        market.id,
        market.chat_id,
        market.creator_id,
        market_message.message_id,
    )


def parse_deadline_string(deadline_str: str) -> timedelta | None:
    text = deadline_str.strip().lower()
    match = re.fullmatch(r"(\d+)([mhd])", text)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        duration = timedelta(minutes=amount)
    elif unit == "h":
        duration = timedelta(hours=amount)
    else:
        duration = timedelta(days=amount)

    if duration < MIN_MARKET_DURATION or duration > MAX_MARKET_DURATION:
        return None
    return duration


def parse_options_string(options_text: str) -> list[str]:
    raw_parts = re.split(r"[\n,]", options_text)
    options: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        option = " ".join(part.strip().split())
        if not option:
            continue
        key = option.casefold()
        if key in seen:
            continue
        seen.add(key)
        options.append(option)

    return _validate_options(options)


def build_market_card_text(
    market: Market,
    pool_by_option: dict[int, int],
) -> str:
    total_pool = sum(pool_by_option.values())
    deadline = _format_deadline(market.deadline)
    lines = [
        f"Poolr market #{market.id}",
        "",
        market.question,
        "",
        f"Pool: {total_pool} Stars",
        f"Min stake: {market.min_bet} Stars",
        f"Deadline: {deadline}",
        "",
        "Options:",
    ]

    for index, option in enumerate(market.options):
        option_pool = pool_by_option.get(index, 0)
        pct = int(round((option_pool / total_pool) * 100)) if total_pool else 0
        lines.append(f"{index + 1}. {option} - {option_pool} Stars ({pct}%)")

    return "\n".join(lines)


def build_market_keyboard(
    market_id: int,
    options: list[str],
    status: MarketStatus,
    mini_app_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status == MarketStatus.ACTIVE:
        for index, option in enumerate(options):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{option}",
                        callback_data=f"bet:{market_id}:{index}",
                    )
                ]
            )

    if mini_app_url:
        rows.append([InlineKeyboardButton(text="Open", url=mini_app_url)])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_inline_market_result(
    market: Market,
    pool_by_option: dict[int, int],
    mini_app_url: str | None = None,
    public_base_url: str | None = None,
) -> InlineQueryResultArticle | InlineQueryResultPhoto:
    public_base_url = resolve_public_base_url(public_base_url)
    if public_base_url:
        photo_url = build_market_card_image_url(public_base_url, market, pool_by_option)
        return InlineQueryResultPhoto(
            id=f"market:{market.id}",
            photo_url=photo_url,
            thumbnail_url=photo_url,
            photo_width=1200,
            photo_height=960,
            title=market.question,
            description=f"Yes/No market, min {market.min_bet} Star",
            caption=build_market_card_caption(market),
            reply_markup=build_market_keyboard(
                market_id=market.id,
                options=market.options,
                status=market.status,
                mini_app_url=mini_app_url,
            ),
        )

    return InlineQueryResultArticle(
        id=f"market:{market.id}",
        title=market.question,
        description=f"Yes/No market, min {market.min_bet} Star",
        input_message_content=InputTextMessageContent(
            message_text=build_market_card_text(market, pool_by_option),
        ),
        reply_markup=build_market_keyboard(
            market_id=market.id,
            options=market.options,
            status=market.status,
            mini_app_url=mini_app_url,
        ),
    )


async def publish_market_card(
    bot: Bot,
    chat_id: int,
    market: Market,
    pool_by_option: dict[int, int],
    mini_app_url: str | None = None,
) -> Message:
    logger.info("Publishing market card: market_id=%d chat_id=%d", market.id, chat_id)
    try:
        return await send_market_card_photo(
            bot,
            chat_id,
            market,
            pool_by_option,
            build_market_keyboard(
                market_id=market.id,
                options=market.options,
                status=market.status,
                mini_app_url=mini_app_url,
            ),
            fallback_text=build_market_card_text(market, pool_by_option),
        )
    except Exception as exc:
        logger.exception("Failed to publish market card: market_id=%d chat_id=%d", market.id, chat_id)
        raise MarketCreationPersistenceError("Failed to publish market card") from exc


async def update_market_card(
    bot: Bot,
    chat_id: int,
    message_id: int,
    market: Market,
    pool_by_option: dict[int, int],
    mini_app_url: str | None = None,
) -> None:
    logger.info(
        "Updating market card: market_id=%d chat_id=%d message_id=%d",
        market.id,
        chat_id,
        message_id,
    )
    try:
        await update_market_card_photo(
            bot,
            chat_id=chat_id,
            message_id=message_id,
            market=market,
            pool_by_option=pool_by_option,
            reply_markup=build_market_keyboard(
                market_id=market.id,
                options=market.options,
                status=market.status,
                mini_app_url=mini_app_url,
            ),
            fallback_text=build_market_card_text(market, pool_by_option),
        )
    except Exception as exc:
        logger.exception("Failed to update market card: market_id=%d", market.id)
        raise MarketCreationPersistenceError("Failed to update market card") from exc


async def update_inline_market_card(
    bot: Bot,
    inline_message_id: str,
    market: Market,
    pool_by_option: dict[int, int],
    mini_app_url: str | None = None,
    public_base_url: str | None = None,
) -> None:
    logger.info(
        "Updating inline market card: market_id=%d inline_message_id_set=%s",
        market.id,
        bool(inline_message_id),
    )
    public_base_url = resolve_public_base_url(public_base_url)
    try:
        await update_market_card_photo(
            bot,
            inline_message_id=inline_message_id,
            market=market,
            pool_by_option=pool_by_option,
            reply_markup=build_market_keyboard(
                market_id=market.id,
                options=market.options,
                status=market.status,
                mini_app_url=mini_app_url,
            ),
            photo_url=(
                build_market_card_image_url(public_base_url, market, pool_by_option)
                if public_base_url
                else None
            ),
            fallback_text=build_market_card_text(market, pool_by_option),
        )
    except Exception as exc:
        logger.exception("Failed to update inline market card: market_id=%d", market.id)
        raise MarketCreationPersistenceError("Failed to update inline market card") from exc


def _message_text(message: Message) -> str:
    if not isinstance(message.text, str) or not message.text.strip():
        raise MarketCreationValidationError("Please send text.")
    return message.text.strip()


def _validate_question(question: str) -> str:
    question = " ".join(question.strip().split())
    if not question:
        raise MarketCreationValidationError("Question cannot be empty.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise MarketCreationValidationError("Question is too long. Keep it under 200 characters.")
    return question


def _validate_options(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(option, str) for option in value):
        raise MarketCreationValidationError("Options are missing or invalid.")

    options = [" ".join(option.strip().split()) for option in value if option.strip()]
    if len(options) < 2 or len(options) > 6:
        raise MarketCreationValidationError("Send 2-6 options.")
    if any(len(option) > 64 for option in options):
        raise MarketCreationValidationError("Each option must be 64 characters or less.")
    return options


def _parse_deadline_from_state(value: object) -> datetime:
    if not isinstance(value, str):
        raise MarketCreationValidationError("Deadline is missing.")
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MarketCreationValidationError("Deadline is invalid.") from exc
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise MarketCreationValidationError("Deadline must be timezone-aware.")
    return deadline


def _parse_min_bet(value: str) -> int:
    try:
        min_bet = int(value.strip())
    except ValueError as exc:
        raise MarketCreationValidationError("Minimum stake must be a whole number.") from exc
    if min_bet < 1:
        raise MarketCreationValidationError("Minimum stake must be at least 1 Star.")
    return min_bet


def _format_deadline(deadline: datetime) -> str:
    return deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_inline_result_market_id(result_id: str) -> int | None:
    prefix = "market:"
    if not result_id.startswith(prefix):
        return None
    raw_market_id = result_id[len(prefix) :]
    if not raw_market_id.isdigit():
        return None
    return int(raw_market_id)
