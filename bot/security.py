from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


logger = logging.getLogger(__name__)


def verify_webhook_request(
    raw_body: bytes,
    secret_token: str,
    signature_header: str,
) -> bool:
    if not secret_token or not signature_header:
        logger.warning("Webhook verification failed: missing secret or header")
        return False

    # Telegram sends the configured secret token directly in the header.
    # raw_body stays in the signature for compatibility with the project plan.
    _ = raw_body
    is_valid = hmac.compare_digest(signature_header, secret_token)
    if not is_valid:
        logger.warning("Webhook verification failed: invalid secret header")
    return is_valid


def validate_webapp_init_data(
    init_data_raw: str,
    bot_token: str,
) -> dict[str, Any] | None:
    if not init_data_raw or not bot_token:
        logger.warning("Mini App initData validation failed: missing input")
        return None

    try:
        parsed_pairs = parse_qsl(init_data_raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        logger.warning("Mini App initData validation failed: malformed query string")
        return None

    init_data = dict(parsed_pairs)
    received_hash = init_data.get("hash")
    if not received_hash:
        logger.warning("Mini App initData validation failed: missing hash")
        return None

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed_pairs)
        if key != "hash"
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        logger.warning("Mini App initData validation failed: invalid hash")
        return None

    user_raw = init_data.get("user")
    if user_raw:
        try:
            init_data["user"] = json.loads(user_raw)
        except json.JSONDecodeError:
            logger.warning("Mini App initData validation failed: malformed user JSON")
            return None

    logger.info(
        "Mini App initData validated: has_user=%s auth_date=%s",
        "user" in init_data,
        init_data.get("auth_date"),
    )
    return init_data


def is_admin(user_id: int, admin_ids: list[int]) -> bool:
    return user_id in set(admin_ids)


class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: list[int] | None = None) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        admin_ids = self._resolve_admin_ids(data)
        user_id = event.from_user.id if event.from_user else None

        if user_id is None or not is_admin(user_id, admin_ids):
            logger.warning("Admin access denied: user_id=%s", user_id)
            await self._notify_denied(event)
            return None

        logger.info("Admin access granted: user_id=%d", user_id)
        return await handler(event, data)

    def _resolve_admin_ids(self, data: dict[str, Any]) -> list[int]:
        if self.admin_ids is not None:
            return self.admin_ids

        admin_ids = data.get("admin_ids")
        if isinstance(admin_ids, list):
            return [int(admin_id) for admin_id in admin_ids]

        config = data.get("config")
        if config is not None and hasattr(config, "ADMIN_IDS"):
            return list(config.ADMIN_IDS)

        return []

    async def _notify_denied(self, event: Message | CallbackQuery) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer("Access denied", show_alert=True)
            return

        await event.answer("Access denied")

