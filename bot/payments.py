from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from aiogram import Bot, F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import (
    DatabaseLayerError,
    DuplicateRecordError,
    InsufficientBalanceError,
    confirm_deposit,
    create_deposit,
    get_deposit_by_charge_id,
    update_user_balance,
)
from bot.models import DepositStatus
from bot.product_limits import MAX_DEPOSIT_STARS, ProductLimitError, require_stars_limit
from bot.users import UserModuleError, ensure_user


logger = logging.getLogger(__name__)

STARS_CURRENCY = "XTR"
PAYLOAD_TYPE_DIRECT_STAKE = "stars_stake"
PAYMENT_TITLE = "Poolr Stars stake"


class PaymentModuleError(RuntimeError):
    """Base error for Module 5 payment operations."""


class PaymentValidationError(PaymentModuleError, ValueError):
    """Raised when a Stars invoice or payment payload is invalid."""


class PaymentPersistenceError(PaymentModuleError):
    """Raised when a valid payment cannot be persisted."""


class PaymentProviderError(PaymentModuleError):
    """Raised when Telegram payment APIs cannot be called successfully."""


async def send_deposit_invoice(
    bot: Bot,
    user_id: int,
    stars_amount: int,
    description: str = "Пополнение баланса",
) -> Message:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(stars_amount, "stars_amount")
    try:
        require_stars_limit(stars_amount, MAX_DEPOSIT_STARS, "Stars amount")
    except ProductLimitError as exc:
        raise PaymentValidationError(str(exc)) from exc
    _require_text(description, "description")

    payload = build_deposit_invoice_payload(user_id, stars_amount)
    logger.info(
        "Sending Stars invoice: user_id=%d stars_amount=%d payload=%s",
        user_id,
        stars_amount,
        _redact_payload(payload),
    )
    try:
        return await bot.send_invoice(
            chat_id=user_id,
            title=PAYMENT_TITLE,
            description=description,
            payload=payload,
            provider_token="",
            currency=STARS_CURRENCY,
            prices=[LabeledPrice(label=f"{stars_amount} Stars", amount=stars_amount)],
        )
    except Exception as exc:
        logger.exception(
            "Failed to send Stars invoice: user_id=%d stars_amount=%d",
            user_id,
            stars_amount,
        )
        raise PaymentProviderError("Failed to send Stars invoice") from exc


async def handle_pre_checkout_query(
    query: PreCheckoutQuery,
    session: AsyncSession,
) -> None:
    logger.info(
        "Handling pre-checkout query: query_id=%s user_id=%s currency=%s amount=%s",
        query.id,
        query.from_user.id if query.from_user else None,
        query.currency,
        query.total_amount,
    )
    try:
        payload = parse_invoice_payload(query.invoice_payload)
        payer_id = query.from_user.id if query.from_user else None
        if payload["type"] == PAYLOAD_TYPE_DIRECT_STAKE:
            _validate_stars_payment(
                currency=query.currency,
                total_amount=query.total_amount,
                payload=payload,
                payer_id=payer_id,
            )
        else:
            from bot.betting import validate_stake_pre_checkout

            validation_error = await validate_stake_pre_checkout(
                session=session,
                payload=payload,
                payer_id=payer_id,
                currency=query.currency,
                total_amount=query.total_amount,
            )
            if validation_error is not None:
                raise PaymentValidationError(validation_error.value)
    except PaymentValidationError as exc:
        logger.warning(
            "Rejecting Stars pre-checkout query: query_id=%s reason=%s",
            query.id,
            exc,
        )
        await _answer_pre_checkout_query(
            query,
            ok=False,
            error_message="Invalid Stars payment. Please try again.",
        )
        return
    except Exception as exc:
        logger.exception("Unexpected Stars pre-checkout failure: query_id=%s", query.id)
        await _answer_pre_checkout_query(
            query,
            ok=False,
            error_message="Payment validation failed. Please try again.",
        )
        return

    await _answer_pre_checkout_query(query, ok=True)
    logger.info("Accepted Stars pre-checkout query: query_id=%s", query.id)


