from bot.infrastructure import (
    DEFAULT_ALLOWED_UPDATES,
    _normalize_async_postgres_url,
    _redact_db_url,
)


def test_normalize_postgres_url_for_asyncpg() -> None:
    assert (
        _normalize_async_postgres_url("postgresql://user:pass@localhost/db")
        == "postgresql+asyncpg://user:pass@localhost/db"
    )
    assert (
        _normalize_async_postgres_url("postgres://user:pass@localhost/db")
        == "postgresql+asyncpg://user:pass@localhost/db"
    )
    assert (
        _normalize_async_postgres_url("postgresql+asyncpg://user:pass@localhost/db")
        == "postgresql+asyncpg://user:pass@localhost/db"
    )


def test_normalize_postgres_url_translates_sslmode_for_asyncpg() -> None:
    assert (
        _normalize_async_postgres_url(
            "postgresql://user:pass@pooler.supabase.com:6543/db?sslmode=require"
        )
        == "postgresql+asyncpg://user:pass@pooler.supabase.com:6543/db?ssl=require"
    )


def test_redact_db_url_hides_password() -> None:
    redacted = _redact_db_url("postgresql+asyncpg://user:pass@localhost/db")

    assert redacted == "postgresql+asyncpg://user:***@localhost/db"
    assert "pass" not in redacted


def test_default_allowed_updates_include_inline_mode() -> None:
    assert "inline_query" in DEFAULT_ALLOWED_UPDATES
    assert "chosen_inline_result" in DEFAULT_ALLOWED_UPDATES
