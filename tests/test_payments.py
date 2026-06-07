from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import create_or_get_user, get_deposit_by_charge_id, get_user_by_id
from bot.database import create_all_tables, create_session_factory
from bot.models import Base, DepositStatus
from bot.payments import (
    PaymentProviderError,
    PaymentValidationError,
    build_deposit_invoice_payload,
    credit_credits,
    debit_credits,
    handle_pre_checkout_query,
    handle_successful_payment,
    parse_deposit_invoice_payload,
    send_deposit_invoice,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_all_tables(engine)
    try:
        yield create_session_factory(engine)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class FakeBot:
    def __init__(self, should_fail: bool = False) -> None:
        self.invoice_kwargs = None
        self.should_fail = should_fail

    async def send_invoice(self, **kwargs):
        if self.should_fail:
            raise RuntimeError("telegram api failed")
        self.invoice_kwargs = kwargs
        return SimpleNamespace(message_id=1)


class FakePreCheckoutQuery:
    def __init__(
        self,
        payload: str,
        currency: str = "XTR",
        total_amount: int = 10,
        user_id: int = 101,
        answer_should_fail: bool = False,
    ) -> None:
        self.id = "pre-checkout-id"
        self.currency = currency
        self.total_amount = total_amount
        self.invoice_payload = payload
        self.from_user = SimpleNamespace(id=user_id)
        self.answer_kwargs = None
        self.answer_should_fail = answer_should_fail

    async def answer(self, **kwargs):
        if self.answer_should_fail:
            raise RuntimeError("answer failed")
        self.answer_kwargs = kwargs


class FakePaymentMessage:
    def __init__(
        self,
        payload: str,
        charge_id: str = "telegram-charge-id",
        user_id: int = 101,
        total_amount: int = 10,
        currency: str = "XTR",
        answer_should_fail: bool = False,
    ) -> None:
        self.from_user = TelegramUser(
            id=user_id,
            is_bot=False,
            first_name="Ada",
            username="ada",
        )
        self.successful_payment = SimpleNamespace(
            currency=currency,
            total_amount=total_amount,
            invoice_payload=payload,
            telegram_payment_charge_id=charge_id,
        )
        self.answers: list[str] = []
        self.answer_should_fail = answer_should_fail

    async def answer(self, text: str) -> None:
        if self.answer_should_fail:
            raise RuntimeError("answer failed")
        self.answers.append(text)


def test_build_and_parse_deposit_invoice_payload() -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)

    assert parse_deposit_invoice_payload(payload) == {
        "user_id": 101,
        "stars_amount": 10,
    }


def test_parse_deposit_invoice_payload_rejects_bad_json() -> None:
    with pytest.raises(PaymentValidationError):
        parse_deposit_invoice_payload("not-json")


@pytest.mark.asyncio
async def test_send_deposit_invoice_uses_stars_currency() -> None:
    bot = FakeBot()

    message = await send_deposit_invoice(bot, user_id=101, stars_amount=10)

    assert message.message_id == 1
    assert bot.invoice_kwargs["chat_id"] == 101
    assert bot.invoice_kwargs["currency"] == "XTR"
    assert bot.invoice_kwargs["provider_token"] == ""
    assert bot.invoice_kwargs["prices"][0].amount == 10


@pytest.mark.asyncio
async def test_send_deposit_invoice_wraps_provider_failures(caplog) -> None:
    bot = FakeBot(should_fail=True)
    caplog.set_level("ERROR", logger="bot.payments")

    with pytest.raises(PaymentProviderError):
        await send_deposit_invoice(bot, user_id=101, stars_amount=10)

    assert "Failed to send Stars invoice" in caplog.text
    assert "user_id=101" in caplog.text


@pytest.mark.asyncio
async def test_handle_pre_checkout_query_accepts_valid_payload(session_factory) -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)
    query = FakePreCheckoutQuery(payload=payload)

    async with session_factory() as session:
        await handle_pre_checkout_query(query, session)

    assert query.answer_kwargs == {"ok": True}


@pytest.mark.asyncio
async def test_handle_pre_checkout_query_rejects_mismatched_amount(session_factory) -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)
    query = FakePreCheckoutQuery(payload=payload, total_amount=9)

    async with session_factory() as session:
        await handle_pre_checkout_query(query, session)

    assert query.answer_kwargs["ok"] is False
    assert "error_message" in query.answer_kwargs


@pytest.mark.asyncio
async def test_handle_pre_checkout_query_wraps_answer_failures(
    session_factory,
    caplog,
) -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)
    query = FakePreCheckoutQuery(payload=payload, answer_should_fail=True)
    caplog.set_level("ERROR", logger="bot.payments")

    async with session_factory() as session:
        with pytest.raises(PaymentProviderError):
            await handle_pre_checkout_query(query, session)

    assert "Failed to answer Stars pre-checkout query" in caplog.text
    assert "query_id=pre-checkout-id" in caplog.text


@pytest.mark.asyncio
async def test_handle_successful_payment_records_deposit_and_credits_user(
    session_factory,
) -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)
    message = FakePaymentMessage(payload=payload)

    async with session_factory() as session:
        await handle_successful_payment(message, session)

        user = await get_user_by_id(session, 101)
        deposit = await get_deposit_by_charge_id(session, "telegram-charge-id")

    assert user is not None
    assert user.balance_credits == 10
    assert deposit is not None
    assert deposit.status == DepositStatus.CONFIRMED
    assert deposit.stars_amount == 10
    assert message.answers == ["Payment received: 10 Stars."]


@pytest.mark.asyncio
async def test_handle_successful_payment_keeps_record_when_confirmation_fails(
    session_factory,
    caplog,
) -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)
    message = FakePaymentMessage(payload=payload, answer_should_fail=True)
    caplog.set_level("ERROR", logger="bot.payments")

    async with session_factory() as session:
        await handle_successful_payment(message, session)

        user = await get_user_by_id(session, 101)
        deposit = await get_deposit_by_charge_id(session, "telegram-charge-id")

    assert user is not None
    assert user.balance_credits == 10
    assert deposit is not None
    assert deposit.status == DepositStatus.CONFIRMED
    assert "Failed to send successful payment confirmation" in caplog.text


@pytest.mark.asyncio
async def test_handle_successful_payment_is_idempotent_for_confirmed_charge(
    session_factory,
) -> None:
    payload = build_deposit_invoice_payload(user_id=101, stars_amount=10)
    message = FakePaymentMessage(payload=payload)

    async with session_factory() as session:
        await handle_successful_payment(message, session)
        await handle_successful_payment(message, session)

        user = await get_user_by_id(session, 101)

    assert user is not None
    assert user.balance_credits == 10


@pytest.mark.asyncio
async def test_debit_and_credit_credits(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        await credit_credits(session, 101, 10, "test_credit")

        assert await debit_credits(session, 101, 7, "test_debit") is True
        assert await debit_credits(session, 101, 4, "test_overdraft") is False

        user = await get_user_by_id(session, 101)

    assert user is not None
    assert user.balance_credits == 3
