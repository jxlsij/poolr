from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import (
    DatabaseLayerError,
    DuplicateRecordError,
    RecordNotFoundError,
    create_ledger_entry,
    create_payout,
    get_bets_for_market,
    get_pool_by_option,
    update_market_status,
    update_user_balance,
)
from bot.handlers.markets import build_market_card_text
from bot.market_cards import update_market_card_photo
from bot.models import Bet, LedgerEntryType, Market, MarketStatus, Payout, PayoutStatus


logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")

RESOLVE_CALLBACK_PREFIX = "resolve"
RESOLUTION_GRACE_PERIOD = timedelta(hours=24)
PAYOUT_HOLD_PERIOD = timedelta(hours=24)


class ResolutionModuleError(RuntimeError):
    """Base error for Module 8 resolution operations."""


class ResolutionValidationError(ResolutionModuleError, ValueError):
    """Raised when a market cannot be resolved with the requested input."""


class ResolutionPersistenceError(ResolutionModuleError):
    """Raised when resolution cannot be persisted."""


class ResolutionProviderError(ResolutionModuleError):
    """Raised when Telegram APIs cannot publish resolution updates."""


@dataclass(frozen=True)
class ResolutionResult:
    market: Market
    payouts: list[Payout]
    platform_fee_collected: int
    total_participants: int
    pool_by_option: dict[int, int]


def resolution_operation(
    operation_name: str,
    wrapped_error: type[ResolutionModuleError] = ResolutionModuleError,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger.debug("Resolution operation started: %s", operation_name)
            try:
                result = await func(*args, **kwargs)
            except ResolutionValidationError:
                logger.warning("Resolution operation rejected input: %s", operation_name, exc_info=True)
                raise
            except wrapped_error:
                logger.warning("Resolution operation failed: %s", operation_name, exc_info=True)
                raise
            except ResolutionModuleError:
                logger.warning("Resolution operation failed: %s", operation_name, exc_info=True)
                raise
            except DatabaseLayerError as exc:
                logger.exception("Resolution operation hit database error: %s", operation_name)
                raise wrapped_error(f"Resolution operation failed: {operation_name}") from exc
            except (ValueError, TypeError):
                logger.warning("Resolution operation rejected invalid input: %s", operation_name, exc_info=True)
                raise
            except Exception as exc:
                logger.exception("Resolution operation failed unexpectedly: %s", operation_name)
                raise wrapped_error(
                    f"Unexpected resolution operation failure: {operation_name}"
                ) from exc

            logger.debug("Resolution operation completed: %s", operation_name)
            return result

        return wrapper

    return decorator


def create_resolution_router(
    platform_fee_pct: float,
    mini_app_url: str | None = None,
) -> Router:
    router = Router(name="resolution")

    @router.callback_query(F.data.startswith(f"{RESOLVE_CALLBACK_PREFIX}:"))
    async def resolve_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
        bot: Bot,
    ) -> None:
        await handle_resolve_callback(
            callback=callback,
            session=db_session,
            bot=bot,
            platform_fee_pct=platform_fee_pct,
            mini_app_url=mini_app_url,
        )

    return router


@resolution_operation("notify_creator_for_resolution", ResolutionProviderError)
async def notify_creator_for_resolution(bot: Bot, market: Market) -> None:
    _require_market_id(market)
    logger.info(
        "Notifying creator for market resolution: market_id=%d creator_id=%d",
        market.id,
        market.creator_id,
    )
    try:
        await bot.send_message(
            chat_id=market.creator_id,
            text=(
                f"Market #{market.id} is ready to resolve.\n\n"
                f"{market.question}\n\n"
                "Choose the winning outcome."
            ),
            reply_markup=build_resolution_keyboard(market.id, market.options),
        )
    except Exception as exc:
        logger.exception("Failed to notify creator for resolution: market_id=%d", market.id)
        raise ResolutionProviderError("Failed to notify creator for resolution") from exc


def build_resolution_keyboard(market_id: int, options: list[str]) -> InlineKeyboardMarkup:
    _require_positive_int(market_id, "market_id")
    if not isinstance(options, list) or not options:
        raise ValueError("options must be a non-empty list")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=option,
                    callback_data=f"{RESOLVE_CALLBACK_PREFIX}:{market_id}:{index}",
                )
            ]
            for index, option in enumerate(options)
        ]
    )


