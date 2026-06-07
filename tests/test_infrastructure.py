from bot.infrastructure import _normalize_async_postgres_url


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