async def handle_successful_payment(
    message: Message,
    session: AsyncSession,
    platform_fee_pct: float = 0.08,
) -> None:
    payment = message.successful_payment
    if payment is None:
        raise PaymentValidationError("message.successful_payment is required")

    payer_id = message.from_user.id if message.from_user else None
    logger.info(
        "Handling successful Stars payment: user_id=%s currency=%s amount=%s charge_id=%s",
        payer_id,
        payment.currency,
        payment.total_amount,
        _redact_charge_id(payment.telegram_payment_charge_id),
    )

    try:
        payload = parse_invoice_payload(payment.invoice_payload)
        if payload["type"] == PAYLOAD_TYPE_DIRECT_STAKE:
            _validate_stars_payment(
                currency=payment.currency,
                total_amount=payment.total_amount,
                payload=payload,
                payer_id=payer_id,
            )
        else:
            from bot.betting import (
                BettingModuleError,
                handle_successful_stake_payment,
            )

            _validate_stars_payment(
                currency=payment.currency,
                total_amount=payment.total_amount,
                payload={"user_id": payload["user_id"], "stars_amount": payload["stars_amount"]},
                payer_id=payer_id,
            )
            try:
                await handle_successful_stake_payment(
                    message=message,
                    session=session,
                    payload=payload,
                    platform_fee_pct=platform_fee_pct,
                )
            except BettingModuleError as exc:
                raise PaymentPersistenceError("Stake payment processing failed") from exc
            return
    except PaymentValidationError:
        logger.warning(
            "Rejected successful payment payload: user_id=%s charge_id=%s",
            payer_id,
            _redact_charge_id(payment.telegram_payment_charge_id),
            exc_info=True,
        )
        raise

    if message.from_user is not None:
        try:
            await ensure_user(session, message.from_user)
        except UserModuleError as exc:
            logger.exception("Could not ensure payment user identity: user_id=%s", payer_id)
            raise PaymentPersistenceError("Could not ensure payment user identity") from exc

    await _record_successful_payment(
        session=session,
        user_id=payload["user_id"],
        stars_amount=payload["stars_amount"],
        charge_id=payment.telegram_payment_charge_id,
    )
    try:
        await message.answer(f"Payment received: {payload['stars_amount']} Stars.")
    except Exception:
        logger.exception(
            "Failed to send successful payment confirmation: user_id=%d charge_id=%s",
            payload["user_id"],
            _redact_charge_id(payment.telegram_payment_charge_id),
        )


async def debit_credits(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str,
) -> bool:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(amount, "amount")
    _require_text(reason, "reason")

    try:
        await update_user_balance(session, user_id, -amount, reason)
    except InsufficientBalanceError:
        logger.info(
            "Credit debit rejected for insufficient balance: user_id=%d amount=%d reason=%s",
            user_id,
            amount,
            reason,
        )
        return False
    except DatabaseLayerError as exc:
        logger.exception(
            "Credit debit failed: user_id=%d amount=%d reason=%s",
            user_id,
            amount,
            reason,
        )
        raise PaymentPersistenceError("Credit debit failed") from exc

    logger.info("Credit debit applied: user_id=%d amount=%d reason=%s", user_id, amount, reason)
    return True


async def credit_credits(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str,
) -> None:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(amount, "amount")
    _require_text(reason, "reason")

    try:
        await update_user_balance(session, user_id, amount, reason)
    except DatabaseLayerError as exc:
        logger.exception(
            "Credit credit failed: user_id=%d amount=%d reason=%s",
            user_id,
            amount,
            reason,
        )
        raise PaymentPersistenceError("Credit credit failed") from exc

    logger.info("Credit credit applied: user_id=%d amount=%d reason=%s", user_id, amount, reason)


