from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Awaitable, Callable, ParamSpec, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import (
    Bet,
    Deposit,
    DepositStatus,
    Dispute,
    DisputeStatus,
    Market,
    MarketStatus,
    Payout,
    User,
    Withdrawal,
    WithdrawalStatus,
)


logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")


class DatabaseLayerError(RuntimeError):
    """Base error for Module 3 database operations."""


class RecordNotFoundError(DatabaseLayerError):
    """Raised when a requested row does not exist."""


class InsufficientBalanceError(DatabaseLayerError):
    """Raised when a balance debit would make the user balance negative."""


class DuplicateRecordError(DatabaseLayerError):
    """Raised when a uniqueness constraint rejects a new row."""


class DatabaseOperationError(DatabaseLayerError):
    """Raised when the database driver or SQLAlchemy operation fails."""


def db_operation(
    operation_name: str,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger.debug("DB operation started: %s", operation_name)
            try:
                result = await func(*args, **kwargs)
            except (ValueError, TypeError):
                logger.warning("DB operation rejected invalid input: %s", operation_name)
                raise
            except DatabaseLayerError:
                logger.warning("DB operation failed: %s", operation_name, exc_info=True)
                raise
            except SQLAlchemyError as exc:
                logger.exception("DB operation failed with SQLAlchemy error: %s", operation_name)
                raise DatabaseOperationError(
                    f"Database operation failed: {operation_name}"
                ) from exc
            except Exception as exc:
                logger.exception("DB operation failed unexpectedly: %s", operation_name)
                raise DatabaseOperationError(
                    f"Unexpected database operation failure: {operation_name}"
                ) from exc

            logger.debug("DB operation completed: %s", operation_name)
            return result

        return wrapper

    return decorator


@db_operation("create_or_get_user")
async def create_or_get_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str,
) -> tuple[User, bool]:
    _require_positive_int(telegram_id, "telegram_id")
    _require_text(first_name, "first_name")

    user = await get_user_by_id(session, telegram_id)
    if user is not None:
        changed = False
        if user.username != username:
            user.username = username
            changed = True
        if user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if changed:
            await session.flush()
            logger.info("Updated user profile: telegram_id=%d", telegram_id)
        return user, False

    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        balance_credits=0,
    )
    session.add(user)
    await session.flush()
    logger.info("Created user: telegram_id=%d", telegram_id)
    return user, True


@db_operation("get_user_by_id")
async def get_user_by_id(session: AsyncSession, telegram_id: int) -> User | None:
    _require_positive_int(telegram_id, "telegram_id")
    return await session.get(User, telegram_id)


@db_operation("update_user_balance")
async def update_user_balance(
    session: AsyncSession,
    telegram_id: int,
    delta: int,
    reason: str,
) -> User:
    _require_positive_int(telegram_id, "telegram_id")
    _require_non_zero_int(delta, "delta")
    _require_text(reason, "reason")

    stmt = select(User).where(User.telegram_id == telegram_id).with_for_update()
    user = await session.scalar(stmt)
    if user is None:
        raise RecordNotFoundError(f"User {telegram_id} was not found")

    new_balance = user.balance_credits + delta
    if new_balance < 0:
        raise InsufficientBalanceError(
            f"User {telegram_id} has {user.balance_credits} credits, cannot apply {delta}"
        )

    user.balance_credits = new_balance
    await session.flush()
    logger.info(
        "Updated user balance: telegram_id=%d delta=%d reason=%s new_balance=%d",
        telegram_id,
        delta,
        reason,
        new_balance,
    )
    return user


@db_operation("create_deposit")
async def create_deposit(
    session: AsyncSession,
    user_id: int,
    stars_amount: int,
    charge_id: str,
) -> Deposit:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(stars_amount, "stars_amount")
    _require_text(charge_id, "charge_id")

    deposit = Deposit(
        user_id=user_id,
        stars_amount=stars_amount,
        charge_id=charge_id,
        status=DepositStatus.PENDING,
    )
    session.add(deposit)
    try:
        await session.flush()
    except IntegrityError as exc:
        logger.warning(
            "Duplicate deposit charge_id rejected: user_id=%d charge_id=%s",
            user_id,
            _redact_charge_id(charge_id),
        )
        raise DuplicateRecordError("Deposit charge_id already exists") from exc

    logger.info(
        "Created deposit: id=%d user_id=%d stars=%d",
        deposit.id,
        user_id,
        stars_amount,
    )
    return deposit


@db_operation("confirm_deposit")
async def confirm_deposit(session: AsyncSession, charge_id: str) -> Deposit:
    _require_text(charge_id, "charge_id")

    stmt = select(Deposit).where(Deposit.charge_id == charge_id).with_for_update()
    deposit = await session.scalar(stmt)
    if deposit is None:
        logger.warning("Deposit not found: charge_id=%s", _redact_charge_id(charge_id))
        raise RecordNotFoundError("Deposit charge_id was not found")

    deposit.status = DepositStatus.CONFIRMED
    await session.flush()
    logger.info("Confirmed deposit: id=%d user_id=%d", deposit.id, deposit.user_id)
    return deposit


@db_operation("get_deposit_by_charge_id")
async def get_deposit_by_charge_id(
    session: AsyncSession,
    charge_id: str,
    for_update: bool = False,
) -> Deposit | None:
    _require_text(charge_id, "charge_id")

    stmt = select(Deposit).where(Deposit.charge_id == charge_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


@db_operation("get_available_charge_ids")
async def get_available_charge_ids(
    session: AsyncSession,
    user_id: int,
    credits_needed: int,
) -> list[str]:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(credits_needed, "credits_needed")

    stmt = (
        select(Deposit)
        .where(
            Deposit.user_id == user_id,
            Deposit.status == DepositStatus.CONFIRMED,
        )
        .order_by(Deposit.created_at.asc(), Deposit.id.asc())
    )
    deposits = list((await session.scalars(stmt)).all())

    total = 0
    charge_ids: list[str] = []
    for deposit in deposits:
        charge_ids.append(deposit.charge_id)
        total += deposit.stars_amount
        if total >= credits_needed:
            break

    if total < credits_needed:
        logger.info(
            "Not enough confirmed charge_ids: user_id=%d needed=%d available=%d",
            user_id,
            credits_needed,
            total,
        )
        return []

    logger.info(
        "Found confirmed charge_ids for withdrawal: user_id=%d needed=%d charges=%d",
        user_id,
        credits_needed,
        len(charge_ids),
    )
    return charge_ids


@db_operation("create_market")
async def create_market(
    session: AsyncSession,
    creator_id: int,
    chat_id: int,
    question: str,
    options: list[str],
    deadline: datetime,
    min_bet: int,
) -> Market:
    _require_positive_int(creator_id, "creator_id")
    _require_int(chat_id, "chat_id")
    _require_text(question, "question")
    _validate_market_options(options)
    _require_timezone_aware_deadline(deadline)
    _require_positive_int(min_bet, "min_bet")

    market = Market(
        creator_id=creator_id,
        chat_id=chat_id,
        question=question,
        options=options,
        deadline=deadline,
        min_bet=min_bet,
        status=MarketStatus.ACTIVE,
    )
    session.add(market)
    await session.flush()
    logger.info(
        "Created market: id=%d creator_id=%d chat_id=%d options=%d min_bet=%d",
        market.id,
        creator_id,
        chat_id,
        len(options),
        min_bet,
    )
    return market


@db_operation("get_market")
async def get_market(session: AsyncSession, market_id: int) -> Market | None:
    _require_positive_int(market_id, "market_id")
    return await session.get(Market, market_id)


@db_operation("get_active_markets_in_chat")
async def get_active_markets_in_chat(
    session: AsyncSession,
    chat_id: int,
) -> list[Market]:
    _require_int(chat_id, "chat_id")
    stmt = (
        select(Market)
        .where(Market.chat_id == chat_id, Market.status == MarketStatus.ACTIVE)
        .order_by(Market.deadline.asc(), Market.id.asc())
    )
    markets = list((await session.scalars(stmt)).all())
    logger.debug("Loaded active markets: chat_id=%d count=%d", chat_id, len(markets))
    return markets


@db_operation("get_markets_past_deadline")
async def get_markets_past_deadline(
    session: AsyncSession,
    grace_hours: int = 24,
) -> list[Market]:
    if grace_hours < 0:
        raise ValueError("grace_hours must be non-negative")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=grace_hours)
    stmt = (
        select(Market)
        .where(Market.status == MarketStatus.ACTIVE, Market.deadline <= cutoff)
        .order_by(Market.deadline.asc(), Market.id.asc())
    )
    markets = list((await session.scalars(stmt)).all())
    logger.info(
        "Loaded markets past deadline: grace_hours=%d count=%d",
        grace_hours,
        len(markets),
    )
    return markets


@db_operation("update_market_status")
async def update_market_status(
    session: AsyncSession,
    market_id: int,
    status: MarketStatus,
    winning_option: int | None = None,
) -> Market:
    _require_positive_int(market_id, "market_id")
    if not isinstance(status, MarketStatus):
        status = MarketStatus(status)

    stmt = select(Market).where(Market.id == market_id).with_for_update()
    market = await session.scalar(stmt)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")

    if winning_option is not None:
        if winning_option < 0 or winning_option >= len(market.options):
            raise ValueError("winning_option must reference an existing option")

    market.status = status
    market.winning_option = winning_option
    if status == MarketStatus.RESOLVED and market.resolved_at is None:
        market.resolved_at = datetime.now(timezone.utc)
    await session.flush()
    logger.info(
        "Updated market status: id=%d status=%s winning_option=%s",
        market_id,
        status.value,
        winning_option,
    )
    return market


@db_operation("update_market_message_id")
async def update_market_message_id(
    session: AsyncSession,
    market_id: int,
    message_id: int,
) -> Market:
    _require_positive_int(market_id, "market_id")
    _require_positive_int(message_id, "message_id")

    stmt = select(Market).where(Market.id == market_id).with_for_update()
    market = await session.scalar(stmt)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")

    market.message_id = message_id
    await session.flush()
    logger.info("Updated market message id: id=%d message_id=%d", market_id, message_id)
    return market


@db_operation("update_market_inline_message_id")
async def update_market_inline_message_id(
    session: AsyncSession,
    market_id: int,
    inline_message_id: str,
) -> Market:
    _require_positive_int(market_id, "market_id")
    _require_text(inline_message_id, "inline_message_id")

    stmt = select(Market).where(Market.id == market_id).with_for_update()
    market = await session.scalar(stmt)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")

    market.inline_message_id = inline_message_id
    await session.flush()
    logger.info("Updated market inline message id: id=%d", market_id)
    return market


@db_operation("create_bet")
async def create_bet(
    session: AsyncSession,
    user_id: int,
    market_id: int,
    option_index: int,
    credits_amount: int,
) -> Bet:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(market_id, "market_id")
    _require_positive_int(credits_amount, "credits_amount")

    market = await get_market(session, market_id)
    if market is None:
        raise RecordNotFoundError(f"Market {market_id} was not found")
    if market.status != MarketStatus.ACTIVE:
        raise ValueError("Cannot create a bet on an inactive market")
    if option_index < 0 or option_index >= len(market.options):
        raise ValueError("option_index must reference an existing option")
    if credits_amount < market.min_bet:
        raise ValueError("credits_amount is below market min_bet")

    bet = Bet(
        user_id=user_id,
        market_id=market_id,
        option_index=option_index,
        credits_amount=credits_amount,
    )
    session.add(bet)
    try:
        await session.flush()
    except IntegrityError as exc:
        logger.warning(
            "Duplicate bet rejected: user_id=%d market_id=%d",
            user_id,
            market_id,
        )
        raise DuplicateRecordError(
            f"User {user_id} already has a bet on market {market_id}"
        ) from exc

    logger.info(
        "Created bet: id=%d user_id=%d market_id=%d credits=%d",
        bet.id,
        user_id,
        market_id,
        credits_amount,
    )
    return bet


@db_operation("get_pool_by_option")
async def get_pool_by_option(
    session: AsyncSession,
    market_id: int,
) -> dict[int, int]:
    _require_positive_int(market_id, "market_id")
    stmt: Select[tuple[int, int]] = (
        select(Bet.option_index, func.coalesce(func.sum(Bet.credits_amount), 0))
        .where(Bet.market_id == market_id)
        .group_by(Bet.option_index)
        .order_by(Bet.option_index.asc())
    )
    rows = (await session.execute(stmt)).all()
    pool = {int(option_index): int(total) for option_index, total in rows}
    logger.debug("Loaded market pool: market_id=%d options=%d", market_id, len(pool))
    return pool


@db_operation("get_user_bet_on_market")
async def get_user_bet_on_market(
    session: AsyncSession,
    user_id: int,
    market_id: int,
) -> Bet | None:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(market_id, "market_id")
    stmt = select(Bet).where(Bet.user_id == user_id, Bet.market_id == market_id)
    return await session.scalar(stmt)


