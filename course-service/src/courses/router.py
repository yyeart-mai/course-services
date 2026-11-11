"""Provide course routes."""

# FastAPI signatures and path parameter names follow the public API contract.
# The shared route shape is intentional.
# pylint: disable=too-many-arguments,too-many-positional-arguments,redefined-builtin,duplicate-code

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.courses import service
from src.courses.schemas import (
    CourseCreate,
    CoursePage,
    CoursePatch,
    CourseRead,
    CourseStatus,
)
from src.web.dependencies import Principal, get_db_session, require_roles
from src.web.errors import ApiError, documented_errors
from src.web.idempotency import IDEMPOTENCY_HEADER, get_idempotency_key
from src.web.pagination import SortOrder

courses_router = APIRouter(
    prefix="/api/v1/courses",
    tags=["Courses"],
)


@courses_router.get(
    "",
    response_model=CoursePage,
    responses=documented_errors(401, 422),
    operation_id="list_courses",
)
async def list_courses(
    principal: Annotated[
        Principal, Depends(require_roles("admin", "professor", "student"))
    ],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    sort: SortOrder = "created_at:desc",
    status: CourseStatus | None = None,
    teacher_id: UUID | None = None,
) -> CoursePage:
    """List courses."""
    courses = await service.list_courses(
        session,
        principal,
        offset=offset,
        limit=limit,
        sort=sort,
        status=status,
        teacher_id=teacher_id,
    )

    return courses


@courses_router.post(
    "",
    response_model=CourseRead,
    status_code=201,
    responses=documented_errors(401, 403, 404, 409, 422),
    openapi_extra=IDEMPOTENCY_HEADER,
    operation_id="create_course",
)
async def create_course(
    body: CourseCreate,
    principal: Annotated[Principal, Depends(require_roles("admin"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    key: Annotated[UUID, Depends(get_idempotency_key)],
) -> JSONResponse:
    """Create course."""
    result = await service.create_course(session, principal, body, key)

    return JSONResponse(
        status_code=result.status,
        content=result.body,
        headers={"Location": result.location},
    )


@courses_router.get(
    "/{id}",
    response_model=CourseRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="get_course",
)
async def get_course(
    id: UUID,
    principal: Annotated[
        Principal, Depends(require_roles("admin", "professor", "student"))
    ],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CourseRead:
    """Get course."""
    course = await service.get_course(session, id, principal)

    return course


@courses_router.patch(
    "/{id}",
    response_model=CourseRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="update_course",
)
async def update_course(
    id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_roles("admin", "professor"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CourseRead:
    """Update course."""
    raw = await request.json()

    if principal.role == "professor" and isinstance(raw, dict) and "teacher_id" in raw:
        raise ApiError(403, "ACCESS_DENIED", "Only admin can change teacher_id")

    try:
        body = CoursePatch.model_validate(raw)
    except ValidationError as e:
        raise ApiError(422, "VALIDATION_ERROR", "Validation error") from e

    updated_course = await service.update_course(session, id, principal, body)

    return updated_course


@courses_router.delete(
    "/{id}",
    status_code=204,
    responses=documented_errors(401, 403, 404, 409, 422),
    operation_id="delete_course",
)
async def delete_course(
    id: UUID,
    principal: Annotated[Principal, Depends(require_roles("admin", "professor"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    """Delete course."""
    await service.delete_course(session, id, principal)

    return Response(status_code=204)
