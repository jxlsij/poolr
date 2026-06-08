from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any

from aiohttp import web
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.betting import (
    BetValidationError,
    estimate_payout,
    send_stake_invoice,
    validate_stake_invoice_request,
)
from bot.crud import (
    DatabaseLayerError,
    get_active_markets_in_chat,
    get_bets_for_market,
    get_market,
    get_pool_by_option,
    get_user_bet_on_market,
)
from bot.database import session_scope
from bot.market_cards import render_market_card_image
from bot.models import Bet, Deposit, Market, MarketStatus, Payout, User, Withdrawal, WithdrawalStatus
from bot.payments import PaymentModuleError, PaymentProviderError, PaymentValidationError, send_deposit_invoice
from bot.security import is_admin, validate_webapp_init_data
from bot.users import UserModuleError, ensure_user_from_webapp_data
from bot.withdrawals import (
    WithdrawalModuleError,
    WithdrawalProviderError,
    WithdrawalValidationError,
    notify_admins_for_withdrawal,
    request_withdrawal,
    validate_ton_wallet,
)


logger = logging.getLogger(__name__)

INIT_DATA_HEADER = "X-Telegram-Init-Data"
AUTH_SCHEME = "tma"
DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 100
API_BOT_KEY = web.AppKey("api_bot", Bot)
API_BOT_TOKEN_KEY = web.AppKey("api_bot_token", str)
API_SESSION_FACTORY_KEY = web.AppKey("api_session_factory", async_sessionmaker[AsyncSession])
API_ADMIN_IDS_KEY = web.AppKey("api_admin_ids", list[int])
API_PLATFORM_FEE_PCT_KEY = web.AppKey("api_platform_fee_pct", float)


