from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import (
    DatabaseLayerError,
    RecordNotFoundError,
    create_dispute,
    get_bets_for_market,
    get_open_dispute_for_market,
    get_payouts_for_market,
    get_pool_by_option,
    update_dispute_status,
    update_market_status,
)
from bot.models import Bet, Deposit, Dispute, DisputeStatus, Market, MarketStatus
from bot.resolution import distribute_payouts, publish_resolution_results
from bot.security import is_admin


logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")

DISPUTE_CALLBACK_PREFIX = "dispute"
ARBITRATE_CALLBACK_PREFIX = "arbitrate"
REJECT_DISPUTE_CALLBACK_PREFIX = "reject_dispute"
DISPUTE_WINDOW = timedelta(hours=24)


class SuspicionLevel(StrEnum):
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"
    HIGH_RISK = "high_risk"


class FraudModuleError(RuntimeError):
    """Base error for Module 10 anti-fraud and dispute operations."""


class FraudValidationError(FraudModuleError, ValueError):
    """Raised when a fraud/dispute action is invalid."""


class FraudPersistenceError(FraudModuleError):
    """Raised when fraud/dispute state cannot be persisted."""


class FraudProviderError(FraudModuleError):
    """Raised when Telegram APIs cannot publish fraud/dispute updates."""


@dataclass(frozen=True)
class DisputeResult:
    dispute: Dispute
    market: Market


@dataclass(frozen=True)
class ArbitrationResult:
    market: Market
    dispute: Dispute | None
    payouts_created: int
    platform_fee_collected: int


def fraud_operation(
    operation_name: str,
    wrapped_error: type[FraudModuleError] = FraudModuleError,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger.debug("Fraud operation started: %s", operation_name)
            try:
                result = await func(*args, **kwargs)
            except FraudValidationError:
                logger.warning("Fraud operation rejected input: %s", operation_name, exc_info=True)
                raise
            except wrapped_error:
                logger.warning("Fraud operation failed: %s", operation_name, exc_info=True)
                raise
            except FraudModuleError:
                logger.warning("Fraud operation failed: %s", operation_name, exc_info=True)
                raise
            except DatabaseLayerError as exc:
                logger.exception("Fraud operation hit database error: %s", operation_name)
                raise wrapped_error(f"Fraud operation failed: {operation_name}") from exc
            except (ValueError, TypeError):
                logger.warning("Fraud operation rejected invalid input: %s", operation_name, exc_info=True)
                raise
            except Exception as exc:
                logger.exception("Fraud operation failed unexpectedly: %s", operation_name)
                raise wrapped_error(f"Unexpected fraud operation failure: {operation_name}") from exc

            logger.debug("Fraud operation completed: %s", operation_name)
            return result

        return wrapper

    return decorator


def create_fraud_router(
    admin_ids: list[int] | None = None,
    platform_fee_pct: float = 0.08,
    mini_app_url: str | None = None,
) -> Router:
    router = Router(name="fraud")
    resolved_admin_ids = admin_ids or []

    @router.callback_query(F.data.startswith(f"{DISPUTE_CALLBACK_PREFIX}:"))
    async def dispute_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
        bot: Bot,
    ) -> None:
        await handle_dispute_callback(callback, db_session, bot, resolved_admin_ids)

    @router.callback_query(F.data.startswith(f"{ARBITRATE_CALLBACK_PREFIX}:"))
    async def arbitrate_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
        bot: Bot,
    ) -> None:
        await handle_arbitrate_callback(
            callback=callback,
            session=db_session,
            bot=bot,
            admin_ids=resolved_admin_ids,
            platform_fee_pct=platform_fee_pct,
            mini_app_url=mini_app_url,
        )

    @router.callback_query(F.data.startswith(f"{REJECT_DISPUTE_CALLBACK_PREFIX}:"))
    async def reject_dispute_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
    ) -> None:
        await handle_reject_dispute_callback(callback, db_session, resolved_admin_ids)

    return router


