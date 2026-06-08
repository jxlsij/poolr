from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import create_async_engine

from bot.database import create_all_tables, create_session_factory
from bot.middleware.database import DatabaseSessionMiddleware
from bot.models import Base
from bot.users import (
    UserIdentityError,
    UserIdentityPersistenceError,
    ensure_user,
    ensure_user_from_webapp_data,
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


@pytest.mark.asyncio
async def test_ensure_user_creates_and_updates_from_telegram_user(session_factory) -> None:
    async with session_factory() as session:
        user, is_new = await ensure_user(
            session,
            TelegramUser(id=101, is_bot=False, first_name="Ada", username="ada"),
        )
        assert is_new is True
        assert user.telegram_id == 101
        assert user.username == "ada"
        assert user.first_name == "Ada"

        same_user, is_new = await ensure_user(
            session,
            TelegramUser(id=101, is_bot=False, first_name="Ada L", username="ada_l"),
        )

        assert is_new is False
        assert same_user.telegram_id == 101
        assert same_user.username == "ada_l"
        assert same_user.first_name == "Ada L"


@pytest.mark.asyncio
async def test_ensure_user_rejects_bot_accounts(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(UserIdentityError):
            await ensure_user(
                session,
                TelegramUser(id=101, is_bot=True, first_name="Bot"),
            )


@pytest.mark.asyncio
async def test_ensure_user_from_webapp_data_creates_user(session_factory) -> None:
    async with session_factory() as session:
        user, is_new = await ensure_user_from_webapp_data(
            session,
            {
                "auth_date": _current_auth_date(),
                "user": {
                    "id": 202,
                    "first_name": "Linus",
                    "username": "@linus",
                },
            },
        )

        assert is_new is True
        assert user.telegram_id == 202
        assert user.username == "linus"
        assert user.first_name == "Linus"


@pytest.mark.asyncio
async def test_ensure_user_from_webapp_data_rejects_missing_user(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(UserIdentityError):
            await ensure_user_from_webapp_data(session, {"auth_date": _current_auth_date()})


@pytest.mark.asyncio
async def test_ensure_user_logs_invalid_identity(session_factory, caplog) -> None:
    caplog.set_level("WARNING", logger="bot.users")

    async with session_factory() as session:
        with pytest.raises(UserIdentityError):
            await ensure_user(
                session,
                TelegramUser(id=101, is_bot=True, first_name="Bot"),
            )

    assert "Rejected Telegram user identity payload" in caplog.text
    assert "telegram_id=101" in caplog.text


@pytest.mark.asyncio
async def test_ensure_user_wraps_persistence_errors(
    session_factory,
    monkeypatch,
    caplog,
) -> None:
    async def broken_create_or_get_user(**_kwargs):
        from bot.crud import DatabaseOperationError

        raise DatabaseOperationError("database is unavailable")

    monkeypatch.setattr("bot.users.create_or_get_user", broken_create_or_get_user)
    caplog.set_level("ERROR", logger="bot.users")

    async with session_factory() as session:
        with pytest.raises(UserIdentityPersistenceError):
            await ensure_user(
                session,
                TelegramUser(id=303, is_bot=False, first_name="Grace"),
            )

    assert "Failed to persist user identity" in caplog.text
    assert "telegram_id=303" in caplog.text


def _current_auth_date() -> str:
    return str(int(datetime.now(timezone.utc).timestamp()))


@pytest.mark.asyncio
async def test_database_session_middleware_logs_handler_failures(
    session_factory,
    caplog,
) -> None:
    middleware = DatabaseSessionMiddleware(session_factory)
    caplog.set_level("ERROR", logger="bot.middleware.database")

    async def broken_handler(_event, data):
        assert data["db_session"] is not None
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        await middleware(broken_handler, object(), {})

    assert "Telegram update handling failed inside database middleware" in caplog.text
    assert "event=object" in caplog.text
