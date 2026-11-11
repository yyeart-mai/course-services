# Creation context and callbacks are supplied as separate arguments.

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import IdempotencyRecord
from src.web.errors import ApiError


@dataclass(frozen=True)
class CreatedResponse:

    status: int
    body: dict[Any, Any]
    location: str


def request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_once(
    session: AsyncSession,
    *,
    user_id: UUID,
    method: str,
    path: str,
    key: UUID,
    payload: dict[str, Any],
    authorize: Callable[[AsyncSession], Awaitable[None]],
    create: Callable[[AsyncSession], Awaitable[CreatedResponse]],
) -> CreatedResponse:
    body_hash = request_hash(payload)
    scope = f"{user_id}:{method}:{path}:{key}"
    lock_key = int.from_bytes(
        hashlib.sha256(scope.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )

    async with session.begin():
        await authorize(session)

        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

        existing = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.method == method,
                IdempotencyRecord.path == path,
                IdempotencyRecord.key == key,
            )
        )

        if existing is not None:
            if existing.request_hash != body_hash:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_CONFLICT",
                    "Key was used with another request body",
                )

            return CreatedResponse(
                status=existing.response_status,
                body=existing.response_body,
                location=existing.location,
            )

        result = await create(session)
        session.add(
            IdempotencyRecord(
                user_id=user_id,
                method=method,
                path=path,
                key=key,
                request_hash=body_hash,
                response_status=result.status,
                response_body=result.body,
                location=result.location,
            )
        )

    return result
