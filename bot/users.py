from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crud import DatabaseLayerError, create_or_get_user
from bot.models import User


logger = logging.getLogger(__name__)


class UserModuleError(RuntimeError):
    """Base error for Module 4 user identity operations."""


class UserIdentityError(UserModuleError, ValueError):
    """Raised when a Telegram user payload cannot identify a real user."""


class UserIdentityPersistenceError(UserModuleError):
    """Raised when user identity validation passed but persistence failed."""


async def ensure_user(
    session: AsyncSession,
    telegram_user: TelegramUser,
) -> tuple[User, bool]:
    telegram_id = getattr(telegram_user, "id", None)
    logger.debug("Ensuring Telegram user identity: telegram_id=%s", telegram_id)

    try:
        if telegram_user is None:
            raise UserIdentityError("telegram_user is required")
        if telegram_user.is_bot:
            raise UserIdentityError("bot accounts cannot be Poolr users")

        normalized_user_id = _require_positive_id(telegram_user.id)
        username = _normalize_username(telegram_user.username)
        first_name = _telegram_display_name(telegram_user)
    except UserIdentityError:
        logger.warning(
            "Rejected Telegram user identity payload: telegram_id=%s",
            telegram_id,
            exc_info=True,
        )
        raise

    user, is_new = await _persist_user_identity(
        session=session,
        telegram_id=normalized_user_id,
        username=username,
        first_name=first_name,
        source="telegram",
    )
    logger.debug(
        "Ensured user identity: telegram_id=%d is_new=%s",
        user.telegram_id,
        is_new,
    )
    return user, is_new


async def ensure_user_from_webapp_data(
    session: AsyncSession,
    init_data: Mapping[str, Any],
) -> tuple[User, bool]:
    """Ensure a user from already validated Telegram Mini App initData."""

    raw_user = init_data.get("user") if isinstance(init_data, Mapping) else None
    telegram_id = raw_user.get("id") if isinstance(raw_user, Mapping) else None
    logger.debug("Ensuring Mini App user identity: telegram_id=%s", telegram_id)

    try:
        if not isinstance(raw_user, Mapping):
            raise UserIdentityError("init_data must contain a user object")
        if raw_user.get("is_bot") is True:
            raise UserIdentityError("bot accounts cannot be Poolr users")

        normalized_user_id = _require_positive_id(raw_user.get("id"))
        username = _normalize_username(raw_user.get("username"))
        first_name = _webapp_display_name(raw_user)
    except UserIdentityError:
        logger.warning(
            "Rejected Mini App user identity payload: telegram_id=%s",
            telegram_id,
            exc_info=True,
        )
        raise

    user, is_new = await _persist_user_identity(
        session=session,
        telegram_id=normalized_user_id,
        username=username,
        first_name=first_name,
        source="mini_app",
    )
    logger.debug(
        "Ensured Mini App user identity: telegram_id=%d is_new=%s",
        user.telegram_id,
        is_new,
    )
    return user, is_new


async def _persist_user_identity(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str,
    source: str,
) -> tuple[User, bool]:
    try:
        user, is_new = await create_or_get_user(
            session=session,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
    except DatabaseLayerError as exc:
        logger.exception(
            "Failed to persist user identity: telegram_id=%d source=%s",
            telegram_id,
            source,
        )
        raise UserIdentityPersistenceError("Failed to persist user identity") from exc
    except Exception as exc:
        logger.exception(
            "Unexpected user identity persistence failure: telegram_id=%d source=%s",
            telegram_id,
            source,
        )
        raise UserIdentityPersistenceError(
            "Unexpected user identity persistence failure"
        ) from exc

    logger.info(
        "User identity persisted: telegram_id=%d source=%s is_new=%s",
        telegram_id,
        source,
        is_new,
    )
    return user, is_new


def _telegram_display_name(telegram_user: TelegramUser) -> str:
    first_name = _clean_text(telegram_user.first_name)
    if first_name:
        return first_name

    full_name = _clean_text(telegram_user.full_name)
    if full_name:
        return full_name

    raise UserIdentityError("telegram_user must have a display name")


def _webapp_display_name(raw_user: Mapping[str, Any]) -> str:
    first_name = _clean_text(raw_user.get("first_name"))
    if first_name:
        return first_name

    full_name = " ".join(
        part
        for part in (
            _clean_text(raw_user.get("first_name")),
            _clean_text(raw_user.get("last_name")),
        )
        if part
    )
    if full_name:
        return full_name

    raise UserIdentityError("webapp user must have a display name")


def _normalize_username(value: Any) -> str | None:
    username = _clean_text(value)
    if username is None:
        return None
    return username.removeprefix("@") or None


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _require_positive_id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise UserIdentityError("telegram user id must be a positive integer")
    return value
