"""Load repeatable local users, a course, and an enrollment."""

import asyncio
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.database import create_database
from src.core.models import Course, Enrollment, User


def demo_id(name: str) -> UUID:
    """Derive a stable UUID for a demonstration record."""
    return uuid5(NAMESPACE_URL, f"course-service-demo:{name}")


ADMIN_ID = demo_id("admin")
PROFESSOR_ID = demo_id("professor")
STUDENT_ID = demo_id("student")
COURSE_ID = demo_id("course")


async def seed_demo_data(session: AsyncSession) -> None:
    """Create demonstration records that are not already present."""
    for user_id, role in (
        (ADMIN_ID, "admin"),
        (PROFESSOR_ID, "professor"),
        (STUDENT_ID, "student"),
    ):
        user = await session.get(User, user_id)
        if user is None:
            session.add(User(id=user_id, role=role))
        elif user.role != role:
            raise RuntimeError(f"User {user_id} has unexpected role")

    await session.flush()

    course = await session.get(Course, COURSE_ID)
    if course is None:
        session.add(
            Course(
                id=COURSE_ID,
                title="Демонстрационный курс Python",
                description=(
                    "Локальный курс для проверки ролей, записи "
                    "студента и маршрутов API."
                ),
                teacher_id=PROFESSOR_ID,
                status="ACTIVE",
            )
        )
    elif course.teacher_id != PROFESSOR_ID:
        raise RuntimeError("Demo course has unexpected professor")

    await session.flush()

    enrollment = await session.get(Enrollment, (STUDENT_ID, COURSE_ID))
    if enrollment is None:
        session.add(Enrollment(student_id=STUDENT_ID, course_id=COURSE_ID))


async def main() -> None:
    """Load demonstration records into a local database."""
    settings = get_settings()
    url = make_url(settings.database_url)

    if url.host not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Seed is allowed only for a local database")

    engine, sessions = create_database(settings.database_url)
    try:
        async with sessions.begin() as session:
            await seed_demo_data(session)
    finally:
        await engine.dispose()

    print(f"admin:     {ADMIN_ID}")
    print(f"professor: {PROFESSOR_ID}")
    print(f"student:   {STUDENT_ID}")
    print(f"course:    {COURSE_ID}")


if __name__ == "__main__":
    asyncio.run(main())
