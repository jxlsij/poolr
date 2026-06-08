from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.crud import DatabaseLayerError, get_pool_by_option
from bot.database import session_scope
from bot.handlers.markets import build_market_card_text, build_market_keyboard
from bot.market_cards import update_market_card_photo
from bot.models import Bet, Market, MarketStatus, NotificationLog, Payout
from bot.resolution import auto_cancel_market, build_resolution_keyboard


logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")

DEADLINE_APPROACHING_WINDOW = timedelta(hours=1)
NOTIFICATION_WORKER_INTERVAL_SECONDS = 300


class NotificationKind(StrEnum):
    DEADLINE_APPROACHING = "deadline_approaching"
    MARKET_CLOSED = "market_closed"
    MARKET_AUTO_CANCELLED = "market_auto_cancelled"


class NotificationModuleError(RuntimeError):
    """Base error for Module 11 notification and scheduler operations."""


class NotificationValidationError(NotificationModuleError, ValueError):
    """Raised when a notification input is invalid."""


class NotificationPersistenceError(NotificationModuleError):
    """Raised when notification state cannot be persisted."""


class NotificationProviderError(NotificationModuleError):
    """Raised when Telegram APIs cannot deliver a notification."""


@dataclass(frozen=True)
class ExpiryCheckResult:
    deadline_approaching_sent: int
    market_closed_sent: int
    auto_cancelled: int


