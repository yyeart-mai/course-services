from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.reviews import service as reviews_service
from src.reviews.schemas import ReviewCreate, ReviewPatch, ReviewRead
from src.submissions import service
from src.submissions.schemas import ArtifactRead, SubmissionCreate, SubmissionRead
from src.web.dependencies import Principal, get_db_session, require_roles
from src.web.errors import documented_errors
from src.web.idempotency import IDEMPOTENCY_HEADER, get_idempotency_key

submissions_router = APIRouter(
    prefix="/api/v1/submissions",
)


@submissions_router.post(
    "",
    response_model=SubmissionRead,
    status_code=201,
    responses=documented_errors(401, 403, 404, 409, 422),
    openapi_extra=IDEMPOTENCY_HEADER,
    operation_id="create_submission",
    tags=["Submissions"],
)
async def create_submission(
    body: SubmissionCreate,
    principal: Annotated[Principal, Depends(require_roles("student"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    key: Annotated[UUID, Depends(get_idempotency_key)],
) -> JSONResponse:
    result = await service.create_submission(session, principal, body, key)

    return JSONResponse(
        status_code=result.status,
        content=result.body,
        headers={"Location": result.location},
    )


@submissions_router.get(
    "/{id}",
    response_model=SubmissionRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="get_submission",
    tags=["Submissions"],
)
async def get_submission(
    id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_roles("professor", "student"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SubmissionRead:
    artifacts_url = str(request.url_for("get_artifacts", id=id))
    submission = await service.get_submission(session, id, principal, artifacts_url)

    return submission


@submissions_router.get(
    "/{id}/artifacts",
    operation_id="get_artifacts",
    tags=["Artifacts"],
    response_model=list[ArtifactRead],
    responses=documented_errors(401, 403, 404, 422),
)
async def get_artifacts(
    id: UUID,
    principal: Annotated[Principal, Depends(require_roles("professor", "student"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ArtifactRead]:
    artifacts = await service.get_artifacts(session, id, principal)

    return artifacts


@submissions_router.get(
    "/{id}/review",
    response_model=ReviewRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="get_review",
    tags=["Reviews"],
)
async def get_review(
    id: UUID,
    principal: Annotated[Principal, Depends(require_roles("professor", "student"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReviewRead:
    review = await reviews_service.get_review(session, id, principal)

    return review


@submissions_router.post(
    "/{id}/review",
    response_model=ReviewRead,
    status_code=201,
    responses=documented_errors(401, 403, 404, 409, 422),
    openapi_extra=IDEMPOTENCY_HEADER,
    operation_id="create_review",
    tags=["Reviews"],
)
async def create_review(
    id: UUID,
    principal: Annotated[Principal, Depends(require_roles("professor"))],
    body: ReviewCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    key: Annotated[UUID, Depends(get_idempotency_key)],
) -> JSONResponse:
    result = await reviews_service.create_review(session, id, principal, body, key)

    return JSONResponse(
        status_code=result.status,
        content=result.body,
        headers={"Location": result.location},
    )


@submissions_router.patch(
    "/{id}/review",
    response_model=ReviewRead,
    responses=documented_errors(401, 403, 404, 422),
    operation_id="update_review",
    tags=["Reviews"],
)
async def update_review(
    id: UUID,
    principal: Annotated[Principal, Depends(require_roles("professor"))],
    body: ReviewPatch,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReviewRead:
    result = await reviews_service.update_review(session, id, principal, body)

    return result
