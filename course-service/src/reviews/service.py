from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.idempotency import CreatedResponse, create_once
from src.core.models import Review
from src.reviews.schemas import ReviewCreate, ReviewPatch, ReviewRead
from src.submissions.service import require_submission_access
from src.web.dependencies import Principal
from src.web.errors import ApiError


async def get_review(
    session: AsyncSession,
    submission_id: UUID,
    principal: Principal,
) -> ReviewRead:
    await require_submission_access(session, submission_id, principal)

    review = await session.scalar(
        select(Review).where(Review.submission_id == submission_id)
    )
    if review is None:
        raise ApiError(404, "REVIEW_NOT_FOUND", "Review not found")

    return ReviewRead.model_validate(review)


async def create_review(
    session: AsyncSession,
    submission_id: UUID,
    principal: Principal,
    body: ReviewCreate,
    key: UUID,
) -> CreatedResponse:
    async def authorize(tx: AsyncSession) -> None:
        await require_submission_access(tx, submission_id, principal)

    async def create(tx: AsyncSession) -> CreatedResponse:
        await require_submission_access(
            tx,
            submission_id,
            principal,
            lock=True,
        )

        existing = await tx.scalar(
            select(Review).where(Review.submission_id == submission_id)
        )
        if existing is not None:
            raise ApiError(409, "REVIEW_ALREADY_EXISTS", "Review already exists")

        review = Review(
            submission_id=submission_id,
            type="manual",
            feedback=body.feedback,
            mark=Decimal(str(body.mark)),
            status=body.status,
        )
        tx.add(review)

        await tx.flush()
        await tx.refresh(review)

        return CreatedResponse(
            status=201,
            body=ReviewRead.model_validate(review).model_dump(mode="json"),
            location=f"/api/v1/submissions/{submission_id}/review",
        )

    result = await create_once(
        session,
        user_id=principal.id,
        method="POST",
        path=f"/api/v1/submissions/{submission_id}/review",
        key=key,
        payload=body.model_dump(mode="json"),
        authorize=authorize,
        create=create,
    )

    return result


async def update_review(
    session: AsyncSession,
    submission_id: UUID,
    principal: Principal,
    body: ReviewPatch,
) -> ReviewRead:
    async with session.begin():
        await require_submission_access(
            session,
            submission_id,
            principal,
            lock=True,
        )

        review = await session.scalar(
            select(Review)
            .where(Review.submission_id == submission_id)
            .with_for_update()
        )
        if review is None:
            raise ApiError(404, "REVIEW_NOT_FOUND", "Review not found")

        changes = body.model_dump(exclude_unset=True)
        if "mark" in changes:
            changes["mark"] = Decimal(str(changes["mark"]))

        for field, value in changes.items():
            setattr(review, field, value)

        review.type = "manual"

        await session.flush()
        await session.refresh(review)

        result = ReviewRead.model_validate(review)

    return result
