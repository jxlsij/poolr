from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import (
    DatabaseOperationError,
    DuplicateRecordError,
    InsufficientBalanceError,
    confirm_deposit,
    create_bet,
    create_deposit,
    create_market,
    create_or_get_user,
    get_active_markets_in_chat,
    get_available_charge_ids,
    get_markets_past_deadline,
    get_pool_by_option,
    get_user_bet_on_market,
    get_user_by_id,
    update_market_inline_message_id,
    update_market_message_id,
    update_market_status,
    update_user_balance,
)
from bot.database import (
    DatabaseSessionError,
    create_all_tables,
    create_session_factory,
    run_sql_migrations,
    session_scope,
)
from bot.models import Base, DepositStatus, MarketStatus


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


@pytest.mark.asyncio
async def test_create_or_get_user_updates_profile(session_factory) -> None:
    async with session_factory() as session:
        user, is_new = await create_or_get_user(session, 101, "ada", "Ada")
        assert is_new is True
        assert user.balance_credits == 0

        same_user, is_new = await create_or_get_user(session, 101, "ada_new", "Ada L")
        assert is_new is False
        assert same_user.telegram_id == 101
        assert same_user.username == "ada_new"
        assert same_user.first_name == "Ada L"


@pytest.mark.asyncio
async def test_update_user_balance_rejects_overdraft(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, None, "Ada")
        user = await update_user_balance(session, 101, 50, "deposit_confirmed")
        assert user.balance_credits == 50

        with pytest.raises(InsufficientBalanceError):
            await update_user_balance(session, 101, -51, "bet_placed")


@pytest.mark.asyncio
async def test_deposits_confirm_and_return_fifo_charge_ids(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, None, "Ada")
        first = await create_deposit(session, 101, 30, "charge-first")
        second = await create_deposit(session, 101, 50, "charge-second")

        await confirm_deposit(session, "charge-first")
        await confirm_deposit(session, "charge-second")

        assert first.status == DepositStatus.CONFIRMED
        assert second.status == DepositStatus.CONFIRMED
        assert await get_available_charge_ids(session, 101, 60) == [
            "charge-first",
            "charge-second",
        ]
        assert await get_available_charge_ids(session, 101, 100) == []


@pytest.mark.asyncio
async def test_duplicate_deposit_charge_id_is_rejected(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, None, "Ada")
        await create_deposit(session, 101, 30, "charge")

        with pytest.raises(DuplicateRecordError):
            await create_deposit(session, 101, 30, "charge")


@pytest.mark.asyncio
async def test_sqlalchemy_errors_are_logged_and_wrapped(session_factory, caplog) -> None:
    class BrokenSession:
        async def get(self, *_args, **_kwargs):
            raise SQLAlchemyError("driver went away")

    caplog.set_level("ERROR", logger="bot.crud")
    with pytest.raises(DatabaseOperationError):
        await get_user_by_id(BrokenSession(), 101)

    assert "DB operation failed" in caplog.text


@pytest.mark.asyncio
async def test_session_scope_rolls_back_and_logs(session_factory, caplog) -> None:
    caplog.set_level("ERROR", logger="bot.database")

    with pytest.raises(RuntimeError):
        async with session_scope(session_factory) as session:
            await create_or_get_user(session, 303, None, "Grace")
            raise RuntimeError("boom")

    async with session_factory() as session:
        user = await get_user_by_id(session, 303)

    assert user is None
    assert "rolling back transaction" in caplog.text


@pytest.mark.asyncio
async def test_market_queries_and_status_update(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, None, "Ada")
        old_deadline = datetime.now(timezone.utc) - timedelta(hours=25)
        future_deadline = datetime.now(timezone.utc) + timedelta(hours=2)

        old_market = await create_market(
            session,
            creator_id=101,
            chat_id=-100,
            question="Will it rain?",
            options=["Yes", "No"],
            deadline=old_deadline,
            min_bet=5,
        )
        future_market = await create_market(
            session,
            creator_id=101,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=future_deadline,
            min_bet=10,
        )

        active = await get_active_markets_in_chat(session, -100)
        assert [market.id for market in active] == [old_market.id, future_market.id]

        past_deadline = await get_markets_past_deadline(session, grace_hours=24)
        assert [market.id for market in past_deadline] == [old_market.id]

        resolved = await update_market_status(
            session,
            future_market.id,
            MarketStatus.RESOLVED,
            winning_option=1,
        )
        assert resolved.status == MarketStatus.RESOLVED
        assert resolved.winning_option == 1

        with_message_id = await update_market_message_id(session, future_market.id, 777)
        assert with_message_id.message_id == 777

        with_inline_message_id = await update_market_inline_message_id(
            session,
            future_market.id,
            "inline-message-id",
        )
        assert with_inline_message_id.inline_message_id == "inline-message-id"


@pytest.mark.asyncio
async def test_bets_and_pool_by_option(session_factory) -> None:
    async with session_factory() as session:
        await create_or_get_user(session, 101, None, "Ada")
        await create_or_get_user(session, 202, None, "Linus")
        market = await create_market(
            session,
            creator_id=101,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=10,
        )

        first_bet = await create_bet(session, 101, market.id, 0, 10)
        await create_bet(session, 202, market.id, 1, 25)

        assert await get_pool_by_option(session, market.id) == {0: 10, 1: 25}
        assert await get_user_bet_on_market(session, 101, market.id) == first_bet


@pytest.mark.asyncio
async def test_run_sql_migrations_applies_sql_files(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_create_test_table.sql").write_text(
        "CREATE TABLE IF NOT EXISTS migration_test (id INTEGER PRIMARY KEY)"
    )
    try:
        await run_sql_migrations(engine, migrations_dir)
        async with engine.begin() as conn:
            result = await conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_test'"
            )
            assert result.scalar_one() == "migration_test"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_sql_migrations_wraps_sqlalchemy_errors(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_bad.sql").write_text("CREATE TABLE broken (")
    try:
        with pytest.raises(DatabaseSessionError):
            await run_sql_migrations(engine, migrations_dir)
    finally:
        await engine.dispose()
