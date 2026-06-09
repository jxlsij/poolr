from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import (
    DatabaseLayerError,
    InsufficientBalanceError,
    RecordNotFoundError,
    create_withdrawal,
    get_pending_withdrawals,
    get_user_by_id,
    get_withdrawal,
    mark_withdrawal_failed,
    mark_withdrawal_paid,
    update_user_balance,
)
from bot.models import User, Withdrawal, WithdrawalStatus
from bot.product_limits import (
    MAX_WITHDRAWAL_STARS,
    ProductLimitError,
    require_stars_limit,
)
from bot.security import is_admin
from bot.users import UserModuleError, ensure_user


logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")

WITHDRAW_PAID_PREFIX = "withdraw_paid"
WITHDRAW_REJECT_PREFIX = "withdraw_reject"
TON_FRIENDLY_WALLET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{48}$")
TON_RAW_WALLET_PATTERN = re.compile(r"^-?[0-9]+:[0-9a-fA-F]{64}$")
TON_TX_HASH_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{12,160}$")


class WithdrawalModuleError(RuntimeError):
    """Base error for Module 9 withdrawal operations."""


class WithdrawalValidationError(WithdrawalModuleError, ValueError):
    """Raised when a withdrawal request is invalid."""


class WithdrawalPersistenceError(WithdrawalModuleError):
    """Raised when withdrawal state cannot be persisted."""


class WithdrawalProviderError(WithdrawalModuleError):
    """Raised when Telegram APIs cannot send withdrawal notifications."""


@dataclass(frozen=True)
class WithdrawalRequestResult:
    withdrawal: Withdrawal
    reserved_stars: int
    remaining_balance: int


@dataclass(frozen=True)
class ManualPayoutResult:
    withdrawal: Withdrawal
    user_id: int
    stars_amount: int
    ton_tx_hash: str | None


class WithdrawalStates(StatesGroup):
    waiting_amount = State()
    waiting_wallet = State()
    waiting_admin_tx_hash = State()


