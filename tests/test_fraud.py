from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot.betting import place_bet
from bot.crud import (
    create_market,
    create_or_get_user,
    get_market,
    get_open_dispute_for_market,
    get_user_by_id,
    update_user_balance,
)
from bot.database import create_all_tables, create_session_factory
from bot.fraud import (
    FraudValidationError,
    SuspicionLevel,
    admin_arbitrate,
    build_admin_dispute_keyboard,
    can_user_bet,
    detect_suspicious_patterns,
    freeze_market_for_dispute,
    handle_arbitrate_callback,
    handle_dispute_callback,
    handle_reject_dispute_callback,
    parse_arbitrate_callback_data,
    parse_dispute_callback_data,
)
from bot.models import Base, Bet, Deposit, DepositStatus, Market, MarketStatus
from bot.resolution import resolve_market


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
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edited_messages.append(kwargs)


class FakeCallback:
    def __init__(self, data: str, user_id: int = 202) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[dict] = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


class FailingAnswerCallback(FakeCallback):
    async def answer(self, **kwargs):
        self.answers.append(kwargs)
        raise RuntimeError("telegram callback answer failed")


def test_can_user_bet_and_suspicion_heuristics() -> None:
    market = _market(status=MarketStatus.ACTIVE)
    assert can_user_bet(202, market) is True
    assert can_user_bet(101, market) is False
    market.status = MarketStatus.DISPUTED
    assert can_user_bet(202, market) is False

    assert detect_suspicious_patterns([], []) == SuspicionLevel.CLEAN
    assert detect_suspicious_patterns(
        [Bet(user_id=202, market_id=index + 1, option_index=0, credits_amount=10) for index in range(8)],
        [],
    ) == SuspicionLevel.SUSPICIOUS
    assert detect_suspicious_patterns(
        [Bet(user_id=202, market_id=1, option_index=0, credits_amount=1001)],
        [Deposit(user_id=202, stars_amount=10, charge_id="charge", status=DepositStatus.CONFIRMED)],
    ) == SuspicionLevel.HIGH_RISK


def test_dispute_callback_parsers_and_admin_keyboard() -> None:
    assert parse_dispute_callback_data("dispute:123") == 123
    assert parse_arbitrate_callback_data("arbitrate:123:1") == (123, 1)

    keyboard = build_admin_dispute_keyboard(123, ["Yes", "No"])

    assert keyboard.inline_keyboard[0][0].callback_data == "arbitrate:123:0"
    assert keyboard.inline_keyboard[1][0].callback_data == "arbitrate:123:1"
    assert keyboard.inline_keyboard[2][0].callback_data == "reject_dispute:123"


@pytest.mark.asyncio
async def test_freeze_market_for_dispute_marks_market_disputed_and_notifies_admin(
    session_factory,
) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_resolved_market(session)

        result = await freeze_market_for_dispute(
            session=session,
            bot=bot,
            market_id=market.id,
            raised_by=202,
            reason="Outcome looks wrong",
            admin_ids=[9001],
        )
        reloaded = await get_market(session, market.id)
        dispute = await get_open_dispute_for_market(session, market.id)

    assert result.market.status == MarketStatus.DISPUTED
    assert reloaded is not None
    assert reloaded.status == MarketStatus.DISPUTED
    assert dispute is not None
    assert dispute.raised_by == 202
    assert bot.sent_messages[0]["chat_id"] == 9001
    assert "Dispute #" in bot.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_freeze_market_for_dispute_rejects_closed_window(session_factory) -> None:
    async with session_factory() as session:
        market = await _create_resolved_market(session)
        market.resolved_at = datetime.now(timezone.utc) - timedelta(hours=25)
        await session.flush()

        with pytest.raises(FraudValidationError):
            await freeze_market_for_dispute(
                session=session,
                bot=FakeBot(),
                market_id=market.id,
                raised_by=202,
                reason="Too late",
                admin_ids=[],
            )


