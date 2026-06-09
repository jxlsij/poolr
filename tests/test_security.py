import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from bot.security import (
    is_admin,
    validate_webapp_init_data,
    verify_webhook_request,
)


def test_verify_webhook_request_accepts_matching_secret() -> None:
    assert verify_webhook_request(b"{}", "secret", "secret") is True


def test_verify_webhook_request_rejects_invalid_secret() -> None:
    assert verify_webhook_request(b"{}", "secret", "wrong") is False


def test_validate_webapp_init_data_accepts_valid_hash() -> None:
    bot_token = "123456:test-token"
    auth_date = _current_auth_date()
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={
            "auth_date": auth_date,
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "user": json.dumps(
                {"id": 42, "first_name": "Ada", "username": "ada"},
                separators=(",", ":"),
            ),
        },
    )

    result = validate_webapp_init_data(init_data_raw, bot_token)

    assert result is not None
    assert result["auth_date"] == auth_date
    assert result["user"]["id"] == 42
    assert result["hash"]


def test_validate_webapp_init_data_rejects_tampered_data() -> None:
    bot_token = "123456:test-token"
    auth_date = _current_auth_date()
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": auth_date, "user": '{"id":42}'},
    )
    tampered = init_data_raw.replace(auth_date, str(int(auth_date) + 1))

    assert validate_webapp_init_data(tampered, bot_token) is None


def test_validate_webapp_init_data_rejects_malformed_user_json() -> None:
    bot_token = "123456:test-token"
    auth_date = _current_auth_date()
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": auth_date, "user": '{"id":42'},
    )

    assert validate_webapp_init_data(init_data_raw, bot_token) is None


def test_validate_webapp_init_data_rejects_duplicate_hash() -> None:
    bot_token = "123456:test-token"
    auth_date = _current_auth_date()
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": auth_date},
    )

    assert validate_webapp_init_data(f"{init_data_raw}&hash=duplicate", bot_token) is None


def test_validate_webapp_init_data_rejects_duplicate_field() -> None:
    bot_token = "123456:test-token"
    auth_date = _current_auth_date()
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": auth_date},
    )

    assert (
        validate_webapp_init_data(f"{init_data_raw}&auth_date={auth_date}", bot_token)
        is None
    )


def test_validate_webapp_init_data_rejects_missing_auth_date() -> None:
    bot_token = "123456:test-token"
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"query_id": "AAHdF6IQAAAAAN0XohDhrOrc"},
    )

    assert validate_webapp_init_data(init_data_raw, bot_token) is None


def test_validate_webapp_init_data_rejects_stale_auth_date() -> None:
    bot_token = "123456:test-token"
    stale_auth_date = str(int(datetime.now(timezone.utc).timestamp()) - 2 * 24 * 60 * 60)
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": stale_auth_date, "user": '{"id":42}'},
    )

    assert validate_webapp_init_data(init_data_raw, bot_token) is None


def test_validate_webapp_init_data_rejects_future_auth_date() -> None:
    bot_token = "123456:test-token"
    future_auth_date = str(int(datetime.now(timezone.utc).timestamp()) + 10 * 60)
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": future_auth_date, "user": '{"id":42}'},
    )

    assert validate_webapp_init_data(init_data_raw, bot_token) is None


def test_validate_webapp_init_data_rejects_non_integer_auth_date() -> None:
    bot_token = "123456:test-token"
    init_data_raw = _build_init_data(
        bot_token=bot_token,
        values={"auth_date": "not-a-timestamp", "user": '{"id":42}'},
    )

    assert validate_webapp_init_data(init_data_raw, bot_token) is None


def test_is_admin() -> None:
    assert is_admin(1, [1, 2, 3]) is True
    assert is_admin(4, [1, 2, 3]) is False
    assert is_admin(1, ["bad"]) is False


def _build_init_data(bot_token: str, values: dict[str, str]) -> str:
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    data_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload = {**values, "hash": data_hash}
    return urlencode(payload, quote_via=quote)


def _current_auth_date() -> str:
    return str(int(datetime.now(timezone.utc).timestamp()))
