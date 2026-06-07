from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import (
    DatabaseLayerError,
    DuplicateRecordError,
    InsufficientBalanceError,
    RecordNotFoundError,
    create_bet,
    get_pool_by_option,
    update_user_balance,
)
from bot.handlers.markets import update_market_card, update_inline_market_card
from bot.models import Bet, DepositStatus, Market, MarketStatus, User
from bot.payments import (
    PAYMENT_TITLE,
    STARS_CURRENCY,
    PaymentProviderError,
    PaymentValidationError,
)
from bot.users import UserModuleError, ensure_user


logger = logging.getLogger(__name__)

PAYLOAD_TYPE_MARKET_STAKE = "market_stake"
BET_CALLBACK_PREFIX = "bet"


class BettingModuleError(RuntimeError):
    """Base error for Module 7 betting operations."""


class BettingPersistenceError(BettingModuleError):
    """Raised when a valid betting operation cannot be persisted."""


class BettingProviderError(BettingModuleError):
    """Raised when Telegram APIs cannot be called for betting."""


class BetValidationError(StrEnum):
    MARKET_CLOSED = "market_closed"
    CREATOR_CANNOT_BET = "creator_cannot_bet"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    BELOW_MIN_BET = "below_min_bet"
    INVALID_OPTION = "invalid_option"
    ALREADY_BET = "already_bet"


@dataclass(frozen=True)
class BetResult:
    success: bool
    bet: Bet | None
    new_balance: int
    pool_by_option: dict[int, int]


class BettingStates(StatesGroup):
    waiting_amount = State()


