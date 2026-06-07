from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Bet, Dispute, DisputeStatus, Market, MarketStatus, Payout, User, Withdrawal, WithdrawalStatus
from bot.security import is_admin


logger = logging.getLogger(__name__)


class AdminModuleError(RuntimeError):
    """Base error for Module 13 admin operations."""


class AdminValidationError(AdminModuleError, ValueError):
    """Raised when an admin request is invalid or unauthorized."""


class AdminProviderError(AdminModuleError):
    """Raised when Telegram admin operations cannot be delivered."""


@dataclass(frozen=True)
class PlatformStats:
    total_users: int
    active_markets: int
    disputed_markets: int
    total_volume_stars: int
    platform_revenue_stars: int
    pending_disputes: int
    pending_withdrawals: int
    daily_new_users: int


class AdminStates(StatesGroup):
    waiting_broadcast_text = State()


def create_admin_router(admin_ids: list[int] | None = None) -> Router:
    router = Router(name="admin")
    resolved_admin_ids = admin_ids or []

    @router.message(Command("admin_stats", ignore_mention=True))
    async def admin_stats_handler(message: Message, db_session: AsyncSession) -> None:
        await handle_admin_stats(message, db_session, resolved_admin_ids)

    @router.message(Command("admin_disputes", ignore_mention=True))
    async def admin_disputes_handler(message: Message, db_session: AsyncSession) -> None:
        await handle_admin_disputes(message, db_session, resolved_admin_ids)

    @router.message(Command("broadcast", ignore_mention=True))
    async def broadcast_command_handler(message: Message, state: FSMContext) -> None:
        await handle_broadcast_command(message, state, resolved_admin_ids)

    @router.message(AdminStates.waiting_broadcast_text)
    async def broadcast_text_handler(
        message: Message,
        state: FSMContext,
        db_session: AsyncSession,
        bot: Bot,
    ) -> None:
        await process_broadcast_text(message, state, db_session, bot, resolved_admin_ids)

    return router


async def handle_admin_stats(
    message: Message,
    session: AsyncSession,
    admin_ids: list[int],
) -> None:
    admin_id = _message_user_id(message)
    if not is_admin(admin_id, admin_ids):
        await _answer_message(message, "Access denied")
        logger.warning("Unauthorized /admin_stats attempt: user_id=%s", admin_id)
        return

    stats = await get_platform_stats(session)
    await _answer_message(message, build_admin_stats_text(stats))
    logger.info("Admin stats sent: admin_id=%d", admin_id)


async def get_platform_stats(session: AsyncSession) -> PlatformStats:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    total_users = await session.scalar(select(func.count()).select_from(User))
    active_markets = await session.scalar(
        select(func.count()).select_from(Market).where(Market.status == MarketStatus.ACTIVE)
    )
    disputed_markets = await session.scalar(
        select(func.count()).select_from(Market).where(Market.status == MarketStatus.DISPUTED)
    )
    total_volume = await session.scalar(select(func.coalesce(func.sum(Bet.credits_amount), 0)))
    payout_total = await session.scalar(select(func.coalesce(func.sum(Payout.credits_won), 0)))
    pending_disputes = await session.scalar(
        select(func.count()).select_from(Dispute).where(Dispute.status == DisputeStatus.OPEN)
    )
    pending_withdrawals = await session.scalar(
        select(func.count()).select_from(Withdrawal).where(Withdrawal.status == WithdrawalStatus.PENDING)
    )
    daily_new_users = await session.scalar(
        select(func.count()).select_from(User).where(User.created_at >= since)
    )
    volume = int(total_volume or 0)

    return PlatformStats(
        total_users=int(total_users or 0),
        active_markets=int(active_markets or 0),
        disputed_markets=int(disputed_markets or 0),
        total_volume_stars=volume,
        platform_revenue_stars=max(0, volume - int(payout_total or 0)),
        pending_disputes=int(pending_disputes or 0),
        pending_withdrawals=int(pending_withdrawals or 0),
        daily_new_users=int(daily_new_users or 0),
    )


