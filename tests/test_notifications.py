from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import (
    create_bet,
    create_market,
    create_or_get_user,
    get_market,
    get_user_by_id,
    update_market_message_id,
)
from bot.database import create_all_tables, create_session_factory
from bot.models import Base, Bet, Market, MarketStatus, NotificationLog, Payout
from bot.notifications import (
    NotificationProviderError,
    build_closed_market_card_text,
    notify_bet_confirmed,
    notify_payout_received,
    run_expiry_check,
    schedule_market_jobs,
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
    def __init__(self, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []
        self.edited_media: list[dict] = []

    async def send_message(self, **kwargs):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent_messages.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edited_messages.append(kwargs)

    async def edit_message_media(self, **kwargs):
        self.edited_media.append(kwargs)


class CreatorBlockedBot(FakeBot):
    async def send_message(self, **kwargs):
        if kwargs.get("chat_id") == 101:
            raise RuntimeError("creator blocked bot")
        await super().send_message(**kwargs)


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, *args, **kwargs) -> None:
        self.jobs.append({"args": args, "kwargs": kwargs})


@pytest.mark.asyncio
async def test_run_expiry_check_sends_deadline_reminder_once(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        market = await create_market(
            session=session,
            creator_id=101,
            chat_id=-100,
            question="Will it rain?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(minutes=30),
            min_bet=1,
        )

        first = await run_expiry_check(session, bot)
        second = await run_expiry_check(session, bot)
        logs = list((await session.scalars(select(NotificationLog))).all())

    assert first.deadline_approaching_sent == 1
    assert second.deadline_approaching_sent == 0
    assert len(bot.sent_messages) == 1
    assert bot.sent_messages[0]["chat_id"] == -100
    assert "closes in about 1 hour" in bot.sent_messages[0]["text"]
    assert [(log.kind, log.market_id) for log in logs] == [("deadline_approaching", market.id)]


@pytest.mark.asyncio
async def test_run_expiry_check_closes_and_auto_cancels_stale_market(session_factory) -> None:
    bot = FakeBot()

    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        await create_or_get_user(session, 202, "grace", "Grace")
        market = await create_market(
            session=session,
            creator_id=101,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) - timedelta(hours=25),
            min_bet=1,
        )
        await update_market_message_id(session, market.id, 555)
        await create_bet(session, 202, market.id, 0, 7)

        result = await run_expiry_check(session, bot)
        reloaded = await get_market(session, market.id)
        bettor = await get_user_by_id(session, 202)
        logs = list((await session.scalars(select(NotificationLog).order_by(NotificationLog.kind))).all())

    assert result.market_closed_sent == 1
    assert result.auto_cancelled == 1
    assert reloaded is not None
    assert reloaded.status == MarketStatus.CANCELLED
    assert bettor is not None
    assert bettor.balance_credits == 7
    assert any(message["chat_id"] == 101 for message in bot.sent_messages)
    assert bot.edited_messages == []
    assert any(edit["chat_id"] == -100 for edit in bot.edited_media)
    assert [(log.kind, log.market_id) for log in logs] == [
        ("market_auto_cancelled", market.id),
        ("market_closed", market.id),
    ]


@pytest.mark.asyncio
async def test_market_closed_notification_tolerates_creator_dm_failure(session_factory) -> None:
    bot = CreatorBlockedBot()

    async with session_factory() as session:
        await create_or_get_user(session, 101, "ada", "Ada")
        market = await create_market(
            session=session,
            creator_id=101,
            chat_id=-100,
            question="Will the deploy pass?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
            min_bet=1,
        )
        await update_market_message_id(session, market.id, 777)

        result = await run_expiry_check(session, bot)
        logs = list((await session.scalars(select(NotificationLog))).all())

    assert result.market_closed_sent == 1
    assert len(bot.edited_media) == 1
    assert bot.edited_media[0]["message_id"] == 777
    assert [(log.kind, log.market_id) for log in logs] == [("market_closed", market.id)]


@pytest.mark.asyncio
async def test_direct_user_notifications_use_stars_wording() -> None:
    bot = FakeBot()
    market = _market()
    bet = Bet(id=1, user_id=202, market_id=123, option_index=0, credits_amount=10)
    payout = Payout(id=2, user_id=202, market_id=123, credits_won=18)

    await notify_bet_confirmed(bot, 202, bet, market, new_balance=3, estimated_payout=18)
    await notify_payout_received(bot, 202, payout, market)

    assert "Bet accepted: 10 Stars" in bot.sent_messages[0]["text"]
    assert "Estimated payout: ~18 Stars" in bot.sent_messages[0]["text"]
    assert "You won 18 Stars" in bot.sent_messages[1]["text"]
    assert "credits" not in bot.sent_messages[0]["text"].lower()
    assert "credits" not in bot.sent_messages[1]["text"].lower()


@pytest.mark.asyncio
async def test_notification_provider_errors_are_wrapped() -> None:
    with pytest.raises(NotificationProviderError):
        await notify_payout_received(FakeBot(fail_send=True), 202, Payout(id=1, user_id=202, market_id=123, credits_won=5), _market())


@pytest.mark.asyncio
async def test_schedule_market_jobs_registers_future_jobs() -> None:
    scheduler = FakeScheduler()
    market = _market(deadline=datetime.now(timezone.utc) + timedelta(hours=2))

    await schedule_market_jobs(scheduler, market)

    assert [job["kwargs"]["id"] for job in scheduler.jobs] == [
        "market:123:deadline_approaching",
        "market:123:market_closed",
        "market:123:market_auto_cancelled",
    ]


def test_build_closed_market_card_text() -> None:
    text = build_closed_market_card_text(_market(), {0: 10, 1: 5})

    assert "Pool: 15 Stars" in text
    assert "Betting is closed" in text


def _market(deadline: datetime | None = None) -> Market:
    return Market(
        id=123,
        creator_id=101,
        chat_id=-100,
        message_id=555,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=deadline or datetime.now(timezone.utc) + timedelta(hours=2),
        min_bet=1,
        status=MarketStatus.ACTIVE,
    )