def can_user_bet(user_id: int, market: Market) -> bool:
    _require_positive_int(user_id, "user_id")
    return market.creator_id != user_id and market.status == MarketStatus.ACTIVE


def detect_suspicious_patterns(
    bets: list[Bet],
    deposits: list[Deposit],
) -> SuspicionLevel:
    total_staked = sum(max(bet.credits_amount, 0) for bet in bets)
    total_deposited = sum(max(deposit.stars_amount, 0) for deposit in deposits)
    market_ids = {bet.market_id for bet in bets}

    if len(bets) >= 20 or total_staked >= 1000:
        return SuspicionLevel.HIGH_RISK
    if len(bets) >= 8 or len(market_ids) >= 5:
        return SuspicionLevel.SUSPICIOUS
    if total_deposited > 0 and total_staked > total_deposited * 3:
        return SuspicionLevel.SUSPICIOUS
    return SuspicionLevel.CLEAN


async def handle_dispute_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    admin_ids: list[int] | None = None,
) -> None:
    try:
        market_id = parse_dispute_callback_data(callback.data)
    except ValueError:
        logger.warning("Invalid dispute callback payload received")
        await _safe_answer_callback(callback, "This dispute button is invalid.", show_alert=True)
        return

    raised_by = callback.from_user.id if callback.from_user else None
    if raised_by is None:
        logger.warning("Dispute callback without Telegram user: market_id=%d", market_id)
        await _safe_answer_callback(callback, "Could not identify you for this dispute.", show_alert=True)
        return

    try:
        await freeze_market_for_dispute(
            session=session,
            bot=bot,
            market_id=market_id,
            raised_by=raised_by,
            reason="User disputed the resolved market from Telegram.",
            admin_ids=admin_ids or [],
        )
        await _safe_answer_callback(callback, "Dispute opened. An admin will review it.")
    except FraudValidationError as exc:
        logger.info(
            "Dispute callback rejected: market_id=%d user_id=%d reason=%s",
            market_id,
            raised_by,
            exc,
        )
        await _safe_answer_callback(callback, str(exc), show_alert=True)
    except FraudModuleError as exc:
        logger.exception("Dispute callback failed: market_id=%d user_id=%d", market_id, raised_by)
        await _safe_answer_callback(callback, "Could not open dispute.", show_alert=True)
        raise FraudPersistenceError("Dispute callback failed") from exc
    except Exception as exc:
        logger.exception("Unexpected dispute callback failure: market_id=%d user_id=%d", market_id, raised_by)
        await _safe_answer_callback(callback, "Could not open dispute.", show_alert=True)
        raise FraudPersistenceError("Unexpected dispute callback failure") from exc


@fraud_operation("freeze_market_for_dispute", FraudPersistenceError)
async def freeze_market_for_dispute(
    session: AsyncSession,
    bot: Bot,
    market_id: int,
    raised_by: int,
    reason: str,
    admin_ids: list[int] | None = None,
) -> DisputeResult:
    _require_positive_int(market_id, "market_id")
    _require_positive_int(raised_by, "raised_by")
    _require_text(reason, "reason")

    market = await _get_market_for_update(session, market_id)
    _validate_dispute_request(market, raised_by)

    existing_dispute = await get_open_dispute_for_market(session, market_id, for_update=True)
    if existing_dispute is not None:
        raise FraudValidationError("This market already has an open dispute.")

    dispute = await create_dispute(session, market_id, raised_by, reason)
    market = await update_market_status(
        session=session,
        market_id=market_id,
        status=MarketStatus.DISPUTED,
        winning_option=market.winning_option,
    )
    logger.info(
        "Market frozen for dispute: market_id=%d dispute_id=%d raised_by=%d",
        market_id,
        dispute.id,
        raised_by,
    )
    await notify_admins_for_dispute(bot, market, dispute, admin_ids or [])
    return DisputeResult(dispute=dispute, market=market)


