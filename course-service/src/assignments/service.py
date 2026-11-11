"""Provide assignment service operations."""

# Pagination and filter fields match the public route parameters.
# SQLAlchemy builds func.count dynamically.
# pylint: disable=too-many-arguments,not-callable

from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.assignments.schemas import (
    AssignmentCreate,
    AssignmentPage,
    AssignmentPatch,
    AssignmentRead,
)
from src.core.idempotency import CreatedResponse, create_once
from src.core.models import Assignment, Course, Submission
from src.courses.service import require_course_access, visible_course_condition
from src.web.dependencies import Principal
from src.web.errors import ApiError
from src.web.pagination import Pagination, SortOrder


async def require_assignment_access(
    session: AsyncSession,
    assignment_id: UUID,
    principal: Principal,
    *,
    lock: bool = False,
) -> Assignment:
    """Load an assignment and enforce object access."""
    assignment = await session.get(Assignment, assignment_id, with_for_update=lock)
    if assignment is None:
        raise ApiError(404, "ASSIGNMENT_NOT_FOUND", "Assignment not found")

    await require_course_access(session, assignment.course_id, principal)

    return assignment


async def list_assignments(
    session: AsyncSession,
    principal: Principal,
    *,
    offset: int,
    limit: int,
    sort: SortOrder,
    course_id: UUID | None,
) -> AssignmentPage:
    """List assignments."""
    condition = visible_course_condition(principal)
    if course_id is not None:
        condition = and_(condition, Assignment.course_id == course_id)

    total = await session.scalar(
        select(func.count())
        .select_from(Assignment)
        .join(Course, Assignment.course_id == Course.id)
        .where(condition)
    )

    created_order = (
        Assignment.created_at.asc()
        if sort.endswith(":asc")
        else Assignment.created_at.desc()
    )

    assignments = await session.scalars(
        select(Assignment)
        .join(Course, Assignment.course_id == Course.id)
        .where(condition)
        .order_by(created_order, Assignment.id.asc())
        .offset(offset)
        .limit(limit)
    )

    return AssignmentPage(
        data=[AssignmentRead.model_validate(item) for item in assignments],
        pagination=Pagination(offset=offset, limit=limit, total=total or 0),
    )


async def get_assignment(
    session: AsyncSession, assignment_id: UUID, principal: Principal
) -> AssignmentRead:
    """Get assignment."""
    assignment = await require_assignment_access(session, assignment_id, principal)

    return AssignmentRead.model_validate(assignment)


async def create_assignment(
    session: AsyncSession,
    principal: Principal,
    body: AssignmentCreate,
    key: UUID,
) -> CreatedResponse:
    """Create assignment."""

    async def authorize(tx: AsyncSession) -> None:
        await require_course_access(tx, body.course_id, principal, lock=True)

    async def create(tx: AsyncSession) -> CreatedResponse:
        values = body.model_dump(exclude={"materials_urls"})

        assignment = Assignment(
            **values,
            materials_urls=[str(url) for url in body.materials_urls],
        )

        tx.add(assignment)
        await tx.flush()
        await tx.refresh(assignment)

        return CreatedResponse(
            status=201,
            body=AssignmentRead.model_validate(assignment).model_dump(mode="json"),
            location=f"/api/v1/assignments/{assignment.id}",
        )

    result = await create_once(
        session,
        user_id=principal.id,
        method="POST",
        path="/api/v1/assignments",
        key=key,
        payload=body.model_dump(mode="json"),
        authorize=authorize,
        create=create,
    )

    return result


async def update_assignment(
    session: AsyncSession,
    assignment_id: UUID,
    principal: Principal,
    body: AssignmentPatch,
) -> AssignmentRead:
    """Update assignment."""
    changes = body.model_dump(exclude_unset=True)
    if "materials_urls" in changes:
        changes["materials_urls"] = [str(url) for url in body.materials_urls or []]

    async with session.begin():
        assignment = await require_assignment_access(
            session, assignment_id, principal, lock=True
        )

        date_start = changes.get("date_start", assignment.date_start)
        date_end = changes.get("date_end", assignment.date_end)
        if date_start > date_end:
            raise ApiError(422, "VALIDATION_ERROR", "date_start exceeds date_end")

        for name, value in changes.items():
            setattr(assignment, name, value)

        await session.flush()
        await session.refresh(assignment)

        result = AssignmentRead.model_validate(assignment)

    return result


async def delete_assignment(
    session: AsyncSession, assignment_id: UUID, principal: Principal
) -> None:
    """Delete assignment."""
    async with session.begin():
        assignment = await require_assignment_access(
            session, assignment_id, principal, lock=True
        )

        submission_id = await session.scalar(
            select(Submission.id)
            .where(Submission.assignment_id == assignment_id)
            .limit(1)
        )
        if submission_id is not None:
            raise ApiError(
                409, "ASSIGNMENT_HAS_SUBMISSIONS", "Assignment has submissions"
            )

        await session.delete(assignment)
