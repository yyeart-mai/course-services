import asyncio
from uuid import UUID

import httpx

from src.checker.client import call_checker
from src.core.config import get_settings

SUBMISSION_ID = UUID("33333333-3333-4333-8333-333333333333")
REPOSITORY_URL = "https://example.com/student/repository"


async def main() -> None:
    settings = get_settings()
    url = str(settings.checker_url)

    async with httpx.AsyncClient(
        timeout=settings.checker_timeout_seconds
    ) as client:
        result = await call_checker(
            client, url, SUBMISSION_ID, REPOSITORY_URL
        )
        print("Успех:", result)

        try:
            await call_checker(
                client, f"{url}?fail=true", SUBMISSION_ID, REPOSITORY_URL
            )
        except httpx.HTTPStatusError as exc:
            print("Ошибка HTTP:", exc.response.status_code)

    async with httpx.AsyncClient(timeout=0.2) as client:
        try:
            await call_checker(
                client,
                f"{url}?delay_seconds=1",
                SUBMISSION_ID,
                REPOSITORY_URL,
            )
        except httpx.TimeoutException:
            print("Таймаут")


asyncio.run(main())