async def handle_resolve_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    platform_fee_pct: float,
    mini_app_url: str | None = None,
) -> None:
    try:
        market_id, winning_option_index = parse_resolve_callback_data(callback.data)
    except ValueError:
        await _answer_callback(callback, "This resolution button is invalid.", show_alert=True)
        return

    resolved_by = callback.from_user.id if callback.from_user else None
    if resolved_by is None:
        await _answer_callback(callback, "Could not identify resolver.", show_alert=True)
        return

    try:
        result = await resolve_market(
            session=session,
            market_id=market_id,
            winning_option_index=winning_option_index,
            resolved_by=resolved_by,
            platform_fee_pct=platform_fee_pct,
        )
    except ResolutionValidationError as exc:
        logger.warning(
            "Resolution rejected: market_id=%d user_id=%s reason=%s",
            market_id,
            resolved_by,
            exc,
        )
        await _answer_callback(callback, str(exc), show_alert=True)
        return
    except (DatabaseLayerError, ResolutionModuleError) as exc:
        logger.exception("Resolution failed: market_id=%d user_id=%s", market_id, resolved_by)
        await _answer_callback(callback, "Could not resolve this market.", show_alert=True)
        raise ResolutionPersistenceError("Resolution failed") from exc
    except Exception as exc:
        logger.exception(
            "Unexpected resolution callback failure: market_id=%d user_id=%s",
            market_id,
            resolved_by,
        )
        await _answer_callback(callback, "Could not resolve this market.", show_alert=True)
        raise ResolutionPersistenceError("Unexpected resolution callback failure") from exc

    try:
        await publish_resolution_results(
            bot=bot,
            market=result.market,
            payouts=result.payouts,
            pool_by_option=result.pool_by_option,
            platform_fee=result.platform_fee_collected,
            mini_app_url=mini_app_url,
        )
    except ResolutionModuleError:
        logger.exception("Resolution was persisted but publishing failed: market_id=%d", market_id)

    await _notify_payouts_best_effort(bot, result.market, result.payouts)
    await _answer_callback(callback, "Market resolved.")


@resolution_operation("resolve_market", ResolutionPersistenceError)
async def resolve_market(
    session: AsyncSession,
    market_id: int,
    winning_option_index: int,
    resolved_by: int,
    platform_fee_pct: float,
) -> ResolutionResult:
    _require_positive_int(market_id, "market_id")
    _require_positive_int(resolved_by, "resolved_by")
    _validate_platform_fee_pct(platform_fee_pct)

    market = await _get_market_for_update(session, market_id)
    _validate_resolution_request(market, winning_option_index, resolved_by)

    pool_by_option = await get_pool_by_option(session, market_id)
    bets = await get_bets_for_market(session, market_id, for_update=True)
    logger.info(
        "Resolving market: market_id=%d winner=%d resolver=%d total_pool=%d bets=%d",
        market_id,
        winning_option_index,
        resolved_by,
        sum(pool_by_option.values()),
        len(bets),
    )
    payouts, platform_fee_collected = await distribute_payouts(
        session=session,
        market=market,
        winning_option_index=winning_option_index,
        platform_fee_pct=platform_fee_pct,
        bets=bets,
    )
    market = await update_market_status(
        session=session,
        market_id=market_id,
        status=MarketStatus.RESOLVED,
        winning_option=winning_option_index,
    )

    logger.info(
        "Resolved market: market_id=%d winner=%d payouts=%d platform_fee=%d participants=%d",
        market_id,
        winning_option_index,
        len(payouts),
        platform_fee_collected,
        len(bets),
    )
    return ResolutionResult(
        market=market,
        payouts=payouts,
        platform_fee_collected=platform_fee_collected,
        total_participants=len(bets),
        pool_by_option=pool_by_option,
    )


