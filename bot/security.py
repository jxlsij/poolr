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


class SecurityValidationError(ValueError):
    """Raised internally when security validation fails."""


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
    if is_valid:
        logger.info("Webhook verification succeeded")
    else:
        logger.warning("Webhook verification failed: invalid secret header")
    return is_valid


def validate_webapp_init_data(
    init_data_raw: str,
    bot_token: str,
) -> dict[str, Any] | None:
    try:
        return _validate_webapp_init_data(init_data_raw, bot_token)
    except SecurityValidationError as exc:
        logger.warning("Mini App initData validation failed: %s", exc)
        return None
    except Exception:
        logger.exception("Mini App initData validation failed unexpectedly")
        return None


def _validate_webapp_init_data(
    init_data_raw: str,
    bot_token: str,
) -> dict[str, Any]:
    if not init_data_raw:
        raise SecurityValidationError("missing initData")
    if not bot_token:
        raise SecurityValidationError("missing bot token")

    try:
        parsed_pairs = parse_qsl(
            init_data_raw,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise SecurityValidationError("malformed query string") from exc

    if not parsed_pairs:
        raise SecurityValidationError("empty query string")

    hash_values = [value for key, value in parsed_pairs if key == "hash"]
    if len(hash_values) != 1 or not hash_values[0]:
        raise SecurityValidationError("missing or duplicated hash")

    seen_keys: set[str] = set()
    for key, _value in parsed_pairs:
        if key == "hash":
            continue
        if key in seen_keys:
            raise SecurityValidationError(f"duplicated field {key}")
        seen_keys.add(key)

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

    received_hash = hash_values[0]
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise SecurityValidationError("invalid hash")

    init_data = dict(parsed_pairs)
    user_raw = init_data.get("user")
    if user_raw:
        try:
            init_data["user"] = json.loads(user_raw)
        except json.JSONDecodeError as exc:
            raise SecurityValidationError("malformed user JSON") from exc

    logger.info(
        "Mini App initData validated: has_user=%s auth_date=%s",
        "user" in init_data,
        init_data.get("auth_date"),
    )
    return init_data


def is_admin(user_id: int, admin_ids: list[int]) -> bool:
    try:
        normalized_ids = {int(admin_id) for admin_id in admin_ids}
    except (TypeError, ValueError) as exc:
        logger.warning("Admin ID list contains invalid values: %s", exc)
        return False

    return user_id in normalized_ids


class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: list[int] | None = None) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        try:
            admin_ids = self._resolve_admin_ids(data)
        except Exception:
            logger.exception("Failed to resolve admin IDs")
            admin_ids = []

        user_id = event.from_user.id if event.from_user else None

        if user_id is None or not is_admin(user_id, admin_ids):
            logger.warning("Admin access denied: user_id=%s", user_id)
            await self._notify_denied(event)
            return None

        logger.info("Admin access granted: user_id=%d", user_id)
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Admin handler failed: user_id=%d", user_id)
            raise

    def _resolve_admin_ids(self, data: dict[str, Any]) -> list[int]:
        if self.admin_ids is not None:
            return self._normalize_admin_ids(self.admin_ids)

        admin_ids = data.get("admin_ids")
        if admin_ids is not None:
            return self._normalize_admin_ids(admin_ids)

        config = data.get("config")
        if config is not None and hasattr(config, "ADMIN_IDS"):
            return self._normalize_admin_ids(config.ADMIN_IDS)

        return []

    def _normalize_admin_ids(self, admin_ids: Any) -> list[int]:
        if not isinstance(admin_ids, (list, tuple, set)):
            raise SecurityValidationError("admin_ids must be a sequence")
        return [int(admin_id) for admin_id in admin_ids]

    async def _notify_denied(self, event: Message | CallbackQuery) -> None:
        try:
            if isinstance(event, CallbackQuery):
                await event.answer("Access denied", show_alert=True)
                return

            await event.answer("Access denied")
        except Exception:
            logger.exception("Failed to send admin access denied notification")
