import asyncio
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.checker.client import call_checker
from src.checker.queue import (
    claim_next_job,
    finish_failure,
    finish_success,
    recover_expired_jobs,
)
from src.core.config import Settings, get_settings
from src.core.database import create_database

ClaimedJob = tuple[UUID, int, UUID, str]


async def process_job(
    claimed: ClaimedJob,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    job_id, attempt, submission_id, repository_url = claimed

    try:
        async with asyncio.timeout(settings.checker_timeout_seconds):
            result = await call_checker(
                client,
                str(settings.checker_url),
                submission_id,
                repository_url,
            )

        await finish_success(sessions, job_id, attempt, result)

    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        await finish_failure(
            sessions,
            job_id,
            attempt,
            error_code=f"UPSTREAM_{status}",
            retryable=status >= 500,
            max_attempts=settings.checker_max_attempts,
        )

    except httpx.TimeoutException, TimeoutError:
        await finish_failure(
            sessions,
            job_id,
            attempt,
            error_code="TIMEOUT",
            retryable=True,
            max_attempts=settings.checker_max_attempts,
        )

    except httpx.RequestError:
        await finish_failure(
            sessions,
            job_id,
            attempt,
            error_code="NETWORK_ERROR",
            retryable=True,
            max_attempts=settings.checker_max_attempts,
        )

    except ValidationError, ValueError:
        await finish_failure(
            sessions,
            job_id,
            attempt,
            error_code="INVALID_CHECKER_RESPONSE",
            retryable=False,
            max_attempts=settings.checker_max_attempts,
        )


async def _process_and_release(
    claimed: ClaimedJob,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    settings: Settings,
    slots: asyncio.Semaphore,
) -> None:
    try:
        await process_job(claimed, sessions, client, settings)

    finally:
        slots.release()


async def run_worker() -> None:
    settings = get_settings()
    engine, sessions = create_database(settings.database_url)
    slots = asyncio.Semaphore(settings.checker_max_concurrency)

    try:
        async with httpx.AsyncClient(
            timeout=settings.checker_timeout_seconds
        ) as client:
            async with asyncio.TaskGroup() as tasks:
                while True:
                    await recover_expired_jobs(sessions, settings.checker_max_attempts)

                    await slots.acquire()
                    try:
                        claimed = await claim_next_job(
                            sessions,
                            settings.worker_lease_seconds,
                        )
                    except BaseException:
                        slots.release()
                        raise

                    if claimed is None:
                        slots.release()
                        await asyncio.sleep(settings.worker_poll_interval_seconds)
                        continue

                    tasks.create_task(
                        _process_and_release(
                            claimed,
                            sessions,
                            client,
                            settings,
                            slots,
                        )
                    )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())

    except KeyboardInterrupt:
        ...
