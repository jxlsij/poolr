from pathlib import Path

from bot.config import ConfigError, _redact_db_url, load_config


def test_load_config_reads_env_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PLATFORM_FEE_PCT", raising=False)
    monkeypatch.delenv("ADMIN_IDS", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/poolr",
                "WEBHOOK_URL=https://example.com/webhook",
                "WEBHOOK_SECRET=secret",
                "PLATFORM_FEE_PCT=0.05",
                "ADMIN_IDS=1, 2 3",
            ]
        )
    )

    config = load_config(str(env_file))

    assert config.BOT_TOKEN == "test-token"
    assert config.DB_URL.endswith("/poolr")
    assert config.WEBHOOK_URL == "https://example.com/webhook"
    assert config.WEBHOOK_SECRET == "secret"
    assert config.PLATFORM_FEE_PCT == 0.05
    assert config.ADMIN_IDS == [1, 2, 3]


def test_load_config_prefers_process_env(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=file-token",
                "DATABASE_URL=postgresql://file",
                "WEBHOOK_URL=https://file.example/webhook",
                "WEBHOOK_SECRET=file-secret",
            ]
        )
    )
    monkeypatch.setenv("BOT_TOKEN", "process-token")

    config = load_config(str(env_file))

    assert config.BOT_TOKEN == "process-token"


def test_load_config_requires_database_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "WEBHOOK_URL=https://example.com/webhook",
                "WEBHOOK_SECRET=secret",
            ]
        )
    )

    try:
        load_config(str(env_file))
    except ConfigError as exc:
        assert "DB_URL or DATABASE_URL" in str(exc)
    else:
        raise AssertionError("ConfigError was not raised")


def test_redact_db_url_hides_password() -> None:
    redacted = _redact_db_url(
        "postgresql://postgres:secret@db.example.supabase.co:5432/postgres"
    )

    assert redacted == "postgresql://postgres:***@db.example.supabase.co:5432/postgres"
    assert "secret" not in redacted