@pytest.mark.asyncio
async def test_handle_dispute_callback_answers_user(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_resolved_market(session)
        callback = FakeCallback(f"dispute:{market.id}", user_id=202)

        await handle_dispute_callback(callback, session, bot, admin_ids=[9001])

    assert callback.answers == [{"text": "Dispute opened. An admin will review it.", "show_alert": False}]
    assert bot.sent_messages[0]["chat_id"] == 9001


@pytest.mark.asyncio
async def test_handle_dispute_callback_tolerates_answer_failure(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_resolved_market(session)
        callback = FailingAnswerCallback(f"dispute:{market.id}", user_id=202)

        await handle_dispute_callback(callback, session, bot, admin_ids=[9001])
        dispute = await get_open_dispute_for_market(session, market.id)

    assert dispute is not None
    assert callback.answers == [{"text": "Dispute opened. An admin will review it.", "show_alert": False}]
    assert bot.sent_messages[0]["chat_id"] == 9001


@pytest.mark.asyncio
async def test_admin_callbacks_handle_invalid_payloads_without_raising(session_factory) -> None:
    async with session_factory() as session:
        arbitrate_callback = FakeCallback("arbitrate:not-an-int:0", user_id=9001)
        reject_callback = FakeCallback("reject_dispute:not-an-int", user_id=9001)

        await handle_arbitrate_callback(
            callback=arbitrate_callback,
            session=session,
            bot=FakeBot(),
            admin_ids=[9001],
            platform_fee_pct=0.08,
        )
        await handle_reject_dispute_callback(reject_callback, session, admin_ids=[9001])

    assert arbitrate_callback.answers == [{"text": "Invalid arbitration button.", "show_alert": True}]
    assert reject_callback.answers == [{"text": "Invalid reject button.", "show_alert": True}]


@pytest.mark.asyncio
async def test_admin_arbitrate_reopens_resolved_status_without_double_payout(
    session_factory,
) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_resolved_market(session)
        winner_before = await get_user_by_id(session, 202)
        await freeze_market_for_dispute(
            session=session,
            bot=bot,
            market_id=market.id,
            raised_by=303,
            reason="Review requested",
            admin_ids=[],
        )

        result = await admin_arbitrate(
            session=session,
            bot=bot,
            market_id=market.id,
            winning_option_index=0,
            admin_id=9001,
            platform_fee_pct=0.08,
        )
        winner_after = await get_user_by_id(session, 202)
        dispute = await get_open_dispute_for_market(session, market.id)

    assert result.market.status == MarketStatus.RESOLVED
    assert result.payouts_created == 1
    assert result.dispute is not None
    assert result.dispute.status.value == "resolved"
    assert dispute is None
    assert winner_before is not None
    assert winner_after is not None
    assert winner_after.balance_credits == winner_before.balance_credits


async def _create_resolved_market(session) -> Market:
    await create_or_get_user(session, 101, "ada", "Ada")
    await create_or_get_user(session, 202, "grace", "Grace")
    await create_or_get_user(session, 303, "max", "Max")
    market = await create_market(
        session=session,
        creator_id=101,
        chat_id=-100,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=datetime.now(timezone.utc) + timedelta(hours=2),
        min_bet=1,
    )
    await update_user_balance(session, 202, 10, "test_stake_balance")
    await update_user_balance(session, 303, 20, "test_stake_balance")
    await place_bet(session, 202, market.id, 0, 10)
    await place_bet(session, 303, market.id, 1, 20)
    market.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.flush()
    result = await resolve_market(
        session=session,
        market_id=market.id,
        winning_option_index=0,
        resolved_by=101,
        platform_fee_pct=0.08,
    )
    return result.market


def _market(status: MarketStatus) -> Market:
    return Market(
        id=123,
        creator_id=101,
        chat_id=-100,
        message_id=555,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
        min_bet=1,
        status=status,
    )
