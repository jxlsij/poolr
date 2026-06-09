from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import User as TelegramUser
from PIL import Image
from sqlalchemy.ext.asyncio import create_async_engine

from bot.crud import create_market, create_or_get_user, get_active_markets_in_chat, get_market
from bot.database import create_all_tables, create_session_factory
from bot.handlers.markets import (
    INLINE_MARKET_CHAT_ID,
    MarketCreationPersistenceError,
    build_market_card_text,
    build_inline_market_preview_result,
    build_inline_market_result,
    build_inline_market_text,
    build_market_url,
    build_market_keyboard,
    handle_chosen_inline_market,
    handle_inline_market_query,
    parse_deadline_string,
    parse_options_string,
    publish_market_card,
    update_market_card,
    update_inline_market_card,
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
        query: str = "Will Max be late?",
        user_id: int = 101,
        inline_message_id: str | None = "inline-message-id",
    ) -> None:
        self.result_id = result_id
        self.query = query
        self.from_user = TelegramUser(
            id=user_id,
            is_bot=False,
            first_name="Ada",
            username="ada",
        )
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

    keyboard = build_market_keyboard(market.id, market.options, market.status, mini_app_url="https://t.me/pooolr_bot/poolr")

    assert keyboard.inline_keyboard[-1][0].text == "Open event"
    assert keyboard.inline_keyboard[-1][0].url == "https://t.me/pooolr_bot/poolr?startapp=market_123"
    assert keyboard.inline_keyboard[-1][0].web_app is None


def test_build_market_url_appends_market_id() -> None:
    assert build_market_url("https://example.com/app", 123) == "https://example.com/app?market_id=123"
    assert build_market_url("https://example.com/app?start=1", 123) == "https://example.com/app?start=1&market_id=123"
    assert build_market_url("https://t.me/pooolr_bot/poolr", 123) == "https://t.me/pooolr_bot/poolr?startapp=market_123"
    assert build_market_url("https://t.me/pooolr_bot/poolr?foo=1", 123) == "https://t.me/pooolr_bot/poolr?foo=1&startapp=market_123"
    assert build_market_url(None, 123) is None


def test_render_market_card_image_produces_png() -> None:
    image_bytes = render_market_card_image(_market(), {0: 30, 1: 10})

    assert image_bytes.startswith(b"\x89PNG")
    with Image.open(BytesIO(image_bytes)) as image:
        assert image.size == (1200, 960)
        assert image.mode == "RGB"


def test_build_inline_market_result() -> None:
    result = build_inline_market_result(_market(), {0: 0, 1: 0}, mini_app_url="https://t.me/pooolr_bot/poolr")

    assert result.id == "market:123"
    assert result.title == "Will Max be late?"
    assert result.input_message_content.message_text.startswith("Poolr market #123")
    assert "Min stake: 10 Stars" in result.input_message_content.message_text
    assert result.reply_markup.inline_keyboard[0][0].callback_data == "bet:123:0"
    assert result.reply_markup.inline_keyboard[-1][0].url == "https://t.me/pooolr_bot/poolr?startapp=market_123"
    assert result.reply_markup.inline_keyboard[-1][0].web_app is None


def test_build_inline_market_preview_result_has_no_market_buttons() -> None:
    result = build_inline_market_preview_result(
        "Will Max be late?",
        mini_app_url="https://t.me/pooolr_bot/poolr",
    )

    assert result.id.startswith("draft:")
    assert result.title == "Will Max be late?"
    assert result.input_message_content.message_text.startswith("Poolr market")
    assert "Creating market..." in result.input_message_content.message_text
    assert result.reply_markup.inline_keyboard[0][0].callback_data == "inline_market_pending"


def test_build_inline_market_text_is_compact() -> None:
    text = build_inline_market_text(_market(), {0: 25, 1: 75})

    assert "Poolr market #123" in text
    assert "Will Max be late?" in text
    assert "Pool: 100 Stars" in text
    assert "Yes: 25 Stars (25%)" in text
    assert "No: 75 Stars (75%)" in text


