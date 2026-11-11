from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.assignments.service import require_assignment_access
from src.core.idempotency import CreatedResponse, create_once
from src.core.models import Artifact, CheckJob, Review, Submission
from src.courses.service import require_course_access
from src.submissions.schemas import ArtifactRead, SubmissionCreate, SubmissionRead
from src.web.dependencies import Principal
from src.web.errors import ApiError


async def require_submission_access(
    session: AsyncSession,
    submission_id: UUID,
    principal: Principal,
    *,
    lock: bool = False,
) -> Submission:
    if principal.role == "admin":
        raise ApiError(403, "ACCESS_DENIED", "No access to this submission")

    submission = await session.get(Submission, submission_id, with_for_update=lock)
    if submission is None:
        raise ApiError(404, "SUBMISSION_NOT_FOUND", "Submission not found")

    if principal.role == "student":
        if principal.id != submission.student_id:
            raise ApiError(403, "ACCESS_DENIED", "No access to this submission")
    else:
        await require_assignment_access(session, submission.assignment_id, principal)

    return submission


async def create_submission(
    session: AsyncSession,
    principal: Principal,
    body: SubmissionCreate,
    key: UUID,
) -> CreatedResponse:
    async def authorize(tx: AsyncSession) -> None:
        await require_assignment_access(tx, body.assignment_id, principal)

    async def create(tx: AsyncSession) -> CreatedResponse:
        assignment = await require_assignment_access(
            tx,
            body.assignment_id,
            principal,
            lock=True,
        )
        course = await require_course_access(
            tx,
            assignment.course_id,
            principal,
            lock=True,
        )

        if course.status != "ACTIVE":
            raise ApiError(409, "COURSE_NOT_ACTIVE", "Course is not active")

        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        if today < assignment.date_start:
            raise ApiError(409, "SUBMISSION_NOT_STARTED", "Submission has not started")
        if today > assignment.date_end:
            raise ApiError(
                409,
                "SUBMISSION_DEADLINE_PASSED",
                "Submission deadline passed",
            )

        used = await tx.scalar(
            select(func.count())
            .select_from(Submission)
            .where(
                Submission.assignment_id == assignment.id,
                Submission.student_id == principal.id,
            )
        )
        if (used or 0) >= assignment.attempts_limit:
            raise ApiError(
                409,
                "SUBMISSION_LIMIT_REACHED",
                "Submission limit reached",
            )

        submission = Submission(
            assignment_id=assignment.id,
            student_id=principal.id,
            repository_url=str(body.repository_url),
            status="SUBMITTED",
        )
        tx.add(submission)
        await tx.flush()

        check_job = CheckJob(
            submission_id=submission.id,
            status="PENDING",
        )
        tx.add(check_job)
        await tx.flush()

        await tx.refresh(submission)

        response = SubmissionRead(
            id=submission.id,
            assignment_id=submission.assignment_id,
            student_id=submission.student_id,
            submitted_at=submission.submitted_at,
            review_id=None,
            repository_url=body.repository_url,
            status=submission.status,
            artifacts_url=None,
            created_at=submission.created_at,
            updated_at=submission.updated_at,
        ).model_dump(mode="json")

        return CreatedResponse(
            status=201,
            body=response,
            location=f"/api/v1/submissions/{submission.id}",
        )

    result = await create_once(
        session,
        user_id=principal.id,
        method="POST",
        path="/api/v1/submissions",
        key=key,
        payload=body.model_dump(mode="json"),
        authorize=authorize,
        create=create,
    )

    return result


async def get_submission(
    session: AsyncSession,
    submission_id: UUID,
    principal: Principal,
    artifacts_url: str,
) -> SubmissionRead:
    submission = await require_submission_access(session, submission_id, principal)

    review_id = await session.scalar(
        select(Review.id).where(Review.submission_id == submission.id)
    )
    artifact_id = await session.scalar(
        select(Artifact.id).where(Artifact.submission_id == submission.id).limit(1)
    )

    result = SubmissionRead(
        id=submission.id,
        assignment_id=submission.assignment_id,
        student_id=submission.student_id,
        submitted_at=submission.submitted_at,
        review_id=review_id,
        repository_url=submission.repository_url,
        status=submission.status,
        artifacts_url=artifacts_url if artifact_id is not None else None,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )

    return result


async def get_artifacts(
    session: AsyncSession,
    submission_id: UUID,
    principal: Principal,
) -> list[ArtifactRead]:
    await require_submission_access(session, submission_id, principal)

    rows = (
        await session.scalars(
            select(Artifact).where(Artifact.submission_id == submission_id)
        )
    ).all()

    return [ArtifactRead.model_validate(row) for row in rows]
