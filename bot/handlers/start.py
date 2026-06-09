from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.users import UserModuleError, ensure_user


logger = logging.getLogger(__name__)

START_MESSAGE_TEXT = """*Привет! Я Poolr.* Создавай prediction markets прямо внутри Telegram чатов.

*Как создать рынок:*
`@pooolr_bot Will Max be late?`
или нажми кнопку *Open Mini App* под этим сообщением.

— Создавай пулы
— Делай предсказания
— Проверяй, кто был прав"""

START_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "start_message.png"
PAY_SUPPORT_TEXT = (
    "Poolr payment support\n\n"
    "If a Stars payment, stake, refund, dispute, or manual TON-equivalent payout looks wrong, "
    "message support with your Telegram user id, market id, payout request id, and any Telegram charge id you have.\n\n"
    "Beta payouts are manual and reviewed by admins. Do not send private keys or seed phrases."
)


def build_start_keyboard(open_url: str | None) -> InlineKeyboardMarkup | None:
    if not open_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open Mini App",
                    web_app=WebAppInfo(url=open_url),
                )
            ]
        ]
    )


def build_web_app_menu_button(open_url: str) -> MenuButtonWebApp:
    return MenuButtonWebApp(
        text="Open",
        web_app=WebAppInfo(url=open_url),
    )


async def ensure_chat_menu_button(message: Message, open_url: str | None) -> None:
    if not open_url:
        return
    try:
        await message.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=build_web_app_menu_button(open_url),
        )
        logger.info("Telegram Web App chat menu button configured: chat_id=%s", message.chat.id)
    except Exception:
        logger.warning("Could not configure chat Web App menu button", exc_info=True)


def create_start_router(open_url: str | None = None) -> Router:
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

        await ensure_chat_menu_button(message, open_url)
        reply_markup = build_start_keyboard(open_url)
        if START_IMAGE_PATH.exists():
            await message.answer_photo(
                photo=FSInputFile(START_IMAGE_PATH),
                caption=START_MESSAGE_TEXT,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return

        logger.warning("Start image not found at %s; sending text fallback", START_IMAGE_PATH)
        await message.answer(
            text=START_MESSAGE_TEXT,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )

    @router.message(Command("paysupport", ignore_mention=True))
    async def handle_pay_support(message: Message) -> None:
        logger.info("Handling /paysupport: user_id=%s", message.from_user.id if message.from_user else None)
        await message.answer(PAY_SUPPORT_TEXT)

    return router