def notification_operation(
    operation_name: str,
    wrapped_error: type[NotificationModuleError] = NotificationModuleError,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger.debug("Notification operation started: %s", operation_name)
            try:
                result = await func(*args, **kwargs)
            except NotificationValidationError:
                logger.warning("Notification operation rejected input: %s", operation_name, exc_info=True)
                raise
            except wrapped_error:
                logger.warning("Notification operation failed: %s", operation_name, exc_info=True)
                raise
            except NotificationModuleError:
                logger.warning("Notification operation failed: %s", operation_name, exc_info=True)
                raise
            except DatabaseLayerError as exc:
                logger.exception("Notification operation hit database error: %s", operation_name)
                raise wrapped_error(f"Notification operation failed: {operation_name}") from exc
            except (ValueError, TypeError):
                logger.warning("Notification operation rejected invalid input: %s", operation_name, exc_info=True)
                raise
            except Exception as exc:
                logger.exception("Notification operation failed unexpectedly: %s", operation_name)
                raise wrapped_error(f"Unexpected notification operation failure: {operation_name}") from exc

            logger.debug("Notification operation completed: %s", operation_name)
            return result

        return wrapper

    return decorator


async def schedule_market_jobs(scheduler: Any, market: Market) -> None:
    """Register nominal market jobs on an APScheduler-like object.

    The production MVP uses the polling worker below so jobs survive restarts via
    database state. This helper keeps the Module 11 API from the plan available
    for a future APScheduler swap.
    """

    _require_market_id(market)
    now = datetime.now(timezone.utc)
    jobs = [
        (
            NotificationKind.DEADLINE_APPROACHING.value,
            market.deadline - DEADLINE_APPROACHING_WINDOW,
            "notify_deadline_approaching",
        ),
        (NotificationKind.MARKET_CLOSED.value, market.deadline, "notify_market_closed"),
        (
            NotificationKind.MARKET_AUTO_CANCELLED.value,
            market.deadline + timedelta(hours=24),
            "auto_cancel_market",
        ),
    ]

    for kind, run_date, func_ref in jobs:
        if run_date <= now:
            logger.debug("Skipping past notification schedule: market_id=%d kind=%s", market.id, kind)
            continue
        scheduler.add_job(
            func_ref,
            trigger="date",
            run_date=run_date,
            id=f"market:{market.id}:{kind}",
            kwargs={"market_id": market.id},
            replace_existing=True,
        )
        logger.info("Scheduled market notification job: market_id=%d kind=%s run_date=%s", market.id, kind, run_date)


def start_notification_worker(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    mini_app_url: str | None = None,
    interval_seconds: int = NOTIFICATION_WORKER_INTERVAL_SECONDS,
) -> asyncio.Task[None]:
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be at least 1")

    logger.info("Starting notification worker: interval_seconds=%d", interval_seconds)
    return asyncio.create_task(
        _notification_worker_loop(
            bot=bot,
            session_factory=session_factory,
            mini_app_url=mini_app_url,
            interval_seconds=interval_seconds,
        ),
        name="poolr-notification-worker",
    )


async def stop_notification_worker(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return

    logger.info("Stopping notification worker")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.info("Notification worker stopped")


async def _notification_worker_loop(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    mini_app_url: str | None,
    interval_seconds: int,
) -> None:
    while True:
        try:
            async with session_scope(session_factory) as session:
                await run_expiry_check(session=session, bot=bot, mini_app_url=mini_app_url)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification worker tick failed")

        await asyncio.sleep(interval_seconds)


@notification_operation("run_expiry_check", NotificationPersistenceError)
async def run_expiry_check(
    session: AsyncSession,
    bot: Bot,
    mini_app_url: str | None = None,
) -> ExpiryCheckResult:
    now = datetime.now(timezone.utc)
    approaching_sent = 0
    closed_sent = 0
    auto_cancelled = 0

    approaching_markets = await _load_deadline_approaching_markets(session, now)
    for market in approaching_markets:
        try:
            if await _notification_was_sent(session, NotificationKind.DEADLINE_APPROACHING, market.id):
                continue
            pool_by_option = await get_pool_by_option(session, market.id)
            await notify_deadline_approaching(bot, market, pool_by_option)
            await _record_notification_sent(session, NotificationKind.DEADLINE_APPROACHING, market.id)
            approaching_sent += 1
        except Exception:
            logger.exception("Deadline-approaching notification failed: market_id=%d", market.id)
            await _rollback_after_item_failure(session, "deadline_approaching", market.id)

    closed_markets = await _load_market_closed_markets(session, now)
    for market in closed_markets:
        try:
            if not await _notification_was_sent(session, NotificationKind.MARKET_CLOSED, market.id):
                pool_by_option = await get_pool_by_option(session, market.id)
                await notify_market_closed(bot, market, pool_by_option, mini_app_url=mini_app_url)
                await _record_notification_sent(session, NotificationKind.MARKET_CLOSED, market.id)
                closed_sent += 1

            if market.deadline + timedelta(hours=24) <= now:
                if await _notification_was_sent(session, NotificationKind.MARKET_AUTO_CANCELLED, market.id):
                    continue
                await auto_cancel_market(session=session, bot=bot, market=market, mini_app_url=mini_app_url)
                await _record_notification_sent(session, NotificationKind.MARKET_AUTO_CANCELLED, market.id)
                auto_cancelled += 1
        except Exception:
            logger.exception("Market expiry handling failed: market_id=%d", market.id)
            await _rollback_after_item_failure(session, "market_expiry", market.id)

    logger.info(
        "Expiry check completed: approaching=%d closed=%d auto_cancelled=%d",
        approaching_sent,
        closed_sent,
        auto_cancelled,
    )
    return ExpiryCheckResult(
        deadline_approaching_sent=approaching_sent,
        market_closed_sent=closed_sent,
        auto_cancelled=auto_cancelled,
    )


@notification_operation("notify_deadline_approaching", NotificationProviderError)
async def notify_deadline_approaching(
    bot: Bot,
    market: Market,
    pool_by_option: dict[int, int] | None = None,
) -> None:
    _require_market_id(market)
    if market.chat_id == 0:
        logger.info("Skipping group deadline reminder for inline market: market_id=%d", market.id)
        return

    total_pool = sum((pool_by_option or {}).values())
    try:
        await bot.send_message(
            chat_id=market.chat_id,
            text=(
                f"Market #{market.id} closes in about 1 hour.\n\n"
                f"{market.question}\n\n"
                f"Current pool: {total_pool} Stars"
            ),
        )
    except Exception as exc:
        logger.exception("Failed to send deadline reminder: market_id=%d", market.id)
        raise NotificationProviderError("Failed to send deadline reminder") from exc

    logger.info("Deadline reminder sent: market_id=%d chat_id=%d", market.id, market.chat_id)


@notification_operation("notify_market_closed", NotificationProviderError)
async def notify_market_closed(
    bot: Bot,
    market: Market,
    pool_by_option: dict[int, int] | None = None,
    mini_app_url: str | None = None,
) -> None:
    _require_market_id(market)
    pool_by_option = pool_by_option or {}
    reply_markup = build_market_keyboard(
        market_id=market.id,
        options=market.options,
        status=MarketStatus.CLOSED,
        mini_app_url=mini_app_url,
    )
    text = build_closed_market_card_text(market, pool_by_option)

    card_updated = await _update_closed_market_card_best_effort(bot, market, text, reply_markup, pool_by_option)
    creator_notified = await _notify_creator_to_resolve_best_effort(bot, market)
    if not card_updated and not creator_notified:
        raise NotificationProviderError("Failed to publish market closed notification")
    logger.info("Market closed notification sent: market_id=%d creator_id=%d", market.id, market.creator_id)


@notification_operation("notify_bet_confirmed", NotificationProviderError)
async def notify_bet_confirmed(
    bot: Bot,
    user_id: int,
    bet: Bet,
    market: Market,
    new_balance: int,
    estimated_payout: int,
) -> None:
    _require_positive_int(user_id, "user_id")
    _require_market_id(market)
    option = _option_label(market, bet.option_index)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"Bet accepted: {bet.credits_amount} Stars on {option}.\n\n"
                f"Market: {market.question}\n"
                f"Estimated payout: ~{estimated_payout} Stars\n"
                f"Withdrawable balance: {new_balance} Stars"
            ),
        )
    except Exception as exc:
        logger.exception("Failed to send bet confirmation: user_id=%d market_id=%d", user_id, market.id)
        raise NotificationProviderError("Failed to send bet confirmation") from exc

    logger.info("Bet confirmation sent: user_id=%d market_id=%d bet_id=%s", user_id, market.id, bet.id)