def create_betting_router(mini_app_url: str | None = None) -> Router:
    router = Router(name="betting")

    @router.callback_query(F.data.startswith(f"{BET_CALLBACK_PREFIX}:"))
    async def bet_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await handle_bet_callback(callback, db_session, state)

    @router.message(BettingStates.waiting_amount)
    async def stake_amount_handler(
        message: Message,
        db_session: AsyncSession,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        await handle_bet_amount_message(
            message=message,
            session=db_session,
            state=state,
            bot=bot,
            mini_app_url=mini_app_url,
        )

    return router


async def handle_bet_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    del session

    try:
        market_id, option_index = parse_bet_callback_data(callback.data)
    except ValueError:
        await _answer_callback(callback, "This bet button is invalid.", show_alert=True)
        return

    await state.clear()
    await state.update_data(market_id=market_id, option_index=option_index)
    await state.set_state(BettingStates.waiting_amount)
    await _answer_callback(callback)

    prompt = "Send your stake amount in Stars. It must be at least the market minimum."
    if callback.message is not None:
        try:
            await callback.message.answer(prompt)
        except Exception as exc:
            logger.exception(
                "Failed to prompt for stake amount: market_id=%d user_id=%s",
                market_id,
                callback.from_user.id if callback.from_user else None,
            )
            raise BettingProviderError("Failed to prompt for stake amount") from exc


async def handle_bet_amount_message(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    mini_app_url: str | None = None,
) -> None:
    if message.from_user is None:
        await message.answer("Could not identify you for this bet.")
        return

    try:
        amount = parse_stake_amount(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    market_id = data.get("market_id")
    option_index = data.get("option_index")
    if not isinstance(market_id, int) or not isinstance(option_index, int):
        await state.clear()
        await message.answer("This bet session expired. Press the market button again.")
        return

    try:
        user, _is_new = await ensure_user(session, message.from_user)
        market = await _get_market_for_read(session, market_id)
        validation_error = validate_stake_invoice_request(user, market, option_index, amount)
        if validation_error is not None:
            await message.answer(_validation_message(validation_error, market))
            await state.clear()
            return

        await send_stake_invoice(
            bot=bot,
            chat_id=message.chat.id,
            user_id=user.telegram_id,
            market=market,
            option_index=option_index,
            stars_amount=amount,
        )
    except (UserModuleError, DatabaseLayerError, BettingModuleError) as exc:
        logger.exception(
            "Could not start stake invoice: user_id=%s market_id=%s",
            message.from_user.id,
            market_id,
        )
        await message.answer("Could not create the Stars invoice. Please try again.")
        raise BettingProviderError("Could not create stake invoice") from exc
    finally:
        await state.clear()

    logger.info(
        "Stake invoice sent: user_id=%d market_id=%d option_index=%d stars=%d",
        message.from_user.id,
        market_id,
        option_index,
        amount,
    )


async def send_stake_invoice(
    bot: Bot,
    chat_id: int,
    user_id: int,
    market: Market,
    option_index: int,
    stars_amount: int,
) -> Message:
    _require_int(chat_id, "chat_id")
    _require_positive_int(user_id, "user_id")
    _require_positive_int(stars_amount, "stars_amount")
    if option_index < 0 or option_index >= len(market.options):
        raise PaymentValidationError("option_index must reference an existing option")

    payload = build_stake_invoice_payload(
        user_id=user_id,
        market_id=market.id,
        option_index=option_index,
        stars_amount=stars_amount,
    )
    description = f"{stars_amount} Stars on {market.options[option_index]} for market #{market.id}"
    try:
        return await bot.send_invoice(
            chat_id=chat_id,
            title=PAYMENT_TITLE,
            description=description,
            payload=payload,
            provider_token="",
            currency=STARS_CURRENCY,
            prices=[LabeledPrice(label=f"{stars_amount} Stars", amount=stars_amount)],
        )
    except Exception as exc:
        logger.exception(
            "Failed to send stake invoice: user_id=%d market_id=%d stars=%d",
            user_id,
            market.id,
            stars_amount,
        )
        raise PaymentProviderError("Failed to send stake invoice") from exc


async def validate_stake_pre_checkout(
    session: AsyncSession,
    payload: dict[str, int],
    payer_id: int | None,
    currency: str,
    total_amount: int,
) -> BetValidationError | None:
    if currency != STARS_CURRENCY:
        raise PaymentValidationError("payment currency must be XTR")
    if total_amount != payload["stars_amount"]:
        raise PaymentValidationError("payment amount does not match invoice payload")
    if payer_id is not None and payer_id != payload["user_id"]:
        raise PaymentValidationError("payment user does not match invoice payload")

    user = await _get_user_for_read(session, payload["user_id"])
    market = await _get_market_for_read(session, payload["market_id"])
    return validate_stake_invoice_request(
        user=user,
        market=market,
        option_index=payload["option_index"],
        stars_amount=payload["stars_amount"],
    )


async def handle_successful_stake_payment(
    message: Message,
    session: AsyncSession,
    payload: dict[str, int],
    platform_fee_pct: float = 0.08,
) -> BetResult | None:
    payment = message.successful_payment
    if payment is None:
        raise PaymentValidationError("message.successful_payment is required")

    user_id = payload["user_id"]
    market_id = payload["market_id"]
    option_index = payload["option_index"]
    stars_amount = payload["stars_amount"]
    charge_id = payment.telegram_payment_charge_id
    _require_text(charge_id, "charge_id")

    try:
        if message.from_user is not None:
            await ensure_user(session, message.from_user)

        newly_recorded = await _record_stake_payment(
            session=session,
            user_id=user_id,
            stars_amount=stars_amount,
            charge_id=charge_id,
        )
        if not newly_recorded:
            logger.info(
                "Skipping already processed stake payment: user_id=%d market_id=%d charge_id=%s",
                user_id,
                market_id,
                _redact_charge_id(charge_id),
            )
            return None

        result = await place_bet(
            session=session,
            user_id=user_id,
            market_id=market_id,
            option_index=option_index,
            credits_amount=stars_amount,
        )
        market = await _get_market_for_read(session, market_id)
        try:
            await update_market_card_for_bet(
                bot=message.bot,
                market=market,
                pool_by_option=result.pool_by_option,
            )
        except Exception:
            logger.exception(
                "Stake was recorded but market card update failed: market_id=%d",
                market_id,
            )
        if result.bet is not None:
            try:
                from bot.notifications import notify_bet_confirmed

                await notify_bet_confirmed(
                    bot=message.bot,
                    user_id=user_id,
                    bet=result.bet,
                    market=market,
                    new_balance=result.new_balance,
                    estimated_payout=_estimate_payout_from_recorded_pool(
                        stars_amount,
                        option_index,
                        result.pool_by_option,
                        platform_fee_pct,
                    ),
                )
            except Exception:
                logger.exception(
                    "Stake was recorded but bet notification failed: market_id=%d user_id=%d",
                    market_id,
                    user_id,
                )
    except (DatabaseLayerError, UserModuleError, SQLAlchemyError) as exc:
        logger.exception(
            "Could not process successful stake payment: user_id=%d market_id=%d charge_id=%s",
            user_id,
            market_id,
            _redact_charge_id(charge_id),
        )
        raise BettingPersistenceError("Could not process successful stake payment") from exc

    try:
        await message.answer(f"Bet placed: {stars_amount} Stars.")
    except Exception:
        logger.exception(
            "Failed to send stake confirmation: user_id=%d market_id=%d",
            user_id,
            market_id,
        )
    return result


def _estimate_payout_from_recorded_pool(
    bet_amount: int,
    option_index: int,
    pool_by_option: dict[int, int],
    platform_fee_pct: float,
) -> int:
    pool_before = dict(pool_by_option)
    pool_before[option_index] = max(pool_before.get(option_index, 0) - bet_amount, 0)
    return estimate_payout(bet_amount, option_index, pool_before, platform_fee_pct)


async def place_bet(
    session: AsyncSession,
    user_id: int,
    market_id: int,
    option_index: int,
    credits_amount: int,
) -> BetResult:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(market_id, "market_id")
    _require_positive_int(credits_amount, "credits_amount")

    try:
        user = await _get_user_for_update(session, user_id)
        market = await _get_market_for_update(session, market_id)
        validation_error = validate_bet_request(user, market, option_index, credits_amount)
        if validation_error is not None:
            raise ValueError(validation_error.value)

        await update_user_balance(
            session=session,
            telegram_id=user_id,
            delta=-credits_amount,
            reason=f"market_stake:{market_id}",
        )
        bet = await create_bet(
            session=session,
            user_id=user_id,
            market_id=market_id,
            option_index=option_index,
            credits_amount=credits_amount,
        )
        pool_by_option = await get_pool_by_option(session, market_id)
        new_balance = user.balance_credits
    except InsufficientBalanceError as exc:
        raise ValueError(BetValidationError.INSUFFICIENT_BALANCE.value) from exc
    except DuplicateRecordError as exc:
        raise ValueError(BetValidationError.ALREADY_BET.value) from exc
    except DatabaseLayerError:
        raise
    except SQLAlchemyError as exc:
        logger.exception("Bet placement failed with SQLAlchemy error: market_id=%d", market_id)
        raise BettingPersistenceError("Bet placement failed") from exc

    logger.info(
        "Bet placed: bet_id=%d user_id=%d market_id=%d option_index=%d stars=%d",
        bet.id,
        user_id,
        market_id,
        option_index,
        credits_amount,
    )
    return BetResult(success=True, bet=bet, new_balance=new_balance, pool_by_option=pool_by_option)


def validate_bet_request(
    user: User,
    market: Market,
    option_index: int,
    credits_amount: int,
) -> BetValidationError | None:
    invoice_error = validate_stake_invoice_request(user, market, option_index, credits_amount)
    if invoice_error is not None:
        return invoice_error
    if user.balance_credits < credits_amount:
        return BetValidationError.INSUFFICIENT_BALANCE
    return None


def validate_stake_invoice_request(
    user: User,
    market: Market,
    option_index: int,
    stars_amount: int,
) -> BetValidationError | None:
    if market.status != MarketStatus.ACTIVE or _is_past_deadline(market.deadline):
        return BetValidationError.MARKET_CLOSED
    if market.creator_id == user.telegram_id:
        return BetValidationError.CREATOR_CANNOT_BET
    if option_index < 0 or option_index >= len(market.options):
        return BetValidationError.INVALID_OPTION
    if stars_amount < market.min_bet:
        return BetValidationError.BELOW_MIN_BET
    return None


def calculate_implied_probability(pool_by_option: dict[int, int]) -> dict[int, float]:
    total_pool = sum(amount for amount in pool_by_option.values() if amount > 0)
    if total_pool <= 0:
        return {option_index: 0.0 for option_index in pool_by_option}
    return {
        option_index: max(amount, 0) / total_pool
        for option_index, amount in pool_by_option.items()
    }


def estimate_payout(
    bet_amount: int,
    option_index: int,
    pool_by_option: dict[int, int],
    platform_fee_pct: float,
) -> int:
    _require_positive_int(bet_amount, "bet_amount")
    if option_index < 0:
        raise ValueError("option_index must be non-negative")
    if platform_fee_pct < 0 or platform_fee_pct >= 1:
        raise ValueError("platform_fee_pct must be in the range [0, 1)")

    pool_with_bet = dict(pool_by_option)
    pool_with_bet[option_index] = pool_with_bet.get(option_index, 0) + bet_amount
    total_pool = sum(pool_with_bet.values())
    winning_pool = pool_with_bet.get(option_index, 0)
    if total_pool <= 0 or winning_pool <= 0:
        return 0

    distributable_pool = int(total_pool * (1 - platform_fee_pct))
    return int((bet_amount / winning_pool) * distributable_pool)


async def update_market_card_for_bet(
    bot: Bot,
    market: Market,
    pool_by_option: dict[int, int],
    mini_app_url: str | None = None,
) -> None:
    if market.inline_message_id:
        await update_inline_market_card(
            bot=bot,
            inline_message_id=market.inline_message_id,
            market=market,
            pool_by_option=pool_by_option,
            mini_app_url=mini_app_url,
        )
        return

    if market.message_id is None:
        logger.info("Skipping market card update without message id: market_id=%d", market.id)
        return

    await update_market_card(
        bot=bot,
        chat_id=market.chat_id,
        message_id=market.message_id,
        market=market,
        pool_by_option=pool_by_option,
        mini_app_url=mini_app_url,
    )


def build_stake_invoice_payload(
    user_id: int,
    market_id: int,
    option_index: int,
    stars_amount: int,
) -> str:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(market_id, "market_id")
    if not isinstance(option_index, int) or isinstance(option_index, bool) or option_index < 0:
        raise PaymentValidationError("option_index must be a non-negative integer")
    _require_positive_int(stars_amount, "stars_amount")
    return json.dumps(
        {
            "t": PAYLOAD_TYPE_MARKET_STAKE,
            "u": user_id,
            "m": market_id,
            "o": option_index,
            "a": stars_amount,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_stake_invoice_payload(payload: str) -> dict[str, int]:
    _require_text(payload, "payload")
    try:
        raw_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PaymentValidationError("invoice payload is not valid JSON") from exc

    if not isinstance(raw_payload, Mapping):
        raise PaymentValidationError("invoice payload must be a JSON object")
    if raw_payload.get("t") != PAYLOAD_TYPE_MARKET_STAKE:
        raise PaymentValidationError("invoice payload type is unsupported")

    return {
        "user_id": _require_positive_int(raw_payload.get("u"), "payload.user_id"),
        "market_id": _require_positive_int(raw_payload.get("m"), "payload.market_id"),
        "option_index": _require_non_negative_int(raw_payload.get("o"), "payload.option_index"),
        "stars_amount": _require_positive_int(raw_payload.get("a"), "payload.stars_amount"),
    }


def parse_bet_callback_data(callback_data: str | None) -> tuple[int, int]:
    if not isinstance(callback_data, str):
        raise ValueError("callback data is required")
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != BET_CALLBACK_PREFIX:
        raise ValueError("callback data must have format bet:{market_id}:{option_index}")
    market_id = _require_positive_int(_parse_int(parts[1], "market_id"), "market_id")
    option_index = _require_non_negative_int(_parse_int(parts[2], "option_index"), "option_index")
    return market_id, option_index


def parse_stake_amount(value: str | None) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Send a whole-number stake amount in Stars.")
    try:
        amount = int(value.strip())
    except ValueError as exc:
        raise ValueError("Stake amount must be a whole number of Stars.") from exc
    if amount < 1:
        raise ValueError("Stake amount must be at least 1 Star.")
    return amount


async def _record_stake_payment(
    session: AsyncSession,
    user_id: int,
    stars_amount: int,
    charge_id: str,
) -> bool:
    from bot.crud import confirm_deposit, create_deposit, get_deposit_by_charge_id

    existing = await get_deposit_by_charge_id(session, charge_id, for_update=True)
    if existing is not None and existing.status == DepositStatus.CONFIRMED:
        return False

    if existing is not None:
        if existing.user_id != user_id or existing.stars_amount != stars_amount:
            raise BettingPersistenceError("Existing payment charge_id payload mismatch")
    else:
        await create_deposit(session, user_id, stars_amount, charge_id)

    await update_user_balance(
        session=session,
        telegram_id=user_id,
        delta=stars_amount,
        reason="stars_stake_payment_confirmed",
    )
    await confirm_deposit(session, charge_id)
    return True


async def _get_user_for_read(session: AsyncSession, user_id: int) -> User:
    stmt = select(User).where(User.telegram_id == user_id)
    user = await session.scalar(stmt)
    if user is None:
        raise RecordNotFoundError(f"User {user_id} was not found")
    return user


async def _get_user_for_update(session: AsyncSession, user_id: int) -> User:
    stmt = select(User).where(User.telegram_id == user_id).with_for_update()
    user = await session.scalar(stmt)
    if user is None:
        raise RecordNotFoundError(f"User {user_id} was not found")
    return user


async def _get_market_for_read(session: AsyncSession, market_id: int) -> Market:
    stmt = select(Market).where(Market.id == market_id)
    market = await session.scalar(stmt)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")
    return market


async def _get_market_for_update(session: AsyncSession, market_id: int) -> Market:
    stmt = select(Market).where(Market.id == market_id).with_for_update()
    market = await session.scalar(stmt)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")
    return market


async def _answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception as exc:
        logger.exception("Failed to answer bet callback")
        raise BettingProviderError("Failed to answer bet callback") from exc


def _validation_message(error: BetValidationError, market: Market | None = None) -> str:
    if error == BetValidationError.MARKET_CLOSED:
        return "This market is closed."
    if error == BetValidationError.CREATOR_CANNOT_BET:
        return "Market creators cannot bet on their own markets."
    if error == BetValidationError.BELOW_MIN_BET:
        minimum = f" Minimum is {market.min_bet} Stars." if market is not None else ""
        return f"Stake is below the market minimum.{minimum}"
    if error == BetValidationError.INVALID_OPTION:
        return "This market option is invalid."
    if error == BetValidationError.ALREADY_BET:
        return "You already have a bet on this market."
    return "Could not place this bet."


def _is_past_deadline(deadline: datetime) -> bool:
    return deadline <= datetime.now(timezone.utc)


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _require_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PaymentValidationError(f"{name} must be an integer")
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PaymentValidationError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PaymentValidationError(f"{name} must be a non-negative integer")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaymentValidationError(f"{name} must be non-empty text")
    return value.strip()


def _redact_charge_id(charge_id: str) -> str:
    if len(charge_id) <= 8:
        return "***"
    return f"{charge_id[:4]}...{charge_id[-4:]}"
