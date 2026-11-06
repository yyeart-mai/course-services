"""Integration checks for the isolated PostgreSQL test service."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError

from scripts.seed_demo import (
    ADMIN_ID,
    COURSE_ID,
    PROFESSOR_ID,
    STUDENT_ID,
    seed_demo_data,
)
from src.core.config import get_settings
from src.core.database import create_database
from src.core.models import Course, Enrollment, User

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TABLES = {
    "users",
    "enrollments",
    "courses",
    "assignments",
    "submissions",
    "reviews",
    "artifacts",
    "idempotency_records",
    "check_jobs",
}


@pytest.fixture(scope="module")
def test_database_url() -> str:
    if os.environ.get("RUN_DB_TESTS") != "1":
        pytest.skip("Set RUN_DB_TESTS=1 and start the postgres-test Compose service")

    development_url = make_url(get_settings().database_url)
    if development_url.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Database tests require a local PostgreSQL service")
    if development_url.port != 5432:
        pytest.fail("Expected the development database on port 5432")

    test_url = development_url.set(port=5433).render_as_string(hide_password=False)
    environment = {**os.environ, "DATABASE_URL": test_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return test_url


@pytest.mark.asyncio
async def test_migration_creates_tables_and_constraints(test_database_url: str) -> None:
    engine, _ = create_database(test_database_url)
    try:
        async with engine.connect() as connection:

            def inspect_schema(sync_connection: Connection) -> dict[str, object]:
                inspector = inspect(sync_connection)
                return {
                    "tables": set(inspector.get_table_names()),
                    "course_columns": {
                        column["name"] for column in inspector.get_columns("courses")
                    },
                    "enrollment_pk": set(
                        inspector.get_pk_constraint("enrollments")[
                            "constrained_columns"
                        ]
                    ),
                    "review_unique": inspector.get_unique_constraints("reviews"),
                    "job_unique": inspector.get_unique_constraints("check_jobs"),
                    "idempotency_unique": inspector.get_unique_constraints(
                        "idempotency_records"
                    ),
                    "assignment_checks": inspector.get_check_constraints("assignments"),
                    "foreign_keys": {
                        table: inspector.get_foreign_keys(table)
                        for table in REQUIRED_TABLES - {"users"}
                    },
                }

            actual = await connection.run_sync(inspect_schema)

        assert REQUIRED_TABLES <= actual["tables"]
        assert {"created_at", "updated_at"} <= actual["course_columns"]
        assert actual["enrollment_pk"] == {"student_id", "course_id"}
        assert any(
            item["column_names"] == ["submission_id"]
            for item in actual["review_unique"]
        )
        assert any(
            item["column_names"] == ["submission_id"] for item in actual["job_unique"]
        )
        assert any(
            item["name"] == "uq_idempotency_scope"
            and item["column_names"] == ["user_id", "method", "path", "key"]
            for item in actual["idempotency_unique"]
        )
        assert {"ck_assignments_dates", "ck_assignments_attempts_limit"} <= {
            item["name"] for item in actual["assignment_checks"]
        }
        assert all(actual["foreign_keys"][table] for table in actual["foreign_keys"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_can_run_twice_without_duplicates(test_database_url: str) -> None:
    engine, sessions = create_database(test_database_url)
    try:
        async with sessions() as session:
            try:
                await seed_demo_data(session)
                await session.flush()
                await seed_demo_data(session)
                await session.flush()

                users = await session.scalars(
                    select(User).where(
                        User.id.in_([ADMIN_ID, PROFESSOR_ID, STUDENT_ID])
                    )
                )
                user_list = list(users)
                assert len(user_list) == 3
                assert {user.id: user.role for user in user_list} == {
                    ADMIN_ID: "admin",
                    PROFESSOR_ID: "professor",
                    STUDENT_ID: "student",
                }
                assert await session.get(Course, COURSE_ID) is not None
                enrollment = await session.get(Enrollment, (STUDENT_ID, COURSE_ID))
                assert enrollment is not None
            finally:
                await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sessions_isolate_uncommitted_data(test_database_url: str) -> None:
    engine, sessions = create_database(test_database_url)
    user_id = uuid4()
    try:
        async with sessions() as writer, sessions() as reader:
            try:
                writer.add(User(id=user_id, role="student"))
                await writer.flush()
                assert await reader.get(User, user_id) is None
            finally:
                await reader.rollback()
                await writer.rollback()

        async with sessions() as reader:
            assert await reader.get(User, user_id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_rejects_invalid_role_and_missing_relations(
    test_database_url: str,
) -> None:
    engine, sessions = create_database(test_database_url)
    try:
        async with sessions() as session:
            try:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        session.add(User(id=uuid4(), role="unknown"))
                        await session.flush()

                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        session.add(Enrollment(student_id=uuid4(), course_id=uuid4()))
                        await session.flush()
            finally:
                await session.rollback()
    finally:
        await engine.dispose()
