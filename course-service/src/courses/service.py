from uuid import UUID

from sqlalchemy import and_, delete, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.core.idempotency import CreatedResponse, create_once
from src.core.models import Assignment, Course, Enrollment, User
from src.courses.schemas import (
    CourseCreate,
    CoursePage,
    CoursePatch,
    CourseRead,
    CourseStatus,
)
from src.web.dependencies import Principal
from src.web.errors import ApiError
from src.web.pagination import Pagination, SortOrder
from src.core.roles import Role


def visible_course_condition(principal: Principal) -> ColumnElement[bool]:
    if principal.role == Role.ADMIN:
        return true()
    if principal.role == Role.PROFESSOR:
        return Course.teacher_id == principal.id
    enrolled = select(Enrollment.course_id).where(
        Enrollment.course_id == Course.id,
        Enrollment.student_id == principal.id,
    )
    return and_(Course.status != "DRAFT", enrolled.exists())


async def require_course_access(
    session: AsyncSession,
    course_id: UUID,
    principal: Principal,
    *,
    lock: bool = False,
) -> Course:
    course = await session.get(Course, course_id, with_for_update=lock)
    if course is None:
        raise ApiError(404, "COURSE_NOT_FOUND", "Course not found")
    if principal.role == Role.ADMIN:
        return course
    if principal.role == Role.PROFESSOR and course.teacher_id == principal.id:
        return course
    if principal.role == Role.STUDENT and course.status != "DRAFT":
        enrollment = await session.get(Enrollment, (principal.id, course_id))
        if enrollment is not None:
            return course
    raise ApiError(403, "ACCESS_DENIED", "No access to this course")


async def require_teacher(session: AsyncSession, teacher_id: UUID) -> None:
    teacher = await session.get(User, teacher_id)
    if teacher is None:
        raise ApiError(404, "TEACHER_NOT_FOUND", "Teacher not found")
    if teacher.role != Role.PROFESSOR:
        raise ApiError(422, "TEACHER_ROLE_REQUIRED", "Teacher must be a professor")


async def list_courses(
    session: AsyncSession,
    principal: Principal,
    *,
    offset: int,
    limit: int,
    sort: SortOrder,
    status: CourseStatus | None,
    teacher_id: UUID | None,
) -> CoursePage:
    condition = visible_course_condition(principal)
    if status is not None:
        condition = and_(condition, Course.status == status)
    if teacher_id is not None:
        condition = and_(condition, Course.teacher_id == teacher_id)

    total = await session.scalar(
        select(func.count()).select_from(Course).where(condition)
    )
    created_order = (
        Course.created_at.asc() if sort.endswith(":asc") else Course.created_at.desc()
    )
    courses = await session.scalars(
        select(Course)
        .where(condition)
        .order_by(created_order, Course.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return CoursePage(
        data=[CourseRead.model_validate(course) for course in courses],
        pagination=Pagination(offset=offset, limit=limit, total=total or 0),
    )


async def get_course(
    session: AsyncSession, course_id: UUID, principal: Principal
) -> CourseRead:
    course = await require_course_access(session, course_id, principal)
    return CourseRead.model_validate(course)


async def create_course(
    session: AsyncSession,
    principal: Principal,
    body: CourseCreate,
    key: UUID,
) -> CreatedResponse:

    async def authorize(_session: AsyncSession) -> None:
        return None

    async def create(tx: AsyncSession) -> CreatedResponse:
        await require_teacher(tx, body.teacher_id)
        course = Course(**body.model_dump())
        tx.add(course)
        await tx.flush()
        await tx.refresh(course)
        return CreatedResponse(
            status=201,
            body=CourseRead.model_validate(course).model_dump(mode="json"),
            location=f"/api/v1/courses/{course.id}",
        )

    return await create_once(
        session,
        user_id=principal.id,
        method="POST",
        path="/api/v1/courses",
        key=key,
        payload=body.model_dump(mode="json"),
        authorize=authorize,
        create=create,
    )


async def update_course(
    session: AsyncSession,
    course_id: UUID,
    principal: Principal,
    body: CoursePatch,
) -> CourseRead:
    changes = body.model_dump(exclude_unset=True)
    if principal.role == Role.PROFESSOR and "teacher_id" in changes:
        raise ApiError(403, "ACCESS_DENIED", "Only admin can change teacher_id")

    async with session.begin():
        course = await require_course_access(session, course_id, principal, lock=True)
        if "teacher_id" in changes:
            await require_teacher(session, changes["teacher_id"])
        for name, value in changes.items():
            setattr(course, name, value)
        await session.flush()
        await session.refresh(course)
        result = CourseRead.model_validate(course)
    return result


async def delete_course(
    session: AsyncSession, course_id: UUID, principal: Principal
) -> None:
    async with session.begin():
        course = await require_course_access(session, course_id, principal, lock=True)

        assignment_id = await session.scalar(
            select(Assignment.id).where(Assignment.course_id == course_id).limit(1)
        )
        if assignment_id is not None:
            raise ApiError(409, "COURSE_HAS_ASSIGNMENTS", "Course has assignments")

        await session.execute(
            delete(Enrollment).where(Enrollment.course_id == course_id)
        )

        await session.delete(course)
