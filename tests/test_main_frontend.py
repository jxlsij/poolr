from aiohttp import web

from main import _app_url_from_webhook, _resolve_market_link_url, _resolve_open_url, setup_frontend_routes


def test_app_url_from_webhook_uses_frontend_path() -> None:
    assert _app_url_from_webhook("https://example.com/webhook?secret=1") == "https://example.com/app"


def test_resolve_open_url_prefers_explicit_mini_app_url(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_URL", "https://mini.example/poolr")

    assert _resolve_open_url("https://example.com/webhook") == "https://mini.example/poolr"


def test_resolve_open_url_defaults_to_embedded_frontend(monkeypatch) -> None:
    monkeypatch.delenv("MINI_APP_URL", raising=False)

    assert _resolve_open_url("https://example.com/webhook") == "https://example.com/app"


def test_resolve_market_link_url_prefers_explicit_direct_url(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_DIRECT_URL", "https://t.me/custom_bot/custom_app?mode=1")

    assert _resolve_market_link_url() == "https://t.me/custom_bot/custom_app?mode=1"


def test_resolve_market_link_url_defaults_to_poolr_direct_link(monkeypatch) -> None:
    monkeypatch.delenv("MINI_APP_DIRECT_URL", raising=False)
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    monkeypatch.delenv("MINI_APP_SHORT_NAME", raising=False)

    assert _resolve_market_link_url() == "https://t.me/pooolr_bot/poolr"


def test_resolve_market_link_url_uses_botfather_env(monkeypatch) -> None:
    monkeypatch.delenv("MINI_APP_DIRECT_URL", raising=False)
    monkeypatch.setenv("BOT_USERNAME", "@example_bot")
    monkeypatch.setenv("MINI_APP_SHORT_NAME", "events")

    assert _resolve_market_link_url() == "https://t.me/example_bot/events"


def test_setup_frontend_routes_registers_app_route() -> None:
    app = web.Application()

    setup_frontend_routes(app)

    paths = {resource.canonical for resource in app.router.resources()}
    assert "/app" in paths
    assert "/app/" in paths
