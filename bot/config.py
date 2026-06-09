from __future__ import annotations

import os
import shlex
import logging
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Config:
    BOT_TOKEN: str
    DB_URL: str
    WEBHOOK_URL: str
    WEBHOOK_SECRET: str
    PLATFORM_FEE_PCT: float
    ADMIN_IDS: list[int]


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


def load_config(env_path: str = ".env") -> Config:
    logger.info("Loading config from %s and process environment", env_path)
    env_values = _read_env_file(env_path)
    values = {**env_values, **os.environ}

    bot_token = _require(values, "BOT_TOKEN")
    db_url = values.get("DB_URL") or values.get("DATABASE_URL")
    if not db_url:
        raise ConfigError("DB_URL or DATABASE_URL is required")

    webhook_url = _require(values, "WEBHOOK_URL")
    webhook_secret = _require(values, "WEBHOOK_SECRET")
    platform_fee_pct = _parse_float(
        values.get("PLATFORM_FEE_PCT", "0.08"),
        "PLATFORM_FEE_PCT",
    )
    admin_ids = _parse_admin_ids(values.get("ADMIN_IDS", ""))

    config = Config(
        BOT_TOKEN=bot_token,
        DB_URL=db_url,
        WEBHOOK_URL=webhook_url,
        WEBHOOK_SECRET=webhook_secret,
        PLATFORM_FEE_PCT=platform_fee_pct,
        ADMIN_IDS=admin_ids,
    )
    logger.info(
        "Config loaded: db=%s webhook_url=%s platform_fee_pct=%s admin_count=%d",
        _redact_db_url(config.DB_URL),
        config.WEBHOOK_URL,
        config.PLATFORM_FEE_PCT,
        len(config.ADMIN_IDS),
    )
    return config


def _read_env_file(env_path: str) -> dict[str, str]:
    path = Path(env_path)
    if not path.exists():
        logger.info("Config file %s not found; using process environment only", env_path)
        return {}

    result: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        logger.exception("Failed to read config file %s", env_path)
        raise ConfigError(f"Could not read env file {env_path}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export ") :].strip()

        if "=" not in line:
            raise ConfigError(f"Invalid .env line {line_number}: expected KEY=value")

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"Invalid .env line {line_number}: empty key")

        result[key] = _strip_env_value(value)

    logger.info("Loaded %d value(s) from %s", len(result), env_path)
    return result


def _strip_env_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""

    if value[0] not in {'"', "'"}:
        return value.split(" #", 1)[0].strip()

    try:
        parsed = shlex.split(value, comments=True, posix=True)
    except ValueError as exc:
        raise ConfigError(f"Invalid quoted .env value: {value}") from exc

    if not parsed:
        return ""

    return parsed[0]


def _require(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _parse_float(value: str, key: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a float") from exc

    if parsed < 0 or parsed >= 1:
        raise ConfigError(f"{key} must be in the range [0, 1)")

    return parsed


def _parse_admin_ids(value: str) -> list[int]:
    if not value.strip():
        return []

    raw_ids = value.replace(",", " ").split()
    try:
        return [int(raw_id) for raw_id in raw_ids]
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS must contain integer Telegram user IDs") from exc


def _redact_db_url(db_url: str) -> str:
    if "@" not in db_url:
        return db_url

    scheme, _, rest = db_url.partition("://")
    credentials, _, host = rest.partition("@")
    username, separator, _password = credentials.partition(":")
    redacted_credentials = username if not separator else f"{username}:***"
    return f"{scheme}://{redacted_credentials}@{host}"
