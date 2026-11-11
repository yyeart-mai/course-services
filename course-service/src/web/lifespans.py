import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from src.core.config import get_settings
from src.core.database import create_database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan_service_router(_app: FastAPI) -> AsyncGenerator[None]:
    logger.debug("Lifespan (service router) startup")
    yield
    logger.debug("Lifespan (service router) shutdown")


@asynccontextmanager
async def lifespan_app(app: FastAPI) -> AsyncGenerator[None]:
    logger.debug("Lifespan (app) startup")

    engine, sessions = create_database(get_settings().database_url)

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1;"))

        app.state.session_factory = sessions
        yield

    finally:
        logger.debug("Lifespan (app) shutdown")
        await engine.dispose()
