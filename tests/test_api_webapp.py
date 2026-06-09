import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import create_async_engine

from api.webapp import API_SESSION_FACTORY_KEY, api_error_middleware, setup_api_routes
from bot.crud import create_bet, create_market, create_or_get_user, create_payout, update_user_balance
from bot.database import create_all_tables, create_session_factory, session_scope
from bot.models import Base, MarketStatus


BOT_TOKEN = "123456:test-token"
VALID_TON_WALLET = "0:" + "a" * 64


class FakeBot:
    def __init__(self) -> None:
        self.invoices: list[dict] = []
        self.messages: list[dict] = []
        self.fail_invoices = False

    async def send_invoice(self, **kwargs):
        if self.fail_invoices:
            raise RuntimeError("telegram unavailable")
        self.invoices.append(kwargs)
        return object()

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return object()


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


@pytest_asyncio.fixture
async def api_client(session_factory):
    fake_bot = FakeBot()
    app = web.Application(middlewares=[api_error_middleware])
    app[API_SESSION_FACTORY_KEY] = session_factory
    setup_api_routes(
        app,
        bot=fake_bot,
        bot_token=BOT_TOKEN,
        admin_ids=[42],
        platform_fee_pct=0.08,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, fake_bot
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_requires_valid_init_data(api_client) -> None:
    client, _fake_bot = api_client

    response = await client.get("/api/profile")

    assert response.status == 401
    body = await response.json()
    assert body["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_profile_returns_balance_and_stats(api_client, session_factory) -> None:
    client, _fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await update_user_balance(session, 42, 30, "test_balance")
        market = await create_market(
            session,
            creator_id=42,
            chat_id=-100,
            question="Will it rain?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        await create_bet(session, 42, market.id, 0, 10)
        await create_payout(session, 42, market.id, 18)

    response = await client.get("/api/profile", headers=auth_headers(42))

    assert response.status == 200
    body = await response.json()
    assert body["user_id"] == 42
    assert body["balance"] == 30
    assert body["stats"]["bets_count"] == 1
    assert body["stats"]["markets_created"] == 1
    assert body["stats"]["total_staked"] == 10
    assert body["stats"]["total_won"] == 18


@pytest.mark.asyncio
async def test_market_detail_includes_pool_odds_and_my_bet(api_client, session_factory) -> None:
    client, _fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_or_get_user(session, 77, "linus", "Linus")
        market = await create_market(
            session,
            creator_id=77,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        await create_bet(session, 42, market.id, 0, 10)
        await create_bet(session, 77, market.id, 1, 30)

    response = await client.get(f"/api/market/{market.id}", headers=auth_headers(42))

    assert response.status == 200
    body = await response.json()
    assert body["question"] == "Will Max be late?"
    assert body["pool_by_option"] == {"0": 10, "1": 30}
    assert body["odds"] == {"0": 0.25, "1": 0.75}
    assert body["total_pool"] == 40
    assert body["my_bet"]["stars_amount"] == 10


@pytest.mark.asyncio
async def test_markets_endpoint_lists_active_markets(api_client, session_factory) -> None:
    client, _fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        active_market = await create_market(
            session,
            creator_id=42,
            chat_id=0,
            question="Will Poolr launch today?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        resolved_market = await create_market(
            session,
            creator_id=42,
            chat_id=0,
            question="Did the demo pass?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        resolved_market.status = MarketStatus.RESOLVED

    response = await client.get("/api/markets", headers=auth_headers(42))

    assert response.status == 200
    body = await response.json()
    assert body["status"] == "active"
    assert [market["id"] for market in body["markets"]] == [active_market.id]


@pytest.mark.asyncio
async def test_markets_endpoint_only_lists_public_feed_markets(api_client, session_factory) -> None:
    client, _fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        public_market = await create_market(
            session,
            creator_id=42,
            chat_id=0,
            question="Will public markets work?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        group_market = await create_market(
            session,
            creator_id=42,
            chat_id=-100,
            question="Will the group see this locally?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        private_chat_market = await create_market(
            session,
            creator_id=42,
            chat_id=42,
            question="Will this stay behind the message button?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )

    response = await client.get("/api/markets?status=all", headers=auth_headers(42))

    assert response.status == 200
    body = await response.json()
    assert [market["id"] for market in body["markets"]] == [public_market.id]

    for market in (group_market, private_chat_market):
        detail_response = await client.get(f"/api/market/{market.id}", headers=auth_headers(42))
        assert detail_response.status == 200


@pytest.mark.asyncio
async def test_place_bet_sends_stars_invoice_without_recording_bet(
    api_client,
    session_factory,
) -> None:
    client, fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_or_get_user(session, 77, "linus", "Linus")
        market = await create_market(
            session,
            creator_id=77,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )

    response = await client.post(
        "/api/bet",
        headers=auth_headers(42),
        json={"market_id": market.id, "option_index": 0, "stars_amount": 10},
    )

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["invoice_sent"] is True
    assert body["estimated_payout"] == 9
    assert fake_bot.invoices[0]["chat_id"] == 42
    assert fake_bot.invoices[0]["currency"] == "XTR"


@pytest.mark.asyncio
async def test_place_bet_rejects_existing_bet_before_invoice(
    api_client,
    session_factory,
) -> None:
    client, fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_or_get_user(session, 77, "linus", "Linus")
        market = await create_market(
            session,
            creator_id=77,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )
        await create_bet(session, 42, market.id, 0, 10)

    response = await client.post(
        "/api/bet",
        headers=auth_headers(42),
        json={"market_id": market.id, "option_index": 1, "stars_amount": 10},
    )

    assert response.status == 400
    body = await response.json()
    assert body["error"]["code"] == "already_bet"
    assert body["error"]["message"] == "You already have a bet on this market."
    assert fake_bot.invoices == []


@pytest.mark.asyncio
async def test_place_bet_provider_failure_returns_502(api_client, session_factory) -> None:
    client, fake_bot = api_client
    fake_bot.fail_invoices = True
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await create_or_get_user(session, 77, "linus", "Linus")
        market = await create_market(
            session,
            creator_id=77,
            chat_id=-100,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=5,
        )

    response = await client.post(
        "/api/bet",
        headers=auth_headers(42),
        json={"market_id": market.id, "option_index": 0, "stars_amount": 10},
    )

    assert response.status == 502
    body = await response.json()
    assert body["error"]["code"] == "provider_error"
    assert body["error"]["message"] == "Could not send Stars invoice."


@pytest.mark.asyncio
async def test_withdrawal_request_reserves_balance(api_client, session_factory) -> None:
    client, _fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await update_user_balance(session, 42, 25, "test_winnings")

    response = await client.post(
        "/api/withdraw",
        headers=auth_headers(42),
        json={
            "stars_amount": 20,
            "ton_wallet_address": VALID_TON_WALLET,
        },
    )

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["reserved_stars"] == 20
    assert body["new_balance"] == 5
    assert body["withdrawal"]["status"] == "pending"


@pytest.mark.asyncio
async def test_withdrawal_validation_error_returns_400(api_client, session_factory) -> None:
    client, _fake_bot = api_client
    async with session_scope(session_factory) as session:
        await create_or_get_user(session, 42, "ada", "Ada")
        await update_user_balance(session, 42, 5, "test_winnings")

    response = await client.post(
        "/api/withdraw",
        headers=auth_headers(42),
        json={
            "stars_amount": 20,
            "ton_wallet_address": VALID_TON_WALLET,
        },
    )

    assert response.status == 400
    body = await response.json()
    assert body["error"]["code"] == "validation_error"
    assert "Insufficient withdrawable Stars" in body["error"]["message"]


def auth_headers(user_id: int) -> dict[str, str]:
    init_data = build_init_data(
        BOT_TOKEN,
        {
            "auth_date": _current_auth_date(),
            "user": json.dumps(
                {"id": user_id, "first_name": "Ada", "username": "ada"},
                separators=(",", ":"),
            ),
        },
    )
    return {"Authorization": f"tma {init_data}"}


def build_init_data(bot_token: str, values: dict[str, str]) -> str:
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    data_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return urlencode({**values, "hash": data_hash}, quote_via=quote)


def _current_auth_date() -> str:
    return str(int(datetime.now(timezone.utc).timestamp()))
