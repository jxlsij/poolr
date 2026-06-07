from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncEngine


DEFAULT_ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "pre_checkout_query",
]


async def setup_webhook(
    bot: "Bot",
    webhook_url: str,
    secret_token: str,
    allowed_updates: list[str],
) -> bool:
    if not webhook_url.startswith("https://"):
        raise ValueError("webhook_url must be an HTTPS URL")

    if not secret_token:
        raise ValueError("secret_token must not be empty")

    result = await bot.set_webhook(
        url=webhook_url,
        secret_token=secret_token,
        allowed_updates=allowed_updates,
        drop_pending_updates=True,
    )
    return bool(result)


async def create_db_pool(db_url: str, pool_size: int = 10) -> "AsyncEngine":
    if pool_size < 1:
        raise ValueError("pool_size must be at least 1")

    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        _normalize_async_postgres_url(db_url),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
    )


def _normalize_async_postgres_url(db_url: str) -> str:
    if db_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + db_url[len("postgres://") :]

    if db_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + db_url[len("postgresql://") :]

    return db_url