@resolution_operation("distribute_payouts", ResolutionPersistenceError)
async def distribute_payouts(
    session: AsyncSession,
    market: Market,
    winning_option_index: int,
    platform_fee_pct: float,
    bets: list[Bet] | None = None,
) -> tuple[list[Payout], int]:
    _require_market_id(market)
    _validate_platform_fee_pct(platform_fee_pct)
    if winning_option_index < 0 or winning_option_index >= len(market.options):
        raise ResolutionValidationError("Winning option is invalid.")

    market_bets = (
        bets
        if bets is not None
        else await get_bets_for_market(session, market.id, for_update=True)
    )
    total_pool = sum(bet.credits_amount for bet in market_bets)
    winning_bets = [bet for bet in market_bets if bet.option_index == winning_option_index]
    winning_total = sum(bet.credits_amount for bet in winning_bets)
    losing_total = total_pool - winning_total
    platform_fee_collected = int(losing_total * platform_fee_pct)
    logger.info(
        "Distributing market payouts: market_id=%d winning_option=%d total_pool=%d winning_total=%d losing_total=%d platform_fee=%d",
        market.id,
        winning_option_index,
        total_pool,
        winning_total,
        losing_total,
        platform_fee_collected,
    )

    if not winning_bets or total_pool <= 0 or winning_total <= 0:
        logger.info(
            "Resolved market with no winning payouts: market_id=%d total_pool=%d winning_total=%d",
            market.id,
            total_pool,
            winning_total,
        )
        return [], total_pool

    payouts: list[Payout] = []
    for bet in winning_bets:
        stars_won = calculate_winner_share(
            user_bet=bet.credits_amount,
            winning_side_total=winning_total,
            total_pool=total_pool,
            platform_fee_pct=platform_fee_pct,
        )
        payout = await create_payout(
            session=session,
            user_id=bet.user_id,
            market_id=market.id,
            credits_won=stars_won,
            available_at=datetime.now(timezone.utc) + PAYOUT_HOLD_PERIOD,
        )
        await create_ledger_entry(
            session=session,
            user_id=bet.user_id,
            amount=stars_won,
            entry_type=LedgerEntryType.PAYOUT_HOLD,
            source_table="payouts",
            source_id=payout.id,
            idempotency_key=f"payout_hold:{payout.id}",
            metadata={"market_id": market.id},
        )
        payouts.append(payout)
        logger.info(
            "Winner payout held: market_id=%d user_id=%d stake=%d payout=%d available_at=%s",
            market.id,
            bet.user_id,
            bet.credits_amount,
            stars_won,
            payout.available_at,
        )

    if platform_fee_collected:
        try:
            await create_ledger_entry(
                session=session,
                user_id=None,
                amount=platform_fee_collected,
                entry_type=LedgerEntryType.PLATFORM_FEE,
                source_table="markets",
                source_id=market.id,
                idempotency_key=f"platform_fee:{market.id}:{winning_option_index}",
                metadata={"winning_option": winning_option_index},
            )
        except DuplicateRecordError:
            logger.info("Platform fee ledger entry already exists: market_id=%d", market.id)

    return payouts, platform_fee_collected


@resolution_operation("release_available_payouts", ResolutionPersistenceError)
async def release_available_payouts(
    session: AsyncSession,
    now: datetime | None = None,
) -> int:
    release_now = now or datetime.now(timezone.utc)
    stmt = (
        select(Payout)
        .join(Market, Market.id == Payout.market_id)
        .where(
            Payout.status == PayoutStatus.HELD,
            Payout.available_at <= release_now,
            Market.status == MarketStatus.RESOLVED,
        )
        .with_for_update()
        .order_by(Payout.available_at.asc(), Payout.id.asc())
    )
    payouts = list((await session.scalars(stmt)).all())
    released = 0
    for payout in payouts:
        if payout.credits_won <= 0:
            payout.status = PayoutStatus.RELEASED
            payout.released_at = release_now
            continue
        await update_user_balance(
            session=session,
            telegram_id=payout.user_id,
            delta=payout.credits_won,
            reason=f"market_payout_released:{payout.market_id}:{payout.id}",
        )
        payout.status = PayoutStatus.RELEASED
        payout.released_at = release_now
        released += 1
    if released:
        await session.flush()
    logger.info("Released available payout holds: count=%d", released)
    return released


