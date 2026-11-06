from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessions
