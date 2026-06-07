from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
