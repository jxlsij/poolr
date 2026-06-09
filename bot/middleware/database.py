from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database import session_scope


logger = logging.getLogger(__name__)


class DatabaseSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_name = type(event).__name__
        logger.debug("Opening database session for Telegram update: event=%s", event_name)
        try:
            async with session_scope(self._session_factory) as session:
                data["db_session"] = session
                logger.debug(
                    "Injected database session into Telegram update handler: event=%s",
                    event_name,
                )
                result = await handler(event, data)
                logger.debug(
                    "Telegram update handled with database session: event=%s",
                    event_name,
                )
                return result
        except Exception:
            logger.exception(
                "Telegram update handling failed inside database middleware: event=%s",
                event_name,
            )
            raise
