import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import jwt
import pytest
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from mock_checker import app as mock_checker_app
from src.assignments.router import assignments_router
from src.checker import worker as checker_worker
from src.checker.client import (
    ArtifactResult,
    AutoReviewResult,
    CheckResult,
    call_checker,
)
from src.checker.queue import (
    claim_next_job,
    finish_failure,
    finish_success,
    recover_expired_jobs,
)
from src.checker.worker import process_job
from src.core.config import get_settings
from src.core.database import create_database
from src.core.models import CheckJob, Enrollment, Submission, User
from src.courses.router import courses_router
from src.submissions.router import submissions_router
from src.web.app import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def test_database_url() -> str:
    if os.environ.get("RUN_DB_TESTS") != "1":
        pytest.skip("Set RUN_DB_TESTS=1 and start the postgres-test Compose service")

    development_url = make_url(get_settings().database_url)
    if development_url.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Database tests require a local PostgreSQL service")
    test_url = development_url.set(port=5433).render_as_string(hide_password=False)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": test_url},
        check=True,
        capture_output=True,
        text=True,
    )
    return test_url


@pytest.fixture
def api(test_database_url: str) -> Iterator[tuple[TestClient, dict[str, UUID]]]:
    ids = {
        name: uuid4()
        for name in (
            "admin",
            "professor",
            "other_professor",
            "student",
            "other_student",
        )
    }

    async def prepare() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE TABLE users CASCADE"))
            async with sessions.begin() as session:
                for name, user_id in ids.items():
                    role = name.removeprefix("other_")
                    session.add(User(id=user_id, role=role))
        finally:
            await engine.dispose()

    asyncio.run(prepare())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, sessions = create_database(test_database_url)
        app.state.session_factory = sessions
        try:
            yield
        finally:
            await engine.dispose()

    app = create_app(
        get_settings(),
        (courses_router, assignments_router, submissions_router),
        lifespan=lifespan,
    )
    with TestClient(app) as client:
        yield client, ids


def auth(user_id: UUID, role: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def create_course_body(teacher_id: UUID) -> dict[str, str]:
    return {
        "title": "Backend-разработка на Python",
        "description": "Учебный курс по проектированию HTTP API и разработке сервисов на Python.",
        "teacher_id": str(teacher_id),
    }


def create_assignment_body(course_id: UUID) -> dict[str, Any]:
    return {
        "title": "Лабораторная работа",
        "description": "Реализовать HTTP API",
        "course_id": str(course_id),
        "date_start": date.today().isoformat(),
        "date_end": date.today().isoformat(),
    }


def post_headers(user_id: UUID, role: str) -> dict[str, str]:
    return {**auth(user_id, role), "Idempotency-Key": str(uuid4())}


def prepare_submission(
    client: TestClient,
    ids: dict[str, UUID],
    database_url: str,
) -> tuple[UUID, UUID]:
    course = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["professor"]),
        headers=post_headers(ids["admin"], "admin"),
    )
    assert course.status_code == 201
    course_id = UUID(course.json()["id"])

    active = client.patch(
        f"/api/v1/courses/{course_id}",
        json={"status": "ACTIVE"},
        headers=auth(ids["admin"], "admin"),
    )
    assert active.status_code == 200

    today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    assignment = client.post(
        "/api/v1/assignments",
        json={
            **create_assignment_body(course_id),
            "date_start": today,
            "date_end": today,
        },
        headers=post_headers(ids["professor"], "professor"),
    )
    assert assignment.status_code == 201

    async def enroll() -> None:
        engine, sessions = create_database(database_url)
        try:
            async with sessions.begin() as session:
                session.add(Enrollment(student_id=ids["student"], course_id=course_id))
        finally:
            await engine.dispose()

    asyncio.run(enroll())
    return course_id, UUID(assignment.json()["id"])


def test_course_and_assignment_crud(api: tuple[TestClient, dict[str, UUID]]) -> None:
    client, ids = api
    course_response = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["professor"]),
        headers=post_headers(ids["admin"], "admin"),
    )
    assert course_response.status_code == 201
    course_id = course_response.json()["id"]
    assert course_response.headers["location"] == f"/api/v1/courses/{course_id}"
    assert course_response.json()["status"] == "DRAFT"

    assert (
        client.get(
            f"/api/v1/courses/{course_id}", headers=auth(ids["professor"], "professor")
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/courses", headers=auth(ids["professor"], "professor")
        ).json()["pagination"]["total"]
        == 1
    )

    updated_course = client.patch(
        f"/api/v1/courses/{course_id}",
        json={"status": "ACTIVE"},
        headers=auth(ids["professor"], "professor"),
    )
    assert updated_course.status_code == 200
    assert updated_course.json()["status"] == "ACTIVE"

    assignment_response = client.post(
        "/api/v1/assignments",
        json=create_assignment_body(UUID(course_id)),
        headers=post_headers(ids["professor"], "professor"),
    )
    assert assignment_response.status_code == 201
    assignment_id = assignment_response.json()["id"]
    assert assignment_response.headers["location"] == (
        f"/api/v1/assignments/{assignment_id}"
    )
    assert assignment_response.json()["attempts_limit"] == 3

    assert (
        client.get(
            f"/api/v1/assignments/{assignment_id}",
            headers=auth(ids["admin"], "admin"),
        ).status_code
        == 200
    )
    assignment_page = client.get(
        "/api/v1/assignments",
        params={"course_id": course_id},
        headers=auth(ids["admin"], "admin"),
    ).json()
    assert assignment_page["pagination"]["total"] == 1
    assert assignment_page["data"][0]["id"] == assignment_id

    updated_assignment = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        json={"attempts_limit": 5, "materials_urls": []},
        headers=auth(ids["professor"], "professor"),
    )
    assert updated_assignment.status_code == 200
    assert updated_assignment.json()["attempts_limit"] == 5

    assert (
        client.delete(
            f"/api/v1/courses/{course_id}", headers=auth(ids["admin"], "admin")
        ).json()["error"]["code"]
        == "COURSE_HAS_ASSIGNMENTS"
    )
    deleted_assignment = client.delete(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(ids["professor"], "professor"),
    )
    assert deleted_assignment.status_code == 204
    assert deleted_assignment.content == b""
    assert (
        client.delete(
            f"/api/v1/courses/{course_id}", headers=auth(ids["admin"], "admin")
        ).status_code
        == 204
    )


