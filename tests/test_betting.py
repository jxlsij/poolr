from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import Chat, Message, SuccessfulPayment, User as TelegramUser
from sqlalchemy.ext.asyncio import create_async_engine

from bot.betting import (
    BetValidationError,
    build_stake_invoice_payload,
    calculate_implied_probability,
    create_betting_router,
    estimate_payout,
    parse_bet_callback_data,
    parse_stake_invoice_payload,
    place_bet,
    validate_bet_request,
)
from bot.crud import (
    create_market,
    create_or_get_user,
    get_deposit_by_charge_id,
    get_pool_by_option,
    get_user_bet_on_market,
    get_user_by_id,
    update_market_message_id,
    update_user_balance,
)
from bot.database import create_all_tables, create_session_factory
from bot.models import Base, DepositStatus, Market, MarketStatus, User
from bot.payments import handle_pre_checkout_query, handle_successful_payment


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
    def __init__(self) -> None:
        self.edited_messages: list[dict] = []
        self.edited_media: list[dict] = []

    async def edit_message_text(self, **kwargs):
        self.edited_messages.append(kwargs)

    async def edit_message_media(self, **kwargs):
        self.edited_media.append(kwargs)


class FakePreCheckoutQuery:
    def __init__(self, payload: str, user_id: int = 202, total_amount: int = 10) -> None:
        self.id = "stake-pre-checkout-id"
        self.currency = "XTR"
        self.total_amount = total_amount
        self.invoice_payload = payload
        self.from_user = SimpleNamespace(id=user_id)
        self.answer_kwargs = None

    async def answer(self, **kwargs):
        self.answer_kwargs = kwargs


class FakePaymentMessage:
    def __init__(self, payload: str, bot: FakeBot, charge_id: str = "stake-charge-id") -> None:
        self.from_user = TelegramUser(
            id=202,
            is_bot=False,
            first_name="Grace",
            username="grace",
        )
        self.successful_payment = SimpleNamespace(
            currency="XTR",
            total_amount=10,
            invoice_payload=payload,
            telegram_payment_charge_id=charge_id,
        )
        self.bot = bot
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def test_build_and_parse_stake_invoice_payload() -> None:
    payload = build_stake_invoice_payload(
        user_id=202,
        market_id=303,
        option_index=1,
        stars_amount=10,
    )

    assert parse_stake_invoice_payload(payload) == {
        "user_id": 202,
        "market_id": 303,
        "option_index": 1,
        "stars_amount": 10,
    }


def test_waiting_amount_handler_is_text_only() -> None:
    router = create_betting_router()
    amount_handler = next(
        handler for handler in router.message.handlers if handler.callback.__name__ == "stake_amount_handler"
    )
    text_filter = amount_handler.filters[1].magic
    text_message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=202, type="private"),
        from_user=TelegramUser(id=202, is_bot=False, first_name="Grace"),
        text="1",
    )
    payment_message = Message(
        message_id=2,
        date=datetime.now(timezone.utc),
        chat=Chat(id=202, type="private"),
        from_user=TelegramUser(id=202, is_bot=False, first_name="Grace"),
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=1,
            invoice_payload="{}",
            telegram_payment_charge_id="charge",
            provider_payment_charge_id="provider",
        ),
    )

    assert len(amount_handler.filters) == 2
    assert text_filter is not None
    assert text_filter.resolve(text_message) == "1"
    assert text_filter.resolve(payment_message) is None


def test_parse_bet_callback_data() -> None:
    assert parse_bet_callback_data("bet:303:1") == (303, 1)

    with pytest.raises(ValueError):
        parse_bet_callback_data("bet:0:1")


def test_validate_bet_request_rejects_creator_and_low_balance() -> None:
    creator = User(telegram_id=101, username="ada", first_name="Ada", balance_credits=100)
    bettor = User(telegram_id=202, username="grace", first_name="Grace", balance_credits=5)
    market = _market(creator_id=101, min_bet=10)

    assert (
        validate_bet_request(creator, market, option_index=0, credits_amount=10)
        == BetValidationError.CREATOR_CANNOT_BET
    )
    assert (
        validate_bet_request(bettor, market, option_index=0, credits_amount=10)
        == BetValidationError.INSUFFICIENT_BALANCE
    )
    assert (
        validate_bet_request(bettor, market, option_index=9, credits_amount=10)
        == BetValidationError.INVALID_OPTION
    )


def test_probability_and_payout_estimate() -> None:
    assert calculate_implied_probability({0: 30, 1: 10}) == {0: 0.75, 1: 0.25}
    assert estimate_payout(10, 0, {0: 30, 1: 10}, 0.08) == 11


@pytest.mark.asyncio
async def test_place_bet_debits_balance_and_updates_pool(session_factory) -> None:
    async with session_factory() as session:
        market = await _create_users_and_market(session)
        await update_user_balance(session, 202, 25, "test_balance")

        result = await place_bet(
            session=session,
            user_id=202,
            market_id=market.id,
            option_index=0,
            credits_amount=10,
        )

        user = await get_user_by_id(session, 202)
        bet = await get_user_bet_on_market(session, 202, market.id)
        pool = await get_pool_by_option(session, market.id)

    assert result.success is True
    assert user is not None
    assert user.balance_credits == 15
    assert bet is not None
    assert bet.credits_amount == 10
    assert pool == {0: 10}


@pytest.mark.asyncio
async def test_stake_payment_records_bet_and_updates_market_card(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_users_and_market(session)
        await update_market_message_id(session, market.id, 555)
        payload = build_stake_invoice_payload(
            user_id=202,
            market_id=market.id,
            option_index=1,
            stars_amount=10,
        )

        pre_checkout = FakePreCheckoutQuery(payload)
        await handle_pre_checkout_query(pre_checkout, session)
        message = FakePaymentMessage(payload, bot)
        await handle_successful_payment(message, session)

        bettor = await get_user_by_id(session, 202)
        deposit = await get_deposit_by_charge_id(session, "stake-charge-id")
        bet = await get_user_bet_on_market(session, 202, market.id)

    assert pre_checkout.answer_kwargs == {"ok": True}
    assert bettor is not None
    assert bettor.balance_credits == 0
    assert deposit is not None
    assert deposit.status == DepositStatus.CONFIRMED
    assert bet is not None
    assert bet.option_index == 1
    assert bot.edited_messages == []
    assert bot.edited_media[0]["chat_id"] == -100
    assert bot.edited_media[0]["message_id"] == 555
    assert bot.edited_media[0]["media"].caption.startswith("Poolr market #")
    assert message.answers == ["Bet placed: 10 Stars."]


async def _create_users_and_market(session) -> Market:
    await create_or_get_user(session, 101, "ada", "Ada")
    await create_or_get_user(session, 202, "grace", "Grace")
    return await create_market(
        session=session,
        creator_id=101,
        chat_id=-100,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=datetime.now(timezone.utc) + timedelta(hours=2),
        min_bet=10,
    )


def _market(creator_id: int = 101, min_bet: int = 10) -> Market:
    return Market(
        id=303,
        creator_id=creator_id,
        chat_id=-100,
        message_id=None,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=datetime.now(timezone.utc) + timedelta(hours=2),
        min_bet=min_bet,
        status=MarketStatus.ACTIVE,
    )
