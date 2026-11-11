from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, cast
from uuid import UUID

import jwt
from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import Settings, get_settings
from src.web.errors import ApiError


@dataclass
class Principal:

    id: UUID
    role: str


bearer = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


async def get_principal(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer),
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if credentials is None:
        raise ApiError(401, "TOKEN_MISSING", "Token is missing")

    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
        user_id = UUID(claims["sub"])
        role = claims["role"]
        if role not in {"admin", "professor", "student"}:
            raise ValueError("unknown role")

    except jwt.ExpiredSignatureError as e:
        raise ApiError(401, "TOKEN_EXPIRED", "Token is expired") from e

    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as e:
        raise ApiError(401, "TOKEN_INVALID", "Token is invalid") from e

    return Principal(id=user_id, role=role)


def require_roles(*allowed: str) -> Callable[[Principal], Awaitable[Principal]]:

    async def check(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        if principal.role not in allowed:
            raise ApiError(403, "ACCESS_DENIED", "No access to this operation")

        return principal

    return check


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