@db_operation("get_bets_for_market")
async def get_bets_for_market(
    session: AsyncSession,
    market_id: int,
    for_update: bool = False,
) -> list[Bet]:
    _require_positive_int(market_id, "market_id")

    stmt = (
        select(Bet)
        .where(Bet.market_id == market_id)
        .order_by(Bet.id.asc())
    )
    if for_update:
        stmt = stmt.with_for_update()
    bets = list((await session.scalars(stmt)).all())
    logger.debug("Loaded market bets: market_id=%d count=%d", market_id, len(bets))
    return bets


@db_operation("create_payout")
async def create_payout(
    session: AsyncSession,
    user_id: int,
    market_id: int,
    credits_won: int,
) -> Payout:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(market_id, "market_id")
    if not isinstance(credits_won, int) or credits_won < 0:
        raise ValueError("credits_won must be a non-negative integer")

    payout = Payout(
        user_id=user_id,
        market_id=market_id,
        credits_won=credits_won,
    )
    session.add(payout)
    await session.flush()
    logger.info(
        "Created payout: id=%d user_id=%d market_id=%d stars=%d",
        payout.id,
        user_id,
        market_id,
        credits_won,
    )
    return payout


@db_operation("get_payouts_for_market")
async def get_payouts_for_market(
    session: AsyncSession,
    market_id: int,
) -> list[Payout]:
    _require_positive_int(market_id, "market_id")

    stmt = (
        select(Payout)
        .where(Payout.market_id == market_id)
        .order_by(Payout.credits_won.desc(), Payout.id.asc())
    )
    payouts = list((await session.scalars(stmt)).all())
    logger.debug("Loaded market payouts: market_id=%d count=%d", market_id, len(payouts))
    return payouts


@db_operation("create_withdrawal")
async def create_withdrawal(
    session: AsyncSession,
    user_id: int,
    credits_amount: int,
    ton_wallet_address: str,
    charge_ids_used: list[str] | None = None,
) -> Withdrawal:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(credits_amount, "credits_amount")
    _require_text(ton_wallet_address, "ton_wallet_address")
    if charge_ids_used is not None and not isinstance(charge_ids_used, list):
        raise TypeError("charge_ids_used must be a list")

    withdrawal = Withdrawal(
        user_id=user_id,
        credits_amount=credits_amount,
        charge_ids_used=charge_ids_used or [],
        ton_wallet_address=ton_wallet_address,
        status=WithdrawalStatus.PENDING,
    )
    session.add(withdrawal)
    await session.flush()
    logger.info(
        "Created withdrawal request: id=%d user_id=%d stars=%d",
        withdrawal.id,
        user_id,
        credits_amount,
    )
    return withdrawal


