from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import User as TelegramUser
from PIL import Image
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import get_active_markets_in_chat, get_market
from bot.database import create_all_tables, create_session_factory
from bot.handlers.markets import (
    INLINE_MARKET_CHAT_ID,
    MarketCreationPersistenceError,
    build_market_card_text,
    build_inline_market_result,
    build_market_keyboard,
    handle_chosen_inline_market,
    handle_inline_market_query,
    parse_deadline_string,
    parse_options_string,
    publish_market_card,
    update_market_card,
)
from bot.market_cards import render_market_card_image
from bot.models import Base, Market, MarketStatus


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
        self.should_fail = should_fail
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.edited_messages: list[dict] = []
        self.edited_media: list[dict] = []

    async def send_message(self, **kwargs):
        if self.should_fail:
            raise RuntimeError("send failed")
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=555)

    async def send_photo(self, **kwargs):
        if self.should_fail:
            raise RuntimeError("send failed")
        self.sent_photos.append(kwargs)
        return SimpleNamespace(message_id=555)

    async def edit_message_text(self, **kwargs):
        if self.should_fail:
            raise RuntimeError("edit failed")
        self.edited_messages.append(kwargs)

    async def edit_message_media(self, **kwargs):
        if self.should_fail:
            raise RuntimeError("edit failed")
        self.edited_media.append(kwargs)


class FakeInlineQuery:
    def __init__(self, query: str = "Will Max be late?", user_id: int = 101) -> None:
        self.id = "inline-query-id"
        self.query = query
        self.from_user = TelegramUser(
            id=user_id,
            is_bot=False,
            first_name="Ada",
            username="ada",
        )
        self.answer_kwargs = None

    async def answer(self, **kwargs):
        self.answer_kwargs = kwargs


class FakeChosenInlineResult:
    def __init__(
        self,
        result_id: str,
        inline_message_id: str | None = "inline-message-id",
    ) -> None:
        self.result_id = result_id
        self.inline_message_id = inline_message_id


def test_parse_deadline_string_accepts_range() -> None:
    assert parse_deadline_string("15m") == timedelta(minutes=15)
    assert parse_deadline_string("2h") == timedelta(hours=2)
    assert parse_deadline_string("7d") == timedelta(days=7)


def test_parse_deadline_string_rejects_invalid_or_out_of_range() -> None:
    assert parse_deadline_string("14m") is None
    assert parse_deadline_string("8d") is None
    assert parse_deadline_string("2w") is None
    assert parse_deadline_string("soon") is None


def test_parse_options_string_accepts_commas_and_lines() -> None:
    assert parse_options_string("Yes, No\nMaybe") == ["Yes", "No", "Maybe"]


def test_parse_options_string_deduplicates_case_insensitive_options() -> None:
    assert parse_options_string("Yes, yes, No") == ["Yes", "No"]


def test_build_market_card_text_and_keyboard() -> None:
    market = _market()

    text = build_market_card_text(market, {0: 30, 1: 10})
    keyboard = build_market_keyboard(market.id, market.options, market.status)

    assert "Poolr market #123" in text
    assert "Will Max be late?" in text
    assert "Pool: 40 Stars" in text
    assert "Yes - 30 Stars (75%)" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "bet:123:0"
    assert keyboard.inline_keyboard[1][0].callback_data == "bet:123:1"


def test_build_market_keyboard_includes_open_button_when_url_present() -> None:
    market = _market()

    keyboard = build_market_keyboard(market.id, market.options, market.status, mini_app_url="https://example.com/app")

    assert keyboard.inline_keyboard[-1][0].text == "Open"
    assert keyboard.inline_keyboard[-1][0].url == "https://example.com/app"


def test_render_market_card_image_produces_png() -> None:
    image_bytes = render_market_card_image(_market(), {0: 30, 1: 10})

    assert image_bytes.startswith(b"\x89PNG")
    with Image.open(BytesIO(image_bytes)) as image:
        assert image.size == (1200, 960)
        assert image.mode == "RGB"


def test_build_inline_market_result() -> None:
    result = build_inline_market_result(_market(), {0: 0, 1: 0})

    assert result.id == "market:123"
    assert result.title == "Will Max be late?"
    assert result.input_message_content.message_text.startswith("Poolr market #123")
    assert result.reply_markup.inline_keyboard[0][0].callback_data == "bet:123:0"


@pytest.mark.asyncio
async def test_handle_inline_market_query_creates_market_and_answers(session_factory) -> None:
    query = FakeInlineQuery()

    async with session_factory() as session:
        await handle_inline_market_query(query, session)

        active_markets = await get_active_markets_in_chat(session, INLINE_MARKET_CHAT_ID)

    assert len(active_markets) == 1
    assert active_markets[0].question == "Will Max be late?"
    assert active_markets[0].options == ["Yes", "No"]
    assert query.answer_kwargs["cache_time"] == 1
    assert query.answer_kwargs["is_personal"] is True
    assert query.answer_kwargs["results"][0].id == f"market:{active_markets[0].id}"


@pytest.mark.asyncio
async def test_handle_inline_market_query_answers_empty_query_without_market(
    session_factory,
) -> None:
    query = FakeInlineQuery(query="")

    async with session_factory() as session:
        await handle_inline_market_query(query, session)
        active_markets = await get_active_markets_in_chat(session, INLINE_MARKET_CHAT_ID)

    assert active_markets == []
    assert query.answer_kwargs["results"] == []
    assert query.answer_kwargs["switch_pm_text"] == "Type a question after @pooolr_bot"


@pytest.mark.asyncio
async def test_handle_chosen_inline_market_saves_inline_message_id(session_factory) -> None:
    async with session_factory() as session:
        query = FakeInlineQuery()
        await handle_inline_market_query(query, session)
        market_id = int(query.answer_kwargs["results"][0].id.removeprefix("market:"))

        await handle_chosen_inline_market(
            FakeChosenInlineResult(result_id=f"market:{market_id}"),
            session,
        )
        market = await get_market(session, market_id)

    assert market is not None
    assert market.inline_message_id == "inline-message-id"


@pytest.mark.asyncio
async def test_publish_market_card_sends_message() -> None:
    bot = FakeBot()
    market = _market()

    message = await publish_market_card(bot, -100, market, {0: 0, 1: 0})

    assert message.message_id == 555
    assert bot.sent_photos[0]["chat_id"] == -100
    assert bot.sent_photos[0]["caption"].startswith("Poolr market #123")
    assert bot.sent_photos[0]["photo"].filename == "market-123.png"


@pytest.mark.asyncio
async def test_publish_market_card_wraps_send_errors() -> None:
    bot = FakeBot(should_fail=True)

    with pytest.raises(MarketCreationPersistenceError):
        await publish_market_card(bot, -100, _market(), {})


@pytest.mark.asyncio
async def test_update_market_card_edits_message() -> None:
    bot = FakeBot()
    market = _market()

    await update_market_card(bot, -100, 555, market, {0: 5, 1: 5})

    assert bot.edited_media[0]["chat_id"] == -100
    assert bot.edited_media[0]["message_id"] == 555
    assert bot.edited_media[0]["media"].caption.startswith("Poolr market #123")


def _market() -> Market:
    return Market(
        id=123,
        creator_id=101,
        chat_id=-100,
        message_id=None,
        question="Will Max be late?",
        options=["Yes", "No"],
        deadline=datetime.now(timezone.utc) + timedelta(hours=2),
        min_bet=10,
        status=MarketStatus.ACTIVE,
    )
