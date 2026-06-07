from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.infrastructure import create_db_pool
from bot.models import Base


logger = logging.getLogger(__name__)


class DatabaseSessionError(RuntimeError):
    """Raised when database session setup or transaction handling fails."""


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    logger.info("Creating async database session factory")
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
            logger.debug("Database session committed")
        except Exception as exc:
            logger.exception("Database session failed; rolling back transaction")
            try:
                await session.rollback()
                logger.debug("Database session rolled back")
            except SQLAlchemyError as rollback_exc:
                logger.exception("Database session rollback failed")
                raise DatabaseSessionError("Database rollback failed") from rollback_exc
            raise


async def create_engine_and_session_factory(
    db_url: str,
    pool_size: int = 10,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    logger.info("Creating database engine and session factory")
    try:
        engine = await create_db_pool(db_url, pool_size=pool_size)
        return engine, create_session_factory(engine)
    except SQLAlchemyError as exc:
        logger.exception("Failed to create database engine/session factory")
        raise DatabaseSessionError("Database engine/session setup failed") from exc


async def create_all_tables(engine: AsyncEngine) -> None:
    logger.info("Creating database tables from SQLAlchemy metadata")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except SQLAlchemyError as exc:
        logger.exception("Failed to create database tables")
        raise DatabaseSessionError("Database table creation failed") from exc
    logger.info("Database tables created")


async def run_sql_migrations(
    engine: AsyncEngine,
    migrations_dir: str | Path = "migrations",
) -> None:
    migrations_path = Path(migrations_dir)
    if not migrations_path.exists():
        logger.warning("Migrations directory not found: %s", migrations_path)
        return

    migration_files = sorted(migrations_path.glob("*.sql"))
    if not migration_files:
        logger.warning("No SQL migrations found in %s", migrations_path)
        return

    logger.info("Running SQL migrations: count=%d", len(migration_files))
    try:
        async with engine.begin() as conn:
            for migration_file in migration_files:
                sql = migration_file.read_text().strip()
                if not sql:
                    logger.debug("Skipping empty SQL migration: %s", migration_file.name)
                    continue
                logger.info("Applying SQL migration: %s", migration_file.name)
                for statement in _split_sql_statements(sql):
                    await conn.execute(text(statement))
    except (OSError, SQLAlchemyError) as exc:
        logger.exception("Failed to run SQL migrations")
        raise DatabaseSessionError("SQL migration failed") from exc

    logger.info("SQL migrations completed")


def _split_sql_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]
