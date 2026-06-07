from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)


class InfrastructureError(RuntimeError):
    """Raised when infrastructure setup fails."""


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

    logger.info(
        "Setting Telegram webhook: url=%s allowed_updates=%s",
        webhook_url,
        ",".join(allowed_updates),
    )
    try:
        result = await bot.set_webhook(
            url=webhook_url,
            secret_token=secret_token,
            allowed_updates=allowed_updates,
            drop_pending_updates=True,
        )
    except Exception as exc:
        logger.exception("Failed to set Telegram webhook at %s", webhook_url)
        raise InfrastructureError("Telegram webhook setup failed") from exc

    success = bool(result)
    if success:
        logger.info("Telegram webhook set successfully")
    else:
        logger.warning("Telegram webhook setup returned a false result")
    return success


async def create_db_pool(db_url: str, pool_size: int = 10) -> "AsyncEngine":
    if pool_size < 1:
        raise ValueError("pool_size must be at least 1")

    from sqlalchemy.ext.asyncio import create_async_engine

    normalized_url = _normalize_async_postgres_url(db_url)
    logger.info(
        "Creating async database engine: url=%s pool_size=%d",
        _redact_db_url(normalized_url),
        pool_size,
    )
    try:
        engine = create_async_engine(
            normalized_url,
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_pre_ping=True,
        )
    except Exception as exc:
        logger.exception("Failed to create async database engine")
        raise InfrastructureError("Database engine creation failed") from exc

    logger.info("Async database engine created")
    return engine


def _normalize_async_postgres_url(db_url: str) -> str:
    if db_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + db_url[len("postgres://") :]

    if db_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + db_url[len("postgresql://") :]

    return db_url


def _redact_db_url(db_url: str) -> str:
    if "@" not in db_url:
        return db_url

    scheme, _, rest = db_url.partition("://")
    credentials, _, host = rest.partition("@")
    username, separator, _password = credentials.partition(":")
    redacted_credentials = username if not separator else f"{username}:***"
    return f"{scheme}://{redacted_credentials}@{host}"