@db_operation("get_withdrawal")
async def get_withdrawal(
    session: AsyncSession,
    withdrawal_id: int,
    for_update: bool = False,
) -> Withdrawal | None:
    _require_positive_int(withdrawal_id, "withdrawal_id")
    stmt = select(Withdrawal).where(Withdrawal.id == withdrawal_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


@db_operation("get_pending_withdrawals")
async def get_pending_withdrawals(session: AsyncSession) -> list[Withdrawal]:
    stmt = (
        select(Withdrawal)
        .where(Withdrawal.status == WithdrawalStatus.PENDING)
        .order_by(Withdrawal.created_at.asc(), Withdrawal.id.asc())
    )
    withdrawals = list((await session.scalars(stmt)).all())
    logger.debug("Loaded pending withdrawals: count=%d", len(withdrawals))
    return withdrawals


@db_operation("mark_withdrawal_paid")
async def mark_withdrawal_paid(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    ton_tx_hash: str,
    admin_note: str | None = None,
) -> Withdrawal:
    _require_positive_int(withdrawal_id, "withdrawal_id")
    _require_positive_int(admin_id, "admin_id")
    _require_text(ton_tx_hash, "ton_tx_hash")

    withdrawal = await get_withdrawal(session, withdrawal_id, for_update=True)
    if withdrawal is None:
        raise RecordNotFoundError(f"Withdrawal {withdrawal_id} was not found")
    if withdrawal.status != WithdrawalStatus.PENDING:
        raise ValueError("withdrawal is not pending")

    withdrawal.status = WithdrawalStatus.COMPLETED
    withdrawal.admin_id = admin_id
    withdrawal.ton_tx_hash = ton_tx_hash
    withdrawal.admin_note = admin_note
    withdrawal.updated_at = datetime.now(timezone.utc)
    await session.flush()
    logger.info(
        "Marked withdrawal paid: id=%d user_id=%d admin_id=%d",
        withdrawal.id,
        withdrawal.user_id,
        admin_id,
    )
    return withdrawal


@db_operation("mark_withdrawal_failed")
async def mark_withdrawal_failed(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    admin_note: str | None = None,
) -> Withdrawal:
    _require_positive_int(withdrawal_id, "withdrawal_id")
    _require_positive_int(admin_id, "admin_id")

    withdrawal = await get_withdrawal(session, withdrawal_id, for_update=True)
    if withdrawal is None:
        raise RecordNotFoundError(f"Withdrawal {withdrawal_id} was not found")
    if withdrawal.status != WithdrawalStatus.PENDING:
        raise ValueError("withdrawal is not pending")

    withdrawal.status = WithdrawalStatus.FAILED
    withdrawal.admin_id = admin_id
    withdrawal.admin_note = admin_note
    withdrawal.updated_at = datetime.now(timezone.utc)
    await session.flush()
    logger.info(
        "Marked withdrawal failed: id=%d user_id=%d admin_id=%d",
        withdrawal.id,
        withdrawal.user_id,
        admin_id,
    )
    return withdrawal


@db_operation("create_dispute")
async def create_dispute(
    session: AsyncSession,
    market_id: int,
    raised_by: int,
    reason: str,
) -> Dispute:
    _require_positive_int(market_id, "market_id")
    _require_positive_int(raised_by, "raised_by")
    _require_text(reason, "reason")

    dispute = Dispute(
        market_id=market_id,
        raised_by=raised_by,
        reason=reason,
        status=DisputeStatus.OPEN,
    )
    session.add(dispute)
    await session.flush()
    logger.info(
        "Created dispute: id=%d market_id=%d raised_by=%d",
        dispute.id,
        market_id,
        raised_by,
    )
    return dispute


@db_operation("get_open_dispute_for_market")
async def get_open_dispute_for_market(
    session: AsyncSession,
    market_id: int,
    for_update: bool = False,
) -> Dispute | None:
    _require_positive_int(market_id, "market_id")
    stmt = (
        select(Dispute)
        .where(Dispute.market_id == market_id, Dispute.status == DisputeStatus.OPEN)
        .order_by(Dispute.created_at.asc(), Dispute.id.asc())
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


@db_operation("update_dispute_status")
async def update_dispute_status(
    session: AsyncSession,
    dispute_id: int,
    status: DisputeStatus,
    resolution_note: str | None = None,
) -> Dispute:
    _require_positive_int(dispute_id, "dispute_id")
    if not isinstance(status, DisputeStatus):
        status = DisputeStatus(status)

    stmt = select(Dispute).where(Dispute.id == dispute_id).with_for_update()
    dispute = await session.scalar(stmt)
    if dispute is None:
        raise RecordNotFoundError(f"Dispute {dispute_id} was not found")

    dispute.status = status
    dispute.resolution_note = resolution_note
    await session.flush()
    logger.info(
        "Updated dispute status: id=%d market_id=%d status=%s",
        dispute.id,
        dispute.market_id,
        status.value,
    )
    return dispute


def _require_int(value: int, name: str) -> None:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")


def _require_positive_int(value: int, name: str) -> None:
    _require_int(value, name)
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _require_non_zero_int(value: int, name: str) -> None:
    _require_int(value, name)
    if value == 0:
        raise ValueError(f"{name} must not be zero")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _validate_market_options(options: list[str]) -> None:
    if len(options) < 2:
        raise ValueError("options must contain at least two choices")
    if any(not isinstance(option, str) or not option.strip() for option in options):
        raise ValueError("options must contain only non-empty text")


def _require_timezone_aware_deadline(deadline: datetime) -> None:
    if not isinstance(deadline, datetime):
        raise TypeError("deadline must be a datetime")
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("deadline must be timezone-aware")


def _redact_charge_id(charge_id: str) -> str:
    if len(charge_id) <= 8:
        return "***"
    return f"{charge_id[:4]}...{charge_id[-4:]}"
