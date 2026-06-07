from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from bot.users import UserModuleError, ensure_user


logger = logging.getLogger(__name__)

START_MESSAGE_TEXT = """*Привет! Я Poolr.* Создавай prediction markets прямо внутри Telegram чатов.

*Как создать рынок:*
`@pooolr_bot Will Max be late?`
или просто нажми *Open* в меню бота.

— Создавай пулы
— Делай предсказания
— Проверяй, кто был прав"""

START_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "start_message.png"


def create_start_router() -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    async def handle_start(
        message: Message,
        db_session: AsyncSession | None = None,
    ) -> None:
        logger.info("Handling /start: user_id=%s", message.from_user.id if message.from_user else None)
        if db_session is not None and message.from_user is not None:
            try:
                user, is_new = await ensure_user(db_session, message.from_user)
                logger.info(
                    "Ensured /start user: telegram_id=%d is_new=%s",
                    user.telegram_id,
                    is_new,
                )
            except UserModuleError:
                logger.warning("Could not ensure /start user identity", exc_info=True)
        elif db_session is None:
            logger.warning("Skipping /start user identity because db_session is missing")

        if START_IMAGE_PATH.exists():
            await message.answer_photo(
                photo=FSInputFile(START_IMAGE_PATH),
                caption=START_MESSAGE_TEXT,
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        logger.warning("Start image not found at %s; sending text fallback", START_IMAGE_PATH)
        await message.answer(
            text=START_MESSAGE_TEXT,
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )

    return router