@fraud_operation("admin_arbitrate", FraudPersistenceError)
async def admin_arbitrate(
    session: AsyncSession,
    bot: Bot,
    market_id: int,
    winning_option_index: int,
    admin_id: int,
    platform_fee_pct: float,
    mini_app_url: str | None = None,
) -> ArbitrationResult:
    _require_positive_int(market_id, "market_id")
    _require_non_negative_int(winning_option_index, "winning_option_index")
    _require_positive_int(admin_id, "admin_id")

    market = await _get_market_for_update(session, market_id)
    if market.status != MarketStatus.DISPUTED:
        raise FraudValidationError("Market is not disputed.")
    if winning_option_index >= len(market.options):
        raise FraudValidationError("Winning option is invalid.")

    dispute = await get_open_dispute_for_market(session, market_id, for_update=True)
    existing_payouts = await get_payouts_for_market(session, market_id)
    payouts = []
    platform_fee_collected = 0

    if existing_payouts:
        logger.warning(
            "Cancelling held payouts before arbitration redistribution: market_id=%d payouts=%d",
            market_id,
            len(existing_payouts),
        )
        for payout in existing_payouts:
            if payout.status.value != "held":
                raise FraudValidationError("This market has released payouts and requires manual ledger repair.")
            payout.credits_won = 0
            payout.status = payout.status
        bets = await get_bets_for_market(session, market_id, for_update=True)
        payouts, platform_fee_collected = await distribute_payouts(
            session=session,
            market=market,
            winning_option_index=winning_option_index,
            platform_fee_pct=platform_fee_pct,
            bets=bets,
        )
    else:
        bets = await get_bets_for_market(session, market_id, for_update=True)
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
    if dispute is not None:
        dispute = await update_dispute_status(
            session=session,
            dispute_id=dispute.id,
            status=DisputeStatus.RESOLVED,
            resolution_note=f"Admin {admin_id} set outcome to option {winning_option_index}.",
        )

    pool_by_option = await get_pool_by_option(session, market_id)
    try:
        await publish_resolution_results(
            bot=bot,
            market=market,
            payouts=payouts or existing_payouts,
            pool_by_option=pool_by_option,
            platform_fee=platform_fee_collected,
            mini_app_url=mini_app_url,
        )
    except Exception:
        logger.exception("Arbitration persisted but result publishing failed: market_id=%d", market_id)

    await _notify_arbitration_payouts_best_effort(bot, market, payouts)
    logger.info(
        "Admin arbitration completed: market_id=%d admin_id=%d winner=%d payouts_created=%d",
        market_id,
        admin_id,
        winning_option_index,
        len(payouts),
    )
    return ArbitrationResult(
        market=market,
        dispute=dispute,
        payouts_created=len(payouts),
        platform_fee_collected=platform_fee_collected,
    )


async def handle_arbitrate_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    admin_ids: list[int],
    platform_fee_pct: float,
    mini_app_url: str | None = None,
) -> None:
    admin_id = callback.from_user.id if callback.from_user else None
    if admin_id is None or not is_admin(admin_id, admin_ids):
        logger.warning("Unauthorized arbitration callback: admin_id=%s", admin_id)
        await _safe_answer_callback(callback, "Access denied", show_alert=True)
        return

    try:
        market_id, winning_option_index = parse_arbitrate_callback_data(callback.data)
        await admin_arbitrate(
            session=session,
            bot=bot,
            market_id=market_id,
            winning_option_index=winning_option_index,
            admin_id=admin_id,
            platform_fee_pct=platform_fee_pct,
            mini_app_url=mini_app_url,
        )
        await _safe_answer_callback(callback, "Dispute arbitrated.")
    except FraudValidationError as exc:
        logger.info("Arbitration callback rejected: admin_id=%s reason=%s", admin_id, exc)
        await _safe_answer_callback(callback, str(exc), show_alert=True)
    except ValueError as exc:
        logger.warning("Invalid arbitration callback payload: admin_id=%s", admin_id, exc_info=True)
        await _safe_answer_callback(callback, "Invalid arbitration button.", show_alert=True)
    except FraudModuleError as exc:
        logger.exception("Arbitration callback failed: admin_id=%s", admin_id)
        await _safe_answer_callback(callback, "Could not arbitrate dispute.", show_alert=True)
        raise FraudPersistenceError("Arbitration callback failed") from exc
    except Exception as exc:
        logger.exception("Unexpected arbitration callback failure: admin_id=%s", admin_id)
        await _safe_answer_callback(callback, "Could not arbitrate dispute.", show_alert=True)
        raise FraudPersistenceError("Unexpected arbitration callback failure") from exc