def calculate_winner_share(
    user_bet: int,
    winning_side_total: int,
    total_pool: int,
    platform_fee_pct: float,
) -> int:
    _require_positive_int(user_bet, "user_bet")
    _require_positive_int(winning_side_total, "winning_side_total")
    _require_positive_int(total_pool, "total_pool")
    _validate_platform_fee_pct(platform_fee_pct)
    if user_bet > winning_side_total:
        raise ValueError("user_bet cannot exceed winning_side_total")

    losing_pool = max(total_pool - winning_side_total, 0)
    distributable_losing_pool = int(losing_pool * (1 - platform_fee_pct))
    winnings = int((user_bet / winning_side_total) * distributable_losing_pool)
    return user_bet + winnings


@resolution_operation("auto_cancel_market", ResolutionPersistenceError)
async def auto_cancel_market(
    session: AsyncSession,
    bot: Bot,
    market: Market,
    mini_app_url: str | None = None,
) -> None:
    _require_market_id(market)
    if market.status != MarketStatus.ACTIVE:
        logger.debug(
            "Skipping auto-cancel for inactive market: market_id=%d status=%s",
            market.id,
            market.status.value,
        )
        return
    if market.deadline + RESOLUTION_GRACE_PERIOD > datetime.now(timezone.utc):
        logger.debug("Skipping auto-cancel before grace period: market_id=%d", market.id)
        return

    bets = await get_bets_for_market(session, market.id, for_update=True)
    logger.info("Auto-cancelling market: market_id=%d bets=%d", market.id, len(bets))
    for bet in bets:
        await update_user_balance(
            session=session,
            telegram_id=bet.user_id,
            delta=bet.credits_amount,
            reason=f"market_cancel_refund:{market.id}",
        )
        logger.info(
            "Refunded cancelled market stake: market_id=%d user_id=%d stars=%d",
            market.id,
            bet.user_id,
            bet.credits_amount,
        )

    market = await update_market_status(session, market.id, MarketStatus.CANCELLED)
    pool_by_option = await get_pool_by_option(session, market.id)
    await _update_market_card_best_effort(
        bot=bot,
        market=market,
        pool_by_option=pool_by_option,
        mini_app_url=mini_app_url,
    )
    logger.info("Auto-cancelled market and refunded stakes: market_id=%d bets=%d", market.id, len(bets))


@resolution_operation("publish_resolution_results", ResolutionProviderError)
async def publish_resolution_results(
    bot: Bot,
    market: Market,
    payouts: list[Payout],
    pool_by_option: dict[int, int],
    platform_fee: int,
    mini_app_url: str | None = None,
) -> None:
    await _update_market_card_best_effort(
        bot=bot,
        market=market,
        pool_by_option=pool_by_option,
        mini_app_url=mini_app_url,
    )
    if market.chat_id == 0:
        logger.info("Skipping group resolution post for inline market: market_id=%d", market.id)
        return

    try:
        logger.info(
            "Publishing resolution result post: market_id=%d chat_id=%d payouts=%d platform_fee=%d",
            market.id,
            market.chat_id,
            len(payouts),
            platform_fee,
        )
        await bot.send_message(
            chat_id=market.chat_id,
            text=build_results_text(market, market.winning_option, payouts, platform_fee),
        )
    except Exception as exc:
        logger.exception("Failed to publish resolution post: market_id=%d", market.id)
        raise ResolutionProviderError("Failed to publish resolution post") from exc


async def _notify_payouts_best_effort(bot: Bot, market: Market, payouts: list[Payout]) -> None:
    if not payouts:
        logger.info("No winner payout notifications to send: market_id=%d", market.id)
        return

    try:
        from bot.notifications import notify_payout_received
    except Exception:
        logger.exception("Could not import payout notification helper: market_id=%d", market.id)
        return

    for payout in payouts:
        try:
            await notify_payout_received(bot=bot, user_id=payout.user_id, payout=payout, market=market)
        except Exception:
            logger.exception(
                "Resolution persisted but payout notification failed: market_id=%d user_id=%d payout_id=%d",
                market.id,
                payout.user_id,
                payout.id,
            )