def withdrawal_operation(
    operation_name: str,
    wrapped_error: type[WithdrawalModuleError] = WithdrawalModuleError,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            logger.debug("Withdrawal operation started: %s", operation_name)
            try:
                result = await func(*args, **kwargs)
            except WithdrawalValidationError:
                logger.warning("Withdrawal operation rejected input: %s", operation_name, exc_info=True)
                raise
            except wrapped_error:
                logger.warning("Withdrawal operation failed: %s", operation_name, exc_info=True)
                raise
            except WithdrawalModuleError:
                logger.warning("Withdrawal operation failed: %s", operation_name, exc_info=True)
                raise
            except DatabaseLayerError as exc:
                logger.exception("Withdrawal operation hit database error: %s", operation_name)
                raise wrapped_error(f"Withdrawal operation failed: {operation_name}") from exc
            except (ValueError, TypeError):
                logger.warning("Withdrawal operation rejected invalid input: %s", operation_name, exc_info=True)
                raise
            except Exception as exc:
                logger.exception("Withdrawal operation failed unexpectedly: %s", operation_name)
                raise wrapped_error(
                    f"Unexpected withdrawal operation failure: {operation_name}"
                ) from exc

            logger.debug("Withdrawal operation completed: %s", operation_name)
            return result

        return wrapper

    return decorator


def create_withdrawals_router(admin_ids: list[int] | None = None) -> Router:
    router = Router(name="withdrawals")
    resolved_admin_ids = admin_ids or []

    @router.message(Command("withdraw", ignore_mention=True))
    async def withdraw_command_handler(
        message: Message,
        db_session: AsyncSession,
        state: FSMContext,
        command: CommandObject,
        bot: Bot,
    ) -> None:
        await handle_withdraw_command(
            message=message,
            session=db_session,
            state=state,
            bot=bot,
            admin_ids=resolved_admin_ids,
            command=command,
        )

    @router.message(WithdrawalStates.waiting_amount)
    async def withdraw_amount_handler(
        message: Message,
        db_session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await process_withdrawal_amount(message, db_session, state)

    @router.message(WithdrawalStates.waiting_wallet)
    async def withdraw_wallet_handler(
        message: Message,
        db_session: AsyncSession,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        await process_withdrawal_wallet(
            message=message,
            session=db_session,
            state=state,
            bot=bot,
            admin_ids=resolved_admin_ids,
        )

    @router.callback_query(F.data.startswith(f"{WITHDRAW_PAID_PREFIX}:"))
    async def admin_paid_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await handle_withdraw_paid_callback(callback, db_session, state, resolved_admin_ids)

    @router.callback_query(F.data.startswith(f"{WITHDRAW_REJECT_PREFIX}:"))
    async def admin_reject_callback_handler(
        callback: CallbackQuery,
        db_session: AsyncSession,
        bot: Bot,
    ) -> None:
        await handle_withdraw_reject_callback(callback, db_session, bot, resolved_admin_ids)

    @router.message(WithdrawalStates.waiting_admin_tx_hash)
    async def admin_tx_hash_handler(
        message: Message,
        db_session: AsyncSession,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        await process_admin_tx_hash(message, db_session, state, bot, resolved_admin_ids)

    return router


async def handle_withdraw_command(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    admin_ids: list[int],
    command: CommandObject | None = None,
) -> None:
    if message.from_user is None:
        await message.answer("Could not identify you for payout request.")
        return

    user_id = message.from_user.id
    logger.info("Handling /withdraw command: user_id=%d has_args=%s", user_id, bool(command and command.args))
    try:
        user, _is_new = await ensure_user(session, message.from_user)
    except UserModuleError as exc:
        logger.exception("Could not ensure withdrawal user identity")
        await _answer_message(message, "Could not prepare your payout request. Please try again.")
        raise WithdrawalPersistenceError("Could not ensure withdrawal user identity") from exc

    args = (command.args or "").split() if command and command.args else []
    if len(args) >= 2:
        try:
            amount = parse_withdrawal_amount(args[0])
            wallet = validate_ton_wallet(args[1])
            result = await request_withdrawal(
                session=session,
                user_id=user.telegram_id,
                stars_amount=amount,
                ton_wallet_address=wallet,
                admin_ids=admin_ids,
            )
            await session.commit()
            await notify_admins_for_withdrawal(bot, result.withdrawal, admin_ids)
            await message.answer(_withdrawal_created_text(result.withdrawal))
        except WithdrawalValidationError as exc:
            await message.answer(str(exc))
        except WithdrawalModuleError as exc:
            logger.exception("Direct withdrawal request failed: user_id=%d", user_id)
            await _answer_message(message, "Could not create the payout request. Please try again.")
            raise WithdrawalPersistenceError("Direct withdrawal request failed") from exc
        except Exception as exc:
            logger.exception("Unexpected direct withdrawal request failure: user_id=%d", user_id)
            await _answer_message(message, "Could not create the payout request. Please try again.")
            raise WithdrawalPersistenceError("Unexpected direct withdrawal request failure") from exc
        return

    await state.clear()
    await state.set_state(WithdrawalStates.waiting_amount)
    logger.debug("Withdrawal FSM waiting for amount: user_id=%d balance=%d", user_id, user.balance_credits)
    await message.answer(
        f"Withdrawable balance: {user.balance_credits} Stars.\n"
        "Send the amount of Stars you want paid out in TON equivalent."
    )


async def process_withdrawal_amount(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    del session

    try:
        amount = parse_withdrawal_amount(message.text)
    except WithdrawalValidationError as exc:
        await message.answer(str(exc))
        return

    await state.update_data(withdrawal_amount=amount)
    await state.set_state(WithdrawalStates.waiting_wallet)
    logger.debug(
        "Withdrawal FSM accepted amount: user_id=%s stars=%d",
        message.from_user.id if message.from_user else None,
        amount,
    )
    await message.answer("Send your TON wallet address for manual payout.")


async def process_withdrawal_wallet(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    admin_ids: list[int],
) -> None:
    if message.from_user is None:
        await message.answer("Could not identify you for payout request.")
        return

    data = await state.get_data()
    amount = data.get("withdrawal_amount")
    if not isinstance(amount, int):
        await state.clear()
        await message.answer("This payout request expired. Send /withdraw again.")
        return

    user_id = message.from_user.id
    try:
        wallet = validate_ton_wallet(message.text)
        result = await request_withdrawal(
            session=session,
            user_id=user_id,
            stars_amount=amount,
            ton_wallet_address=wallet,
            admin_ids=admin_ids,
        )
        await session.commit()
        await notify_admins_for_withdrawal(bot, result.withdrawal, admin_ids)
        await message.answer(_withdrawal_created_text(result.withdrawal))
    except WithdrawalValidationError as exc:
        await message.answer(str(exc))
        return
    except WithdrawalModuleError as exc:
        logger.exception("Withdrawal wallet step failed: user_id=%d stars=%d", user_id, amount)
        await _answer_message(message, "Could not create the payout request. Please try again.")
        await state.clear()
        raise WithdrawalPersistenceError("Withdrawal wallet step failed") from exc
    except Exception as exc:
        logger.exception("Unexpected withdrawal wallet step failure: user_id=%d stars=%d", user_id, amount)
        await _answer_message(message, "Could not create the payout request. Please try again.")
        await state.clear()
        raise WithdrawalPersistenceError("Unexpected withdrawal wallet step failure") from exc

    await state.clear()


@withdrawal_operation("request_withdrawal", WithdrawalPersistenceError)
async def request_withdrawal(
    session: AsyncSession,
    user_id: int,
    stars_amount: int,
    ton_wallet_address: str,
    admin_ids: list[int] | None = None,
) -> WithdrawalRequestResult:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(stars_amount, "stars_amount")
    if admin_ids is not None and not admin_ids:
        raise WithdrawalValidationError("Payout requests are temporarily unavailable: no payout admins are configured.")
    try:
        require_stars_limit(stars_amount, MAX_WITHDRAWAL_STARS, "Withdrawal amount")
    except ProductLimitError as exc:
        raise WithdrawalValidationError(str(exc)) from exc
    wallet = validate_ton_wallet(ton_wallet_address)

    user = await _get_user_for_update(session, user_id)
    if user.balance_credits < stars_amount:
        logger.info(
            "Withdrawal request rejected for insufficient Stars: user_id=%d requested=%d available=%d",
            user_id,
            stars_amount,
            user.balance_credits,
        )
        raise WithdrawalValidationError(
            f"Insufficient withdrawable Stars. Available: {user.balance_credits} Stars."
        )

    try:
        updated_user = await update_user_balance(
            session=session,
            telegram_id=user_id,
            delta=-stars_amount,
            reason="manual_ton_withdrawal_reserved",
        )
    except InsufficientBalanceError as exc:
        raise WithdrawalValidationError("Insufficient withdrawable Stars.") from exc

    withdrawal = await create_withdrawal(
        session=session,
        user_id=user_id,
        credits_amount=stars_amount,
        ton_wallet_address=wallet,
        charge_ids_used=[],
    )
    logger.info(
        "Withdrawal requested: id=%d user_id=%d stars=%d remaining_balance=%d",
        withdrawal.id,
        user_id,
        stars_amount,
        updated_user.balance_credits,
    )
    return WithdrawalRequestResult(
        withdrawal=withdrawal,
        reserved_stars=stars_amount,
        remaining_balance=updated_user.balance_credits,
    )


@withdrawal_operation("mark_manual_payout_paid", WithdrawalPersistenceError)
async def mark_manual_payout_paid(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    ton_tx_hash: str,
    admin_note: str | None = None,
) -> ManualPayoutResult:
    tx_hash = validate_ton_tx_hash(ton_tx_hash)
    withdrawal = await mark_withdrawal_paid(
        session=session,
        withdrawal_id=withdrawal_id,
        admin_id=admin_id,
        ton_tx_hash=tx_hash,
        admin_note=admin_note,
    )
    logger.info(
        "Manual TON-equivalent payout recorded: withdrawal_id=%d user_id=%d admin_id=%d stars=%d",
        withdrawal.id,
        withdrawal.user_id,
        admin_id,
        withdrawal.credits_amount,
    )
    return ManualPayoutResult(
        withdrawal=withdrawal,
        user_id=withdrawal.user_id,
        stars_amount=withdrawal.credits_amount,
        ton_tx_hash=withdrawal.ton_tx_hash,
    )


@withdrawal_operation("reject_withdrawal", WithdrawalPersistenceError)
async def reject_withdrawal(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    admin_note: str | None = None,
) -> ManualPayoutResult:
    withdrawal = await get_withdrawal(session, withdrawal_id, for_update=True)
    if withdrawal is None:
        raise RecordNotFoundError(f"Withdrawal {withdrawal_id} was not found")
    if withdrawal.status != WithdrawalStatus.PENDING:
        raise WithdrawalValidationError("Withdrawal is not pending.")

    await update_user_balance(
        session=session,
        telegram_id=withdrawal.user_id,
        delta=withdrawal.credits_amount,
        reason=f"manual_ton_withdrawal_rejected:{withdrawal.id}",
    )
    withdrawal = await mark_withdrawal_failed(
        session=session,
        withdrawal_id=withdrawal_id,
        admin_id=admin_id,
        admin_note=admin_note,
    )
    logger.info(
        "Withdrawal rejected and Stars returned: withdrawal_id=%d user_id=%d stars=%d admin_id=%d",
        withdrawal.id,
        withdrawal.user_id,
        withdrawal.credits_amount,
        admin_id,
    )
    return ManualPayoutResult(
        withdrawal=withdrawal,
        user_id=withdrawal.user_id,
        stars_amount=withdrawal.credits_amount,
        ton_tx_hash=None,
    )


async def handle_withdraw_paid_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    admin_ids: list[int],
) -> None:
    del session

    admin_id = _callback_user_id(callback)
    if not is_admin(admin_id, admin_ids):
        await _answer_callback(callback, "Access denied", show_alert=True)
        return

    try:
        withdrawal_id = parse_withdrawal_callback_data(callback.data, WITHDRAW_PAID_PREFIX)
    except ValueError:
        await _answer_callback(callback, "Invalid payout button.", show_alert=True)
        return

    await state.clear()
    await state.update_data(withdrawal_id=withdrawal_id)
    await state.set_state(WithdrawalStates.waiting_admin_tx_hash)
    logger.info(
        "Admin payout mark-paid flow started: withdrawal_id=%d admin_id=%d",
        withdrawal_id,
        admin_id,
    )
    await _answer_callback(callback, "Send TON transaction hash.")


async def handle_withdraw_reject_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    admin_ids: list[int],
) -> None:
    admin_id = _callback_user_id(callback)
    if not is_admin(admin_id, admin_ids):
        await _answer_callback(callback, "Access denied", show_alert=True)
        return

    try:
        withdrawal_id = parse_withdrawal_callback_data(callback.data, WITHDRAW_REJECT_PREFIX)
        result = await reject_withdrawal(
            session=session,
            withdrawal_id=withdrawal_id,
            admin_id=admin_id,
            admin_note="Rejected by admin from bot callback.",
        )
        await session.commit()
        await notify_user_withdrawal_rejected(bot, result.withdrawal)
        await _answer_callback(callback, "Payout request rejected and Stars returned.")
    except WithdrawalValidationError as exc:
        await _answer_callback(callback, str(exc), show_alert=True)
    except WithdrawalModuleError as exc:
        logger.exception("Withdrawal rejection callback failed")
        await _answer_callback(callback, "Could not reject payout request.", show_alert=True)
        raise WithdrawalPersistenceError("Withdrawal rejection callback failed") from exc
    except Exception as exc:
        logger.exception("Unexpected withdrawal rejection callback failure")
        await _answer_callback(callback, "Could not reject payout request.", show_alert=True)
        raise WithdrawalPersistenceError("Unexpected withdrawal rejection callback failure") from exc


async def process_admin_tx_hash(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
    admin_ids: list[int],
) -> None:
    admin_id = message.from_user.id if message.from_user else None
    if admin_id is None or not is_admin(admin_id, admin_ids):
        await message.answer("Access denied")
        return

    data = await state.get_data()
    withdrawal_id = data.get("withdrawal_id")
    if not isinstance(withdrawal_id, int):
        await state.clear()
        await message.answer("This admin payout session expired.")
        return

    try:
        tx_hash = validate_ton_tx_hash(message.text)
        result = await mark_manual_payout_paid(
            session=session,
            withdrawal_id=withdrawal_id,
            admin_id=admin_id,
            ton_tx_hash=tx_hash,
        )
        await session.commit()
        await notify_user_withdrawal_paid(bot, result.withdrawal)
        await message.answer(f"Payout #{withdrawal_id} marked paid.")
    except WithdrawalValidationError as exc:
        await message.answer(str(exc))
        return
    except WithdrawalModuleError as exc:
        logger.exception(
            "Admin tx hash payout completion failed: withdrawal_id=%d admin_id=%d",
            withdrawal_id,
            admin_id,
        )
        await _answer_message(message, "Could not mark this payout paid. Please try again.")
        await state.clear()
        raise WithdrawalPersistenceError("Admin tx hash payout completion failed") from exc
    except Exception as exc:
        logger.exception(
            "Unexpected admin tx hash payout completion failure: withdrawal_id=%d admin_id=%d",
            withdrawal_id,
            admin_id,
        )
        await _answer_message(message, "Could not mark this payout paid. Please try again.")
        await state.clear()
        raise WithdrawalPersistenceError("Unexpected admin tx hash payout completion failure") from exc

    await state.clear()


@withdrawal_operation("notify_admins_for_withdrawal", WithdrawalProviderError)
async def notify_admins_for_withdrawal(
    bot: Bot,
    withdrawal: Withdrawal,
    admin_ids: list[int],
) -> None:
    if not admin_ids:
        logger.warning("No admins configured for withdrawal notification: withdrawal_id=%d", withdrawal.id)
        return

    keyboard = build_admin_withdrawal_keyboard(withdrawal.id)
    text = build_admin_withdrawal_text(withdrawal)
    for admin_id in admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
            logger.info(
                "Admin withdrawal notification sent: withdrawal_id=%d admin_id=%d",
                withdrawal.id,
                admin_id,
            )
        except Exception:
            logger.exception(
                "Failed to notify admin about withdrawal: withdrawal_id=%d admin_id=%d",
                withdrawal.id,
                admin_id,
            )


async def notify_user_withdrawal_paid(bot: Bot, withdrawal: Withdrawal) -> None:
    try:
        await bot.send_message(
            chat_id=withdrawal.user_id,
            text=(
                f"Your payout request #{withdrawal.id} was marked paid.\n"
                f"Amount: {withdrawal.credits_amount} Stars in TON equivalent.\n"
                f"TON tx: {withdrawal.ton_tx_hash}"
            ),
        )
    except Exception:
        logger.exception("Failed to notify user about paid withdrawal: withdrawal_id=%d", withdrawal.id)


async def notify_user_withdrawal_rejected(bot: Bot, withdrawal: Withdrawal) -> None:
    try:
        await bot.send_message(
            chat_id=withdrawal.user_id,
            text=(
                f"Your payout request #{withdrawal.id} was rejected.\n"
                f"{withdrawal.credits_amount} Stars were returned to your balance."
            ),
        )
    except Exception:
        logger.exception("Failed to notify user about rejected withdrawal: withdrawal_id=%d", withdrawal.id)


def build_admin_withdrawal_keyboard(withdrawal_id: int) -> InlineKeyboardMarkup:
    _require_positive_int(withdrawal_id, "withdrawal_id")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Mark paid",
                    callback_data=f"{WITHDRAW_PAID_PREFIX}:{withdrawal_id}",
                ),
                InlineKeyboardButton(
                    text="Reject",
                    callback_data=f"{WITHDRAW_REJECT_PREFIX}:{withdrawal_id}",
                ),
            ]
        ]
    )


def build_admin_withdrawal_text(withdrawal: Withdrawal) -> str:
    return (
        f"Payout request #{withdrawal.id}\n\n"
        f"User: {withdrawal.user_id}\n"
        f"Amount: {withdrawal.credits_amount} Stars\n"
        f"TON wallet: {withdrawal.ton_wallet_address}\n\n"
        "Pay manually in TON equivalent, then press Mark paid and send the TON tx hash."
    )


def parse_withdrawal_amount(value: str | None) -> int:
    if not isinstance(value, str) or not value.strip():
        raise WithdrawalValidationError("Send a whole-number amount of Stars.")
    try:
        amount = int(value.strip())
    except ValueError as exc:
        raise WithdrawalValidationError("Withdrawal amount must be a whole number of Stars.") from exc
    if amount < 1:
        raise WithdrawalValidationError("Withdrawal amount must be at least 1 Star.")
    try:
        require_stars_limit(amount, MAX_WITHDRAWAL_STARS, "Withdrawal amount")
    except ProductLimitError as exc:
        raise WithdrawalValidationError(str(exc)) from exc
    return amount


def validate_ton_wallet(value: str | None) -> str:
    if not isinstance(value, str):
        raise WithdrawalValidationError("TON wallet address is required.")
    wallet = value.strip()
    if TON_RAW_WALLET_PATTERN.fullmatch(wallet):
        return wallet
    if TON_FRIENDLY_WALLET_PATTERN.fullmatch(wallet) and wallet[0] in {"E", "U", "k", "0"}:
        return wallet
    raise WithdrawalValidationError(
        "TON wallet address looks invalid. Use a 48-character TON friendly address or raw workchain:hex format."
    )


def validate_ton_tx_hash(value: str | None) -> str:
    if not isinstance(value, str):
        raise WithdrawalValidationError("TON transaction hash is required.")
    tx_hash = value.strip()
    if not TON_TX_HASH_PATTERN.fullmatch(tx_hash):
        raise WithdrawalValidationError("TON transaction hash looks invalid.")
    return tx_hash


def parse_withdrawal_callback_data(callback_data: str | None, prefix: str) -> int:
    if not isinstance(callback_data, str):
        raise ValueError("callback data is required")
    parts = callback_data.split(":")
    if len(parts) != 2 or parts[0] != prefix:
        raise ValueError(f"callback data must have format {prefix}:{{withdrawal_id}}")
    return _require_positive_int(_parse_int(parts[1], "withdrawal_id"), "withdrawal_id")


async def list_pending_withdrawals(session: AsyncSession) -> list[Withdrawal]:
    return await get_pending_withdrawals(session)


def _withdrawal_created_text(withdrawal: Withdrawal) -> str:
    return (
        f"Payout request #{withdrawal.id} created.\n"
        f"Reserved: {withdrawal.credits_amount} Stars.\n"
        "An admin will review and pay the TON equivalent manually during beta."
    )


async def _get_user_for_update(session: AsyncSession, user_id: int) -> User:
    stmt = select(User).where(User.telegram_id == user_id).with_for_update()
    user = await session.scalar(stmt)
    if user is None:
        raise RecordNotFoundError(f"User {user_id} was not found")
    return user


async def _answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception as exc:
        logger.exception("Failed to answer withdrawal callback")
        raise WithdrawalProviderError("Failed to answer withdrawal callback") from exc


async def _answer_message(message: Message, text: str) -> None:
    try:
        await message.answer(text)
    except Exception as exc:
        logger.exception("Failed to answer withdrawal message")
        raise WithdrawalProviderError("Failed to answer withdrawal message") from exc


def _callback_user_id(callback: CallbackQuery) -> int:
    user_id = callback.from_user.id if callback.from_user else None
    if not isinstance(user_id, int):
        return 0
    return user_id


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