async def handle_reject_dispute_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    admin_ids: list[int],
) -> None:
    admin_id = callback.from_user.id if callback.from_user else None
    if admin_id is None or not is_admin(admin_id, admin_ids):
        logger.warning("Unauthorized reject-dispute callback: admin_id=%s", admin_id)
        await _safe_answer_callback(callback, "Access denied", show_alert=True)
        return

    try:
        market_id = parse_reject_dispute_callback_data(callback.data)
        market = await _get_market_for_update(session, market_id)
        dispute = await get_open_dispute_for_market(session, market_id, for_update=True)
        if dispute is None:
            raise FraudValidationError("No open dispute for this market.")
        await update_dispute_status(
            session=session,
            dispute_id=dispute.id,
            status=DisputeStatus.REJECTED,
            resolution_note=f"Admin {admin_id} rejected the dispute.",
        )
        await update_market_status(
            session=session,
            market_id=market_id,
            status=MarketStatus.RESOLVED,
            winning_option=market.winning_option,
        )
        await _safe_answer_callback(callback, "Dispute rejected.")
    except FraudValidationError as exc:
        logger.info("Reject-dispute callback rejected: admin_id=%s reason=%s", admin_id, exc)
        await _safe_answer_callback(callback, str(exc), show_alert=True)
    except ValueError as exc:
        logger.warning("Invalid reject-dispute callback payload: admin_id=%s", admin_id, exc_info=True)
        await _safe_answer_callback(callback, "Invalid reject button.", show_alert=True)
    except (DatabaseLayerError, FraudModuleError) as exc:
        logger.exception("Reject dispute callback failed: admin_id=%s", admin_id)
        await _safe_answer_callback(callback, "Could not reject dispute.", show_alert=True)
        raise FraudPersistenceError("Reject dispute callback failed") from exc
    except Exception as exc:
        logger.exception("Unexpected reject dispute callback failure: admin_id=%s", admin_id)
        await _safe_answer_callback(callback, "Could not reject dispute.", show_alert=True)
        raise FraudPersistenceError("Unexpected reject dispute callback failure") from exc


@fraud_operation("notify_admins_for_dispute", FraudProviderError)
async def notify_admins_for_dispute(
    bot: Bot,
    market: Market,
    dispute: Dispute,
    admin_ids: list[int],
) -> None:
    if not admin_ids:
        logger.warning("No admins configured for dispute notification: dispute_id=%d", dispute.id)
        return

    keyboard = build_admin_dispute_keyboard(market.id, market.options)
    text = build_admin_dispute_text(market, dispute)
    failures = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
            logger.info("Admin dispute notification sent: dispute_id=%d admin_id=%d", dispute.id, admin_id)
        except Exception:
            failures += 1
            logger.exception("Failed to notify admin about dispute: dispute_id=%d admin_id=%d", dispute.id, admin_id)
    if failures == len(admin_ids):
        logger.error("All admin dispute notifications failed: dispute_id=%d admins=%d", dispute.id, len(admin_ids))


async def _notify_arbitration_payouts_best_effort(bot: Bot, market: Market, payouts: list[Any]) -> None:
    if not payouts:
        return

    try:
        from bot.notifications import notify_payout_received
    except Exception:
        logger.exception("Could not import arbitration payout notification helper: market_id=%d", market.id)
        return

    for payout in payouts:
        try:
            await notify_payout_received(bot=bot, user_id=payout.user_id, payout=payout, market=market)
        except Exception:
            logger.exception(
                "Arbitration persisted but payout notification failed: market_id=%d user_id=%d payout_id=%d",
                market.id,
                payout.user_id,
                payout.id,
            )


