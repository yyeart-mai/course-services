from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase): ...


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'professor', 'student')",
            name="ck_users_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)


class Enrollment(Base):
    __tablename__ = "enrollments"

    student_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        primary_key=True,
    )
    course_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("courses.id"),
        primary_key=True,
    )


class Course(Timestamps, Base):
    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'CLOSED')",
            name="ck_coursed_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    teacher_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="DRAFT"
    )


class Assignment(Timestamps, Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint("date_start <= date_end", name="ck_assignments_dates"),
        CheckConstraint(
            "attempts_limit BETWEEN 3 AND 10",
            name="ck_assignments_attempts_limit",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    course_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("courses.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    attempts_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3"
    )
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_end: Mapped[date] = mapped_column(Date, nullable=False)
    materials_urls: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class Submission(Timestamps, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SUBMITTED', 'PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_submissions_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    assignment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assignments.id"), nullable=False
    )
    student_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    repository_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="SUBMITTED"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Review(Timestamps, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("type IN ('auto', 'manual')", name="ck_reviews_type"),
        CheckConstraint("status IN ('PASSED', 'FAILED')", name="ck_reviews_status"),
        CheckConstraint("mark BETWEEN 0 AND 10", name="ck_reviews_mark"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("submissions.id"), nullable=False, unique=True
    )
    type: Mapped[str] = mapped_column(String(6), nullable=False)
    feedback: Mapped[str] = mapped_column(Text, nullable=False)
    mark: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(6), nullable=False)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("submission_id", "name", name="uq_artifact_name"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("submissions.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    download_url: Mapped[str] = mapped_column(Text, nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "method",
            "path",
            "key",
            name="uq_idempotency_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)


class CheckJob(Base):
    __tablename__ = "check_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')",
            name="ck_check_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_check_jobs_attempts"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submissions.id"),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default="PENDING"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
