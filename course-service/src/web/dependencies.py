from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessions = cast(
        async_sessionmaker[AsyncSession],
        request.app.state.session_factory,
    )

    async with sessions() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            await session.close()