def build_dispute_keyboard(market_id: int) -> InlineKeyboardMarkup:
    _require_positive_int(market_id, "market_id")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Dispute", callback_data=f"{DISPUTE_CALLBACK_PREFIX}:{market_id}")]
        ]
    )


def build_admin_dispute_keyboard(market_id: int, options: list[str]) -> InlineKeyboardMarkup:
    _require_positive_int(market_id, "market_id")
    rows = [
        [
            InlineKeyboardButton(
                text=f"Set {option}",
                callback_data=f"{ARBITRATE_CALLBACK_PREFIX}:{market_id}:{index}",
            )
        ]
        for index, option in enumerate(options)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Reject dispute",
                callback_data=f"{REJECT_DISPUTE_CALLBACK_PREFIX}:{market_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_admin_dispute_text(market: Market, dispute: Dispute) -> str:
    winning = "Not set"
    if market.winning_option is not None and 0 <= market.winning_option < len(market.options):
        winning = market.options[market.winning_option]
    return (
        f"Dispute #{dispute.id} opened for market #{market.id}\n\n"
        f"{market.question}\n\n"
        f"Current outcome: {winning}\n"
        f"Raised by: {dispute.raised_by}\n"
        f"Reason: {dispute.reason}"
    )


def parse_dispute_callback_data(callback_data: str | None) -> int:
    if not isinstance(callback_data, str):
        raise ValueError("callback data is required")
    parts = callback_data.split(":")
    if len(parts) != 2 or parts[0] != DISPUTE_CALLBACK_PREFIX:
        raise ValueError("callback data must have format dispute:{market_id}")
    return _require_positive_int(_parse_int(parts[1], "market_id"), "market_id")


def parse_arbitrate_callback_data(callback_data: str | None) -> tuple[int, int]:
    if not isinstance(callback_data, str):
        raise ValueError("callback data is required")
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != ARBITRATE_CALLBACK_PREFIX:
        raise ValueError("callback data must have format arbitrate:{market_id}:{option_index}")
    return (
        _require_positive_int(_parse_int(parts[1], "market_id"), "market_id"),
        _require_non_negative_int(_parse_int(parts[2], "option_index"), "option_index"),
    )


def parse_reject_dispute_callback_data(callback_data: str | None) -> int:
    if not isinstance(callback_data, str):
        raise ValueError("callback data is required")
    parts = callback_data.split(":")
    if len(parts) != 2 or parts[0] != REJECT_DISPUTE_CALLBACK_PREFIX:
        raise ValueError("callback data must have format reject_dispute:{market_id}")
    return _require_positive_int(_parse_int(parts[1], "market_id"), "market_id")


async def _get_market_for_update(session: AsyncSession, market_id: int) -> Market:
    stmt = select(Market).where(Market.id == market_id).with_for_update()
    market = await session.scalar(stmt)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")
    return market


def _validate_dispute_request(market: Market, raised_by: int) -> None:
    if market.status != MarketStatus.RESOLVED:
        raise FraudValidationError("Only resolved markets can be disputed.")
    if market.resolved_at is None:
        raise FraudValidationError("This market does not have a recorded resolution time.")
    if market.resolved_at + DISPUTE_WINDOW < datetime.now(timezone.utc):
        raise FraudValidationError("The dispute window has closed.")
    if market.creator_id == raised_by:
        raise FraudValidationError("Market creators cannot dispute their own resolution.")


async def _answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception as exc:
        logger.exception("Failed to answer fraud callback")
        raise FraudProviderError("Failed to answer fraud callback") from exc


async def _safe_answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    try:
        await _answer_callback(callback, text=text, show_alert=show_alert)
    except FraudProviderError:
        logger.warning("Continuing after failed fraud callback answer", exc_info=True)


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