def build_deposit_invoice_payload(user_id: int, stars_amount: int) -> str:
    _require_positive_int(user_id, "user_id")
    _require_positive_int(stars_amount, "stars_amount")
    try:
        require_stars_limit(stars_amount, MAX_DEPOSIT_STARS, "Stars amount")
    except ProductLimitError as exc:
        raise PaymentValidationError(str(exc)) from exc
    return json.dumps(
        {
            "t": PAYLOAD_TYPE_DIRECT_STAKE,
            "u": user_id,
            "a": stars_amount,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_deposit_invoice_payload(payload: str) -> dict[str, int]:
    parsed = parse_invoice_payload(payload)
    if parsed["type"] != PAYLOAD_TYPE_DIRECT_STAKE:
        raise PaymentValidationError("invoice payload type is unsupported")
    return {"user_id": parsed["user_id"], "stars_amount": parsed["stars_amount"]}


def parse_invoice_payload(payload: str) -> dict[str, Any]:
    _require_text(payload, "payload")
    try:
        raw_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PaymentValidationError("invoice payload is not valid JSON") from exc

    if not isinstance(raw_payload, Mapping):
        raise PaymentValidationError("invoice payload must be a JSON object")

    payload_type = raw_payload.get("t")
    if payload_type == PAYLOAD_TYPE_DIRECT_STAKE:
        user_id = _require_positive_int(raw_payload.get("u"), "payload.user_id")
        stars_amount = _require_positive_int(raw_payload.get("a"), "payload.stars_amount")
        return {
            "type": PAYLOAD_TYPE_DIRECT_STAKE,
            "user_id": user_id,
            "stars_amount": stars_amount,
        }

    from bot.betting import PAYLOAD_TYPE_MARKET_STAKE, parse_stake_invoice_payload

    if payload_type == PAYLOAD_TYPE_MARKET_STAKE:
        parsed_stake = parse_stake_invoice_payload(payload)
        parsed_stake["type"] = PAYLOAD_TYPE_MARKET_STAKE
        return parsed_stake

    raise PaymentValidationError("invoice payload type is unsupported")


def create_payments_router(platform_fee_pct: float = 0.08) -> Router:
    router = Router(name="payments")

    @router.pre_checkout_query()
    async def pre_checkout_handler(
        query: PreCheckoutQuery,
        db_session: AsyncSession,
    ) -> None:
        await handle_pre_checkout_query(query, db_session)

    @router.message(F.successful_payment)
    async def successful_payment_handler(
        message: Message,
        db_session: AsyncSession,
    ) -> None:
        try:
            await handle_successful_payment(message, db_session, platform_fee_pct=platform_fee_pct)
        except PaymentModuleError:
            logger.exception("Successful payment handling failed")
            await db_session.rollback()
            try:
                await message.answer(
                    "Payment received, but processing is delayed. Support was notified."
                )
            except Exception:
                logger.exception("Failed to send successful payment failure fallback")

    return router


async def _record_successful_payment(
    session: AsyncSession,
    user_id: int,
    stars_amount: int,
    charge_id: str,
) -> None:
    _require_text(charge_id, "charge_id")

    try:
        existing = await get_deposit_by_charge_id(session, charge_id, for_update=True)
        if existing is not None and existing.status == DepositStatus.CONFIRMED:
            logger.info(
                "Skipping already confirmed Stars payment: user_id=%d charge_id=%s",
                user_id,
                _redact_charge_id(charge_id),
            )
            return

        if existing is not None:
            if existing.user_id != user_id or existing.stars_amount != stars_amount:
                logger.error(
                    "Existing Stars payment charge_id payload mismatch: charge_id=%s",
                    _redact_charge_id(charge_id),
                )
                raise PaymentPersistenceError("Existing payment charge_id payload mismatch")

        if existing is None:
            await create_deposit(session, user_id, stars_amount, charge_id)

        await credit_credits(session, user_id, stars_amount, "stars_payment_confirmed")
        await confirm_deposit(session, charge_id)
    except DuplicateRecordError:
        logger.info(
            "Stars payment charge_id already exists during create; rechecking: user_id=%d charge_id=%s",
            user_id,
            _redact_charge_id(charge_id),
        )
        existing = await get_deposit_by_charge_id(session, charge_id, for_update=True)
        if existing is not None and existing.status == DepositStatus.CONFIRMED:
            return
        raise PaymentPersistenceError("Duplicate payment charge_id is not confirmed")
    except DatabaseLayerError as exc:
        logger.exception(
            "Failed to record successful Stars payment: user_id=%d stars_amount=%d charge_id=%s",
            user_id,
            stars_amount,
            _redact_charge_id(charge_id),
        )
        raise PaymentPersistenceError("Failed to record successful Stars payment") from exc

    logger.info(
        "Recorded successful Stars payment: user_id=%d stars_amount=%d charge_id=%s",
        user_id,
        stars_amount,
        _redact_charge_id(charge_id),
    )


async def _answer_pre_checkout_query(
    query: PreCheckoutQuery,
    ok: bool,
    error_message: str | None = None,
) -> None:
    answer_kwargs: dict[str, Any] = {"ok": ok}
    if error_message is not None:
        answer_kwargs["error_message"] = error_message

    try:
        await query.answer(**answer_kwargs)
    except Exception as exc:
        logger.exception(
            "Failed to answer Stars pre-checkout query: query_id=%s ok=%s",
            query.id,
            ok,
        )
        raise PaymentProviderError("Failed to answer Stars pre-checkout query") from exc


def _validate_stars_payment(
    currency: str,
    total_amount: int,
    payload: dict[str, int],
    payer_id: int | None,
) -> None:
    if currency != STARS_CURRENCY:
        raise PaymentValidationError("payment currency must be XTR")
    if total_amount != payload["stars_amount"]:
        raise PaymentValidationError("payment amount does not match invoice payload")
    if payer_id is not None and payer_id != payload["user_id"]:
        raise PaymentValidationError("payment user does not match invoice payload")


def _require_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PaymentValidationError(f"{name} must be a positive integer")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaymentValidationError(f"{name} must be non-empty text")
    return value.strip()


def _redact_charge_id(charge_id: str) -> str:
    if len(charge_id) <= 8:
        return "***"
    return f"{charge_id[:4]}...{charge_id[-4:]}"


def _redact_payload(payload: str) -> str:
    try:
        parsed = parse_deposit_invoice_payload(payload)
    except PaymentValidationError:
        return "***"
    return f"{PAYLOAD_TYPE_DIRECT_STAKE}:user={parsed['user_id']}:stars={parsed['stars_amount']}"
