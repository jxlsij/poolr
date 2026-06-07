from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message


logger = logging.getLogger(__name__)

START_MESSAGE_TEXT = """👋 *Привет! Я Poolr.*

Я превращаю Telegram чаты в prediction markets.

*Создать рынок:*

`@pooolr_bot Will Max be late?`

или просто нажми *Open* ниже.

⭐ Создавай рынки
📈 Делай предсказания
🏆 Узнавай, кто оказался прав"""

START_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "start_message.png"


def create_start_router(open_url: str) -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        logger.info("Handling /start: user_id=%s", message.from_user.id if message.from_user else None)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Open", url=open_url)],
            ]
        )

        if START_IMAGE_PATH.exists():
            await message.answer_photo(
                photo=FSInputFile(START_IMAGE_PATH),
                caption=START_MESSAGE_TEXT,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return

        logger.warning("Start image not found at %s; sending text fallback", START_IMAGE_PATH)
        await message.answer(
            text=START_MESSAGE_TEXT,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    return router

