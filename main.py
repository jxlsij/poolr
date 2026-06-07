from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from api.webapp import API_SESSION_FACTORY_KEY, api_error_middleware, setup_api_routes
from bot.admin import create_admin_router
from bot.config import ConfigError, load_config
from bot.database import create_engine_and_session_factory, run_sql_migrations
from bot.betting import create_betting_router
from bot.fraud import create_fraud_router
from bot.handlers.markets import create_markets_router
from bot.handlers.start import create_start_router
from bot.infrastructure import DEFAULT_ALLOWED_UPDATES, InfrastructureError, setup_webhook
from bot.middleware.database import DatabaseSessionMiddleware
from bot.monitoring import BOT_APP_KEY, DB_SESSION_FACTORY_APP_KEY
from bot.monitoring import health_check as detailed_health_check
from bot.monitoring import setup_logging
from bot.notifications import start_notification_worker, stop_notification_worker
from bot.payments import create_payments_router
from bot.resolution import create_resolution_router
from bot.withdrawals import create_withdrawals_router


logger = logging.getLogger(__name__)


async def health_check(request: web.Request) -> web.Response:
    logger.info("Health ping received from %s", request.remote)
    return web.Response(text="OK")


async def on_startup(bot: Bot, webhook_url: str, webhook_secret: str, open_url: str) -> None:
    await setup_web_app_menu_button(bot=bot, open_url=open_url)

    ok = await setup_webhook(
        bot=bot,
        webhook_url=webhook_url,
        secret_token=webhook_secret,
        allowed_updates=DEFAULT_ALLOWED_UPDATES,
    )
    logger.info("Webhook setup result: %s", ok)
    if not ok:
        raise InfrastructureError("Telegram webhook setup returned false")


async def setup_web_app_menu_button(bot: Bot, open_url: str) -> None:
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Open",
            web_app=WebAppInfo(url=open_url),
        )
    )
    logger.info("Telegram Web App menu button configured")


def create_bot(token: str) -> Bot:
    telegram_api_url = os.getenv("TELEGRAM_API_URL")
    if not telegram_api_url:
        logger.info("Using default Telegram API endpoint")
        return Bot(token=token)

    api_base = _normalize_aiogram_api_base(telegram_api_url)
    session = AiohttpSession(api=TelegramAPIServer.from_base(api_base))
    logger.info("Using custom Telegram API base: %s", api_base)
    return Bot(token=token, session=session)


def create_app() -> web.Application:
    logger.info("Creating web application")
    config = load_config()
    bot = create_bot(config.BOT_TOKEN)
    dispatcher = Dispatcher()
    open_url = _resolve_open_url(config.WEBHOOK_URL)
    dispatcher.include_router(create_start_router())
    dispatcher.include_router(create_markets_router(os.getenv("MINI_APP_URL")))
    dispatcher.include_router(create_betting_router(os.getenv("MINI_APP_URL")))
    dispatcher.include_router(
        create_resolution_router(
            platform_fee_pct=config.PLATFORM_FEE_PCT,
            mini_app_url=os.getenv("MINI_APP_URL"),
        )
    )
    dispatcher.include_router(
        create_fraud_router(
            admin_ids=config.ADMIN_IDS,
            platform_fee_pct=config.PLATFORM_FEE_PCT,
            mini_app_url=os.getenv("MINI_APP_URL"),
        )
    )
    dispatcher.include_router(create_withdrawals_router(config.ADMIN_IDS))
    dispatcher.include_router(create_admin_router(config.ADMIN_IDS))
    dispatcher.include_router(create_payments_router(platform_fee_pct=config.PLATFORM_FEE_PCT))
    webhook_path = _webhook_path_from_url(config.WEBHOOK_URL)
    logger.info("Registering webhook handler at path %s", webhook_path)

    async def startup_handler(bot: Bot) -> None:
        engine, session_factory = await create_engine_and_session_factory(config.DB_URL)
        app["db_engine"] = engine
        app["db_session_factory"] = session_factory
        app[DB_SESSION_FACTORY_APP_KEY] = session_factory
        app[API_SESSION_FACTORY_KEY] = session_factory
        await run_sql_migrations(engine)
        dispatcher.update.middleware(DatabaseSessionMiddleware(session_factory))
        logger.info("Database session middleware registered")
        app["notification_worker"] = start_notification_worker(
            bot=bot,
            session_factory=session_factory,
            mini_app_url=os.getenv("MINI_APP_URL"),
            interval_seconds=_notification_interval_seconds(),
        )
        await on_startup(
            bot=bot,
            webhook_url=config.WEBHOOK_URL,
            webhook_secret=config.WEBHOOK_SECRET,
            open_url=open_url,
        )

    dispatcher.startup.register(startup_handler)

    app = web.Application(middlewares=[api_error_middleware])
    app[BOT_APP_KEY] = bot
    app.router.add_get("/", health_check)
    app.router.add_get("/health", detailed_health_check)
    app.on_cleanup.append(cleanup_database)
    setup_api_routes(
        app,
        bot=bot,
        bot_token=config.BOT_TOKEN,
        admin_ids=config.ADMIN_IDS,
        platform_fee_pct=config.PLATFORM_FEE_PCT,
    )

    webhook_handler = SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=config.WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=webhook_path)
    setup_application(app, dispatcher, bot=bot)

    return app


async def cleanup_database(app: web.Application) -> None:
    await stop_notification_worker(app.get("notification_worker"))

    engine = app.get("db_engine")
    if engine is None:
        return

    await engine.dispose()
    logger.info("Database engine disposed")


def _normalize_aiogram_api_base(telegram_api_url: str) -> str:
    telegram_api_url = telegram_api_url.rstrip("/")
    for suffix in ("/bot{0}/{1}", "/bot{token}/{method}"):
        if telegram_api_url.endswith(suffix):
            return telegram_api_url[: -len(suffix)]
    return telegram_api_url


def _webhook_path_from_url(webhook_url: str) -> str:
    path = urlparse(webhook_url).path
    return path or "/"


def _resolve_open_url(webhook_url: str) -> str:
    return os.getenv("MINI_APP_URL") or webhook_url


def _notification_interval_seconds() -> int:
    raw_value = os.getenv("NOTIFICATION_CHECK_INTERVAL_SECONDS", "300")
    try:
        interval = int(raw_value)
    except ValueError as exc:
        logger.exception("NOTIFICATION_CHECK_INTERVAL_SECONDS must be an integer")
        raise RuntimeError("NOTIFICATION_CHECK_INTERVAL_SECONDS must be an integer") from exc
    if interval < 1:
        raise RuntimeError("NOTIFICATION_CHECK_INTERVAL_SECONDS must be at least 1")
    return interval


def main() -> None:
    setup_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format=os.getenv("LOG_FORMAT", "json"),
        log_file=os.getenv("LOG_FILE") or None,
    )

    try:
        app = create_app()
    except ConfigError as exc:
        logger.warning("Config is incomplete: %s", exc)
        app = web.Application()
        app.router.add_get("/", health_check)
    except Exception:
        logger.exception("Application setup failed")
        raise

    try:
        port = int(os.getenv("PORT", "7860"))
    except ValueError as exc:
        logger.exception("PORT must be an integer")
        raise RuntimeError("PORT must be an integer") from exc

    logger.info("Starting web server on 0.0.0.0:%d", port)
    try:
        web.run_app(app, host="0.0.0.0", port=port)
    except Exception:
        logger.exception("Web server stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()
