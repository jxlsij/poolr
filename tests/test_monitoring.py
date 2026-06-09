import json
import logging

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import confirm_deposit, create_deposit, create_or_get_user, create_withdrawal
from bot.database import create_all_tables, create_session_factory, session_scope
from bot.models import Base, WithdrawalStatus
from bot.monitoring import (
    BOT_APP_KEY,
    DB_SESSION_FACTORY_APP_KEY,
    JsonLogFormatter,
    health_check,
    monitor_payment_anomalies,
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
async def test_health_check_reports_connected_db(session_factory) -> None:
    app = web.Application()
    app[DB_SESSION_FACTORY_APP_KEY] = session_factory
    app[BOT_APP_KEY] = object()
    request = make_mocked_request("GET", "/health", app=app)

    response = await health_check(request)
    body = json.loads(response.text)

    assert response.status == 200
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert body["bot"] == "running"


@pytest.mark.asyncio
async def test_monitor_payment_anomalies_flags_high_withdrawal_pressure(session_factory) -> None:
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_deposit(session, 42, 10, "charge")
        await confirm_deposit(session, "charge")
        withdrawal = await create_withdrawal(
            session,
            user_id=42,
            credits_amount=150,
            ton_wallet_address="0:" + "a" * 64,
        )
        withdrawal.status = WithdrawalStatus.PENDING

        reports = await monitor_payment_anomalies(session)

    assert len(reports) == 1
    assert reports[0].user_id == 42
    assert reports[0].kind == "high_withdrawal_pressure"


def test_json_log_formatter_outputs_json() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="poolr.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "poolr.test"
    assert payload["message"] == "hello world"
