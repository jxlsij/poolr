from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot.betting import place_bet
from bot.crud import (
    create_market,
    create_or_get_user,
    get_payouts_for_market,
    get_user_by_id,
    update_market_message_id,
    update_user_balance,
)
from bot.database import create_all_tables, create_session_factory
from bot.models import Base, Market, MarketStatus, Payout
from bot.resolution import (
    ResolutionPersistenceError,
    ResolutionValidationError,
    auto_cancel_market,
    build_resolution_keyboard,
    build_results_text,
    calculate_winner_share,
    handle_resolve_callback,
    parse_resolve_callback_data,
    publish_resolution_results,
    resolve_market,
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
    def __init__(self) -> None:
        self.edited_messages: list[dict] = []
        self.sent_messages: list[dict] = []

    async def edit_message_text(self, **kwargs):
        self.edited_messages.append(kwargs)

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[dict] = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


def test_calculate_winner_share_returns_stake_plus_net_losing_pool_share() -> None:
    assert calculate_winner_share(10, 40, 80, 0.08) == 19
    assert calculate_winner_share(30, 40, 80, 0.08) == 57


def test_resolution_callback_and_keyboard() -> None:
    assert parse_resolve_callback_data("resolve:123:1") == (123, 1)

    keyboard = build_resolution_keyboard(123, ["Yes", "No"])

    assert keyboard.inline_keyboard[0][0].callback_data == "resolve:123:0"
    assert keyboard.inline_keyboard[1][0].callback_data == "resolve:123:1"


@pytest.mark.asyncio
async def test_resolve_market_distributes_withdrawable_stars(session_factory) -> None:
    async with session_factory() as session:
        market = await _create_market_with_bets(session)
        market.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        result = await resolve_market(
            session=session,
            market_id=market.id,
            winning_option_index=0,
            resolved_by=101,
            platform_fee_pct=0.08,
        )

        winner_one = await get_user_by_id(session, 202)
        winner_two = await get_user_by_id(session, 303)
        loser = await get_user_by_id(session, 404)
        payouts = await get_payouts_for_market(session, market.id)

    assert result.platform_fee_collected == 3
    assert result.total_participants == 3
    assert result.market.status == MarketStatus.RESOLVED
    assert result.market.winning_option == 0
    assert winner_one is not None
    assert winner_one.balance_credits == 19
    assert winner_two is not None
    assert winner_two.balance_credits == 57
    assert loser is not None
    assert loser.balance_credits == 0
    assert [payout.credits_won for payout in payouts] == [57, 19]


@pytest.mark.asyncio
async def test_resolve_market_rejects_non_creator(session_factory) -> None:
    async with session_factory() as session:
        market = await _create_market_with_bets(session)
        market.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        with pytest.raises(ResolutionValidationError):
            await resolve_market(
                session=session,
                market_id=market.id,
                winning_option_index=0,
                resolved_by=202,
                platform_fee_pct=0.08,
            )


@pytest.mark.asyncio
async def test_resolve_market_wraps_unexpected_operation_errors(
    session_factory,
    monkeypatch,
    caplog,
) -> None:
    async def fail_pool_load(*_args, **_kwargs):
        raise RuntimeError("pool exploded")

    monkeypatch.setattr("bot.resolution.get_pool_by_option", fail_pool_load)
    caplog.set_level("ERROR", logger="bot.resolution")

    async with session_factory() as session:
        market = await _create_market_with_bets(session)
        market.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        with pytest.raises(ResolutionPersistenceError):
            await resolve_market(
                session=session,
                market_id=market.id,
                winning_option_index=0,
                resolved_by=101,
                platform_fee_pct=0.08,
            )

    assert "Resolution operation failed unexpectedly: resolve_market" in caplog.text


@pytest.mark.asyncio
async def test_handle_resolve_callback_publishes_results(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_market_with_bets(session)
        await update_market_message_id(session, market.id, 555)
        market.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.flush()

        callback = FakeCallback(f"resolve:{market.id}:0")
        await handle_resolve_callback(callback, session, bot, platform_fee_pct=0.08)

    assert callback.answers[-1] == {"text": "Market resolved.", "show_alert": False}
    assert bot.edited_messages[0]["chat_id"] == -100
    assert bot.edited_messages[0]["message_id"] == 555
    assert "Resolved: Yes" in bot.edited_messages[0]["text"]
    assert bot.sent_messages[0]["chat_id"] == -100
    assert "Winning outcome: Yes" in bot.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_auto_cancel_market_refunds_stakes_after_grace_period(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        market = await _create_market_with_bets(session)
        await update_market_message_id(session, market.id, 555)
        market.deadline = datetime.now(timezone.utc) - timedelta(hours=25)
        await session.flush()

        await auto_cancel_market(session, bot, market)

        bettor_one = await get_user_by_id(session, 202)
        bettor_two = await get_user_by_id(session, 303)
        bettor_three = await get_user_by_id(session, 404)

    assert bettor_one is not None
    assert bettor_one.balance_credits == 10
    assert bettor_two is not None
    assert bettor_two.balance_credits == 30
    assert bettor_three is not None
    assert bettor_three.balance_credits == 40
    assert "Cancelled: stakes refunded." in bot.edited_messages[0]["text"]


def test_build_results_text_uses_stars_language() -> None:
    market = _market()
    payouts = [
        Payout(user_id=202, market_id=123, credits_won=19),
        Payout(user_id=303, market_id=123, credits_won=57),
    ]

    text = build_results_text(market, 0, payouts, platform_fee=3)

    assert "Paid to winners: 76 Stars" in text
    assert "Platform fee: 3 Stars" in text
    assert "credits" not in text.lower()


@pytest.mark.asyncio
async def test_publish_resolution_results_skips_group_post_for_inline_market() -> None:
    bot = FakeBot()
    market = _market(chat_id=0)
    market.inline_message_id = "inline-message-id"
    market.status = MarketStatus.RESOLVED
    market.winning_option = 0

    await publish_resolution_results(bot, market, [], {0: 10}, platform_fee=0)

    assert bot.edited_messages[0]["inline_message_id"] == "inline-message-id"
    assert bot.sent_messages == []


async def _create_market_with_bets(session) -> Market:
    await create_or_get_user(session, 101, "ada", "Ada")
    await create_or_get_user(session, 202, "grace", "Grace")
    await create_or_get_user(session, 303, "max", "Max")
    await create_or_get_user(session, 404, "lin", "Lin")
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
    await update_user_balance(session, 303, 30, "test_stake_balance")
    await update_user_balance(session, 404, 40, "test_stake_balance")
    await place_bet(session, 202, market.id, 0, 10)
    await place_bet(session, 303, market.id, 0, 30)
    await place_bet(session, 404, market.id, 1, 40)
    return market


def _market(chat_id: int = -100) -> Market:
    return Market(
        id=123,
        creator_id=101,
        chat_id=chat_id,
        message_id=555,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
        min_bet=1,
        status=MarketStatus.RESOLVED,
        winning_option=0,
    )
