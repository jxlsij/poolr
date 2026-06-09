from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot.admin import (
    AdminProviderError,
    AdminStates,
    build_admin_disputes_text,
    build_admin_stats_text,
    fetch_star_transactions,
    get_platform_stats,
    handle_admin_stats,
    process_broadcast_text,
)
from bot.crud import create_bet, create_market, create_or_get_user, update_market_status
from bot.database import create_all_tables, create_session_factory, session_scope
from bot.models import Base, Dispute, DisputeStatus, MarketStatus


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


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id: int, text: str | None = None) -> None:
        self.from_user = FakeUser(user_id)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs):
        self.answers.append(text)


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True
        self.state = None

    async def set_state(self, state) -> None:
        self.state = state


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeStarBot:
    async def get_star_transactions(self, offset: int, limit: int):
        return ["tx", offset, limit]


@pytest.mark.asyncio
async def test_get_platform_stats_and_text(session_factory) -> None:
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_or_get_user(session, 77, "linus", "Linus")
        market = await create_market(
            session,
            creator_id=42,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        await create_bet(session, 77, market.id, 0, 10)
        await update_market_status(session, market.id, MarketStatus.DISPUTED)
        session.add(
            Dispute(
                market_id=market.id,
                raised_by=77,
                reason="Outcome looks wrong",
                status=DisputeStatus.OPEN,
            )
        )

        stats = await get_platform_stats(session)

    assert stats.total_users == 2
    assert stats.disputed_markets == 1
    assert stats.pending_disputes == 1
    assert stats.total_volume_stars == 10
    assert "Poolr admin stats" in build_admin_stats_text(stats)


@pytest.mark.asyncio
async def test_admin_stats_denies_non_admin(session_factory) -> None:
    message = FakeMessage(12)
    async with session_scope(session_factory) as session:
        await handle_admin_stats(message, session, admin_ids=[42])

    assert message.answers == ["Access denied"]


@pytest.mark.asyncio
async def test_process_broadcast_text_sends_to_all_users(session_factory) -> None:
    message = FakeMessage(42, text="Hello Poolr users")
    state = FakeState()
    bot = FakeBot()
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_or_get_user(session, 77, "linus", "Linus")
        await process_broadcast_text(message, state, session, bot, admin_ids=[42])

    assert state.cleared is True
    assert len(bot.messages) == 2
    assert message.answers[-1] == "Broadcast finished.\nDelivered: 2\nFailed: 0"


@pytest.mark.asyncio
async def test_fetch_star_transactions_uses_bot_api() -> None:
    assert await fetch_star_transactions(FakeStarBot(), offset=2, limit=3) == ["tx", 2, 3]


@pytest.mark.asyncio
async def test_fetch_star_transactions_requires_method() -> None:
    with pytest.raises(AdminProviderError):
        await fetch_star_transactions(object())


def test_build_admin_disputes_text_empty() -> None:
    assert build_admin_disputes_text([]) == "No open disputes."