@pytest.mark.asyncio
async def test_handle_inline_market_query_answers_preview_without_creating_market(session_factory) -> None:
    query = FakeInlineQuery()

    async with session_factory() as session:
        await handle_inline_market_query(query, session, mini_app_url="https://t.me/pooolr_bot/poolr")

        active_markets = await get_active_markets_in_chat(session, INLINE_MARKET_CHAT_ID)

    assert active_markets == []
    assert query.answer_kwargs["cache_time"] == 1
    assert query.answer_kwargs["is_personal"] is True
    assert query.answer_kwargs["results"][0].id.startswith("draft:")
    assert query.answer_kwargs["results"][0].input_message_content.message_text.startswith(
        "Poolr market"
    )
    assert "Creating market..." in query.answer_kwargs["results"][0].input_message_content.message_text
    assert query.answer_kwargs["results"][0].reply_markup.inline_keyboard[-1][0].callback_data == "inline_market_pending"
    assert query.answer_kwargs["results"][0].reply_markup.inline_keyboard[-1][0].web_app is None


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
async def test_handle_chosen_inline_market_creates_market_and_updates_inline_message(session_factory) -> None:
    bot = FakeBot()
    async with session_factory() as session:
        query = FakeInlineQuery()
        await handle_inline_market_query(query, session)
        draft_result_id = query.answer_kwargs["results"][0].id

        await handle_chosen_inline_market(
            FakeChosenInlineResult(result_id=draft_result_id),
            session,
            bot=bot,
            mini_app_url="https://t.me/pooolr_bot/poolr",
        )
        active_markets = await get_active_markets_in_chat(session, INLINE_MARKET_CHAT_ID)
        market = active_markets[0]

    assert market is not None
    assert market.question == "Will Max be late?"
    assert market.options == ["Yes", "No"]
    assert market.inline_message_id == "inline-message-id"
    assert bot.edited_messages[0]["inline_message_id"] == "inline-message-id"
    assert bot.edited_messages[0]["text"].startswith(f"Poolr market #{market.id}")
    assert bot.edited_messages[0]["reply_markup"].inline_keyboard[0][0].callback_data == f"bet:{market.id}:0"
    assert bot.edited_messages[0]["reply_markup"].inline_keyboard[-1][0].url == (
        f"https://t.me/pooolr_bot/poolr?startapp=market_{market.id}"
    )


@pytest.mark.asyncio
async def test_handle_chosen_inline_market_keeps_legacy_market_result_support(session_factory) -> None:
    async with session_factory() as session:
        query = FakeInlineQuery()
        await handle_inline_market_query(query, session)

        await create_or_get_user(session, 101, "ada", "Ada")
        market = await create_market(
            session,
            creator_id=101,
            chat_id=INLINE_MARKET_CHAT_ID,
            question="Will Max be late?",
            options=["Yes", "No"],
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            min_bet=1,
        )

        await handle_chosen_inline_market(
            FakeChosenInlineResult(result_id=f"market:{market.id}"),
            session,
        )
        reloaded = await get_market(session, market.id)

    assert reloaded is not None
    assert reloaded.inline_message_id == "inline-message-id"


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


@pytest.mark.asyncio
async def test_update_inline_market_card_edits_compact_text() -> None:
    bot = FakeBot()
    market = _market()

    await update_inline_market_card(
        bot,
        "inline-message-id",
        market,
        {0: 5, 1: 5},
        mini_app_url="https://t.me/pooolr_bot/poolr",
    )

    assert bot.edited_messages[0]["inline_message_id"] == "inline-message-id"
    assert bot.edited_messages[0]["text"].startswith("Poolr market #123")
    assert bot.edited_messages[0]["reply_markup"].inline_keyboard[-1][0].url == "https://t.me/pooolr_bot/poolr?startapp=market_123"
    assert bot.edited_messages[0]["reply_markup"].inline_keyboard[-1][0].web_app is None


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
