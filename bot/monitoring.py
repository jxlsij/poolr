from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Bet, Deposit, DepositStatus, Withdrawal, WithdrawalStatus


logger = logging.getLogger(__name__)
BOT_APP_KEY = web.AppKey("bot", object)
DB_SESSION_FACTORY_APP_KEY = web.AppKey("db_session_factory", object)


@dataclass(frozen=True)
class AnomalyReport:
    user_id: int
    kind: str
    severity: str
    details: dict[str, Any]


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    format: str = "json",
    log_file: str | None = None,
) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    formatter: logging.Formatter
    if format == "json":
        formatter = JsonLogFormatter()
    elif format == "pretty":
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        raise ValueError("LOG_FORMAT must be json or pretty")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)
    for handler in handlers:
        handler.setLevel(log_level)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    logger.info("Logging configured: level=%s format=%s file=%s", level, format, bool(log_file))


async def health_check(request: web.Request) -> web.Response:
    try:
        db_status = await _check_database(request)
        bot_status = "running" if _app_get(request.app, BOT_APP_KEY, "bot") is not None else "unconfigured"
        status = "ok" if db_status == "connected" and bot_status == "running" else "degraded"
        payload = {
            "status": status,
            "db": db_status,
            "bot": bot_status,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Health check: status=%s db=%s bot=%s", status, db_status, bot_status)
        return web.json_response(payload, status=200 if status == "ok" else 503)
    except Exception:
        logger.exception("Health check failed unexpectedly")
        return web.json_response(
            {
                "status": "degraded",
                "db": "unknown",
                "bot": "unknown",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            status=503,
        )


async def monitor_payment_anomalies(session: AsyncSession) -> list[AnomalyReport]:
    reports: list[AnomalyReport] = []
    reports.extend(await _users_with_high_withdrawal_pressure(session))
    reports.extend(await _users_with_many_small_bets(session))
    logger.info("Payment anomaly scan completed: count=%d", len(reports))
    return reports


async def _check_database(request: web.Request) -> str:
    session_factory = _app_get(request.app, DB_SESSION_FACTORY_APP_KEY, "db_session_factory")
    if session_factory is None:
        return "unconfigured"

    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return "connected"
    except Exception:
        logger.exception("Health check database probe failed")
        return "error"


def _app_get(app: web.Application, typed_key: web.AppKey[Any], string_key: str) -> Any:
    value = app.get(typed_key)
    if value is not None:
        return value
    return app.get(string_key)


async def _users_with_high_withdrawal_pressure(session: AsyncSession) -> list[AnomalyReport]:
    deposit_rows = (
        await session.execute(
            select(Deposit.user_id, func.coalesce(func.sum(Deposit.stars_amount), 0))
            .where(Deposit.status == DepositStatus.CONFIRMED)
            .group_by(Deposit.user_id)
        )
    ).all()
    deposits_by_user = {int(user_id): int(total) for user_id, total in deposit_rows}

    withdrawal_rows = (
        await session.execute(
            select(Withdrawal.user_id, func.coalesce(func.sum(Withdrawal.credits_amount), 0))
            .where(Withdrawal.status.in_([WithdrawalStatus.PENDING, WithdrawalStatus.COMPLETED]))
            .group_by(Withdrawal.user_id)
        )
    ).all()

    reports: list[AnomalyReport] = []
    for user_id, total_withdrawn_raw in withdrawal_rows:
        total_withdrawn = int(total_withdrawn_raw or 0)
        total_deposited = deposits_by_user.get(int(user_id), 0)
        if total_withdrawn >= 100 and (total_deposited == 0 or total_withdrawn >= total_deposited * 3):
            reports.append(
                AnomalyReport(
                    user_id=int(user_id),
                    kind="high_withdrawal_pressure",
                    severity="high" if total_deposited == 0 else "medium",
                    details={
                        "withdrawn_stars": total_withdrawn,
                        "deposited_stars": total_deposited,
                    },
                )
            )
    return reports


async def _users_with_many_small_bets(session: AsyncSession) -> list[AnomalyReport]:
    rows = (
        await session.execute(
            select(Bet.user_id, func.count(), func.coalesce(func.sum(Bet.credits_amount), 0))
            .where(Bet.credits_amount <= 2)
            .group_by(Bet.user_id)
        )
    ).all()

    reports: list[AnomalyReport] = []
    for user_id, count_raw, total_raw in rows:
        count = int(count_raw or 0)
        if count >= 20:
            reports.append(
                AnomalyReport(
                    user_id=int(user_id),
                    kind="many_small_bets",
                    severity="low",
                    details={
                        "bet_count": count,
                        "total_stars": int(total_raw or 0),
                    },
                )
            )
    return reports
