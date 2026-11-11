# Failure handling receives claim details and retry policy separately.

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.checker.client import CheckResult
from src.core.models import Artifact, CheckJob, Review, Submission


async def claim_next_job(
    sessions: async_sessionmaker[AsyncSession],
    lease_seconds: float,
) -> tuple[UUID, int, UUID, str] | None:
    now = datetime.now(timezone.utc)

    async with sessions.begin() as tx:
        job = await tx.scalar(
            select(CheckJob)
            .where(
                CheckJob.status == "PENDING",
                CheckJob.next_run_at <= now,
            )
            .order_by(
                CheckJob.next_run_at,
                CheckJob.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        if job is None:
            return None

        submission = await tx.get(Submission, job.submission_id)
        if submission is None:
            raise RuntimeError(f"Submission with id {job.submission_id} not found")

        job.status = "RUNNING"
        job.attempts += 1
        job.leased_until = now + timedelta(seconds=lease_seconds)
        submission.status = "PROCESSING"

        return job.id, job.attempts, submission.id, submission.repository_url


async def finish_success(
    sessions: async_sessionmaker[AsyncSession],
    job_id: UUID,
    claim_attempt: int,
    result: CheckResult,
) -> bool:
    async with sessions.begin() as tx:
        job = await tx.get(CheckJob, job_id, with_for_update=True)

        if job is None or job.status != "RUNNING" or job.attempts != claim_attempt:
            return False

        if job.submission_id != result.submission_id:
            raise ValueError("Checker returned another submission_id")

        submission = await tx.get(Submission, job.submission_id, with_for_update=True)
        if submission is None:
            raise RuntimeError(f"Submission with id {job.submission_id} not found")

        if result.review is not None:
            review = await tx.scalar(
                select(Review)
                .where(
                    Review.submission_id == submission.id,
                )
                .with_for_update()
            )

            if review is None or review.type == "auto":
                if review is None:
                    review = Review(submission_id=submission.id, type="auto")
                    tx.add(review)

                review.feedback = result.review.feedback
                review.mark = Decimal(str(result.review.mark))
                review.status = result.review.status

        for item in result.artifacts:
            artifact = Artifact(
                submission_id=submission.id,
                name=item.name,
                content_type=item.content_type,
                download_url=str(item.download_url),
            )
            tx.add(artifact)

        submission.status = result.status
        job.status = "DONE"
        job.leased_until = None
        job.last_error = None

        return True


async def finish_failure(
    sessions: async_sessionmaker[AsyncSession],
    job_id: UUID,
    claim_attempt: int,
    *,
    error_code: str,
    retryable: bool,
    max_attempts: int,
) -> bool:
    now = datetime.now(timezone.utc)

    async with sessions.begin() as tx:
        job = await tx.get(CheckJob, job_id, with_for_update=True)

        if job is None or job.status != "RUNNING" or job.attempts != claim_attempt:
            return False

        submission = await tx.get(Submission, job.submission_id, with_for_update=True)
        if submission is None:
            raise RuntimeError(f"Submission with id {job.submission_id} not found")

        job.last_error = error_code
        job.leased_until = None

        if retryable and job.attempts < max_attempts:
            job.status = "PENDING"
            job.next_run_at = now + timedelta(seconds=2 ** (job.attempts - 1))
            submission.status = "SUBMITTED"
        else:
            job.status = "FAILED"
            submission.status = "FAILED"

        return True


async def recover_expired_jobs(
    sessions: async_sessionmaker[AsyncSession],
    max_attempts: int,
) -> int:
    now = datetime.now(timezone.utc)
    processed = 0

    async with sessions.begin() as tx:
        jobs = (
            await tx.scalars(
                select(CheckJob)
                .where(
                    CheckJob.status == "RUNNING",
                    CheckJob.leased_until <= now,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()

        for job in jobs:
            submission = await tx.get(
                Submission,
                job.submission_id,
                with_for_update=True,
            )
            if submission is None:
                raise RuntimeError(f"Submission with id {job.submission_id} not found")

            if job.attempts < max_attempts:
                job.status = "PENDING"
                submission.status = "SUBMITTED"
                job.next_run_at = now

            else:
                job.status = "FAILED"
                submission.status = "FAILED"

            job.leased_until = None
            job.last_error = "LEASE_EXPIRED"

            processed += 1

    return processed
