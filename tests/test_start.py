from bot.handlers.start import build_start_keyboard, build_web_app_menu_button


def test_build_start_keyboard_uses_web_app_button() -> None:
    keyboard = build_start_keyboard("https://example.com/app")

    assert keyboard is not None
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Open Mini App"
    assert button.web_app is not None
    assert button.web_app.url == "https://example.com/app"


def test_build_start_keyboard_skips_missing_url() -> None:
    assert build_start_keyboard(None) is None


def test_build_web_app_menu_button_uses_open_url() -> None:
    menu_button = build_web_app_menu_button("https://example.com/app")

    assert menu_button.text == "Open"
    assert menu_button.web_app.url == "https://example.com/app"