def test_access_validation_and_idempotency(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    body = create_course_body(ids["professor"])
    headers = post_headers(ids["admin"], "admin")
    first = client.post("/api/v1/courses", json=body, headers=headers)
    assert first.status_code == 201
    course_id = first.json()["id"]

    repeated = client.post("/api/v1/courses", json=body, headers=headers)
    assert repeated.status_code == first.status_code
    assert repeated.json() == first.json()
    assert repeated.headers["location"] == first.headers["location"]
    conflict = client.post(
        "/api/v1/courses", json={**body, "status": "ACTIVE"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert conflict.json()["error"]["request_id"] == conflict.headers["x-request-id"]

    missing_key = client.post(
        "/api/v1/courses", json=body, headers=auth(ids["admin"], "admin")
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISSING"
    wrong_teacher = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["student"]),
        headers=post_headers(ids["admin"], "admin"),
    )
    assert wrong_teacher.status_code == 422
    assert wrong_teacher.json()["error"]["code"] == "TEACHER_ROLE_REQUIRED"

    assert client.get(f"/api/v1/courses/{course_id}").status_code == 401
    assert (
        client.post(
            "/api/v1/courses",
            json=body,
            headers=post_headers(ids["student"], "student"),
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/courses/{course_id}", headers=auth(ids["student"], "student")
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/courses/{course_id}",
            json={"teacher_id": str(ids["other_professor"])},
            headers=auth(ids["professor"], "professor"),
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/courses/{course_id}",
            json={},
            headers=auth(ids["admin"], "admin"),
        ).status_code
        == 422
    )

    async def enroll() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            async with sessions.begin() as session:
                session.add(
                    Enrollment(student_id=ids["student"], course_id=UUID(course_id))
                )
        finally:
            await engine.dispose()

    asyncio.run(enroll())
    assert (
        client.get(
            f"/api/v1/courses/{course_id}", headers=auth(ids["student"], "student")
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/courses/{course_id}",
            json={"status": "ACTIVE"},
            headers=auth(ids["admin"], "admin"),
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/courses/{course_id}", headers=auth(ids["student"], "student")
        ).status_code
        == 200
    )

    assignment_body = create_assignment_body(UUID(course_id))
    assignment_headers = post_headers(ids["professor"], "professor")
    assignment = client.post(
        "/api/v1/assignments", json=assignment_body, headers=assignment_headers
    )
    assert assignment.status_code == 201
    assignment_id = assignment.json()["id"]
    assert (
        client.get(
            f"/api/v1/assignments/{assignment_id}",
            headers=auth(ids["student"], "student"),
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/assignments",
            params={"course_id": str(uuid4())},
            headers=auth(ids["student"], "student"),
        ).json()["pagination"]["total"]
        == 0
    )
    assert (
        client.get(
            "/api/v1/assignments",
            params={"limit": 101},
            headers=auth(ids["student"], "student"),
        ).status_code
        == 422
    )

    repeated_assignment = client.post(
        "/api/v1/assignments", json=assignment_body, headers=assignment_headers
    )
    assert repeated_assignment.status_code == 201
    assert repeated_assignment.json() == assignment.json()
    assert (
        client.patch(
            f"/api/v1/assignments/{assignment_id}",
            json={"date_start": "2030-01-01"},
            headers=auth(ids["professor"], "professor"),
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/assignments/{assignment_id}",
            json={"materials_urls": None},
            headers=auth(ids["admin"], "admin"),
        ).status_code
        == 422
    )


def test_idempotency_ignores_json_key_order(
    api: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = api
    body = create_course_body(ids["professor"])
    headers = post_headers(ids["admin"], "admin")

    first = client.post("/api/v1/courses", json=body, headers=headers)
    reordered = client.post(
        "/api/v1/courses", json=dict(reversed(list(body.items()))), headers=headers
    )

    assert first.status_code == reordered.status_code == 201
    assert first.json() == reordered.json()
    assert first.headers["location"] == reordered.headers["location"]
    page = client.get("/api/v1/courses", headers=auth(ids["admin"], "admin"))
    assert page.json()["pagination"]["total"] == 1


def test_concurrent_posts_with_same_idempotency_key(
    api: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = api
    body = create_course_body(ids["professor"])
    headers = post_headers(ids["admin"], "admin")
    barrier = Barrier(3)

    def submit() -> Any:
        barrier.wait(timeout=5)
        return client.post("/api/v1/courses", json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit) for _ in range(2)]
        barrier.wait(timeout=5)
        responses = [future.result(timeout=10) for future in futures]

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert responses[0].headers["location"] == responses[1].headers["location"]
    page = client.get("/api/v1/courses", headers=auth(ids["admin"], "admin"))
    assert page.json()["pagination"]["total"] == 1


def test_idempotency_key_is_scoped_to_user_and_path(
    api: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = api
    shared_key = str(uuid4())
    admin_headers = {**auth(ids["admin"], "admin"), "Idempotency-Key": shared_key}
    professor_headers = {
        **auth(ids["professor"], "professor"),
        "Idempotency-Key": shared_key,
    }

    course = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["professor"]),
        headers=admin_headers,
    )
    assert course.status_code == 201

    assignment_body = create_assignment_body(UUID(course.json()["id"]))
    admin_assignment = client.post(
        "/api/v1/assignments", json=assignment_body, headers=admin_headers
    )
    professor_assignment = client.post(
        "/api/v1/assignments", json=assignment_body, headers=professor_headers
    )

    assert admin_assignment.status_code == professor_assignment.status_code == 201
    assert admin_assignment.json()["id"] != professor_assignment.json()["id"]
    page = client.get("/api/v1/assignments", headers=auth(ids["admin"], "admin"))
    assert page.json()["pagination"]["total"] == 2


def test_rejected_post_does_not_reserve_idempotency_key(
    api: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = api
    headers = post_headers(ids["admin"], "admin")

    rejected = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["student"]),
        headers=headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "TEACHER_ROLE_REQUIRED"

    accepted = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["professor"]),
        headers=headers,
    )
    assert accepted.status_code == 201
    repeated = client.post(
        "/api/v1/courses",
        json=create_course_body(ids["professor"]),
        headers=headers,
    )
    assert repeated.status_code == 201
    assert repeated.json() == accepted.json()
    page = client.get("/api/v1/courses", headers=auth(ids["admin"], "admin"))
    assert page.json()["pagination"]["total"] == 1