@notification_operation("notify_payout_received", NotificationProviderError)
async def notify_payout_received(
    bot: Bot,
    user_id: int,
    payout: Payout,
    market: Market,
) -> None:
    _require_positive_int(user_id, "user_id")
    _require_market_id(market)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"You won {payout.credits_won} Stars.\n\n"
                f"Market: {market.question}\n"
                "The amount is now available for manual TON-equivalent payout."
            ),
        )
    except Exception as exc:
        logger.exception("Failed to send payout notification: user_id=%d market_id=%d", user_id, market.id)
        raise NotificationProviderError("Failed to send payout notification") from exc

    logger.info("Payout notification sent: user_id=%d market_id=%d payout_id=%s", user_id, market.id, payout.id)


def build_closed_market_card_text(market: Market, pool_by_option: dict[int, int]) -> str:
    return (
        build_market_card_text(market, pool_by_option)
        + "\n\nBetting is closed. Waiting for the creator to resolve this market."
    )


async def _update_closed_market_card(
    bot: Bot,
    market: Market,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    pool_by_option: dict[int, int],
) -> None:
    try:
        await update_market_card_photo(
            bot,
            inline_message_id=market.inline_message_id,
            chat_id=market.chat_id,
            message_id=market.message_id,
            market=market,
            pool_by_option=pool_by_option,
            reply_markup=reply_markup,
            fallback_text=text,
        )
    except Exception as exc:
        logger.exception("Failed to update closed market card: market_id=%d", market.id)
        raise NotificationProviderError("Failed to update closed market card") from exc


