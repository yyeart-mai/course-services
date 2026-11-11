"""Provide assignment routes."""

# FastAPI signatures and path parameter names follow the public API contract.
# pylint: disable=too-many-arguments,too-many-positional-arguments,redefined-builtin

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.assignments import service
from src.assignments.schemas import (
    AssignmentCreate,
    AssignmentPage,
    AssignmentPatch,
    AssignmentRead,
)
from src.web.dependencies import Principal, get_db_session, require_roles
from src.web.errors import documented_errors
from src.web.idempotency import IDEMPOTENCY_HEADER, get_idempotency_key
from src.web.pagination import SortOrder

assignments_router = APIRouter(prefix="/api/v1/assignments", tags=["Assignments"])


@assignments_router.get(
    "",
    response_model=AssignmentPage,
    responses=documented_errors(401, 422),
    operation_id="list_assignments",
)
async def list_assignments(
    principal: Annotated[
        Principal, Depends(require_roles("admin", "professor", "student"))
    ],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    sort: SortOrder = "created_at:desc",
    course_id: UUID | None = None,
) -> AssignmentPage:
    """List assignments."""
    assignments = await service.list_assignments(
        session,
        principal,
        offset=offset,
        limit=limit,
        sort=sort,
        course_id=course_id,
    )

    return assignments


@assignments_router.post(
    "",
    response_model=AssignmentRead,
    status_code=201,
    responses=documented_errors(401, 403, 404, 409, 422),
    openapi_extra=IDEMPOTENCY_HEADER,
    operation_id="create_assignment",
)
async def create_assignment(
    body: AssignmentCreate,
    principal: Annotated[Principal, Depends(require_roles("admin", "professor"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    key: Annotated[UUID, Depends(get_idempotency_key)],
) -> JSONResponse:
    """Create assignment."""
    result = await service.create_assignment(session, principal, body, key)

    return JSONResponse(
        status_code=result.status,
        content=result.body,
        headers={"Location": result.location},
    )


@assignments_router.get(
    "/{id}",
    response_model=AssignmentRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="get_assignment",
)
async def get_assignment(
    id: UUID,
    principal: Annotated[
        Principal, Depends(require_roles("admin", "professor", "student"))
    ],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AssignmentRead:
    """Get assignment."""
    assignment = await service.get_assignment(session, id, principal)

    return assignment


@assignments_router.patch(
    "/{id}",
    response_model=AssignmentRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="update_assignment",
)
async def update_assignment(
    id: UUID,
    body: AssignmentPatch,
    principal: Annotated[Principal, Depends(require_roles("admin", "professor"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AssignmentRead:
    """Update assignment."""
    updated_assignment = await service.update_assignment(session, id, principal, body)

    return updated_assignment


@assignments_router.delete(
    "/{id}",
    status_code=204,
    responses=documented_errors(401, 403, 404, 409, 422),
    operation_id="delete_assignment",
)
async def delete_assignment(
    id: UUID,
    principal: Annotated[Principal, Depends(require_roles("admin", "professor"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    """Delete assignment."""
    await service.delete_assignment(session, id, principal)

    return Response(status_code=204)
