"""Integration checks for the isolated PostgreSQL test service."""

import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from scripts.seed_demo import (
    ADMIN_ID,
    COURSE_ID,
    PROFESSOR_ID,
    STUDENT_ID,
    seed_demo_data,
)
from src.checker.queue import claim_next_job, recover_expired_jobs
from src.checker.worker import process_job
from src.core.config import get_settings
from src.core.database import create_database
from src.core.models import (
    Assignment,
    Base,
    CheckJob,
    Course,
    Enrollment,
    Submission,
    User,
)

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


@pytest.mark.asyncio
async def test_expired_jobs_recover_and_retry(test_database_url: str) -> None:
    engine, _ = create_database(test_database_url)
    settings = get_settings().model_copy(update={"checker_max_attempts": 3})
    schema = f"checker_recovery_{uuid4().hex}"
    professor_id, student_id = uuid4(), uuid4()
    course_id, assignment_id = uuid4(), uuid4()
    retry_submission_id, failed_submission_id = uuid4(), uuid4()
    retry_job_id, failed_job_id = uuid4(), uuid4()
    expired_lease = datetime.now(timezone.utc) - timedelta(minutes=1)

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await connection.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(
                    connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )

                async with sessions.begin() as session:
                    session.add_all(
                        [
                            User(id=professor_id, role="professor"),
                            User(id=student_id, role="student"),
                        ]
                    )
                    await session.flush()
                    session.add(
                        Course(
                            id=course_id,
                            teacher_id=professor_id,
                            title="Recovery test",
                            description="Recovery test",
                            status="ACTIVE",
                        )
                    )
                    await session.flush()
                    session.add(
                        Assignment(
                            id=assignment_id,
                            course_id=course_id,
                            title="Recovery test",
                            description="Recovery test",
                            date_start=date.today(),
                            date_end=date.today(),
                        )
                    )
                    await session.flush()
                    session.add_all(
                        [
                            Submission(
                                id=retry_submission_id,
                                assignment_id=assignment_id,
                                student_id=student_id,
                                repository_url="https://example.com/retry",
                                status="PROCESSING",
                            ),
                            Submission(
                                id=failed_submission_id,
                                assignment_id=assignment_id,
                                student_id=student_id,
                                repository_url="https://example.com/failed",
                                status="PROCESSING",
                            ),
                        ]
                    )
                    await session.flush()
                    session.add_all(
                        [
                            CheckJob(
                                id=retry_job_id,
                                submission_id=retry_submission_id,
                                status="RUNNING",
                                attempts=settings.checker_max_attempts - 1,
                                leased_until=expired_lease,
                            ),
                            CheckJob(
                                id=failed_job_id,
                                submission_id=failed_submission_id,
                                status="RUNNING",
                                attempts=settings.checker_max_attempts,
                                leased_until=expired_lease,
                            ),
                        ]
                    )

                assert (
                    await recover_expired_jobs(sessions, settings.checker_max_attempts)
                    == 2
                )

                async with sessions() as session:
                    retry_job = await session.get(CheckJob, retry_job_id)
                    failed_job = await session.get(CheckJob, failed_job_id)
                    retry_submission = await session.get(
                        Submission, retry_submission_id
                    )
                    failed_submission = await session.get(
                        Submission, failed_submission_id
                    )
                    assert retry_job is not None and retry_submission is not None
                    assert failed_job is not None and failed_submission is not None
                    assert (retry_job.status, retry_submission.status) == (
                        "PENDING",
                        "SUBMITTED",
                    )
                    assert (failed_job.status, failed_submission.status) == (
                        "FAILED",
                        "FAILED",
                    )
                    assert retry_job.leased_until is None
                    assert failed_job.leased_until is None
                    assert (
                        retry_job.last_error == failed_job.last_error == "LEASE_EXPIRED"
                    )

                claimed = await claim_next_job(sessions, settings.worker_lease_seconds)
                assert claimed is not None
                assert claimed[:3] == (
                    retry_job_id,
                    settings.checker_max_attempts,
                    retry_submission_id,
                )

                def checker_response(request: httpx.Request) -> httpx.Response:
                    assert request.method == "POST"
                    return httpx.Response(
                        200,
                        json={
                            "submission_id": str(retry_submission_id),
                            "status": "COMPLETED",
                        },
                    )

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(checker_response)
                ) as client:
                    await process_job(claimed, sessions, client, settings)

                async with sessions() as session:
                    retry_job = await session.get(CheckJob, retry_job_id)
                    retry_submission = await session.get(
                        Submission, retry_submission_id
                    )
                    failed_job = await session.get(CheckJob, failed_job_id)
                    assert retry_job is not None and retry_submission is not None
                    assert failed_job is not None
                    assert retry_job.status == "DONE"
                    assert retry_submission.status == "COMPLETED"
                    assert retry_job.attempts == settings.checker_max_attempts
                    assert failed_job.status == "FAILED"
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