async def _update_closed_market_card_best_effort(
    bot: Bot,
    market: Market,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    pool_by_option: dict[int, int],
) -> bool:
    try:
        await _update_closed_market_card(bot, market, text, reply_markup, pool_by_option)
    except NotificationProviderError:
        logger.warning("Continuing after closed market card update failure: market_id=%d", market.id, exc_info=True)
        return False
    return True


async def _notify_creator_to_resolve(bot: Bot, market: Market) -> None:
    try:
        await bot.send_message(
            chat_id=market.creator_id,
            text=(
                f"Market #{market.id} reached its deadline.\n\n"
                f"{market.question}\n\n"
                "Choose the winning outcome."
            ),
            reply_markup=build_resolution_keyboard(market.id, market.options),
        )
    except Exception as exc:
        logger.exception("Failed to notify creator about closed market: market_id=%d", market.id)
        raise NotificationProviderError("Failed to notify creator about closed market") from exc


async def _notify_creator_to_resolve_best_effort(bot: Bot, market: Market) -> bool:
    try:
        await _notify_creator_to_resolve(bot, market)
    except NotificationProviderError:
        logger.warning("Continuing after creator resolution notification failure: market_id=%d", market.id, exc_info=True)
        return False
    return True


async def _load_deadline_approaching_markets(session: AsyncSession, now: datetime) -> list[Market]:
    stmt = (
        select(Market)
        .where(
            Market.status == MarketStatus.ACTIVE,
            Market.deadline > now,
            Market.deadline <= now + DEADLINE_APPROACHING_WINDOW,
        )
        .order_by(Market.deadline.asc(), Market.id.asc())
    )
    markets = list((await session.scalars(stmt)).all())
    logger.debug("Loaded deadline-approaching markets: count=%d", len(markets))
    return markets


async def _load_market_closed_markets(session: AsyncSession, now: datetime) -> list[Market]:
    stmt = (
        select(Market)
        .where(Market.status == MarketStatus.ACTIVE, Market.deadline <= now)
        .order_by(Market.deadline.asc(), Market.id.asc())
    )
    markets = list((await session.scalars(stmt)).all())
    logger.debug("Loaded closed markets for notification: count=%d", len(markets))
    return markets


async def _notification_was_sent(
    session: AsyncSession,
    kind: NotificationKind,
    market_id: int,
    user_id: int = 0,
) -> bool:
    stmt = select(NotificationLog.id).where(
        NotificationLog.kind == kind.value,
        NotificationLog.market_id == market_id,
        NotificationLog.user_id == user_id,
    )
    return await session.scalar(stmt) is not None


async def _record_notification_sent(
    session: AsyncSession,
    kind: NotificationKind,
    market_id: int,
    user_id: int = 0,
) -> NotificationLog:
    log = NotificationLog(kind=kind.value, market_id=market_id, user_id=user_id)
    session.add(log)
    try:
        await session.flush()
    except SQLAlchemyError as exc:
        logger.exception("Failed to record notification: kind=%s market_id=%d user_id=%d", kind.value, market_id, user_id)
        raise NotificationPersistenceError("Failed to record notification") from exc
    logger.info("Notification recorded: kind=%s market_id=%d user_id=%d", kind.value, market_id, user_id)
    return log


async def _rollback_after_item_failure(session: AsyncSession, operation: str, market_id: int) -> None:
    try:
        await session.rollback()
        logger.info("Rolled back notification item failure: operation=%s market_id=%d", operation, market_id)
    except SQLAlchemyError:
        logger.exception("Failed to roll back notification item failure: operation=%s market_id=%d", operation, market_id)
        raise NotificationPersistenceError("Failed to roll back notification item failure")


def _option_label(market: Market, option_index: int) -> str:
    if 0 <= option_index < len(market.options):
        return market.options[option_index]
    return f"option {option_index + 1}"


def _require_market_id(market: Market) -> None:
    if not isinstance(market.id, int) or market.id < 1:
        raise NotificationValidationError("market.id must be a positive integer")


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise NotificationValidationError(f"{name} must be a positive integer")
    return value