def test_contract_paths_are_registered() -> None:
    from src.main import app

    with (PROJECT_ROOT / "openapi.yaml").open(encoding="utf-8") as source:
        contract = yaml.safe_load(source)
    published = app.openapi()
    methods = {"get", "post", "put", "patch", "delete"}
    expected = {
        (path, method): operation
        for path, operations in contract["paths"].items()
        for method, operation in operations.items()
        if method in methods
    }
    actual = {
        (path, method): operation
        for path, operations in published["paths"].items()
        if path.startswith("/api/v1/")
        for method, operation in operations.items()
        if method in methods
    }
    assert set(actual) == set(expected)
    for route, operation in expected.items():
        generated = actual[route]
        assert set(generated["responses"]) == set(operation["responses"]), route
        assert generated["operationId"] == operation["operationId"], route
        assert generated["security"] == contract["security"], route
        if route[1] == "post":
            assert any(
                parameter["name"] == "Idempotency-Key" and parameter["required"]
                for parameter in generated["parameters"]
            ), route


@pytest.mark.parametrize(
    ("claims", "secret", "expected_code"),
    [
        ({"role": "admin"}, "wrong-secret-with-at-least-32-bytes", "TOKEN_INVALID"),
        ({"role": "admin", "expired": True}, None, "TOKEN_EXPIRED"),
        ({"role": "admin", "missing_exp": True}, None, "TOKEN_INVALID"),
        ({"role": "unknown"}, None, "TOKEN_INVALID"),
        ({"role": "admin", "sub": "not-a-uuid"}, None, "TOKEN_INVALID"),
    ],
)
def test_invalid_tokens_have_contract_error(
    api: tuple[TestClient, dict[str, UUID]],
    claims: dict[str, Any],
    secret: str | None,
    expected_code: str,
) -> None:
    client, ids = api
    payload = {
        "sub": claims.get("sub", str(ids["admin"])),
        "role": claims["role"],
    }
    if not claims.get("missing_exp"):
        payload["exp"] = datetime.now(timezone.utc) + timedelta(
            hours=-1 if claims.get("expired") else 1
        )
    token = jwt.encode(
        payload,
        secret or get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    response = client.get(
        "/api/v1/courses", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["message"]
    assert error == {
        "code": expected_code,
        "message": error["message"],
        "status": 401,
        "path": "/api/v1/courses",
        "request_id": response.headers["x-request-id"],
    }


@pytest.mark.parametrize("request_id", [str(uuid4()), "invalid-uuid"])
def test_request_id_is_returned_and_matches_error(
    api: tuple[TestClient, dict[str, UUID]], request_id: str
) -> None:
    client, _ = api
    response = client.get("/api/v1/courses", headers={"X-Request-ID": request_id})
    actual_id = response.headers["x-request-id"]
    UUID(actual_id)
    assert actual_id == response.json()["error"]["request_id"]
    assert (actual_id == request_id) == (request_id != "invalid-uuid")


def test_course_list_counts_after_access_filter_before_pagination(
    api: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = api
    for professor in ("professor", "other_professor"):
        response = client.post(
            "/api/v1/courses",
            json=create_course_body(ids[professor]),
            headers=post_headers(ids["admin"], "admin"),
        )
        assert response.status_code == 201

    professor_auth = auth(ids["professor"], "professor")
    visible = client.get(
        "/api/v1/courses",
        params={"offset": 0, "limit": 1},
        headers=professor_auth,
    )
    assert visible.status_code == 200
    assert visible.json()["pagination"] == {"offset": 0, "limit": 1, "total": 1}
    assert len(visible.json()["data"]) == 1

    empty_page = client.get(
        "/api/v1/courses",
        params={"offset": 1, "limit": 1},
        headers=professor_auth,
    )
    assert empty_page.status_code == 200
    assert empty_page.json()["pagination"] == {"offset": 1, "limit": 1, "total": 1}
    assert empty_page.json()["data"] == []

    invalid_sort = client.get(
        "/api/v1/courses", params={"sort": "title:asc"}, headers=professor_auth
    )
    assert invalid_sort.status_code == 422
    assert invalid_sort.json()["error"]["code"] == "VALIDATION_ERROR"


def test_submission_creates_job_and_replays_after_deadline(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    body = {
        "assignment_id": str(assignment_id),
        "repository_url": "ssh://git.example.com/student/course.git",
    }
    headers = post_headers(ids["student"], "student")

    created = client.post("/api/v1/submissions", json=body, headers=headers)
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])
    assert created.headers["location"] == f"/api/v1/submissions/{submission_id}"
    assert created.json()["student_id"] == str(ids["student"])
    assert created.json()["status"] == "SUBMITTED"
    assert created.json()["review_id"] is None
    assert created.json()["artifacts_url"] is None
    assert created.json()["repository_url"] == body["repository_url"]

    yesterday = (
        datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    ).isoformat()
    changed = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        json={"date_start": yesterday, "date_end": yesterday},
        headers=auth(ids["professor"], "professor"),
    )
    assert changed.status_code == 200

    repeated = client.post("/api/v1/submissions", json=body, headers=headers)
    assert repeated.status_code == 201
    assert repeated.json() == created.json()
    assert repeated.headers["location"] == created.headers["location"]

    new_request = client.post(
        "/api/v1/submissions",
        json=body,
        headers=post_headers(ids["student"], "student"),
    )
    assert new_request.status_code == 409
    assert new_request.json()["error"]["code"] == "SUBMISSION_DEADLINE_PASSED"

    async def check_rows() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            async with sessions() as session:
                submissions = (
                    await session.scalars(
                        select(Submission).where(
                            Submission.assignment_id == assignment_id
                        )
                    )
                ).all()
                jobs = (
                    await session.scalars(
                        select(CheckJob).where(CheckJob.submission_id == submission_id)
                    )
                ).all()
                assert len(submissions) == len(jobs) == 1
                assert submissions[0].status == "SUBMITTED"
                assert jobs[0].status == "PENDING"
        finally:
            await engine.dispose()

    asyncio.run(check_rows())


def test_submission_attempt_limit_is_atomic(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    barrier = Barrier(5)

    def submit() -> Any:
        barrier.wait(timeout=10)
        return client.post(
            "/api/v1/submissions",
            json={
                "assignment_id": str(assignment_id),
                "repository_url": "https://git.example.com/student/course.git",
            },
            headers=post_headers(ids["student"], "student"),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(submit) for _ in range(4)]
        barrier.wait(timeout=10)
        responses = [future.result(timeout=20) for future in futures]

    assert sorted(response.status_code for response in responses) == [
        201,
        201,
        201,
        409,
    ]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error"]["code"] == "SUBMISSION_LIMIT_REACHED"

    async def count_rows() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            async with sessions() as session:
                submissions = (
                    await session.scalars(
                        select(Submission).where(
                            Submission.assignment_id == assignment_id
                        )
                    )
                ).all()
                jobs = (
                    await session.scalars(
                        select(CheckJob).where(
                            CheckJob.submission_id.in_(item.id for item in submissions)
                        )
                    )
                ).all()
                assert len(submissions) == len(jobs) == 3
        finally:
            await engine.dispose()

    asyncio.run(count_rows())


def test_submission_rejection_does_not_reserve_key(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    course_id, assignment_id = prepare_submission(client, ids, test_database_url)
    body = {
        "assignment_id": str(assignment_id),
        "repository_url": "https://git.example.com/student/course.git",
    }
    headers = post_headers(ids["student"], "student")

    assert (
        client.post(
            "/api/v1/submissions",
            json=body,
            headers=post_headers(ids["professor"], "professor"),
        ).status_code
        == 403
    )
    missing = client.post(
        "/api/v1/submissions",
        json={**body, "assignment_id": str(uuid4())},
        headers=headers,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ASSIGNMENT_NOT_FOUND"

    closed = client.patch(
        f"/api/v1/courses/{course_id}",
        json={"status": "CLOSED"},
        headers=auth(ids["admin"], "admin"),
    )
    assert closed.status_code == 200
    inactive = client.post("/api/v1/submissions", json=body, headers=headers)
    assert inactive.status_code == 409
    assert inactive.json()["error"]["code"] == "COURSE_NOT_ACTIVE"

    reopened = client.patch(
        f"/api/v1/courses/{course_id}",
        json={"status": "ACTIVE"},
        headers=auth(ids["admin"], "admin"),
    )
    assert reopened.status_code == 200
    tomorrow = (
        datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1)
    ).isoformat()
    future = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        json={"date_start": tomorrow, "date_end": tomorrow},
        headers=auth(ids["professor"], "professor"),
    )
    assert future.status_code == 200
    early = client.post("/api/v1/submissions", json=body, headers=headers)
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "SUBMISSION_NOT_STARTED"

    today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    opened = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        json={"date_start": today, "date_end": today},
        headers=auth(ids["professor"], "professor"),
    )
    assert opened.status_code == 200
    accepted = client.post("/api/v1/submissions", json=body, headers=headers)
    assert accepted.status_code == 201


def test_claim_next_job_once(api, test_database_url: str) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)

    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])

    async def check() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            lease = get_settings().worker_lease_seconds
            first = await claim_next_job(sessions, lease)
            second = await claim_next_job(sessions, lease)

            assert first is not None
            assert first[2] == submission_id
            assert second is None

            async with sessions() as session:
                job = await session.scalar(
                    select(CheckJob).where(CheckJob.submission_id == submission_id)
                )
                submission = await session.get(Submission, submission_id)

                assert job is not None
                assert submission is not None
                assert job.status == "RUNNING"
                assert job.attempts == 1
                assert job.leased_until > datetime.now(timezone.utc)
                assert submission.status == "PROCESSING"
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_review_routes_and_manual_review(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created_submission = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/review",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created_submission.status_code == 201
    submission_id = created_submission.json()["id"]
    review_path = f"/api/v1/submissions/{submission_id}/review"
    artifact_path = f"/api/v1/submissions/{submission_id}/artifacts"
    professor_auth = auth(ids["professor"], "professor")
    student_auth = auth(ids["student"], "student")

    missing_review = client.get(review_path, headers=student_auth)
    assert missing_review.status_code == 404
    assert missing_review.json()["error"]["code"] == "REVIEW_NOT_FOUND"
    assert client.get(artifact_path, headers=student_auth).json() == []
    assert client.get(review_path).status_code == 401
    assert (
        client.get(review_path, headers=auth(ids["admin"], "admin")).status_code == 403
    )
    assert (
        client.get(artifact_path, headers=auth(ids["admin"], "admin")).status_code
        == 403
    )
    assert (
        client.get(
            review_path, headers=auth(ids["other_professor"], "professor")
        ).status_code
        == 403
    )
    assert (
        client.get(
            artifact_path, headers=auth(ids["other_professor"], "professor")
        ).status_code
        == 403
    )
    for path in (review_path, artifact_path):
        response = client.get(path, headers=auth(ids["other_student"], "student"))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ACCESS_DENIED"

    body = {"feedback": "Checked", "mark": 9.55, "status": "PASSED"}
    for name, role in (
        ("admin", "admin"),
        ("student", "student"),
        ("other_student", "student"),
        ("other_professor", "professor"),
    ):
        denied_post = client.post(
            review_path, json=body, headers=post_headers(ids[name], role)
        )
        assert denied_post.status_code == 403
        assert denied_post.json()["error"]["code"] == "ACCESS_DENIED"

        denied_patch = client.patch(
            review_path, json={"feedback": "Denied"}, headers=auth(ids[name], role)
        )
        assert denied_patch.status_code == 403
        assert denied_patch.json()["error"]["code"] == "ACCESS_DENIED"

    assert client.get(review_path, headers=student_auth).status_code == 404

    headers = post_headers(ids["professor"], "professor")
    invalid = client.post(review_path, json={**body, "mark": 9.555}, headers=headers)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    created = client.post(review_path, json=body, headers=headers)
    assert created.status_code == 201
    assert created.headers["location"] == review_path
    assert created.json()["type"] == "manual"
    assert created.json()["mark"] == 9.55
    assert client.get(review_path, headers=student_auth).json() == created.json()
    assert client.get(review_path, headers=professor_auth).json() == created.json()

    changed = client.patch(
        review_path, json={"feedback": "Confirmed"}, headers=professor_auth
    )
    assert changed.status_code == 200
    assert changed.json()["type"] == "manual"
    assert changed.json()["feedback"] == "Confirmed"
    assert changed.json()["mark"] == 9.55
    assert changed.json()["status"] == "PASSED"
    assert client.patch(review_path, json={}, headers=professor_auth).status_code == 422
    assert (
        client.patch(
            review_path, json={"mark": None}, headers=professor_auth
        ).status_code
        == 422
    )
    assert (
        client.patch(
            review_path, json={"mark": 9.555}, headers=professor_auth
        ).status_code
        == 422
    )

    replay = client.post(review_path, json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == created.json()
    assert replay.headers["location"] == review_path
    conflict = client.post(
        review_path, json=body, headers=post_headers(ids["professor"], "professor")
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REVIEW_ALREADY_EXISTS"
    assert (
        client.get(
            f"/api/v1/submissions/{uuid4()}/review", headers=student_auth
        ).json()["error"]["code"]
        == "SUBMISSION_NOT_FOUND"
    )

    schema = client.get("/openapi.json").json()["paths"][
        "/api/v1/submissions/{id}/review"
    ]["post"]
    assert "201" in schema["responses"]
    assert any(
        item["name"] == "Idempotency-Key" and item["required"]
        for item in schema["parameters"]
    )


def test_auto_review_artifacts_and_manual_priority(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    student_auth = auth(ids["student"], "student")
    professor_auth = auth(ids["professor"], "professor")

    def submit(url: str) -> UUID:
        response = client.post(
            "/api/v1/submissions",
            json={"assignment_id": str(assignment_id), "repository_url": url},
            headers=post_headers(ids["student"], "student"),
        )
        assert response.status_code == 201
        return UUID(response.json()["id"])

    auto_first = submit("https://example.com/student/auto-first")
    manual_first = submit("https://example.com/student/manual-first")
    artifact = ArtifactResult(
        name="result.json",
        content_type="application/json",
        download_url="https://example.com/artifacts/result.json",
    )
    with pytest.raises(ValidationError):
        AutoReviewResult(feedback="Invalid", mark=8.555, status="PASSED")

    manual_path = f"/api/v1/submissions/{manual_first}/review"
    manual = client.post(
        manual_path,
        json={"feedback": "Manual first", "mark": 9.75, "status": "PASSED"},
        headers=post_headers(ids["professor"], "professor"),
    )
    assert manual.status_code == 201

    async def finish_both() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            for expected_id in (auto_first, manual_first):
                claimed = await claim_next_job(
                    sessions, get_settings().worker_lease_seconds
                )
                assert claimed is not None
                job_id, attempt, claimed_id, _ = claimed
                assert claimed_id == expected_id
                result = CheckResult(
                    submission_id=expected_id,
                    status="COMPLETED",
                    review=AutoReviewResult(
                        feedback="Automatic", mark=8.25, status="FAILED"
                    ),
                    artifacts=[artifact],
                )
                assert await finish_success(sessions, job_id, attempt, result)
        finally:
            await engine.dispose()

    asyncio.run(finish_both())

    auto_path = f"/api/v1/submissions/{auto_first}/review"
    auto = client.get(auto_path, headers=student_auth)
    assert auto.status_code == 200
    assert (auto.json()["type"], auto.json()["mark"]) == ("auto", 8.25)
    assert (
        client.post(
            auto_path,
            json={"feedback": "New", "mark": 9.0, "status": "PASSED"},
            headers=post_headers(ids["professor"], "professor"),
        ).status_code
        == 409
    )
    confirmed = client.patch(auto_path, json={"mark": 9.5}, headers=professor_auth)
    assert confirmed.status_code == 200
    assert confirmed.json()["type"] == "manual"
    assert confirmed.json()["feedback"] == "Automatic"
    assert confirmed.json()["status"] == "FAILED"
    assert confirmed.json()["mark"] == 9.5

    after_auto = client.get(manual_path, headers=student_auth)
    assert after_auto.status_code == 200
    assert after_auto.json() == manual.json()
    for submission_id in (auto_first, manual_first):
        path = f"/api/v1/submissions/{submission_id}/artifacts"
        response = client.get(path, headers=student_auth)
        assert response.status_code == 200
        assert response.json() == [artifact.model_dump(mode="json")]
        assert client.get(path, headers=professor_auth).status_code == 200


def test_checker_and_manual_patch_race_keeps_manual_review(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    submission = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/race",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert submission.status_code == 201
    submission_id = UUID(submission.json()["id"])
    review_path = f"/api/v1/submissions/{submission_id}/review"
    professor_auth = auth(ids["professor"], "professor")
    initial = client.post(
        review_path,
        json={"feedback": "Initial", "mark": 7.0, "status": "FAILED"},
        headers=post_headers(ids["professor"], "professor"),
    )
    assert initial.status_code == 201

    async def claim() -> tuple[UUID, int, UUID, str]:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(
                sessions, get_settings().worker_lease_seconds
            )
            assert claimed is not None
            return claimed
        finally:
            await engine.dispose()

    claimed = asyncio.run(claim())
    barrier = Barrier(3)

    def checker_finishes() -> bool:
        async def finish() -> bool:
            engine, sessions = create_database(test_database_url)
            try:
                result = CheckResult(
                    submission_id=submission_id,
                    status="COMPLETED",
                    review=AutoReviewResult(
                        feedback="Automatic", mark=8.25, status="PASSED"
                    ),
                )
                return await finish_success(sessions, claimed[0], claimed[1], result)
            finally:
                await engine.dispose()

        barrier.wait(timeout=5)
        return asyncio.run(finish())

    def professor_patches() -> Any:
        barrier.wait(timeout=5)
        return client.patch(
            review_path,
            json={"feedback": "Final", "mark": 9.5, "status": "PASSED"},
            headers=professor_auth,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        checker = executor.submit(checker_finishes)
        professor = executor.submit(professor_patches)
        barrier.wait(timeout=5)
        assert checker.result(timeout=10)
        assert professor.result(timeout=10).status_code == 200

    final = client.get(review_path, headers=auth(ids["student"], "student"))
    assert final.status_code == 200
    assert final.json()["type"] == "manual"
    assert final.json()["feedback"] == "Final"
    assert final.json()["mark"] == 9.5
    assert final.json()["status"] == "PASSED"


def test_checker_success_finishes_job(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])

    async def check() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(
                sessions, get_settings().worker_lease_seconds
            )
            assert claimed is not None
            job_id, claim_attempt, claimed_submission_id, repository_url = claimed
            assert claimed_submission_id == submission_id

            transport = httpx.ASGITransport(app=mock_checker_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://checker.test"
            ) as checker_client:
                result = await call_checker(
                    checker_client,
                    "http://checker.test/check",
                    claimed_submission_id,
                    repository_url,
                )

            assert result.submission_id == submission_id
            assert result.status == "COMPLETED"
            assert await finish_success(sessions, job_id, claim_attempt, result)
            assert not await finish_success(sessions, job_id, claim_attempt, result)

            async with sessions() as session:
                job = await session.get(CheckJob, job_id)
                submission = await session.get(Submission, submission_id)
                assert job is not None
                assert submission is not None
                assert job.status == "DONE"
                assert job.attempts == 1
                assert job.leased_until is None
                assert submission.status == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_checker_persists_nonempty_artifacts(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])
    artifact = ArtifactResult(
        name="test-report.json",
        content_type="application/json",
        download_url="https://example.com/artifacts/test-report.json",
    )

    async def finish() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(
                sessions, get_settings().worker_lease_seconds
            )
            assert claimed is not None
            job_id, attempt, claimed_submission_id, _ = claimed
            assert claimed_submission_id == submission_id
            result = CheckResult(
                submission_id=submission_id,
                status="COMPLETED",
                artifacts=[artifact],
            )
            assert await finish_success(sessions, job_id, attempt, result)
        finally:
            await engine.dispose()

    asyncio.run(finish())

    response = client.get(
        f"/api/v1/submissions/{submission_id}/artifacts",
        headers=auth(ids["student"], "student"),
    )
    assert response.status_code == 200
    assert response.json() == [artifact.model_dump(mode="json")]

    submission = client.get(
        f"/api/v1/submissions/{submission_id}",
        headers=auth(ids["student"], "student"),
    )
    assert submission.status_code == 200
    assert submission.json()["artifacts_url"].endswith(
        f"/api/v1/submissions/{submission_id}/artifacts"
    )


@pytest.mark.parametrize(
    ("max_attempts", "expected_job_status", "expected_submission_status"),
    [(3, "PENDING", "SUBMITTED"), (1, "FAILED", "FAILED")],
)
def test_checker_failure_updates_job_and_rejects_stale_attempt(
    api: tuple[TestClient, dict[str, UUID]],
    test_database_url: str,
    max_attempts: int,
    expected_job_status: str,
    expected_submission_status: str,
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])

    async def check() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(
                sessions, get_settings().worker_lease_seconds
            )
            assert claimed is not None
            job_id, claim_attempt, claimed_submission_id, _ = claimed
            assert claimed_submission_id == submission_id

            before_failure = datetime.now(timezone.utc)
            assert await finish_failure(
                sessions,
                job_id,
                claim_attempt,
                error_code="UPSTREAM_503",
                retryable=True,
                max_attempts=max_attempts,
            )

            async with sessions() as session:
                job = await session.get(CheckJob, job_id)
                submission = await session.get(Submission, submission_id)
                assert job is not None
                assert submission is not None
                assert job.status == expected_job_status
                assert job.attempts == 1
                assert job.leased_until is None
                assert job.last_error == "UPSTREAM_503"
                assert submission.status == expected_submission_status
                if expected_job_status == "PENDING":
                    assert job.next_run_at > before_failure

            assert not await finish_failure(
                sessions,
                job_id,
                claim_attempt,
                error_code="STALE_FAILURE",
                retryable=True,
                max_attempts=max_attempts,
            )

            async with sessions() as session:
                job = await session.get(CheckJob, job_id)
                submission = await session.get(Submission, submission_id)
                assert job is not None
                assert submission is not None
                assert job.status == expected_job_status
                assert job.attempts == 1
                assert job.last_error == "UPSTREAM_503"
                assert submission.status == expected_submission_status
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize(
    (
        "max_attempts",
        "lease_expired",
        "expected_recovered",
        "expected_job_status",
        "expected_submission_status",
    ),
    [
        (3, True, 1, "PENDING", "SUBMITTED"),
        (1, True, 1, "FAILED", "FAILED"),
        (3, False, 0, "RUNNING", "PROCESSING"),
    ],
)
def test_recover_expired_jobs(
    api: tuple[TestClient, dict[str, UUID]],
    test_database_url: str,
    max_attempts: int,
    lease_expired: bool,
    expected_recovered: int,
    expected_job_status: str,
    expected_submission_status: str,
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])

    async def check() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(sessions, lease_seconds=60)
            assert claimed is not None
            job_id, claim_attempt, claimed_submission_id, _ = claimed
            assert claimed_submission_id == submission_id

            lease = datetime.now(timezone.utc) + timedelta(
                days=-1 if lease_expired else 1
            )
            async with sessions.begin() as session:
                job = await session.get(CheckJob, job_id)
                assert job is not None
                job.leased_until = lease

            before_recovery = datetime.now(timezone.utc)
            assert (
                await recover_expired_jobs(sessions, max_attempts) == expected_recovered
            )

            async with sessions() as session:
                job = await session.get(CheckJob, job_id)
                submission = await session.get(Submission, submission_id)
                assert job is not None
                assert submission is not None
                assert job.status == expected_job_status
                assert job.attempts == claim_attempt == 1
                assert submission.status == expected_submission_status
                if lease_expired:
                    assert job.leased_until is None
                    assert job.last_error == "LEASE_EXPIRED"
                    if expected_job_status == "PENDING":
                        assert job.next_run_at >= before_recovery
                else:
                    assert job.leased_until == lease
                    assert job.last_error is None
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_recovered_job_rejects_stale_success(
    api: tuple[TestClient, dict[str, UUID]], test_database_url: str
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])

    async def check() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(sessions, lease_seconds=60)
            assert claimed is not None
            job_id, old_attempt, claimed_submission_id, _ = claimed
            assert claimed_submission_id == submission_id

            async with sessions.begin() as session:
                job = await session.get(CheckJob, job_id)
                assert job is not None
                job.leased_until = datetime.now(timezone.utc) - timedelta(days=1)

            assert await recover_expired_jobs(sessions, max_attempts=3) == 1
            stale_result = CheckResult(submission_id=submission_id, status="COMPLETED")
            assert not await finish_success(sessions, job_id, old_attempt, stale_result)

            reclaimed = await claim_next_job(sessions, lease_seconds=60)
            assert reclaimed is not None
            assert reclaimed[:3] == (job_id, old_attempt + 1, submission_id)
            assert not await finish_success(sessions, job_id, old_attempt, stale_result)

            async with sessions() as session:
                job = await session.get(CheckJob, job_id)
                submission = await session.get(Submission, submission_id)
                assert job is not None
                assert submission is not None
                assert job.status == "RUNNING"
                assert job.attempts == old_attempt + 1
                assert submission.status == "PROCESSING"
        finally:
            await engine.dispose()

    asyncio.run(check())


@asynccontextmanager
async def live_mock_checker(app: FastAPI = mock_checker_app) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("Mock checker did not start")

            yield f"http://127.0.0.1:{port}/check"
        finally:
            server.should_exit = True
            await server_task


@pytest.mark.parametrize(
    ("query", "timeout", "job_status", "submission_status", "error_code"),
    [
        ("", 1.0, "DONE", "COMPLETED", None),
        ("?fail=true", 1.0, "PENDING", "SUBMITTED", "UPSTREAM_503"),
        ("?delay_seconds=0.3", 0.05, "PENDING", "SUBMITTED", "TIMEOUT"),
    ],
)
def test_process_job_with_mock(
    api: tuple[TestClient, dict[str, UUID]],
    test_database_url: str,
    query: str,
    timeout: float,
    job_status: str,
    submission_status: str,
    error_code: str | None,
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = UUID(created.json()["id"])

    async def check() -> None:
        engine, sessions = create_database(test_database_url)
        try:
            claimed = await claim_next_job(
                sessions, get_settings().worker_lease_seconds
            )
            assert claimed is not None
            job_id, attempt, claimed_submission_id, _ = claimed
            assert claimed_submission_id == submission_id

            async with live_mock_checker() as checker_url:
                settings = get_settings().model_copy(
                    update={
                        "checker_url": f"{checker_url}{query}",
                        "checker_timeout_seconds": timeout,
                    }
                )
                async with httpx.AsyncClient(timeout=timeout) as checker_client:
                    before_processing = datetime.now(timezone.utc)
                    await process_job(claimed, sessions, checker_client, settings)

            async with sessions() as session:
                job = await session.get(CheckJob, job_id)
                submission = await session.get(Submission, submission_id)
                assert job is not None
                assert submission is not None
                assert job.status == job_status
                assert job.attempts == attempt == 1
                assert job.leased_until is None
                assert job.last_error == error_code
                assert submission.status == submission_status
                if job_status == "PENDING":
                    assert job.next_run_at > before_processing
        finally:
            await engine.dispose()

    asyncio.run(check())


async def wait_for_completed_jobs(database_url: str, expected: int) -> None:
    engine, sessions = create_database(database_url)
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as session:
                statuses = (await session.scalars(select(CheckJob.status))).all()
            if len(statuses) == expected and all(
                status == "DONE" for status in statuses
            ):
                return
            await asyncio.sleep(0.02)
        pytest.fail(f"Expected {expected} completed checker jobs, got {statuses}")
    finally:
        await engine.dispose()


def test_submission_reaches_completed_via_worker(
    api: tuple[TestClient, dict[str, UUID]],
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    created = client.post(
        "/api/v1/submissions",
        json={
            "assignment_id": str(assignment_id),
            "repository_url": "https://example.com/student/repository",
        },
        headers=post_headers(ids["student"], "student"),
    )
    assert created.status_code == 201
    submission_id = created.json()["id"]
    assert created.json()["status"] == "SUBMITTED"

    async def check() -> None:
        async with live_mock_checker() as checker_url:
            settings = get_settings().model_copy(
                update={
                    "database_url": test_database_url,
                    "checker_url": checker_url,
                    "worker_poll_interval_seconds": 0.01,
                }
            )
            monkeypatch.setattr(checker_worker, "get_settings", lambda: settings)
            task = asyncio.create_task(checker_worker.run_worker())
            try:
                await wait_for_completed_jobs(test_database_url, 1)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(check())
    result = client.get(
        f"/api/v1/submissions/{submission_id}",
        headers=auth(ids["student"], "student"),
    )
    assert result.status_code == 200
    assert result.json()["status"] == "COMPLETED"


def test_worker_limits_parallel_checker_calls(
    api: tuple[TestClient, dict[str, UUID]],
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, ids = api
    _, assignment_id = prepare_submission(client, ids, test_database_url)
    for _ in range(3):
        created = client.post(
            "/api/v1/submissions",
            json={
                "assignment_id": str(assignment_id),
                "repository_url": "https://example.com/student/repository",
            },
            headers=post_headers(ids["student"], "student"),
        )
        assert created.status_code == 201

    async def check() -> None:
        checker_app = FastAPI()
        two_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0

        @checker_app.post("/check")
        async def check_request(payload: dict[str, str]) -> dict[str, str]:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            try:
                await release.wait()
            finally:
                active -= 1
            return {"submission_id": payload["submission_id"], "status": "COMPLETED"}

        async with live_mock_checker(checker_app) as checker_url:
            settings = get_settings().model_copy(
                update={
                    "database_url": test_database_url,
                    "checker_url": checker_url,
                    "checker_max_concurrency": 2,
                    "worker_poll_interval_seconds": 0.01,
                }
            )
            monkeypatch.setattr(checker_worker, "get_settings", lambda: settings)
            task = asyncio.create_task(checker_worker.run_worker())
            try:
                await asyncio.wait_for(two_started.wait(), timeout=5)
                await asyncio.sleep(0.05)
                assert peak == 2
                release.set()
                await wait_for_completed_jobs(test_database_url, 3)
            finally:
                release.set()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert peak == 2

    asyncio.run(check())