async def handle_admin_disputes(
    message: Message,
    session: AsyncSession,
    admin_ids: list[int],
) -> None:
    admin_id = _message_user_id(message)
    if not is_admin(admin_id, admin_ids):
        await _answer_message(message, "Access denied")
        logger.warning("Unauthorized /admin_disputes attempt: user_id=%s", admin_id)
        return

    disputes = list(
        (
            await session.execute(
                select(Dispute, Market)
                .join(Market, Market.id == Dispute.market_id)
                .where(Dispute.status == DisputeStatus.OPEN)
                .order_by(Dispute.created_at.asc(), Dispute.id.asc())
                .limit(10)
            )
        ).all()
    )
    await _answer_message(message, build_admin_disputes_text(disputes))
    logger.info("Admin disputes sent: admin_id=%d count=%d", admin_id, len(disputes))


async def handle_broadcast_command(
    message: Message,
    state: FSMContext,
    admin_ids: list[int],
) -> None:
    admin_id = _message_user_id(message)
    if not is_admin(admin_id, admin_ids):
        await _answer_message(message, "Access denied")
        logger.warning("Unauthorized /broadcast attempt: user_id=%s", admin_id)
        return

    await state.clear()
    await state.set_state(AdminStates.waiting_broadcast_text)
    await _answer_message(message, "Send the broadcast text. It will be delivered to all known users.")
    logger.info("Admin broadcast flow started: admin_id=%d", admin_id)


async def process_broadcast_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    admin_ids: list[int],
) -> None:
    admin_id = _message_user_id(message)
    if not is_admin(admin_id, admin_ids):
        await state.clear()
        await _answer_message(message, "Access denied")
        logger.warning("Unauthorized broadcast text attempt: user_id=%s", admin_id)
        return

    text = _clean_broadcast_text(message.text)
    if text is None:
        await _answer_message(message, "Broadcast text cannot be empty.")
        return

    user_ids = list((await session.scalars(select(User.telegram_id).order_by(User.telegram_id.asc()))).all())
    delivered = 0
    failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=text)
            delivered += 1
        except Exception:
            failed += 1
            logger.exception("Broadcast delivery failed: admin_id=%d user_id=%d", admin_id, user_id)

    await state.clear()
    await _answer_message(
        message,
        f"Broadcast finished.\nDelivered: {delivered}\nFailed: {failed}",
    )
    logger.info(
        "Admin broadcast completed: admin_id=%d recipients=%d delivered=%d failed=%d",
        admin_id,
        len(user_ids),
        delivered,
        failed,
    )


async def fetch_star_transactions(bot: Bot, offset: int = 0, limit: int = 100) -> list[Any]:
    if offset < 0:
        raise AdminValidationError("offset must be non-negative")
    if limit < 1 or limit > 100:
        raise AdminValidationError("limit must be in range 1..100")

    method = getattr(bot, "get_star_transactions", None)
    if method is None:
        raise AdminProviderError("Bot does not expose get_star_transactions")

    try:
        result = await method(offset=offset, limit=limit)
    except Exception as exc:
        logger.exception("Failed to fetch Star transactions: offset=%d limit=%d", offset, limit)
        raise AdminProviderError("Failed to fetch Star transactions") from exc

    transactions = getattr(result, "transactions", result)
    return list(transactions or [])


def build_admin_stats_text(stats: PlatformStats) -> str:
    return (
        "Poolr admin stats\n\n"
        f"Users: {stats.total_users} (+{stats.daily_new_users}/24h)\n"
        f"Active markets: {stats.active_markets}\n"
        f"Disputed markets: {stats.disputed_markets}\n"
        f"Pending disputes: {stats.pending_disputes}\n"
        f"Pending withdrawals: {stats.pending_withdrawals}\n"
        f"Volume: {stats.total_volume_stars} Stars\n"
        f"Provisional revenue: {stats.platform_revenue_stars} Stars"
    )


def build_admin_disputes_text(rows: list[tuple[Dispute, Market]]) -> str:
    if not rows:
        return "No open disputes."

    lines = ["Open disputes"]
    for dispute, market in rows:
        lines.append(
            f"#{dispute.id} market #{market.id}: {market.question}\n"
            f"Raised by: {dispute.raised_by}"
        )
    return "\n\n".join(lines)


def _message_user_id(message: Message) -> int:
    user_id = message.from_user.id if message.from_user else None
    return user_id if isinstance(user_id, int) else 0


def _clean_broadcast_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


async def _answer_message(message: Message, text: str) -> None:
    try:
        await message.answer(text)
    except Exception as exc:
        logger.exception("Failed to send admin message")
        raise AdminProviderError("Failed to send admin message") from exc
