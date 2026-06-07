from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from bot.config import ConfigError, load_config
from bot.infrastructure import DEFAULT_ALLOWED_UPDATES, setup_webhook


logger = logging.getLogger(__name__)


async def health_check(request: web.Request) -> web.Response:
    logger.info("Health ping received")
    return web.Response(text="OK")


async def on_startup(bot: Bot, webhook_url: str, webhook_secret: str) -> None:
    ok = await setup_webhook(
        bot=bot,
        webhook_url=webhook_url,
        secret_token=webhook_secret,
        allowed_updates=DEFAULT_ALLOWED_UPDATES,
    )
    logger.info("Webhook setup result: %s", ok)


def create_bot(token: str) -> Bot:
    telegram_api_url = os.getenv("TELEGRAM_API_URL")
    if not telegram_api_url:
        return Bot(token=token)

    api_base = _normalize_aiogram_api_base(telegram_api_url)
    session = AiohttpSession(api=TelegramAPIServer.from_base(api_base))
    logger.info("Using custom Telegram API base: %s", api_base)
    return Bot(token=token, session=session)


def create_app() -> web.Application:
    config = load_config()
    bot = create_bot(config.BOT_TOKEN)
    dispatcher = Dispatcher()
    webhook_path = _webhook_path_from_url(config.WEBHOOK_URL)

    dispatcher.startup.register(
        lambda bot: on_startup(
            bot=bot,
            webhook_url=config.WEBHOOK_URL,
            webhook_secret=config.WEBHOOK_SECRET,
        )
    )

    app = web.Application()
    app.router.add_get("/", health_check)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=config.WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=webhook_path)
    setup_application(app, dispatcher, bot=bot)

    return app


def _normalize_aiogram_api_base(telegram_api_url: str) -> str:
    telegram_api_url = telegram_api_url.rstrip("/")
    for suffix in ("/bot{0}/{1}", "/bot{token}/{method}"):
        if telegram_api_url.endswith(suffix):
            return telegram_api_url[: -len(suffix)]
    return telegram_api_url


def _webhook_path_from_url(webhook_url: str) -> str:
    path = urlparse(webhook_url).path
    return path or "/"


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        app = create_app()
    except ConfigError as exc:
        logger.warning("Config is incomplete: %s", exc)
        app = web.Application()
        app.router.add_get("/", health_check)

    port = int(os.getenv("PORT", "7860"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