class ApiError(RuntimeError):
    """Base API error with a stable client-facing code."""

    def __init__(self, message: str, *, status: int = 500, code: str = "api_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ApiAuthError(ApiError):
    def __init__(self, message: str = "Mini App authorization is required.") -> None:
        super().__init__(message, status=401, code="unauthorized")


class ApiValidationError(ApiError, ValueError):
    def __init__(self, message: str, *, code: str = "validation_error") -> None:
        super().__init__(message, status=400, code=code)


class ApiNotFoundError(ApiError):
    def __init__(self, message: str, *, code: str = "not_found") -> None:
        super().__init__(message, status=404, code=code)


class ApiPersistenceError(ApiError):
    def __init__(self, message: str = "Could not persist Mini App request.") -> None:
        super().__init__(message, status=500, code="persistence_error")


class ApiProviderError(ApiError):
    def __init__(self, message: str = "Telegram provider request failed.") -> None:
        super().__init__(message, status=502, code="provider_error")


@dataclass(frozen=True)
class ApiContext:
    session: AsyncSession
    user: User
    init_data: Mapping[str, Any]


def setup_api_routes(
    app: web.Application,
    *,
    bot: Bot,
    bot_token: str,
    admin_ids: list[int] | None = None,
    platform_fee_pct: float = 0.08,
) -> None:
    app[API_BOT_KEY] = bot
    app[API_BOT_TOKEN_KEY] = bot_token
    app[API_ADMIN_IDS_KEY] = admin_ids or []
    app[API_PLATFORM_FEE_PCT_KEY] = platform_fee_pct

    app.router.add_get("/api/profile", api_get_profile)
    app.router.add_get("/api/markets", api_get_markets)
    app.router.add_get("/api/market/{market_id}", api_get_market)
    app.router.add_get("/api/market/{market_id}/card.png", api_get_market_card_png)
    app.router.add_get("/api/chat/{chat_id}/markets", api_get_chat_markets)
    app.router.add_get("/api/bets", api_get_my_bets)
    app.router.add_get("/api/withdrawals", api_get_my_withdrawals)
    app.router.add_get("/api/admin/overview", api_get_admin_overview)
    app.router.add_post("/api/bet", api_place_bet)
    app.router.add_post("/api/deposit", api_get_deposit_link)
    app.router.add_post("/api/withdraw", api_request_withdrawal)
    logger.info("Mini App API routes registered")


async def api_get_market_card_png(request: web.Request) -> web.Response:
    market_id = parse_positive_int(request.match_info.get("market_id"), "market_id")
    session_factory = request.app[API_SESSION_FACTORY_KEY]
    async with session_factory() as session:
        market = await get_market(session, market_id)
        if market is None:
            raise ApiNotFoundError("Market was not found.", code="market_not_found")
        pool_by_option = await get_pool_by_option(session, market.id)

    return web.Response(
        body=render_market_card_image(market, pool_by_option),
        content_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


def api_operation(operation_name: str) -> Any:
    def decorator(handler: Any) -> Any:
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            logger.debug(
                "Mini App API operation started: operation=%s method=%s path=%s",
                operation_name,
                request.method,
                request.path,
            )
            try:
                response = await handler(request)
            except ApiError:
                logger.warning(
                    "Mini App API operation failed: operation=%s method=%s path=%s",
                    operation_name,
                    request.method,
                    request.path,
                    exc_info=True,
                )
                raise
            except Exception:
                logger.exception(
                    "Mini App API operation failed unexpectedly: operation=%s method=%s path=%s",
                    operation_name,
                    request.method,
                    request.path,
                )
                raise

            logger.debug(
                "Mini App API operation completed: operation=%s method=%s path=%s status=%d",
                operation_name,
                request.method,
                request.path,
                response.status,
            )
            return response

        return wrapper

    return decorator


@api_operation("get_profile")
async def api_get_profile(request: web.Request) -> web.Response:
    async with api_context(request) as ctx:
        stats = await get_user_stats(ctx.session, ctx.user.telegram_id)
        return json_ok(
            {
                "user_id": ctx.user.telegram_id,
                "username": ctx.user.username,
                "first_name": ctx.user.first_name,
                "balance": ctx.user.balance_credits,
                "stats": stats,
            }
        )


@api_operation("get_markets")
async def api_get_markets(request: web.Request) -> web.Response:
    limit = parse_limit(request.query.get("limit"))
    offset = parse_offset(request.query.get("offset"))
    status_filter = parse_market_status_filter(request.query.get("status"))
    async with api_context(request) as ctx:
        stmt = select(Market)
        if status_filter is not None:
            stmt = stmt.where(Market.status == status_filter)
        stmt = stmt.order_by(Market.created_at.desc(), Market.id.desc()).limit(limit).offset(offset)
        markets = list((await ctx.session.scalars(stmt)).all())
        return json_ok(
            {
                "markets": [
                    await serialize_market_summary(ctx.session, market, ctx.user.telegram_id)
                    for market in markets
                ],
                "limit": limit,
                "offset": offset,
                "status": status_filter.value if status_filter is not None else "all",
            }
        )


@api_operation("get_market")
async def api_get_market(request: web.Request) -> web.Response:
    market_id = parse_positive_int(request.match_info.get("market_id"), "market_id")
    async with api_context(request) as ctx:
        market = await get_market(ctx.session, market_id)
        if market is None:
            raise ApiNotFoundError("Market was not found.", code="market_not_found")
        return json_ok(await serialize_market_detail(ctx.session, market, ctx.user.telegram_id))


@api_operation("get_chat_markets")
async def api_get_chat_markets(request: web.Request) -> web.Response:
    chat_id = parse_int(request.match_info.get("chat_id"), "chat_id")
    async with api_context(request) as ctx:
        markets = await get_active_markets_in_chat(ctx.session, chat_id)
        return json_ok(
            {
                "chat_id": chat_id,
                "markets": [
                    await serialize_market_summary(ctx.session, market, ctx.user.telegram_id)
                    for market in markets
                ],
            }
        )


@api_operation("get_my_bets")
async def api_get_my_bets(request: web.Request) -> web.Response:
    limit = parse_limit(request.query.get("limit"))
    offset = parse_offset(request.query.get("offset"))
    async with api_context(request) as ctx:
        rows = (
            await ctx.session.execute(
                select(Bet, Market)
                .join(Market, Market.id == Bet.market_id)
                .where(Bet.user_id == ctx.user.telegram_id)
                .order_by(Bet.created_at.desc(), Bet.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return json_ok(
            {
                "bets": [
                    {
                        **serialize_bet(bet),
                        "market": serialize_market_base(market),
                    }
                    for bet, market in rows
                ],
                "limit": limit,
                "offset": offset,
            }
        )


@api_operation("get_my_withdrawals")
async def api_get_my_withdrawals(request: web.Request) -> web.Response:
    limit = parse_limit(request.query.get("limit"))
    offset = parse_offset(request.query.get("offset"))
    async with api_context(request) as ctx:
        withdrawals = list(
            (
                await ctx.session.scalars(
                    select(Withdrawal)
                    .where(Withdrawal.user_id == ctx.user.telegram_id)
                    .order_by(Withdrawal.created_at.desc(), Withdrawal.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        return json_ok(
            {
                "withdrawals": [serialize_withdrawal(withdrawal) for withdrawal in withdrawals],
                "limit": limit,
                "offset": offset,
            }
        )


@api_operation("get_admin_overview")
async def api_get_admin_overview(request: web.Request) -> web.Response:
    async with api_context(request) as ctx:
        if not is_admin(ctx.user.telegram_id, request.app[API_ADMIN_IDS_KEY]):
            raise ApiError("Admin access denied.", status=403, code="forbidden")

        total_users = await ctx.session.scalar(select(func.count()).select_from(User))
        active_markets = await ctx.session.scalar(
            select(func.count()).select_from(Market).where(Market.status == MarketStatus.ACTIVE)
        )
        pending_withdrawals = await ctx.session.scalar(
            select(func.count())
            .select_from(Withdrawal)
            .where(Withdrawal.status == WithdrawalStatus.PENDING)
        )
        total_volume = await ctx.session.scalar(
            select(func.coalesce(func.sum(Bet.credits_amount), 0))
        )
        payouts_total = await ctx.session.scalar(
            select(func.coalesce(func.sum(Payout.credits_won), 0))
        )
        deposits_total = await ctx.session.scalar(
            select(func.coalesce(func.sum(Deposit.stars_amount), 0))
        )
        return json_ok(
            {
                "total_users": int(total_users or 0),
                "active_markets": int(active_markets or 0),
                "pending_withdrawals": int(pending_withdrawals or 0),
                "total_volume_stars": int(total_volume or 0),
                "payouts_total_stars": int(payouts_total or 0),
                "deposits_total_stars": int(deposits_total or 0),
            }
        )


@api_operation("place_bet")
async def api_place_bet(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    market_id = parse_positive_int(body.get("market_id"), "market_id")
    option_index = parse_non_negative_int(body.get("option_index"), "option_index")
    stars_amount = parse_positive_int(
        body.get("stars_amount", body.get("credits_amount")),
        "stars_amount",
    )

    async with api_context(request) as ctx:
        market = await get_market(ctx.session, market_id)
        if market is None:
            raise ApiNotFoundError("Market was not found.", code="market_not_found")

        validation_error = validate_stake_invoice_request(
            user=ctx.user,
            market=market,
            option_index=option_index,
            stars_amount=stars_amount,
        )
        if validation_error is not None:
            raise ApiValidationError(
                market_validation_message(validation_error),
                code=validation_error.value,
            )

        pool_by_option = normalized_pool(
            await get_pool_by_option(ctx.session, market.id),
            len(market.options),
        )
        estimated_payout = estimate_payout(
            stars_amount,
            option_index,
            pool_by_option,
            request.app[API_PLATFORM_FEE_PCT_KEY],
        )
        try:
            await send_stake_invoice(
                bot=request.app[API_BOT_KEY],
                chat_id=ctx.user.telegram_id,
                user_id=ctx.user.telegram_id,
                market=market,
                option_index=option_index,
                stars_amount=stars_amount,
            )
        except PaymentProviderError as exc:
            logger.exception(
                "Mini App stake invoice send failed: user_id=%d market_id=%d stars=%d",
                ctx.user.telegram_id,
                market.id,
                stars_amount,
            )
            raise ApiProviderError("Could not send Stars invoice.") from exc

        logger.info(
            "Mini App stake invoice sent: user_id=%d market_id=%d option_index=%d stars=%d",
            ctx.user.telegram_id,
            market.id,
            option_index,
            stars_amount,
        )
        return json_ok(
            {
                "success": True,
                "invoice_sent": True,
                "estimated_payout": estimated_payout,
                "balance": ctx.user.balance_credits,
            }
        )


@api_operation("get_deposit_link")
async def api_get_deposit_link(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    stars_amount = parse_positive_int(body.get("stars_amount"), "stars_amount")

    async with api_context(request) as ctx:
        try:
            await send_deposit_invoice(
                bot=request.app[API_BOT_KEY],
                user_id=ctx.user.telegram_id,
                stars_amount=stars_amount,
                description=f"Add {stars_amount} Stars to Poolr",
            )
        except PaymentProviderError as exc:
            logger.exception(
                "Mini App deposit invoice send failed: user_id=%d stars=%d",
                ctx.user.telegram_id,
                stars_amount,
            )
            raise ApiProviderError("Could not send Stars invoice.") from exc

        logger.info(
            "Mini App deposit invoice sent: user_id=%d stars=%d",
            ctx.user.telegram_id,
            stars_amount,
        )
        return json_ok(
            {
                "success": True,
                "invoice_sent": True,
                "stars_amount": stars_amount,
            }
        )


@api_operation("request_withdrawal")
async def api_request_withdrawal(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    stars_amount = parse_positive_int(
        body.get("stars_amount", body.get("credits_amount")),
        "stars_amount",
    )
    ton_wallet_address = validate_ton_wallet(body.get("ton_wallet_address"))

    async with api_context(request) as ctx:
        result = await request_withdrawal(
            session=ctx.session,
            user_id=ctx.user.telegram_id,
            stars_amount=stars_amount,
            ton_wallet_address=ton_wallet_address,
        )
        await notify_admins_for_withdrawal(
            bot=request.app[API_BOT_KEY],
            withdrawal=result.withdrawal,
            admin_ids=request.app[API_ADMIN_IDS_KEY],
        )
        logger.info(
            "Mini App withdrawal requested: withdrawal_id=%d user_id=%d stars=%d",
            result.withdrawal.id,
            ctx.user.telegram_id,
            result.reserved_stars,
        )
        return json_ok(
            {
                "success": True,
                "withdrawal": serialize_withdrawal(result.withdrawal),
                "reserved_stars": result.reserved_stars,
                "new_balance": result.remaining_balance,
                "message": "Payout request created for manual TON-equivalent review.",
            }
        )


async def get_user_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    bets_count = await session.scalar(select(func.count()).select_from(Bet).where(Bet.user_id == user_id))
    markets_created = await session.scalar(
        select(func.count()).select_from(Market).where(Market.creator_id == user_id)
    )
    total_staked = await session.scalar(
        select(func.coalesce(func.sum(Bet.credits_amount), 0)).where(Bet.user_id == user_id)
    )
    total_won = await session.scalar(
        select(func.coalesce(func.sum(Payout.credits_won), 0)).where(Payout.user_id == user_id)
    )
    pending_withdrawals = await session.scalar(
        select(func.count())
        .select_from(Withdrawal)
        .where(Withdrawal.user_id == user_id, Withdrawal.status == WithdrawalStatus.PENDING)
    )
    return {
        "bets_count": int(bets_count or 0),
        "markets_created": int(markets_created or 0),
        "total_staked": int(total_staked or 0),
        "total_won": int(total_won or 0),
        "pending_withdrawals": int(pending_withdrawals or 0),
    }


async def serialize_market_detail(
    session: AsyncSession,
    market: Market,
    user_id: int,
) -> dict[str, Any]:
    pool_by_option = normalized_pool(await get_pool_by_option(session, market.id), len(market.options))
    odds = calculate_odds(pool_by_option)
    my_bet = await get_user_bet_on_market(session, user_id, market.id)
    bets = await get_bets_for_market(session, market.id)
    return {
        **serialize_market_base(market),
        "pool_by_option": pool_by_option,
        "odds": odds,
        "total_pool": sum(pool_by_option.values()),
        "my_bet": serialize_bet(my_bet) if my_bet is not None else None,
        "bets_count": len(bets),
    }


async def serialize_market_summary(
    session: AsyncSession,
    market: Market,
    user_id: int,
) -> dict[str, Any]:
    detail = await serialize_market_detail(session, market, user_id)
    return {
        key: detail[key]
        for key in (
            "id",
            "creator_id",
            "chat_id",
            "question",
            "options",
            "deadline",
            "min_bet",
            "status",
            "winning_option",
            "resolved_at",
            "created_at",
            "pool_by_option",
            "odds",
            "total_pool",
            "my_bet",
            "bets_count",
        )
    }


def serialize_market_base(market: Market) -> dict[str, Any]:
    return {
        "id": market.id,
        "creator_id": market.creator_id,
        "chat_id": market.chat_id,
        "message_id": market.message_id,
        "inline_message_id": market.inline_message_id,
        "question": market.question,
        "options": list(market.options),
        "deadline": isoformat(market.deadline),
        "min_bet": market.min_bet,
        "status": market.status.value,
        "winning_option": market.winning_option,
        "created_at": isoformat(market.created_at),
        "resolved_at": isoformat(market.resolved_at),
    }


def serialize_bet(bet: Bet) -> dict[str, Any]:
    return {
        "id": bet.id,
        "user_id": bet.user_id,
        "market_id": bet.market_id,
        "option_index": bet.option_index,
        "stars_amount": bet.credits_amount,
        "created_at": isoformat(bet.created_at),
    }


def serialize_withdrawal(withdrawal: Withdrawal) -> dict[str, Any]:
    return {
        "id": withdrawal.id,
        "user_id": withdrawal.user_id,
        "stars_amount": withdrawal.credits_amount,
        "ton_wallet_address": withdrawal.ton_wallet_address,
        "ton_tx_hash": withdrawal.ton_tx_hash,
        "status": withdrawal.status.value,
        "created_at": isoformat(withdrawal.created_at),
        "updated_at": isoformat(withdrawal.updated_at),
    }


@asynccontextmanager
async def api_context(request: web.Request) -> AsyncIterator[ApiContext]:
    init_data = authorize_request(request)
    session_factory = request.app[API_SESSION_FACTORY_KEY]
    async with session_scope(session_factory) as session:
        try:
            user, _is_new = await ensure_user_from_webapp_data(session, init_data)
        except UserModuleError as exc:
            logger.exception("Mini App API identity persistence failed")
            raise ApiError("Could not prepare Mini App user identity.") from exc
        try:
            yield ApiContext(session=session, user=user, init_data=init_data)
        except ApiError:
            raise
        except (PaymentValidationError, WithdrawalValidationError) as exc:
            logger.warning(
                "Mini App API validation failed after identity: user_id=%d",
                user.telegram_id,
                exc_info=True,
            )
            raise ApiValidationError(str(exc)) from exc
        except (PaymentProviderError, WithdrawalProviderError) as exc:
            logger.exception(
                "Mini App API provider operation failed: user_id=%d",
                user.telegram_id,
            )
            raise ApiProviderError() from exc
        except (DatabaseLayerError, PaymentModuleError, WithdrawalModuleError) as exc:
            logger.exception(
                "Mini App API persistence operation failed: user_id=%d",
                user.telegram_id,
            )
            raise ApiPersistenceError() from exc


def authorize_request(request: web.Request) -> Mapping[str, Any]:
    init_data_raw = extract_init_data(request)
    if init_data_raw is None:
        raise ApiAuthError()

    init_data = validate_webapp_init_data(init_data_raw, request.app[API_BOT_TOKEN_KEY])
    if init_data is None:
        raise ApiAuthError("Mini App authorization is invalid.")
    user_id = init_data.get("user", {}).get("id") if isinstance(init_data.get("user"), Mapping) else None
    logger.debug("Mini App API auth accepted: has_user=%s user_id=%s", "user" in init_data, user_id)
    return init_data


def extract_init_data(request: web.Request) -> str | None:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == AUTH_SCHEME and value.strip():
            return value.strip()
    header_value = request.headers.get(INIT_DATA_HEADER)
    if header_value and header_value.strip():
        return header_value.strip()
    return None


@web.middleware
async def api_error_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    start_time = time.perf_counter()
    try:
        response = await handler(request)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.info(
            "Mini App API request completed: method=%s path=%s status=%d duration_ms=%d",
            request.method,
            request.path,
            response.status,
            duration_ms,
        )
        return response
    except ApiError as exc:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.warning(
            "Mini App API request failed: method=%s path=%s status=%d code=%s duration_ms=%d",
            request.method,
            request.path,
            exc.status,
            exc.code,
            duration_ms,
        )
        return json_error(str(exc), status=exc.status, code=exc.code)
    except WithdrawalValidationError as exc:
        logger.warning(
            "Mini App API withdrawal validation failed: method=%s path=%s",
            request.method,
            request.path,
        )
        return json_error(str(exc), status=400, code="withdrawal_validation_error")
    except ValueError as exc:
        logger.warning(
            "Mini App API validation failed: method=%s path=%s",
            request.method,
            request.path,
        )
        return json_error(str(exc), status=400, code="validation_error")
    except Exception:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.exception(
            "Mini App API request failed unexpectedly: method=%s path=%s duration_ms=%d",
            request.method,
            request.path,
            duration_ms,
        )
        return json_error("Mini App request failed.", status=500, code="api_error")


async def read_json_body(request: web.Request) -> Mapping[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiValidationError("Request body must be valid JSON.") from exc
    if not isinstance(body, Mapping):
        raise ApiValidationError("Request body must be a JSON object.")
    return body


def json_ok(data: Mapping[str, Any], *, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def json_error(message: str, *, status: int, code: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def normalized_pool(pool_by_option: dict[int, int], options_count: int) -> dict[int, int]:
    return {
        option_index: int(pool_by_option.get(option_index, 0))
        for option_index in range(options_count)
    }


def calculate_odds(pool_by_option: dict[int, int]) -> dict[int, float]:
    total_pool = sum(amount for amount in pool_by_option.values() if amount > 0)
    if total_pool <= 0:
        return {option_index: 0.0 for option_index in pool_by_option}
    return {
        option_index: round(max(amount, 0) / total_pool, 4)
        for option_index, amount in pool_by_option.items()
    }


def market_validation_message(error: BetValidationError) -> str:
    messages = {
        BetValidationError.MARKET_CLOSED: "Market is closed.",
        BetValidationError.CREATOR_CANNOT_BET: "Market creators cannot bet on their own markets.",
        BetValidationError.INSUFFICIENT_BALANCE: "Insufficient Stars balance.",
        BetValidationError.BELOW_MIN_BET: "Stake is below the market minimum.",
        BetValidationError.INVALID_OPTION: "Option does not exist.",
        BetValidationError.ALREADY_BET: "You already have a bet on this market.",
    }
    return messages[error]


def parse_limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_PAGE_LIMIT
    limit = parse_positive_int(value, "limit")
    return min(limit, MAX_PAGE_LIMIT)


def parse_offset(value: str | None) -> int:
    if value is None:
        return 0
    return parse_non_negative_int(value, "offset")


def parse_market_status_filter(value: str | None) -> MarketStatus | None:
    if value is None or value.strip().lower() == "active":
        return MarketStatus.ACTIVE
    normalized = value.strip().lower()
    if normalized == "all":
        return None
    for status in MarketStatus:
        if status.value == normalized:
            return status
    raise ApiValidationError("status must be active, closed, resolved, cancelled, disputed, or all.")


def parse_positive_int(value: Any, name: str) -> int:
    number = parse_int(value, name)
    if number < 1:
        raise ApiValidationError(f"{name} must be a positive integer.")
    return number


def parse_non_negative_int(value: Any, name: str) -> int:
    number = parse_int(value, name)
    if number < 0:
        raise ApiValidationError(f"{name} must be a non-negative integer.")
    return number


def parse_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ApiValidationError(f"{name} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ApiValidationError(f"{name} must be an integer.") from exc
    raise ApiValidationError(f"{name} must be an integer.")


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