def build_results_text(
    market: Market,
    winning_option_index: int | None,
    payouts: list[Payout],
    platform_fee: int,
) -> str:
    if winning_option_index is None:
        winning_label = "Not set"
    elif 0 <= winning_option_index < len(market.options):
        winning_label = market.options[winning_option_index]
    else:
        winning_label = "Unknown"

    total_paid = sum(payout.credits_won for payout in payouts)
    lines = [
        f"Market #{market.id} resolved",
        "",
        market.question,
        "",
        f"Winning outcome: {winning_label}",
        f"Paid to winners: {total_paid} Stars",
        f"Platform fee: {platform_fee} Stars",
    ]

    if payouts:
        lines.extend(["", "Top winners:"])
        sorted_payouts = sorted(payouts, key=lambda item: item.credits_won, reverse=True)
        for index, payout in enumerate(sorted_payouts[:3], start=1):
            lines.append(f"{index}. User {payout.user_id}: {payout.credits_won} Stars")
    else:
        lines.extend(["", "No winning bets were placed."])

    return "\n".join(lines)


def build_resolved_market_card_text(
    market: Market,
    pool_by_option: dict[int, int],
) -> str:
    base_text = build_market_card_text(market, pool_by_option)
    if market.status == MarketStatus.RESOLVED and market.winning_option is not None:
        winning_label = market.options[market.winning_option]
        return f"{base_text}\n\nResolved: {winning_label}"
    if market.status == MarketStatus.CANCELLED:
        return f"{base_text}\n\nCancelled: stakes refunded."
    return base_text


def parse_resolve_callback_data(callback_data: str | None) -> tuple[int, int]:
    if not isinstance(callback_data, str):
        raise ValueError("callback data is required")
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != RESOLVE_CALLBACK_PREFIX:
        raise ValueError("callback data must have format resolve:{market_id}:{option_index}")
    return (
        _require_positive_int(_parse_int(parts[1], "market_id"), "market_id"),
        _require_non_negative_int(_parse_int(parts[2], "winning_option_index"), "winning_option_index"),
    )


async def _update_market_card_best_effort(
    bot: Bot,
    market: Market,
    pool_by_option: dict[int, int],
    mini_app_url: str | None = None,
) -> None:
    del mini_app_url
    try:
        await update_market_card_photo(
            bot,
            inline_message_id=market.inline_message_id,
            chat_id=market.chat_id,
            message_id=market.message_id,
            market=market,
            pool_by_option=pool_by_option,
            reply_markup=_build_post_resolution_keyboard(market),
            fallback_text=build_resolved_market_card_text(market, pool_by_option),
        )
    except Exception:
        logger.exception("Failed to update resolved market card: market_id=%d", market.id)


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
        logger.exception("Failed to answer resolve callback")
        raise ResolutionProviderError("Failed to answer resolve callback") from exc


def _validate_resolution_request(
    market: Market,
    winning_option_index: int,
    resolved_by: int,
) -> None:
    if market.creator_id != resolved_by:
        raise ResolutionValidationError("Only the market creator can resolve this market.")
    if market.status != MarketStatus.ACTIVE:
        raise ResolutionValidationError("This market is not active.")
    if market.deadline > datetime.now(timezone.utc):
        raise ResolutionValidationError("This market is not past its deadline yet.")
    if winning_option_index < 0 or winning_option_index >= len(market.options):
        raise ResolutionValidationError("Winning option is invalid.")


def _build_post_resolution_keyboard(market: Market) -> InlineKeyboardMarkup | None:
    if market.status != MarketStatus.RESOLVED:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Dispute", callback_data=f"dispute:{market.id}")]
        ]
    )


def _validate_platform_fee_pct(platform_fee_pct: float) -> None:
    if not isinstance(platform_fee_pct, int | float) or isinstance(platform_fee_pct, bool):
        raise TypeError("platform_fee_pct must be a number")
    if platform_fee_pct < 0 or platform_fee_pct >= 1:
        raise ValueError("platform_fee_pct must be in the range [0, 1)")


def _require_market_id(market: Market) -> None:
    if not isinstance(market.id, int) or market.id < 1:
        raise ValueError("market.id must be a positive integer")


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
